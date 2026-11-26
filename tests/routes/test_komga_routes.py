"""/api/komga/* (routes/komga.py), moved out of app.py so it can be tested."""
import sys
from unittest.mock import MagicMock, patch

import pytest

CONFIG = {
    "server_url": "http://komga.local:25600",
    "username": "reader",
    "password": "s3cret",
    "enabled": True,
    "frequency": "daily",
    "time": "05:00",
    "weekday": 0,
}


@pytest.fixture
def app_module(app):
    """The stand-in for app.py that routes import from (tests/routes/conftest.py)."""
    return sys.modules["app"]


def _save(client, **overrides):
    return client.post("/api/komga/config", json={**CONFIG, **overrides})


class TestConfig:
    def test_unconfigured_is_an_empty_config(self, client):
        body = client.get("/api/komga/config").get_json()
        assert body["success"] is True
        assert not body["config"].get("server_url")

    def test_a_saved_config_round_trips(self, client):
        assert _save(client).get_json()["success"] is True
        cfg = client.get("/api/komga/config").get_json()["config"]
        assert cfg["server_url"] == CONFIG["server_url"]
        assert cfg["username"] == "reader"

    def test_the_password_is_never_returned(self, client):
        _save(client)
        resp = client.get("/api/komga/config")
        assert resp.get_json()["config"]["password"] == "***"
        assert "s3cret" not in resp.get_data(as_text=True)

    def test_an_unset_password_is_reported_as_empty(self, client):
        _save(client, password="")
        assert client.get("/api/komga/config").get_json()["config"]["password"] == ""

    def test_saving_reschedules_the_sync(self, client, app_module):
        app_module.configure_komga_sync_schedule.reset_mock()
        _save(client)
        app_module.configure_komga_sync_schedule.assert_called_once_with()

    def test_a_failed_save_is_a_500_and_does_not_reschedule(self, client, app_module):
        app_module.configure_komga_sync_schedule.reset_mock()
        with patch("core.database.save_komga_config", return_value=False):
            resp = _save(client)
        assert resp.status_code == 500
        assert resp.get_json()["success"] is False
        app_module.configure_komga_sync_schedule.assert_not_called()


class TestLibraryMappings:
    @pytest.fixture
    def library_id(self, app):
        from core.database import add_library
        return add_library("Komga Comics", "/komga-comics")

    def _prefixes(self, client):
        cfg = client.get("/api/komga/config").get_json()["config"]
        return {m["library_id"]: m["komga_path_prefix"] for m in cfg["library_mappings"]}

    def test_a_mapping_round_trips(self, client, library_id):
        mapping = [{"library_id": library_id, "komga_path_prefix": "/books/comics"}]
        assert _save(client, library_mappings=mapping).get_json()["success"] is True
        assert self._prefixes(client)[library_id] == "/books/comics"

    def test_a_blank_prefix_clears_the_mapping(self, client, library_id):
        _save(client, library_mappings=[{"library_id": library_id, "komga_path_prefix": "/books"}])
        _save(client, library_mappings=[{"library_id": library_id, "komga_path_prefix": "  "}])
        assert self._prefixes(client)[library_id] == ""

    def test_a_malformed_mapping_fails_the_save_without_locking_the_database(self, client, library_id):
        """A row with no library_id raised after the DELETE and leaked the
        connection holding the write lock: the schedule save that follows
        waited out the 30s busy_timeout and failed, and the route said success."""
        import time
        from core.database import get_schedule

        _save(client, library_mappings=[{"library_id": library_id, "komga_path_prefix": "/books"}])
        started = time.monotonic()
        resp = _save(client, frequency="weekly", time="06:30", weekday=2,
                     library_mappings=[{"komga_path_prefix": "/no-library-id"}])
        elapsed = time.monotonic() - started

        assert resp.status_code == 500 and resp.get_json()["success"] is False
        assert elapsed < 10, f"the save blocked for {elapsed:.0f}s on its own leaked write lock"
        assert self._prefixes(client)[library_id] == "/books", "the old mappings are rolled back to"
        sched = get_schedule("komga")
        assert (sched["frequency"], sched["time"], sched["weekday"]) == ("weekly", "06:30", 2)


