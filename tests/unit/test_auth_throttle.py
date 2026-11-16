"""core.auth_throttle: thresholds, growth, cap, reset, isolation, bounds."""
import logging
import threading

import pytest

from core.auth_throttle import (
    ADDRESS_FAILURE_THRESHOLD,
    BASE_DELAY_SECONDS,
    FAILURE_THRESHOLD,
    FORGET_AFTER_SECONDS,
    MAX_DELAY_SECONDS,
    MAX_TRACKED_ACCOUNTS,
    SCOPE_LOGIN,
    SCOPE_OPDS,
    SCOPE_TOKEN,
    SPRAY_WINDOW_SECONDS,
    AuthThrottle,
    retry_after_text,
    token_account_key,
)


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def throttle(clock):
    return AuthThrottle(clock=clock, logger=logging.getLogger("test.auth_throttle"))


def _fail(throttle, n, address="10.0.0.1", account="owner", scope=SCOPE_LOGIN):
    for _ in range(n):
        throttle.record_failure(scope, address, account)


class TestConstants:
    def test_numbers_are_the_documented_ones(self):
        assert FAILURE_THRESHOLD == 5
        assert BASE_DELAY_SECONDS == 30
        assert MAX_DELAY_SECONDS == 15 * 60
        assert ADDRESS_FAILURE_THRESHOLD > FAILURE_THRESHOLD


class TestThreshold:
    def test_below_threshold_is_never_throttled(self, throttle):
        _fail(throttle, FAILURE_THRESHOLD - 1)
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == 0

    def test_threshold_opens_the_base_window(self, throttle):
        _fail(throttle, FAILURE_THRESHOLD)
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == BASE_DELAY_SECONDS

    def test_window_counts_down_and_ends(self, throttle, clock):
        _fail(throttle, FAILURE_THRESHOLD)
        clock.advance(10)
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == BASE_DELAY_SECONDS - 10
        clock.advance(BASE_DELAY_SECONDS)
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == 0

    def test_account_key_is_case_insensitive(self, throttle):
        _fail(throttle, FAILURE_THRESHOLD, account="Owner")
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "OWNER") > 0


class TestGrowthAndCap:
    def test_each_failure_after_a_window_doubles_it(self, throttle, clock):
        _fail(throttle, FAILURE_THRESHOLD)
        expected = BASE_DELAY_SECONDS
        for _ in range(4):
            assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == expected
            clock.advance(expected)
            _fail(throttle, 1)
            expected *= 2
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == expected

    def test_window_is_capped(self, throttle, clock):
        _fail(throttle, FAILURE_THRESHOLD + 200)
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == MAX_DELAY_SECONDS

    def test_idle_key_is_forgotten(self, throttle, clock):
        _fail(throttle, FAILURE_THRESHOLD - 1)
        clock.advance(FORGET_AFTER_SECONDS)
        _fail(throttle, 1)
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == 0


class TestResetOnSuccess:
    def test_success_clears_the_count(self, throttle):
        _fail(throttle, FAILURE_THRESHOLD - 1)
        throttle.record_success(SCOPE_LOGIN, "10.0.0.1", "owner")
        _fail(throttle, FAILURE_THRESHOLD - 1)
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == 0

    def test_success_clears_an_open_window(self, throttle):
        _fail(throttle, FAILURE_THRESHOLD)
        throttle.record_success(SCOPE_LOGIN, "10.0.0.1", "owner")
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == 0

    def test_reset_forgets_everything(self, throttle):
        _fail(throttle, FAILURE_THRESHOLD)
        throttle.reset()
        assert len(throttle) == 0
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == 0


class TestIsolation:
    def test_blocked_address_does_not_block_another_for_the_same_account(self, throttle):
        _fail(throttle, FAILURE_THRESHOLD, address="203.0.113.9", account="owner")
        assert throttle.retry_after(SCOPE_LOGIN, "203.0.113.9", "owner") > 0
        assert throttle.retry_after(SCOPE_LOGIN, "192.168.1.20", "owner") == 0

    def test_account_is_never_locked_globally(self, throttle):
        for i in range(50):
            _fail(throttle, FAILURE_THRESHOLD, address=f"198.51.100.{i}", account="owner")
        assert throttle.retry_after(SCOPE_LOGIN, "192.168.1.20", "owner") == 0

    def test_other_usernames_at_the_same_address_pass_until_the_address_limit(self, throttle):
        _fail(throttle, FAILURE_THRESHOLD, account="owner")
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "kid") == 0

    def test_address_key_stops_username_spraying(self, throttle):
        for i in range(ADDRESS_FAILURE_THRESHOLD):
            _fail(throttle, 1, account=f"user{i}")
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "never-tried") > 0
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.2", "never-tried") == 0

    def test_scopes_are_independent(self, throttle):
        _fail(throttle, FAILURE_THRESHOLD, scope=SCOPE_OPDS)
        assert throttle.retry_after(SCOPE_OPDS, "10.0.0.1", "owner") > 0
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == 0

    def test_token_scope_is_per_address(self, throttle):
        key = token_account_key("stale-test-token")
        _fail(throttle, FAILURE_THRESHOLD, account=key, scope=SCOPE_TOKEN)
        assert throttle.retry_after(SCOPE_TOKEN, "10.0.0.1", key) > 0
        assert throttle.retry_after(SCOPE_TOKEN, "10.0.0.2", key) == 0

    def test_loopback_is_not_exempt(self, throttle):
        _fail(throttle, FAILURE_THRESHOLD, address="127.0.0.1")
        assert throttle.retry_after(SCOPE_LOGIN, "127.0.0.1", "owner") > 0


