"""Tests for core/provider_client.py -- the call policy shared by providers.

Exercised through a fake ``GuardedClient`` so the policy is pinned on its own,
independent of any one provider's errors. Metron's classification is tested
in tests/mocked/test_metron_model.py.
"""
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from core import provider_client
from core.provider_client import (
    AuthLockout,
    GuardedClient,
    QuotaLatch,
    Verdict,
    lockout_for,
)


class _PrefStore:
    """A real user_preferences table, so persistence is actually exercised."""

    def __init__(self, path):
        self.path = str(path)
        self.unavailable = False
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE user_preferences (key TEXT PRIMARY KEY, value TEXT, category TEXT)"
        )
        conn.commit()
        conn.close()

    def connect(self):
        return None if self.unavailable else sqlite3.connect(self.path)

    def set(self, key, value, category=None):
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT OR REPLACE INTO user_preferences (key, value, category) VALUES (?, ?, ?)",
            (key, json.dumps(value), category),
        )
        conn.commit()
        conn.close()
        return True

    def get(self, key):
        conn = sqlite3.connect(self.path)
        row = conn.execute(
            "SELECT value FROM user_preferences WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        return json.loads(row[0]) if row else None


@pytest.fixture
def prefs(tmp_path):
    store = _PrefStore(tmp_path / "prefs.db")
    with patch("core.database.get_db_connection", side_effect=store.connect), \
            patch("core.database.set_user_preference", side_effect=store.set), \
            patch("core.database.update_provider_validity") as validity:
        store.validity = validity
        yield store


@pytest.fixture
def lockout(prefs):
    """A lockout for a provider that does not exist, unregistered afterwards."""
    lock = AuthLockout("testprov", "TestProv")
    yield lock
    with provider_client._LOCKOUTS_LOCK:
        provider_client._LOCKOUTS.pop("testprov", None)


class _Fail(Exception):
    """An exception that carries the verdict the fake client should return."""

    def __init__(self, verdict):
        super().__init__(verdict.kind)
        self.verdict = verdict


class _FakeClient(GuardedClient):
    def __init__(self, lockout, quota):
        super().__init__("TestProv", lockout, quota)
        self.gated = 0
        self.succeeded = 0

    def _classify(self, exc):
        return getattr(exc, "verdict", None)

    def _gate(self):
        self.gated += 1

    def _after_success(self):
        self.succeeded += 1


@pytest.fixture
def client(lockout):
    return _FakeClient(lockout, QuotaLatch("TestProv", "daily"))


class TestAuthLockout:

    def test_pref_keys_are_derived_from_the_provider(self, lockout):
        assert lockout.pref_blocked == "testprov_auth_blocked"
        assert lockout.pref_error == "testprov_auth_error"
        assert lockout.pref_blocked_at == "testprov_auth_blocked_at"

    def test_metron_keeps_the_keys_it_always_had(self):
        """Moving the lockout here must not need a migration."""
        import models.metron as metron

        assert metron._auth_block.pref_blocked == metron.PREF_AUTH_BLOCKED == "metron_auth_blocked"
        assert metron._auth_block.pref_error == metron.PREF_AUTH_ERROR
        assert metron._auth_block.pref_blocked_at == metron.PREF_AUTH_BLOCKED_AT

    def test_registered_for_the_reset_route(self, lockout):
        assert lockout_for("testprov") is lockout
        assert lockout_for("no-such-provider") is None

    def test_latch_persists_and_marks_the_provider_invalid(self, lockout, prefs):
        lockout.latch(401, "Invalid token.", "fetching a thing")

        assert lockout.blocked() is True
        assert prefs.get("testprov_auth_blocked") is True
        assert "HTTP 401" in prefs.get("testprov_auth_error")
        prefs.validity.assert_called_once_with("testprov", False)

        lockout.forget()  # a restart: the memo is gone, the store is not
        assert lockout.blocked() is True

    def test_latching_twice_announces_once(self, lockout, prefs):
        lockout.latch(401, "", "")
        lockout.latch(401, "", "")
        prefs.validity.assert_called_once()

    def test_clear_re_enables(self, lockout, prefs):
        lockout.latch(403, "", "")
        lockout.clear()
        assert lockout.blocked() is False
        assert prefs.get("testprov_auth_blocked") is False

    def test_an_unreadable_store_is_not_cached_as_unblocked(self, lockout, prefs):
        prefs.set("testprov_auth_blocked", True)
        prefs.unavailable = True
        lockout.forget()

        assert lockout.blocked() is False  # fails open...

        prefs.unavailable = False
        assert lockout.blocked() is True   # ...but tries again


class TestQuotaLatch:

    def test_latched_until_the_reset(self):
        quota = QuotaLatch("TestProv")
        quota.latch_for(0.05)
        assert quota.reached() is True
        time.sleep(0.08)
        assert quota.reached() is False

    def test_a_reset_in_the_past_does_not_latch(self):
        quota = QuotaLatch("TestProv")
        quota.latch_until(datetime.now(timezone.utc) - timedelta(seconds=1))
        assert quota.reached() is False

    def test_a_naive_reset_is_treated_as_utc(self):
        quota = QuotaLatch("TestProv")
        quota.latch_until(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1))
        assert quota.reached() is True

    def test_announced_on_each_transition_only(self, caplog):
        quota = QuotaLatch("TestProv")
        with caplog.at_level("INFO", logger="app_logger"):
            quota.latch_for(0.05, "first")
            quota.latch_for(0.05, "second")
            for _ in range(5):
                quota.reached()
            time.sleep(0.08)
            for _ in range(5):
                quota.reached()
        messages = [r.getMessage() for r in caplog.records]
        assert sum("rate limit exhausted" in m for m in messages) == 1
        assert sum("has reset; resuming" in m for m in messages) == 1

    def test_reset_clears_it(self):
        quota = QuotaLatch("TestProv")
        quota.latch_for(3600)
        quota.reset()
        assert quota.reached() is False