class TestConnectionTest:
    def test_unconfigured_says_to_save_first(self, client):
        body = client.post("/api/komga/test").get_json()
        assert body["success"] is False and body["valid"] is False
        assert "not configured" in body["error"]

    def test_missing_credentials_are_reported_without_a_request(self, client):
        _save(client, username="", password="")
        with patch("models.komga.KomgaClient") as komga:
            body = client.post("/api/komga/test").get_json()
        assert body["valid"] is False and "credentials" in body["error"]
        komga.assert_not_called()

    def test_a_working_server_is_valid_and_recorded(self, client):
        _save(client)
        komga = MagicMock()
        komga.return_value.test_connection.return_value = (True, "Komga 1.11")
        with patch("models.komga.KomgaClient", komga), \
                patch("core.database.update_komga_connection_valid") as record:
            body = client.post("/api/komga/test").get_json()
        assert body == {"success": True, "valid": True, "message": "Komga 1.11"}
        komga.assert_called_once_with(CONFIG["server_url"], "reader", "s3cret")
        record.assert_called_once_with(True)

    def test_a_rejected_login_is_invalid_and_recorded(self, client):
        _save(client)
        komga = MagicMock()
        komga.return_value.test_connection.return_value = (False, "401 Unauthorized")
        with patch("models.komga.KomgaClient", komga), \
                patch("core.database.update_komga_connection_valid") as record:
            body = client.post("/api/komga/test").get_json()
        assert body == {"success": True, "valid": False, "error": "401 Unauthorized"}
        record.assert_called_once_with(False)

    def test_an_unreachable_server_is_reported_not_raised(self, client):
        _save(client)
        with patch("models.komga.KomgaClient", side_effect=ConnectionError("refused")), \
                patch("core.database.update_komga_connection_valid") as record:
            resp = client.post("/api/komga/test")
        assert resp.status_code == 200
        assert resp.get_json() == {"success": False, "valid": False, "error": "refused"}
        record.assert_called_once_with(False)

    def test_the_password_is_not_logged(self, client):
        _save(client)
        komga = MagicMock()
        komga.return_value.test_connection.return_value = (True, "ok")
        with patch("models.komga.KomgaClient", komga), patch("routes.komga.app_logger") as log:
            client.post("/api/komga/test")
        logged = " ".join(str(c) for c in log.method_calls)
        assert "s3cret" not in logged


class TestSync:
    def test_sync_runs_in_the_background(self, client, app_module):
        started = []

        class _Thread:
            def __init__(self, target=None, daemon=None):
                self.target, self.daemon = target, daemon

            def start(self):
                started.append(self)

        with patch("routes.komga.threading.Thread", _Thread):
            body = client.post("/api/komga/sync").get_json()
        assert body["success"] is True
        assert len(started) == 1
        assert started[0].target is app_module.run_komga_sync
        assert started[0].daemon is True

    def test_status_before_any_sync(self, client, app_module):
        app_module.get_next_run_for_job.return_value = "Not scheduled"
        body = client.get("/api/komga/sync/status").get_json()
        assert body["success"] is True
        assert body["total_synced_read"] == 0
        assert body["total_synced_progress"] == 0
        assert body["last_sync_read_count"] == 0
        assert body["next_run"] is None, "'Not scheduled' is a display string, not a time"

    def test_status_reports_the_next_run_for_the_komga_job(self, client, app_module):
        app_module.get_next_run_for_job.return_value = "2026-01-15 03:00:00"
        body = client.get("/api/komga/sync/status").get_json()
        assert body["next_run"] == "2026-01-15 03:00:00"
        app_module.get_next_run_for_job.assert_called_with("komga_sync")

    def test_status_counts_come_from_the_stored_config(self, client):
        stats = {"total_synced_read": 12, "total_synced_progress": 3, "last_sync": None}
        cfg = {"last_sync_read_count": 4, "last_sync_progress_count": 1}
        with patch("core.database.get_komga_sync_stats", return_value=stats), \
                patch("core.database.get_komga_config", return_value=cfg):
            body = client.get("/api/komga/sync/status").get_json()
        assert (body["total_synced_read"], body["total_synced_progress"]) == (12, 3)
        assert (body["last_sync_read_count"], body["last_sync_progress_count"]) == (4, 1)