class TestAddressKeyMeasuresSpraying:
    """The address key counts distinct failing accounts, never repetition."""

    def test_one_pair_failing_100_times_does_not_throttle_another_username(
            self, throttle, clock):
        for _ in range(100):
            if throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "stale-reader") == 0:
                _fail(throttle, 1, account="stale-reader")
            clock.advance(60)
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "stale-reader") > 0
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == 0

    def test_one_pair_failing_100_times_without_waiting(self, throttle):
        _fail(throttle, 100, account="stale-reader")
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == 0

    def test_one_stale_token_does_not_throttle_another_token(self, throttle, clock):
        stale = token_account_key("revoked-test-token")
        good = token_account_key("valid-test-token")
        for _ in range(100):
            if throttle.retry_after(SCOPE_TOKEN, "10.0.0.1", stale) == 0:
                throttle.record_failure(SCOPE_TOKEN, "10.0.0.1", stale)
            clock.advance(30)
        assert throttle.retry_after(SCOPE_TOKEN, "10.0.0.1", stale) > 0
        assert throttle.retry_after(SCOPE_TOKEN, "10.0.0.1", good) == 0

    def test_n_distinct_usernames_throttle_the_address(self, throttle):
        for i in range(ADDRESS_FAILURE_THRESHOLD - 1):
            _fail(throttle, 1, account=f"user{i}")
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == 0
        _fail(throttle, 1, account="one-more")
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == BASE_DELAY_SECONDS

    def test_n_distinct_tokens_throttle_the_address(self, throttle):
        for i in range(ADDRESS_FAILURE_THRESHOLD):
            throttle.record_failure(SCOPE_TOKEN, "10.0.0.1",
                                    token_account_key(f"guess-{i}"))
        assert throttle.retry_after(SCOPE_TOKEN, "10.0.0.1",
                                    token_account_key("valid-test-token")) > 0

    def test_repeats_of_known_accounts_do_not_advance_it(self, throttle):
        for i in range(ADDRESS_FAILURE_THRESHOLD - 1):
            _fail(throttle, 3, account=f"user{i}")
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == 0

    def test_each_new_account_past_the_threshold_doubles_the_window(self, throttle, clock):
        for i in range(ADDRESS_FAILURE_THRESHOLD):
            _fail(throttle, 1, account=f"user{i}")
        clock.advance(BASE_DELAY_SECONDS)
        _fail(throttle, 1, account="another")
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == BASE_DELAY_SECONDS * 2

    def test_success_does_not_clear_spray_state(self, throttle):
        for i in range(ADDRESS_FAILURE_THRESHOLD - 1):
            _fail(throttle, 1, account=f"user{i}")
        throttle.record_success(SCOPE_LOGIN, "10.0.0.1", "my-own-account")
        throttle.record_success(SCOPE_LOGIN, "10.0.0.1", "user0")
        _fail(throttle, 1, account="one-more")
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") > 0

    def test_spray_state_ages_out(self, throttle, clock):
        for i in range(ADDRESS_FAILURE_THRESHOLD - 1):
            _fail(throttle, 1, account=f"user{i}")
        clock.advance(SPRAY_WINDOW_SECONDS)
        _fail(throttle, 1, account="one-more")
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == 0

    def test_tracked_accounts_per_address_are_capped(self, throttle):
        for i in range(500):
            _fail(throttle, 1, account=f"user{i}")
        addr_entry = throttle._entries[("addr", SCOPE_LOGIN, "10.0.0.1")]
        assert len(addr_entry.accounts) == MAX_TRACKED_ACCOUNTS
        assert throttle.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == MAX_DELAY_SECONDS


