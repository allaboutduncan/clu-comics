"""Failed-credential throttling on /login, OPDS Basic auth and bearer tokens.

The throttle is process-global; tests/conftest.py resets it around every test.
Its clock is replaced with a fake one here, so no test sleeps.
"""
import base64
from unittest.mock import patch

import pytest

from core.auth_throttle import (
    BASE_DELAY_SECONDS,
    FAILURE_THRESHOLD,
    throttle,
)
from core.database import create_api_token, create_user, get_user_by_username, update_user

OTHER_ADDRESS = {"REMOTE_ADDR": "192.168.1.50"}


class FakeClock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = FakeClock()
    monkeypatch.setattr(throttle, "clock", fake)
    return fake


@pytest.fixture
def multi_user(db_connection, monkeypatch):
    """Two accounts, so login is required."""
    monkeypatch.delenv("CLU_USERNAME", raising=False)
    monkeypatch.delenv("CLU_PASSWORD", raising=False)
    create_user("owner", password="owner-test-pass", role="owner")
    create_user("kid", password="kid-test-pass", role="reader")


def _login(client, username, password, **kw):
    return client.post("/login", data={"username": username, "password": password}, **kw)


def _basic(username, password):
    creds = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# /login
# ---------------------------------------------------------------------------
class TestLoginThrottle:
    def test_throttles_after_threshold(self, client, multi_user, clock):
        for _ in range(FAILURE_THRESHOLD):
            r = _login(client, "owner", "wrong-pass")
            assert r.status_code == 200
        r = _login(client, "owner", "wrong-pass")
        assert r.status_code == 429
        assert r.headers["Retry-After"] == str(BASE_DELAY_SECONDS)
        assert b"Too many attempts. Try again in 1 minute." in r.data

    def test_correct_password_is_refused_inside_the_window(self, client, multi_user, clock):
        for _ in range(FAILURE_THRESHOLD):
            _login(client, "owner", "wrong-pass")
        with patch("routes.auth.verify_password") as verify:
            r = _login(client, "owner", "owner-test-pass")
        assert r.status_code == 429
        verify.assert_not_called()  # the credential is not even checked
        with client.session_transaction() as sess:
            assert "user_id" not in sess

    def test_recovers_after_the_window(self, client, multi_user, clock):
        for _ in range(FAILURE_THRESHOLD):
            _login(client, "owner", "wrong-pass")
        clock.advance(BASE_DELAY_SECONDS)
        r = _login(client, "owner", "owner-test-pass")
        assert r.status_code == 302
        with client.session_transaction() as sess:
            assert sess.get("user_id")

    def test_success_resets_the_count(self, client, multi_user, clock):
        for _ in range(FAILURE_THRESHOLD - 1):
            _login(client, "owner", "wrong-pass")
        assert _login(client, "owner", "owner-test-pass").status_code == 302
        client.post("/logout")
        for _ in range(FAILURE_THRESHOLD - 1):
            assert _login(client, "owner", "wrong-pass").status_code == 200
        assert _login(client, "owner", "owner-test-pass").status_code == 302

    def test_window_grows_on_the_next_failure(self, client, multi_user, clock):
        for _ in range(FAILURE_THRESHOLD):
            _login(client, "owner", "wrong-pass")
        clock.advance(BASE_DELAY_SECONDS)
        _login(client, "owner", "wrong-pass")
        r = _login(client, "owner", "wrong-pass")
        assert r.status_code == 429
        assert r.headers["Retry-After"] == str(BASE_DELAY_SECONDS * 2)

    def test_other_address_is_not_blocked(self, client, multi_user, clock):
        for _ in range(FAILURE_THRESHOLD):
            _login(client, "owner", "wrong-pass")
        assert _login(client, "owner", "wrong-pass").status_code == 429
        r = _login(client, "owner", "owner-test-pass", environ_base=OTHER_ADDRESS)
        assert r.status_code == 302

    def test_valid_login_from_a_shared_address_works_while_a_device_is_stuck(
            self, client, multi_user, clock):
        """Every client behind one Docker gateway / proxy shares remote_addr."""
        statuses = set()
        for _ in range(100):
            statuses.add(_login(client, "kid", "stale-pass").status_code)
            clock.advance(60)
        assert statuses == {200, 429}  # the device is throttled on and off...
        # ...and everyone else at the address signs in.
        assert _login(client, "owner", "owner-test-pass").status_code == 302

    def test_spraying_usernames_throttles_the_address(self, client, multi_user, clock):
        from core.auth_throttle import ADDRESS_FAILURE_THRESHOLD

        for i in range(ADDRESS_FAILURE_THRESHOLD):
            _login(client, f"guess{i}", "wrong-pass")
        assert _login(client, "owner", "owner-test-pass").status_code == 429
        assert _login(client, "owner", "owner-test-pass",
                      environ_base=OTHER_ADDRESS).status_code == 302

    def test_unknown_user_is_throttled_the_same_way(self, client, multi_user, clock):
        for _ in range(FAILURE_THRESHOLD):
            _login(client, "nobody", "wrong-pass")
        known = []
        for _ in range(FAILURE_THRESHOLD):
            known.append(_login(client, "owner", "wrong-pass", environ_base=OTHER_ADDRESS))
        r_unknown = _login(client, "nobody", "wrong-pass")
        r_known = _login(client, "owner", "wrong-pass", environ_base=OTHER_ADDRESS)
        assert r_unknown.status_code == r_known.status_code == 429
        assert r_unknown.data == r_known.data
        assert r_unknown.headers["Retry-After"] == r_known.headers["Retry-After"]

    def test_next_parameter_still_followed_after_throttle(self, client, multi_user, clock):
        for _ in range(FAILURE_THRESHOLD):
            _login(client, "owner", "wrong-pass")
        clock.advance(BASE_DELAY_SECONDS)
        r = client.post("/login?next=/files",
                        data={"username": "owner", "password": "owner-test-pass"},
                        headers={"Origin": "http://localhost"})
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/files")

    def test_cross_site_post_is_still_refused(self, client, multi_user, clock):
        r = client.post("/login", data={"username": "owner", "password": "owner-test-pass"},
                        headers={"Origin": "http://evil.example"})
        assert r.status_code == 403


