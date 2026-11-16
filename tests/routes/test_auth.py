"""
Routes: /login, /logout and the before_app_request auth gate.

Auth is now opt-in and DB-user based (see routes/auth.py + core/auth.py):
- Implicit-owner mode: no accounts + no env gate → no login required.
- Login required once >1 account exists (or the CLU_USERNAME/CLU_PASSWORD env
  gate is set), validated against the users table.
"""
from urllib.parse import parse_qs, urlsplit

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

    def _login_location(self, client, next_value, **kw):
        r = client.post(
            "/login",
            query_string={"next": next_value},
            data={"username": "owner", "password": "ownerpass"},
            follow_redirects=False,
            **kw,
        )
        assert r.status_code == 302
        return r.headers["Location"]

    def test_next_keeps_query_string(self, client):
        loc = self._login_location(client, "/collection?page=2")
        assert loc == "/collection?page=2"

    @pytest.mark.parametrize("next_value", [
        "http://evil.example/",
        "https://evil.example/config",
        "//evil.example",
        "///evil.example",
        "/\\evil.example",
        "\\\\evil.example",
        "\\/evil.example",
        "/\t/evil.example",
        "/\n/evil.example",
        "\t//evil.example",
        " //evil.example",
        "/\x00/evil.example",
        "javascript:alert(1)",
        "config",
    ])
    def test_unsafe_next_falls_back_to_index(self, client, next_value):
        loc = self._login_location(client, next_value)
        assert "evil.example" not in loc
        assert "javascript" not in loc
        assert loc in ("/", "http://localhost/")

    def test_next_under_script_root_is_followed(self, client):
        loc = self._login_location(
            client, "/clu/config", base_url="http://localhost/clu/")
        assert loc == "/clu/config"

    def test_next_outside_script_root_falls_back(self, client):
        loc = self._login_location(
            client, "/config", base_url="http://localhost/clu/")
        assert loc in ("/clu/", "http://localhost/clu/")

    def test_gate_puts_script_root_and_query_in_next(self, client):
        r = client.get("/config?tab=x", base_url="http://localhost/clu/")
        assert r.status_code == 302
        loc = r.headers["Location"]
        parts = urlsplit(loc)
        assert parts.path == "/clu/login"
        assert parse_qs(parts.query)["next"] == ["/clu/config?tab=x"]

    def test_logout_clears_session(self, client):
        client.post("/login", data={"username": "owner", "password": "ownerpass"})
        assert client.get("/").status_code == 200

        r = client.post("/logout")
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

    def test_download_exempt_from_auth(self, client):
        # The browser extension POSTs {link:...} with no session cookie; the auth
        # gate must not redirect it to /login (regression: multi-user login gate).
        r = client.post(
            "/download",
            json={"link": "https://example.com/x.cbz"},
            follow_redirects=False,
        )
        assert r.status_code != 302
        assert "/login" not in r.headers.get("Location", "")

    def test_download_status_requires_login(self, client):
        # Only POST /download is exempt; the queue's status endpoints are used
        # by the logged-in Status page (tests/routes/test_download_queue_auth.py).
        r = client.get("/download_status_all", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]


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
