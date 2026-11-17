"""Cross-site request forgery: the same-origin check, the session cookie and
POST-only logout, against the test app (core/request_origin.py, routes/auth.py).

The rest of the suite POSTs with Flask's test client, which sends no Origin,
Referer or Sec-Fetch-Site; that is the non-browser case and must keep passing.

Every assertion about who is logged in is made by a *fresh request* reading its
own session, never by reading ``g.current_user`` afterwards (pytest-flask's
shared context leaks it on Linux CI).
"""
from unittest.mock import MagicMock

import pytest
from werkzeug.middleware.proxy_fix import ProxyFix

import core.app_logging
from core.database import create_user, get_user_setting_override
from core.request_origin import TRUSTED_ORIGINS_ENV, _reset_logged

EVIL = {"Origin": "http://evil.example"}
SAME = {"Origin": "http://localhost"}
PAGE_WIDTH = "/api/account/page-width"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("CLU_USERNAME", raising=False)
    monkeypatch.delenv("CLU_PASSWORD", raising=False)
    monkeypatch.delenv(TRUSTED_ORIGINS_ENV, raising=False)
    _reset_logged()
    yield
    _reset_logged()


def _set_width(client, width="wide", headers=None, **kw):
    return client.post(PAGE_WIDTH, json={"pageWidth": width}, headers=headers or {}, **kw)


def _owner_width():
    from core.database import get_owner_user
    return get_user_setting_override("page_width", user_id=get_owner_user()["id"])