class TestLoginResponsesDoNotRevealUsernames:
    def test_unknown_user_and_wrong_password_look_the_same(self, client, multi_user):
        r_unknown = _login(client, "nobody", "wrong-pass")
        r_wrong = _login(client, "owner", "wrong-pass")
        assert r_unknown.status_code == r_wrong.status_code == 200
        assert r_unknown.data == r_wrong.data

    def test_disabled_account_looks_like_a_wrong_password(self, client, multi_user):
        kid = get_user_by_username("kid")
        update_user(kid["id"], is_active=False)
        r_disabled = _login(client, "kid", "kid-test-pass")
        r_wrong = _login(client, "owner", "wrong-pass")
        assert r_disabled.status_code == r_wrong.status_code == 200
        assert r_disabled.data == r_wrong.data


class TestVerifyPasswordDoesTheSameWork:
    """Structural, not wall-clock: every path calls the hash check once."""

    def _spy(self):
        import werkzeug.security as ws
        return patch.object(ws, "check_password_hash", wraps=ws.check_password_hash)

    def test_unknown_user_checks_a_hash(self, db_connection):
        from core.database import _dummy_password_hash, verify_password

        with self._spy() as spy:
            assert verify_password("nobody", "wrong-pass") is None
        spy.assert_called_once()
        assert spy.call_args[0][0] == _dummy_password_hash()

    def test_wrong_password_checks_a_hash(self, db_connection):
        from core.database import verify_password

        create_user("owner", password="owner-test-pass", role="owner")
        with self._spy() as spy:
            assert verify_password("owner", "wrong-pass") is None
        spy.assert_called_once()

    def test_disabled_account_checks_its_real_hash(self, db_connection):
        from core.database import verify_password

        uid = create_user("kid", password="kid-test-pass", role="reader")
        update_user(uid, is_active=False)
        with self._spy() as spy:
            assert verify_password("kid", "kid-test-pass") is None
        spy.assert_called_once()
        assert spy.call_args[0][0] == get_user_by_username("kid")["password_hash"]

    def test_placeholder_account_checks_the_dummy(self, db_connection):
        from core.database import _dummy_password_hash, verify_password

        create_user("owner", password=None, role="owner", needs_setup=1)
        with self._spy() as spy:
            assert verify_password("owner", "") is None
        spy.assert_called_once()
        assert spy.call_args[0][0] == _dummy_password_hash()

    def test_dummy_uses_the_same_method_as_real_hashes(self, db_connection):
        from core.database import _dummy_password_hash

        create_user("owner", password="owner-test-pass", role="owner")
        real = get_user_by_username("owner")["password_hash"]
        # werkzeug hashes are "method$salt$hash"; method carries the cost.
        assert _dummy_password_hash().split("$")[0] == real.split("$")[0]


