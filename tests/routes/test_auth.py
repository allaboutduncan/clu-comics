"""
Routes: /login, /logout and the before_app_request auth gate.

Auth is now opt-in and DB-user based (see routes/auth.py + core/auth.py):
- Implicit-owner mode: no accounts + no env gate → no login required.
- Login required once >1 account exists (or the CLU_USERNAME/CLU_PASSWORD env
  gate is set), validated against the users table.
"""
import pytest

from core.database import create_user, seed_owner_if_needed


class TestImplicitOwnerMode:
    """With no real accounts and no env gate, the app requires no login."""

    def test_pages_accessible_without_login(self, client):
        r = client.get("/")
        assert r.status_code == 200

    def test_login_page_redirects_to_index(self, client):
        r = client.get("/login")
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/")


class TestLoginRequiredMultiUser:
    """More than one account exists → browser login is enforced."""

    @pytest.fixture(autouse=True)
    def _two_users(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("owner", password="ownerpass", role="owner")
        create_user("reader", password="readerpass", role="reader")
        yield

    def test_unauthenticated_redirects_to_login(self, client):
        r = client.get("/")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_login_page_renders(self, client):
        r = client.get("/login")
        assert r.status_code == 200
        assert "Sign In" in r.get_data(as_text=True)

    def test_correct_credentials_authenticate(self, client):
        r = client.post(
            "/login",
            data={"username": "reader", "password": "readerpass"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert client.get("/").status_code == 200

    def test_wrong_credentials_show_error(self, client):
        r = client.post("/login", data={"username": "reader", "password": "wrong"})
        assert r.status_code == 200
        assert "Invalid" in r.get_data(as_text=True)

    def test_next_param_redirect(self, client):
        r = client.post(
            "/login?next=/config",
            data={"username": "owner", "password": "ownerpass"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/config")

    def test_logout_clears_session(self, client):
        client.post("/login", data={"username": "owner", "password": "ownerpass"})
        assert client.get("/").status_code == 200

        r = client.get("/logout")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

        # Session cleared → gated again.
        r = client.get("/")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_opds_exempt_from_auth(self, client):
        r = client.get("/opds")
        assert "/login" not in r.headers.get("Location", "")

    def test_static_exempt_from_auth(self, client):
        r = client.get("/static/images/clu.png")
        assert r.status_code != 302 or "/login" not in r.headers.get("Location", "")


class TestEnvGateBackwardCompat:
    """The legacy CLU_USERNAME/CLU_PASSWORD env gate still forces login, now
    routed through a seeded owner account."""

    @pytest.fixture(autouse=True)
    def _env_gate(self, db_connection, monkeypatch):
        monkeypatch.setenv("CLU_USERNAME", "admin")
        monkeypatch.setenv("CLU_PASSWORD", "secret")
        seed_owner_if_needed()  # migrate env creds into a hashed owner account
        yield

    def test_unauthenticated_redirects_to_login(self, client):
        r = client.get("/")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_env_credentials_authenticate(self, client):
        r = client.post(
            "/login",
            data={"username": "admin", "password": "secret"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert client.get("/").status_code == 200