class TestGuardedCall:

    def test_success_runs_the_hooks(self, client):
        assert client.call(lambda: "ok", "ctx") == "ok"
        assert client.gated == 1
        assert client.succeeded == 1

    def test_auth_latches_and_stops_every_later_call(self, client):
        fn = MagicMock(side_effect=_Fail(Verdict.auth(401, "bad token")))
        assert client.call(fn, "ctx", default="fallback") == "fallback"
        assert client.lockout.blocked() is True

        later = MagicMock(return_value="ok")
        assert client.call(later, "ctx", default="fallback") == "fallback"
        later.assert_not_called()

    def test_auth_can_be_re_raised_for_the_connection_test(self, client):
        with pytest.raises(_Fail):
            client.call(
                MagicMock(side_effect=_Fail(Verdict.auth(401))),
                "ctx",
                ignore_auth_lock=True,
                raise_auth_errors=True,
            )

    def test_a_refusing_gate_is_classified_like_any_failure(self, client):
        """ComicVine's interactive cap refuses from _gate(); that must reach
        _classify, not escape call() raw."""
        client._gate = MagicMock(side_effect=_Fail(Verdict.error()))
        fn = MagicMock(return_value="ok")
        assert client.call(fn, "ctx", default="fallback") == "fallback"
        fn.assert_not_called()

    def test_retry_wait_hook_scales_the_sleep(self, client):
        client._retry_wait = lambda wait, attempt: wait * (attempt + 1)
        fn = MagicMock(side_effect=_Fail(Verdict.retry(2.0)))
        with patch("core.provider_client.time.sleep") as sleep:
            client.call(fn, "ctx")
        assert [c.args[0] for c in sleep.call_args_list] == [2.0, 4.0]

    def test_a_provider_without_a_lockout_never_pauses_on_auth(self):
        """Anonymous providers (MangaDex...) have nothing to reject; an AUTH
        verdict from one is an ordinary error, not a latch."""
        anon = _FakeClient(None, QuotaLatch("TestProv", "daily"))
        fn = MagicMock(side_effect=_Fail(Verdict.auth(401)))
        assert anon.call(fn, "ctx", default="fallback") == "fallback"
        assert anon.paused_reason() is None
        with pytest.raises(_Fail):
            anon.call(fn, "ctx", raise_api_errors=True)
        assert anon.call(lambda: "ok", "ctx") == "ok"

    def test_ignore_auth_lock_runs_while_blocked(self, client):
        client.lockout.latch(401, "", "")
        assert client.call(lambda: "ok", "ctx", ignore_auth_lock=True) == "ok"

    def test_quota_latches_for_its_wait_and_does_not_retry(self, client):
        fn = MagicMock(side_effect=_Fail(Verdict.quota(3600)))
        with patch("core.provider_client.time.sleep") as sleep:
            assert client.call(fn, "ctx", default="fallback") == "fallback"
        assert fn.call_count == 1
        sleep.assert_not_called()
        assert client.quota.reached() is True

        later = MagicMock(return_value="ok")
        assert client.call(later, "ctx", default="fallback") == "fallback"
        later.assert_not_called()

    def test_quota_with_no_wait_gives_up_without_latching(self, client):
        """wait == 0 means the classifier latched it already."""
        fn = MagicMock(side_effect=_Fail(Verdict.quota()))
        assert client.call(fn, "ctx", default="fallback") == "fallback"
        assert fn.call_count == 1
        assert client.quota.reached() is False

    def test_retry_without_a_wait_leaves_the_waiting_to_the_pacer(self, client):
        fn = MagicMock(side_effect=[_Fail(Verdict.retry(0)), "ok"])
        with patch("core.provider_client.time.sleep") as sleep:
            assert client.call(fn, "ctx") == "ok"
        sleep.assert_not_called()
        assert client.gated == 2  # the gate runs before every attempt

    def test_retry_with_a_wait_sleeps_it(self, client):
        fn = MagicMock(side_effect=[_Fail(Verdict.retry(2)), "ok"])
        with patch("core.provider_client.time.sleep") as sleep:
            assert client.call(fn, "ctx") == "ok"
        sleep.assert_called_once_with(2)

    def test_retries_are_bounded(self, client):
        fn = MagicMock(side_effect=_Fail(Verdict.retry(0)))
        assert client.call(fn, "ctx", default="fallback") == "fallback"
        assert fn.call_count == client.max_retries

    @pytest.mark.parametrize("verdict", [Verdict.error(), Verdict.connection()])
    def test_errors_return_the_default(self, client, verdict):
        fn = MagicMock(side_effect=_Fail(verdict))
        assert client.call(fn, "ctx", default="fallback") == "fallback"
        assert fn.call_count == 1

    @pytest.mark.parametrize("verdict", [Verdict.error(), Verdict.connection()])
    def test_raise_api_errors_re_raises_them(self, client, verdict):
        with pytest.raises(_Fail):
            client.call(MagicMock(side_effect=_Fail(verdict)), "ctx", raise_api_errors=True)

    def test_raise_api_errors_still_latches_auth(self, client):
        fn = MagicMock(side_effect=_Fail(Verdict.auth(401)))
        assert client.call(fn, "ctx", default="d", raise_api_errors=True) == "d"
        assert client.lockout.blocked() is True

    def test_an_unclassified_exception_propagates(self, client):
        with pytest.raises(KeyError):
            client.call(MagicMock(side_effect=KeyError("x")), "ctx")

    def test_paused_reason(self, client):
        assert client.paused_reason() is None

        client.quota.latch_for(3600)
        assert client.paused_reason() == "TestProv's daily request limit was reached"

        client.lockout.latch(401, "", "")
        assert client.paused_reason() == "TestProv rejected the saved credentials"
        assert client.paused_reason(ignore_auth_lock=True) == (
            "TestProv's daily request limit was reached"
        )

    def test_call_never_paces_on_its_own(self, client):
        """Pacing belongs where each HTTP request is sent. A call can be many
        requests, so a slot taken here undercounts (#703)."""
        with patch("core.provider_client.time.sleep") as sleep:
            for _ in range(50):
                client.call(lambda: "ok", "ctx")
        sleep.assert_not_called()