class TestSingleUserUnaffected:
    def test_login_post_redirects_and_never_throttles(self, client, db_connection, monkeypatch, clock):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        for _ in range(FAILURE_THRESHOLD * 3):
            r = _login(client, "owner", "wrong-pass")
            assert r.status_code == 302
        assert len(throttle) == 0

    def test_opds_needs_no_auth(self, client, db_connection, monkeypatch, clock):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        for _ in range(FAILURE_THRESHOLD * 2):
            r = client.get("/opds/", headers=_basic("owner", "wrong-pass"))
            assert r.status_code != 429
        assert len(throttle) == 0


# ---------------------------------------------------------------------------
# OPDS Basic auth
# ---------------------------------------------------------------------------
class TestOpdsThrottle:
    def test_429_without_a_challenge(self, client, multi_user, clock):
        for _ in range(FAILURE_THRESHOLD):
            r = client.get("/opds/", headers=_basic("owner", "wrong-pass"))
            assert r.status_code == 401
            assert "WWW-Authenticate" in r.headers
        r = client.get("/opds/", headers=_basic("owner", "wrong-pass"))
        assert r.status_code == 429
        assert r.headers["Retry-After"] == str(BASE_DELAY_SECONDS)
        assert "WWW-Authenticate" not in r.headers

    def test_correct_password_refused_inside_the_window(self, client, multi_user, clock):
        for _ in range(FAILURE_THRESHOLD):
            client.get("/opds/", headers=_basic("owner", "wrong-pass"))
        assert client.get("/opds/", headers=_basic("owner", "owner-test-pass")).status_code == 429
        clock.advance(BASE_DELAY_SECONDS)
        assert client.get("/opds/", headers=_basic("owner", "owner-test-pass")).status_code == 200

    def test_no_credentials_do_not_count(self, client, multi_user, clock):
        for _ in range(FAILURE_THRESHOLD * 3):
            r = client.get("/opds/")
            assert r.status_code == 401
        assert len(throttle) == 0

    def test_stale_reader_does_not_block_another_account_at_the_address(
            self, client, multi_user, clock):
        statuses = set()
        for _ in range(100):
            statuses.add(client.get("/opds/", headers=_basic("kid", "stale-pass")).status_code)
            clock.advance(60)
        assert statuses == {401, 429}
        assert client.get("/opds/", headers=_basic("owner", "owner-test-pass")).status_code == 200

    def test_opds_failures_do_not_block_web_login(self, client, multi_user, clock):
        for _ in range(FAILURE_THRESHOLD):
            client.get("/opds/", headers=_basic("owner", "wrong-pass"))
        assert _login(client, "owner", "owner-test-pass").status_code == 302


