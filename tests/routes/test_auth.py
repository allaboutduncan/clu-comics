"""
Routes: /login, /logout and the before_app_request auth gate.
"""
import pytest


class TestAuthDisabled:
    """When CLU_USERNAME / CLU_PASSWORD are not set, auth is off."""

    def test_pages_accessible_without_login(self, client):
        r = client.get("/")
        assert r.status_code == 200

    def test_login_page_redirects_to_index(self, client):
        r = client.get("/login")
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/")


class TestAuthEnabled:
    """When CLU_USERNAME and CLU_PASSWORD are set, auth is required."""

    @pytest.fixture(autouse=True)
    def _enable_auth(self, app):
        app.config["CLU_USERNAME"] = "admin"
        app.config["CLU_PASSWORD"] = "secret"
        yield
        app.config["CLU_USERNAME"] = ""
        app.config["CLU_PASSWORD"] = ""

    def test_unauthenticated_redirects_to_login(self, client):
        r = client.get("/")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_login_page_renders(self, client):
        r = client.get("/login")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Sign In" in body

    def test_correct_credentials_authenticate(self, client):
        r = client.post(
            "/login",
            data={"username": "admin", "password": "secret"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        # After login, index should be accessible
        r2 = client.get("/")
        assert r2.status_code == 200

    def test_wrong_credentials_show_error(self, client):
        r = client.post(
            "/login",
            data={"username": "admin", "password": "wrong"},
        )
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Invalid" in body

    def test_next_param_redirect(self, client):
        r = client.post(
            "/login?next=/config",
            data={"username": "admin", "password": "secret"},
            follow_redirects=False,
        )
        assert r.status_code == 302
        assert r.headers["Location"].endswith("/config")

    def test_logout_clears_session(self, client):
        # Login first
        client.post(
            "/login",
            data={"username": "admin", "password": "secret"},
        )
        # Verify authenticated
        r = client.get("/")
        assert r.status_code == 200

        # Logout
        r = client.get("/logout")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

        # Should be redirected again
        r = client.get("/")
        assert r.status_code == 302
        assert "/login" in r.headers["Location"]

    def test_opds_exempt_from_auth(self, client):
        r = client.get("/opds")
        # OPDS may return 200 or its own status, but NOT a redirect to /login
        assert "/login" not in r.headers.get("Location", "")

    def test_static_exempt_from_auth(self, client):
        r = client.get("/static/images/clu.png")
        assert r.status_code != 302 or "/login" not in r.headers.get("Location", "")
