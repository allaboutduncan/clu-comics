"""A settings save must not sign the user out.

``load_flask_config()`` runs again on every settings save -- provider
credentials, file processing, download/API, system perf, and the main config
POST. It used to mint a fresh ``app.secret_key`` on each call whenever no
SECRET_KEY env var was set (the default Docker setup), which invalidated every
session cookie signed with the previous key. Flask sends no replacement cookie
on those requests because the session itself is never modified, so the save
returned 200 while the *next* request arrived unauthenticated and bounced to
/login.

Only installs where login is actually required saw it: with a single user
``is_login_required()`` is False and ``resolve_current_user`` falls back to the
implicit owner, which masked the dead cookie.
"""
import pytest

from core.database import create_user


def _login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


class TestSessionSurvivesSettingsSave:

    @pytest.fixture(autouse=True)
    def _users(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        monkeypatch.delenv("SECRET_KEY", raising=False)
        # Two users, so login is genuinely required and a dead cookie shows up
        # rather than falling back to the implicit owner.
        create_user("owner", password="ownerpass", role="owner")
        create_user("reader", password="readerpass", role="reader")
        yield

    def test_saving_provider_credentials_keeps_the_session(self, client, app):
        _login(client, "owner", "ownerpass")
        assert client.get("/config").status_code == 200

        before = app.secret_key
        resp = client.post(
            "/api/providers/comicvine/credentials",
            json={"api_key": "abc123"},
        )
        assert resp.status_code == 200

        assert app.secret_key == before, "settings save rotated the secret key"
        assert client.get("/config").status_code == 200, \
            "user was logged out by saving provider credentials"

    def test_repeated_saves_keep_the_session(self, client, app):
        """The user-visible loop: every save logged them straight back out."""
        _login(client, "owner", "ownerpass")

        for key in ("abc123", "def456", "ghi789"):
            assert client.post(
                "/api/providers/comicvine/credentials", json={"api_key": key}
            ).status_code == 200
            assert client.get("/config").status_code == 200

    def test_provider_test_endpoint_reachable_after_a_save(self, client):
        """Saving then testing is the exact flow from the bug report."""
        _login(client, "owner", "ownerpass")
        client.post("/api/providers/comicvine/credentials", json={"api_key": "abc123"})

        resp = client.post("/api/providers/comicvine/test")
        assert resp.status_code == 200, "test endpoint bounced to /login"
        assert resp.is_json, "test endpoint returned the login page, not JSON"
