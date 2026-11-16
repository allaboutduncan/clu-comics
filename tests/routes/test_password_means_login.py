"""
A password means login (core.auth.is_login_required).

Login used to start only with a *second* account, so an owner who set a
password on their own, single account still ran an open install: no login,
and anyone who could reach the port was the owner. Now any account holding a
password turns login on. These cover the whole state table:

    accounts  password set  env gate  -> login required?
    1         no            no           no  (open, unchanged)
    1         yes           no           yes (the fix)
    2         yes           no           yes (unchanged; test_auth.py)
    any       any           yes          yes (unchanged; test_auth.py)

plus the request that sets the first password, and the gate's freshness after
every write that can change its answer.
"""
import base64
import sqlite3

import pytest

import core.database as database
from core.auth import is_login_required
from core.database import (
    accounts_require_login,
    count_users,
    create_api_token,
    create_user,
    delete_user,
    get_owner_user,
    get_user_by_username,
    seed_owner_if_needed,
    set_user_password,
)

OWNER_NAME = "boss"
OWNER_PASS = "owner-pass-for-tests"


def _basic(username, password):
    creds = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


def _login(client, username, password):
    return client.post("/login", data={"username": username, "password": password},
                       follow_redirects=False)


def _clear_owner_password():
    """The documented recovery step: clear the first owner's password hash."""
    conn = database.get_db_connection()
    try:
        conn.execute(
            "UPDATE users SET password_hash = NULL, needs_setup = 1 WHERE id = "
            "(SELECT id FROM users WHERE role = 'owner' ORDER BY id LIMIT 1)"
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _no_env_gate(monkeypatch):
    monkeypatch.delenv("CLU_USERNAME", raising=False)
    monkeypatch.delenv("CLU_PASSWORD", raising=False)


# ---------------------------------------------------------------------------
# One account, no password: open, exactly as before
# ---------------------------------------------------------------------------
class TestOpenInstall:
    @pytest.fixture(autouse=True)
    def _placeholder_owner(self, db_connection):
        seed_owner_if_needed()  # passwordless placeholder, as a fresh install
        assert get_owner_user()["password_hash"] is None
        yield

    def test_login_is_not_required(self):
        assert is_login_required() is False

    def test_pages_open_without_login(self, client):
        assert client.get("/").status_code == 200
        assert client.get("/config").status_code == 200

    def test_login_page_sends_you_home(self, client):
        r = client.get("/login")
        assert r.status_code == 302
        assert "/login" not in r.headers["Location"]

    def test_opds_needs_no_credentials(self, client):
        assert client.get("/opds/").status_code != 401

    def test_users_list_says_the_install_is_open(self, client):
        data = client.get("/api/admin/users").get_json()
        assert data["login_required"] is False

    def test_users_page_carries_the_unauthenticated_notice(self):
        """The test app stubs /users (app.py cannot be imported), so the
        template is checked directly: the notice exists, says what it must,
        and is driven by the list response's login_required."""
        import os

        root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        with open(os.path.join(root, "templates", "users.html"), encoding="utf-8") as f:
            html = f.read()
        assert 'id="open-install-notice"' in html
        assert "This instance is unauthenticated." in html
        assert "turns on login" in html
        assert "data.login_required === false" in html


# ---------------------------------------------------------------------------
# One account WITH a password: login required (the fix)
# ---------------------------------------------------------------------------
class TestPasswordOnTheOnlyAccount:
    @pytest.fixture(autouse=True)
    def _passworded_owner(self, db_connection):
        create_user(OWNER_NAME, password=OWNER_PASS, role="owner")
        assert count_users() == 1
        yield

    def test_login_is_required(self):
        assert is_login_required() is True

    def test_pages_redirect_to_login(self, client):
        for path in ("/", "/config", "/users", "/account"):
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 302, path
            assert "/login" in r.headers["Location"], path

    def test_api_returns_401_json(self, client):
        r = client.get("/api/admin/users")
        assert r.status_code == 401
        assert r.get_json() == {"error": "unauthorized"}

    def test_mutating_api_returns_401_not_a_change(self, client):
        r = client.post("/api/admin/users", json={
            "username": "intruder", "password": "x", "role": "owner"})
        assert r.status_code == 401
        assert get_user_by_username("intruder") is None

    def test_opds_demands_basic_auth(self, client):
        r = client.get("/opds/")
        assert r.status_code == 401
        assert r.headers["WWW-Authenticate"].startswith("Basic")

    def test_opds_rejects_a_wrong_password(self, client):
        r = client.get("/opds/", headers=_basic(OWNER_NAME, "wrong"))
        assert r.status_code == 401

    def test_opds_accepts_the_owner_password(self, client):
        r = client.get("/opds/", headers=_basic(OWNER_NAME, OWNER_PASS))
        assert r.status_code == 200

    def test_api_v1_requires_a_bearer_token(self, client):
        token = create_api_token(get_user_by_username(OWNER_NAME)["id"], name="t")
        assert client.get("/api/v1/auth/ping").status_code == 401
        r = client.get("/api/v1/auth/ping", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200

    def test_download_post_stays_auth_free(self, client):
        r = client.post("/download", json={"link": "https://example.com/x.cbz"},
                        follow_redirects=False)
        assert r.status_code != 302
        assert "/login" not in r.headers.get("Location", "")

    def test_static_and_login_reachable(self, client):
        r = client.get("/static/images/clu.png", follow_redirects=False)
        assert "/login" not in r.headers.get("Location", "")
        r = client.get("/login")
        assert r.status_code == 200
        assert "Sign In" in r.get_data(as_text=True)

    def test_the_password_logs_in(self, client):
        r = _login(client, OWNER_NAME, OWNER_PASS)
        assert r.status_code == 302
        assert client.get("/config").status_code == 200
        data = client.get("/api/admin/users").get_json()
        assert data["login_required"] is True

    def test_logged_in_header_shows_logout(self, client):
        _login(client, OWNER_NAME, OWNER_PASS)
        html = client.get("/reading-lists").get_data(as_text=True)
        assert 'id="userMenu"' in html
        assert "/logout" in html

    def test_logout_gates_again(self, client):
        _login(client, OWNER_NAME, OWNER_PASS)
        assert client.post("/logout").status_code == 302
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]


# ---------------------------------------------------------------------------
# The request that sets the first password
# ---------------------------------------------------------------------------
class TestFirstPasswordTransition:
    @pytest.fixture(autouse=True)
    def _placeholder_owner(self, db_connection):
        seed_owner_if_needed()
        yield

    def _assert_login_now_works(self, app):
        """Another browser is gated, and the new password lets it in."""
        other = app.test_client()
        r = other.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]
        assert other.get("/opds/").status_code == 401
        assert _login(other, OWNER_NAME, OWNER_PASS).status_code == 302
        assert other.get("/config").status_code == 200

    def test_setup_owner_keeps_this_browser_signed_in(self, client, app):
        assert client.get("/").status_code == 200  # open, never logged in

        r = client.post("/api/admin/setup-owner", json={
            "username": OWNER_NAME, "password": OWNER_PASS})
        assert r.status_code == 200
        assert r.get_json()["login_required"] is True

        # Same browser, very next requests: still the owner, now with a session.
        assert client.get("/config").status_code == 200
        assert client.get("/api/admin/users").status_code == 200
        html = client.get("/reading-lists").get_data(as_text=True)
        assert "/logout" in html

        self._assert_login_now_works(app)

    def test_setup_owner_then_logout_then_password_works(self, client):
        client.post("/api/admin/setup-owner", json={
            "username": OWNER_NAME, "password": OWNER_PASS})
        client.post("/logout")
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 302 and "/login" in r.headers["Location"]
        assert _login(client, OWNER_NAME, OWNER_PASS).status_code == 302
        assert client.get("/").status_code == 200

    def test_users_page_password_keeps_this_browser_signed_in(self, client, app):
        owner_id = get_owner_user()["id"]
        r = client.put(f"/api/admin/users/{owner_id}", json={
            "username": OWNER_NAME, "password": OWNER_PASS})
        assert r.status_code == 200
        body = r.get_json()
        assert body["login_required"] is True
        assert body["login_turned_on"] is True
        assert get_owner_user()["needs_setup"] == 0

        assert client.get("/api/admin/users").status_code == 200
        self._assert_login_now_works(app)

    def test_a_later_password_change_does_not_report_a_transition(self, client):
        owner_id = get_owner_user()["id"]
        client.put(f"/api/admin/users/{owner_id}", json={"password": OWNER_PASS})
        r = client.put(f"/api/admin/users/{owner_id}", json={"password": OWNER_PASS + "2"})
        assert r.status_code == 200
        assert r.get_json()["login_turned_on"] is False

    def test_setting_the_password_does_not_sign_in_a_bystander(self, client, app):
        bystander = app.test_client()
        assert bystander.get("/").status_code == 200  # open a moment ago
        client.post("/api/admin/setup-owner", json={
            "username": OWNER_NAME, "password": OWNER_PASS})
        assert bystander.get("/api/admin/users").status_code == 401