class TestTokenAccountKey:
    def test_digest_never_contains_the_token(self):
        token = "Zx9-very-distinctive-test-token-value"
        key = token_account_key(token)
        assert token not in key
        assert "distinctive" not in key
        assert key.startswith("token:") and len(key) == len("token:") + 16

    def test_distinct_tokens_get_distinct_keys(self):
        assert token_account_key("a-test-token") != token_account_key("b-test-token")
        assert token_account_key("a-test-token") == token_account_key("a-test-token")

    def test_stored_keys_never_contain_the_token(self, throttle):
        token = "Zx9-very-distinctive-test-token-value"
        for _ in range(FAILURE_THRESHOLD):
            throttle.record_failure(SCOPE_TOKEN, "10.0.0.1", token_account_key(token))
        assert not any(token in repr(k) for k in throttle.keys())

    def test_log_line_never_contains_the_token(self, clock, caplog):
        logger = logging.getLogger("test.auth_throttle.tok")
        t = AuthThrottle(clock=clock, logger=logger)
        token = "Zx9-very-distinctive-test-token-value"
        with caplog.at_level(logging.INFO, logger=logger.name):
            for _ in range(FAILURE_THRESHOLD):
                t.record_failure(SCOPE_TOKEN, "10.0.0.1", token_account_key(token))
        assert caplog.records
        assert all(token not in r.getMessage() for r in caplog.records)


class TestSizeBound:
    def test_entries_never_exceed_the_cap(self, clock):
        t = AuthThrottle(clock=clock, max_entries=50,
                         logger=logging.getLogger("test.auth_throttle"))
        for i in range(500):
            t.record_failure(SCOPE_LOGIN, f"10.1.{i // 256}.{i % 256}", f"u{i}")
        assert len(t) <= 50

    def test_oldest_is_evicted_first(self, clock):
        t = AuthThrottle(clock=clock, max_entries=4,
                         logger=logging.getLogger("test.auth_throttle"))
        for _ in range(FAILURE_THRESHOLD):
            t.record_failure(SCOPE_TOKEN, "old")
        for i in range(4):
            t.record_failure(SCOPE_TOKEN, f"new{i}")
        assert t.retry_after(SCOPE_TOKEN, "old") == 0
        assert len(t) == 4

    def test_a_flooder_keeps_its_own_address_key(self, clock):
        t = AuthThrottle(clock=clock, max_entries=10,
                         logger=logging.getLogger("test.auth_throttle"))
        for i in range(ADDRESS_FAILURE_THRESHOLD + 50):
            t.record_failure(SCOPE_LOGIN, "10.0.0.1", f"junk{i}")
        assert t.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") > 0


class TestThreadSafety:
    def test_concurrent_failures_are_all_counted(self, clock):
        t = AuthThrottle(clock=clock, threshold=1000, address_threshold=10_000,
                         logger=logging.getLogger("test.auth_throttle"))
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            for _ in range(100):
                t.record_failure(SCOPE_LOGIN, "10.0.0.1", "owner")
                t.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        # 800 failures < 1000: still open...
        assert t.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == 0
        # ...and exactly 200 more reach the threshold.
        for _ in range(199):
            t.record_failure(SCOPE_LOGIN, "10.0.0.1", "owner")
        assert t.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") == 0
        t.record_failure(SCOPE_LOGIN, "10.0.0.1", "owner")
        assert t.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner") > 0


class TestLogging:
    def test_one_warning_on_entering_the_window(self, clock, caplog):
        logger = logging.getLogger("test.auth_throttle.log")
        t = AuthThrottle(clock=clock, logger=logger)
        with caplog.at_level(logging.INFO, logger=logger.name):
            for _ in range(FAILURE_THRESHOLD):
                t.record_failure(SCOPE_LOGIN, "10.0.0.1", "owner")
            # Refused requests are not recorded, so they log nothing.
            for _ in range(20):
                t.retry_after(SCOPE_LOGIN, "10.0.0.1", "owner")
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "10.0.0.1" in warnings[0].getMessage()
        assert "'owner'" in warnings[0].getMessage()
        assert len(caplog.records) == 1

    def test_escalation_is_one_info_line(self, clock, caplog):
        logger = logging.getLogger("test.auth_throttle.log2")
        t = AuthThrottle(clock=clock, logger=logger)
        for _ in range(FAILURE_THRESHOLD):
            t.record_failure(SCOPE_LOGIN, "10.0.0.1", "owner")
        clock.advance(BASE_DELAY_SECONDS)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=logger.name):
            t.record_failure(SCOPE_LOGIN, "10.0.0.1", "owner")
        assert [r.levelno for r in caplog.records] == [logging.INFO]

    def test_username_is_made_printable(self, clock, caplog):
        logger = logging.getLogger("test.auth_throttle.log3")
        t = AuthThrottle(clock=clock, logger=logger)
        with caplog.at_level(logging.INFO, logger=logger.name):
            for _ in range(FAILURE_THRESHOLD):
                t.record_failure(SCOPE_LOGIN, "10.0.0.1", "evil\nINJECTED line")
        assert "\n" not in caplog.records[0].getMessage()


class TestRetryAfterText:
    def test_rounds_up_to_whole_minutes(self):
        assert retry_after_text(30) == "Too many attempts. Try again in 1 minute."
        assert retry_after_text(61) == "Too many attempts. Try again in 2 minutes."
        assert retry_after_text(900) == "Too many attempts. Try again in 15 minutes."