class TestSingleUserMode:
    """No login and no cookie: exactly the case a hostile page would target."""

    @pytest.fixture(autouse=True)
    def _owner(self, db_connection):
        # An open install: no password anywhere (a password means login).
        create_user("owner", password=None, role="owner", needs_setup=1)

    def test_cross_site_post_refused_and_changes_nothing(self, client):
        r = _set_width(client, headers=EVIL)
        assert r.status_code == 403
        body = r.get_json()
        assert "Cross-site request refused" in body["error"]
        assert TRUSTED_ORIGINS_ENV in body["error"]
        assert _owner_width() is None

    def test_cross_site_fetch_metadata_refused(self, client):
        r = _set_width(client, headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 403
        assert _owner_width() is None

    def test_null_origin_refused(self, client):
        assert _set_width(client, headers={"Origin": "null"}).status_code == 403

    def test_same_origin_post_allowed(self, client):
        r = _set_width(client, headers=SAME)
        assert r.status_code == 200
        assert _owner_width() == "wide"

    def test_same_origin_fetch_metadata_allowed(self, client):
        r = _set_width(client, headers={"Sec-Fetch-Site": "same-origin", **SAME})
        assert r.status_code == 200

    def test_same_origin_referer_only_allowed(self, client):
        r = _set_width(client, headers={"Referer": "http://localhost/account"})
        assert r.status_code == 200

    def test_non_browser_client_allowed(self, client):
        assert _set_width(client).status_code == 200

    def test_get_never_blocked(self, client):
        r = client.get("/reading-lists", headers={**EVIL, "Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 200

    def test_form_post_gets_plain_text_403(self, client):
        r = client.post("/logout", headers=EVIL)
        assert r.status_code == 403
        assert r.mimetype == "text/plain"
        assert TRUSTED_ORIGINS_ENV in r.get_data(as_text=True)

    def test_trusted_origins_env_accepted(self, client, monkeypatch):
        monkeypatch.setenv(TRUSTED_ORIGINS_ENV, "https://comics.example.com")
        r = _set_width(client, headers={"Origin": "https://comics.example.com"})
        assert r.status_code == 200
        assert _set_width(client, headers=EVIL).status_code == 403

    def test_x_forwarded_host_accepted(self, client):
        r = _set_width(client, headers={
            "Origin": "https://comics.example.com",
            "X-Forwarded-Host": "comics.example.com",
        })
        assert r.status_code == 200

    def test_extension_download_endpoint_not_refused(self, client):
        r = client.post("/download", json={"link": "https://example.com/x.cbz"},
                        headers={"Origin": "chrome-extension://abcdefghijklmnop"})
        assert r.status_code != 403

    def test_api_v1_without_bearer_refused(self, client):
        r = client.post("/api/v1/progress", json={}, headers=EVIL)
        assert r.status_code == 403
        assert "Cross-site request refused" in r.get_json()["error"]

    def test_api_v1_with_bearer_reaches_the_token_check(self, client):
        r = client.post("/api/v1/progress", json={},
                        headers={**EVIL, "Authorization": "Bearer not-a-real-value"})
        # Past the origin check; the token API answers for itself.
        assert r.status_code != 403

    def test_warning_logged_once_per_origin(self, client, monkeypatch):
        logger = MagicMock()
        monkeypatch.setattr(core.app_logging, "app_logger", logger)
        for _ in range(3):
            _set_width(client, headers=EVIL)
        _set_width(client, headers={"Origin": "http://other.example"})
        assert logger.warning.call_count == 2


class TestLoggedIn:
    @pytest.fixture(autouse=True)
    def _users(self, db_connection):
        create_user("owner", password="ownerpass", role="owner")
        create_user("reader", password="readerpass", role="reader")

    def _login(self, client, headers=None, **kw):
        return client.post("/login", data={"username": "owner", "password": "ownerpass"},
                           headers=headers or SAME, follow_redirects=False, **kw)

    def test_login_from_own_page(self, client):
        r = self._login(client)
        assert r.status_code == 302
        assert client.get("/").status_code == 200

    def test_login_with_fetch_metadata(self, client):
        r = self._login(client, headers={"Sec-Fetch-Site": "same-origin", **SAME})
        assert r.status_code == 302

    def test_cross_site_login_refused(self, client):
        r = self._login(client, headers=EVIL)
        assert r.status_code == 403
        assert client.get("/").status_code == 302  # still anonymous

    def test_login_on_a_subpath(self, client):
        r = self._login(client, headers={"Origin": "http://localhost"},
                        base_url="http://localhost/clu/")
        assert r.status_code == 302
        assert client.get("/", base_url="http://localhost/clu/").status_code == 200

    def test_login_behind_proxyfix_on_a_subpath(self, app):
        """As app.py wraps it: the proxy rewrites Host and forwards the public one."""
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
        client = app.test_client()
        headers = {
            "Host": "clu:5577",
            "X-Forwarded-Host": "comics.example.com",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Prefix": "/clu",
            "Origin": "https://comics.example.com",
        }
        r = client.post("/login", data={"username": "owner", "password": "ownerpass"},
                        headers=headers, follow_redirects=False)
        assert r.status_code == 302
        r = client.post("/login", data={"username": "owner", "password": "ownerpass"},
                        headers={**headers, **EVIL}, follow_redirects=False)
        assert r.status_code == 403

    def test_cross_site_post_refused_when_logged_in(self, client):
        self._login(client)
        r = _set_width(client, headers=EVIL)
        assert r.status_code == 403
        assert _owner_width() is None

    def test_same_origin_post_allowed_when_logged_in(self, client):
        self._login(client)
        r = _set_width(client, headers=SAME)
        assert r.status_code == 200
        assert _owner_width() == "wide"

    def test_session_cookie_attributes(self, client):
        r = self._login(client)
        cookies = [v for v in r.headers.getlist("Set-Cookie") if v.startswith("session=")]
        assert cookies, r.headers.getlist("Set-Cookie")
        attrs = [a.strip().lower() for a in cookies[0].split(";")]
        assert "samesite=lax" in attrs
        assert "httponly" in attrs
        # Not Secure: most installs are plain HTTP and would be locked out.
        assert "secure" not in attrs

    def test_cookie_config(self, app):
        assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
        assert app.config["SESSION_COOKIE_HTTPONLY"] is True
        assert not app.config.get("SESSION_COOKIE_SECURE")


class TestLogoutIsPostOnly:
    @pytest.fixture(autouse=True)
    def _users(self, db_connection):
        create_user("owner", password="ownerpass", role="owner")
        create_user("reader", password="readerpass", role="reader")

    def _login(self, client):
        r = client.post("/login", data={"username": "reader", "password": "readerpass"})
        assert r.status_code == 302

    def test_get_does_not_log_out(self, client):
        self._login(client)
        r = client.get("/logout", follow_redirects=False)
        assert r.status_code == 302
        assert "/login" not in r.headers["Location"]
        assert client.get("/").status_code == 200  # still signed in

    def test_post_logs_out(self, client):
        self._login(client)
        r = client.post("/logout", headers=SAME, follow_redirects=False)
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]
        r = client.get("/")
        assert r.status_code == 302 and "/login" in r.headers["Location"]

    def test_reader_may_post_logout(self, client):
        """A POST defaults to Clerk in the role check; /logout must stay exempt."""
        self._login(client)
        assert client.post("/logout").status_code == 302
        assert client.get("/").status_code == 302

    def test_cross_site_post_does_not_log_out(self, client):
        self._login(client)
        assert client.post("/logout", headers=EVIL).status_code == 403
        assert client.get("/").status_code == 200

    def test_header_renders_logout_as_a_post_form(self, client):
        self._login(client)
        html = client.get("/reading-lists").get_data(as_text=True)
        assert 'method="post" action="/logout"' in html
        assert 'href="/logout"' not in html