# ---------------------------------------------------------------------------
# Bearer tokens: /api/v1 and /api/insights
# ---------------------------------------------------------------------------
class TestApiV1Throttle:
    @pytest.fixture
    def token(self, multi_user):
        return create_api_token(get_user_by_username("kid")["id"], name="phone")

    def test_429_after_threshold(self, client, token, clock):
        for _ in range(FAILURE_THRESHOLD):
            assert client.get("/api/v1/auth/ping", headers=_bearer("wrong-token")).status_code == 401
        r = client.get("/api/v1/auth/ping", headers=_bearer("wrong-token"))
        assert r.status_code == 429
        assert r.headers["Retry-After"] == str(BASE_DELAY_SECONDS)
        assert r.get_json()["error"] == "too_many_attempts"

    def test_stale_token_throttles_only_itself(self, client, token, clock):
        """A dashboard polling a revoked token every 30s from the shared
        address must not lock out a valid client at that address."""
        statuses = set()
        for _ in range(100):
            statuses.add(client.get("/api/v1/auth/ping",
                                    headers=_bearer("revoked-token")).status_code)
            clock.advance(30)
        assert statuses == {401, 429}
        assert client.get("/api/v1/auth/ping", headers=_bearer(token)).status_code == 200

    def test_spraying_tokens_refuses_a_valid_one_then_recovers(self, client, token, clock):
        from core.auth_throttle import ADDRESS_FAILURE_THRESHOLD

        for i in range(ADDRESS_FAILURE_THRESHOLD):
            client.get("/api/v1/auth/ping", headers=_bearer(f"guess-{i}"))
        with patch("core.auth.user_for_bearer_token") as check:
            r = client.get("/api/v1/auth/ping", headers=_bearer(token))
        assert r.status_code == 429
        check.assert_not_called()  # the token is not even checked
        clock.advance(BASE_DELAY_SECONDS)
        assert client.get("/api/v1/auth/ping", headers=_bearer(token)).status_code == 200

    def test_other_address_unaffected(self, client, token, clock):
        from core.auth_throttle import ADDRESS_FAILURE_THRESHOLD

        for i in range(ADDRESS_FAILURE_THRESHOLD):
            client.get("/api/v1/auth/ping", headers=_bearer(f"guess-{i}"))
        r = client.get("/api/v1/auth/ping", headers=_bearer(token), environ_base=OTHER_ADDRESS)
        assert r.status_code == 200

    def test_no_credential_does_not_count(self, client, token, clock):
        for _ in range(FAILURE_THRESHOLD * 3):
            assert client.get("/api/v1/auth/ping").status_code == 401
            assert client.get("/api/v1/auth/ping",
                              headers={"Authorization": "Bearer   "}).status_code == 401
            assert client.get("/api/v1/auth/ping",
                              headers={"Authorization": "Basic abc"}).status_code == 401
        assert len(throttle) == 0
        assert client.get("/api/v1/auth/ping", headers=_bearer(token)).status_code == 200

    def test_legacy_token_and_non_ascii_is_a_plain_401(self, client, db_connection, clock):
        from core.database import set_user_preference

        set_user_preference("api_token", "legacy-test-value", category="security")
        r = client.get("/api/v1/auth/ping",
                       headers={"Authorization": "Bearer café-wrong"})
        assert r.status_code == 401
        for _ in range(FAILURE_THRESHOLD):
            client.get("/api/v1/auth/ping", headers=_bearer("wrong-token"))
        assert client.get("/api/v1/auth/ping",
                          headers=_bearer("wrong-token")).status_code == 429
        assert client.get("/api/v1/auth/ping",
                          headers=_bearer("legacy-test-value")).status_code == 200


class TestInsightsThrottle:
    def test_bad_token_throttled_but_anonymous_is_not(self, client, multi_user, clock):
        for _ in range(FAILURE_THRESHOLD):
            assert client.get("/api/insights", headers=_bearer("wrong-token")).status_code == 401
        r = client.get("/api/insights", headers=_bearer("wrong-token"))
        assert r.status_code == 429
        assert r.headers["Retry-After"] == str(BASE_DELAY_SECONDS)
        # No credential is not a guess: the anonymous view still answers.
        assert client.get("/api/insights").status_code != 429

    def test_insights_and_api_v1_share_the_token_scope(self, client, multi_user, clock):
        create_api_token(get_user_by_username("kid")["id"])
        for _ in range(FAILURE_THRESHOLD):
            client.get("/api/insights", headers=_bearer("wrong-token"))
        assert client.get("/api/v1/auth/ping", headers=_bearer("wrong-token")).status_code == 429
