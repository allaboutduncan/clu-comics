"""Tests for routes/admin.py -- /api/admin/api-token endpoints used by the config page."""
import io
import json
import zipfile

from core.database import get_api_browse_mode, get_api_token, set_user_preference


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


class TestApiBrowseMode:

    def test_default_is_metadata(self, db_connection, client):
        resp = client.get("/api/admin/api-browse-mode")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["mode"] == "metadata"

    def test_put_persists_filesystem(self, db_connection, client):
        resp = client.put(
            "/api/admin/api-browse-mode",
            data=json.dumps({"mode": "filesystem"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["mode"] == "filesystem"
        # Subsequent GET reflects it
        assert client.get("/api/admin/api-browse-mode").get_json()["mode"] == "filesystem"
        assert get_api_browse_mode() == "filesystem"

    def test_put_invalid_mode_400(self, db_connection, client):
        resp = client.put(
            "/api/admin/api-browse-mode",
            data=json.dumps({"mode": "bogus"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        # Preference unchanged (still metadata)
        assert get_api_browse_mode() == "metadata"


class TestDebugPackage:

    def test_returns_zip_attachment(self, db_connection, client):
        resp = client.get("/api/admin/debug-package")
        assert resp.status_code == 200
        assert resp.mimetype == "application/zip"
        assert "attachment" in resp.headers["Content-Disposition"]
        assert "clu-debug-" in resp.headers["Content-Disposition"]

    def test_zip_contains_expected_members(self, db_connection, client):
        resp = client.get("/api/admin/debug-package")
        with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
            names = set(zf.namelist())
        assert {
            "README.txt",
            "system_info.json",
            "config.ini",
            "db_settings.json",
            "logs/app.log",
            "logs/monitor.log",
        } <= names

    def test_db_settings_is_valid_json(self, db_connection, client):
        resp = client.get("/api/admin/debug-package")
        with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
            payload = json.loads(zf.read("db_settings.json"))
        assert "user_preferences" in payload
        assert "tables" in payload

    def test_secret_preference_is_redacted(self, db_connection, client):
        # Seed a preference whose key marks it as sensitive.
        set_user_preference(
            "COMICVINE_API_KEY", "SECRET1234VALUE", category="providers"
        )
        resp = client.get("/api/admin/debug-package")
        # Raw secret must not appear anywhere in the package bytes.
        assert b"SECRET1234VALUE" not in resp.data
        with zipfile.ZipFile(io.BytesIO(resp.data)) as zf:
            payload = json.loads(zf.read("db_settings.json"))
        entry = next(
            p for p in payload["user_preferences"] if p["key"] == "COMICVINE_API_KEY"
        )
        assert "SECRET1234VALUE" not in str(entry["value"])
        assert "..." in str(entry["value"])

    def test_nested_header_secrets_redacted(self, db_connection, client):
        # An innocuous-keyed preference whose value carries nested secrets.
        set_user_preference(
            "custom_headers",
            '{"CF-Access-Client-Id": "4tyjwrtyhjdtyj.access", '
            '"CF-Access-Client-Secret": "e5yjthyjfghjsecret"}',
            category="downloads",
        )
        resp = client.get("/api/admin/debug-package")
        assert b"4tyjwrtyhjdtyj.access" not in resp.data
        assert b"e5yjthyjfghjsecret" not in resp.data

    def test_put_missing_mode_400(self, db_connection, client):
        resp = client.put(
            "/api/admin/api-browse-mode",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert resp.status_code == 400