# ---------------------------------------------------------------------------
# What counts as "has a password", and that the answer is never stale
# ---------------------------------------------------------------------------
class TestAccountsRequireLogin:
    def test_no_users_table_is_open(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty.db"
        sqlite3.connect(str(empty)).close()
        monkeypatch.setattr(database, "get_db_path", lambda: str(empty))
        assert accounts_require_login() is False

    def test_no_accounts_is_open(self, db_connection):
        assert count_users() == 0
        assert accounts_require_login() is False

    def test_placeholder_owner_is_open(self, db_connection):
        seed_owner_if_needed()
        assert accounts_require_login() is False

    def test_empty_string_hash_is_not_a_password(self, db_connection):
        seed_owner_if_needed()
        conn = database.get_db_connection()
        conn.execute("UPDATE users SET password_hash = ''")
        conn.commit()
        conn.close()
        assert accounts_require_login() is False

    def test_one_account_with_a_password_requires_login(self, db_connection):
        create_user(OWNER_NAME, password=OWNER_PASS, role="owner")
        assert accounts_require_login() is True

    def test_a_disabled_passworded_account_still_counts(self, db_connection):
        """Requiring login is the safe direction; nothing is opened by
        disabling an account."""
        uid = create_user(OWNER_NAME, password=OWNER_PASS, role="owner")
        database.update_user(uid, is_active=False)
        assert accounts_require_login() is True

    def test_two_accounts_require_login_regardless(self, db_connection):
        create_user("a", password=None, role="owner")
        create_user("b", password=None, role="reader")
        assert accounts_require_login() is True

    def test_answer_follows_every_write_immediately(self, db_connection):
        """Not cached: each write is visible on the very next call."""
        seed_owner_if_needed()
        owner_id = get_owner_user()["id"]
        assert is_login_required() is False

        set_user_password(owner_id, OWNER_PASS)          # set
        assert is_login_required() is True

        _clear_owner_password()                          # recovery clears it
        assert is_login_required() is False

        kid = create_user("kid", password=OWNER_PASS, role="reader")
        assert is_login_required() is True               # create
        delete_user(kid)
        assert is_login_required() is False              # delete

    def test_unreadable_database_keeps_the_last_answer(
            self, db_connection, monkeypatch):
        """A corrupt or locked database must not open a passworded install."""
        create_user(OWNER_NAME, password=OWNER_PASS, role="owner")
        assert accounts_require_login() is True

        monkeypatch.setattr(database, "get_db_connection", lambda: None)
        assert accounts_require_login() is True

    def test_unreadable_database_with_no_history_is_open(self, monkeypatch):
        monkeypatch.setattr(database, "_accounts_require_login_last", False)
        monkeypatch.setattr(database, "get_db_connection", lambda: None)
        assert accounts_require_login() is False


# ---------------------------------------------------------------------------
# Upgrade: an owner who set a password before this change
# ---------------------------------------------------------------------------
class TestUpgradeFromSingleAccountWithPassword:
    """Installs where the owner went through /setup-owner while theirs was the
    only account were open until now. After upgrading they are asked for that
    password, and it has to work: it was stored by the same set_user_password
    (werkzeug hash) that verify_password checks."""

    def test_existing_setup_owner_password_logs_in(self, client, db_connection):
        seed_owner_if_needed()
        set_user_password(get_owner_user()["id"], OWNER_PASS)  # what setup-owner did
        database.update_user(get_owner_user()["id"], username=OWNER_NAME)

        r = client.get("/", follow_redirects=False)
        assert r.status_code == 302 and "/login" in r.headers["Location"]
        assert _login(client, OWNER_NAME, OWNER_PASS).status_code == 302
        assert client.get("/config").status_code == 200
