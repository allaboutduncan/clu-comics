"""Tests for routes/admin.py -- /api/admin/api-token endpoints used by the config page."""
from core.database import get_api_token, set_user_preference


class TestGetApiToken:

    def test_returns_unconfigured_when_no_token(self, db_connection, client):
        resp = client.get("/api/admin/api-token")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["configured"] is False
        assert body["token"] == ""

    def test_returns_token_when_present(self, db_connection, client):
        set_user_preference("api_token", "preset-token", category="security")
        resp = client.get("/api/admin/api-token")
        body = resp.get_json()
        assert body["configured"] is True
        assert body["token"] == "preset-token"


class TestRotateApiToken:

    def test_rotate_generates_token_when_none_exists(self, db_connection, client):
        assert get_api_token() is None
        resp = client.post("/api/admin/api-token/rotate")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["token"]
        # Persisted to user_preferences
        assert get_api_token() == body["token"]

    def test_rotate_replaces_existing_token(self, db_connection, client):
        set_user_preference("api_token", "old-token", category="security")
        resp = client.post("/api/admin/api-token/rotate")
        body = resp.get_json()
        assert body["success"] is True
        assert body["token"] != "old-token"
        assert get_api_token() == body["token"]

    def test_rotated_tokens_are_distinct(self, db_connection, client):
        first = client.post("/api/admin/api-token/rotate").get_json()["token"]
        second = client.post("/api/admin/api-token/rotate").get_json()["token"]
        assert first and second
        assert first != second
        # The latest rotation wins
        assert get_api_token() == second
