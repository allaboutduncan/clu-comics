"""Tests for routes/downloads.py -- download management endpoints."""
import pytest
from unittest.mock import patch, MagicMock


class TestGetcomicsSearch:

    @patch("models.getcomics.search_getcomics", return_value=[
        {"title": "Batman #1", "url": "https://getcomics.org/batman-1"},
    ])
    def test_search(self, mock_search, client):
        resp = client.get("/api/getcomics/search?q=batman")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["results"]) == 1

    def test_empty_query(self, client):
        resp = client.get("/api/getcomics/search?q=")
        assert resp.status_code == 400

    @patch("models.getcomics.search_getcomics", side_effect=Exception("error"))
    def test_search_error(self, mock_search, client):
        resp = client.get("/api/getcomics/search?q=batman")
        assert resp.status_code == 500


class TestGetcomicsDownload:

    def test_no_url(self, client):
        resp = client.post("/api/getcomics/download", json={})
        assert resp.status_code == 400

    @patch("api.download_queue")
    @patch("api.download_progress", {})
    @patch("models.getcomics.get_download_links", return_value={
        "pixeldrain": "https://pixeldrain.com/u/abc123",
    })
    @patch("core.config.config")
    def test_download_queued(self, mock_config, mock_links, mock_queue, client):
        mock_config.get.return_value = "pixeldrain,download_now,mega"
        resp = client.post("/api/getcomics/download",
                           json={"url": "https://getcomics.org/batman", "filename": "b.cbz"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "download_id" in data

    @patch("models.getcomics.get_download_links", return_value={})
    @patch("core.config.config")
    def test_no_download_link(self, mock_config, mock_links, client):
        mock_config.get.return_value = "pixeldrain"
        resp = client.post("/api/getcomics/download",
                           json={"url": "https://getcomics.org/x"})
        assert resp.status_code == 404


class TestGetcomicsDownloadStatus:
    """The UI polls this endpoint so it can surface a Cloudflare-blocked
    download with a manual link instead of silently failing."""

    def test_unknown_download_returns_404(self, client):
        resp = client.get("/api/getcomics/download-status/does-not-exist")
        assert resp.status_code == 404
        assert resp.get_json()["success"] is False

    @patch("api.download_progress", {
        "abc-123": {"status": "in_progress", "progress": 42, "error": None,
                    "manual_url": None, "filename": "b.cbz", "provider": "pixeldrain"},
    })
    def test_in_progress(self, client):
        resp = client.get("/api/getcomics/download-status/abc-123")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["status"] == "in_progress"
        assert data["progress"] == 42
        assert data["manual_url"] is None

    @patch("api.download_progress", {
        "cf-1": {
            "status": "error",
            "progress": -1,
            "error": "fs2.comicfiles.ru is protected by a Cloudflare challenge...",
            # The getcomics post page (where the user clicks download themselves),
            # NOT the resolved mirror URL or /dls/ link which 403 in a browser.
            "manual_url": "https://getcomics.org/comic/geiger-ground-zero-2",
            "filename": "Geiger.cbz",
            "provider": "getcomics",
        },
    })
    def test_error_surfaces_manual_url(self, client):
        resp = client.get("/api/getcomics/download-status/cf-1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["status"] == "error"
        assert data["manual_url"] == "https://getcomics.org/comic/geiger-ground-zero-2"


class TestSyncSchedule:

    @patch("core.database.get_sync_schedule", return_value=None)
    def test_get_schedule_default(self, mock_sched, client):
        resp = client.get("/api/get-sync-schedule")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["schedule"]["frequency"] == "disabled"

    @patch("core.database.get_sync_schedule", return_value={
        "frequency": "daily", "time": "03:00", "weekday": 0, "last_sync": None,
    })
    def test_get_schedule_configured(self, mock_sched, client):
        mock_app = MagicMock()
        mock_app.get_next_run_for_job.return_value = "2024-01-01 03:00"
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.get("/api/get-sync-schedule")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["schedule"]["frequency"] == "daily"

    @patch("core.database.save_sync_schedule", return_value=True)
    def test_save_schedule(self, mock_save, client):
        mock_app = MagicMock()
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.post("/api/save-sync-schedule",
                               json={"frequency": "daily", "time": "04:00"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_save_invalid_frequency(self, client):
        with patch.dict("sys.modules", {"app": MagicMock()}):
            resp = client.post("/api/save-sync-schedule",
                               json={"frequency": "hourly"})
        assert resp.status_code == 400


class TestGetcomicsSchedule:

    @patch("core.database.get_getcomics_schedule", return_value=None)
    def test_get_default(self, mock_sched, client):
        resp = client.get("/api/get-getcomics-schedule")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["schedule"]["frequency"] == "disabled"

    @patch("core.database.save_getcomics_schedule", return_value=True)
    def test_save_schedule(self, mock_save, client):
        mock_app = MagicMock()
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.post("/api/save-getcomics-schedule",
                               json={"frequency": "weekly", "time": "08:00",
                                     "weekday": 3})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_save_invalid_time(self, client):
        with patch.dict("sys.modules", {"app": MagicMock()}):
            resp = client.post("/api/save-getcomics-schedule",
                               json={"frequency": "daily", "time": "25:00"})
        assert resp.status_code == 400


class TestRunGetcomicsNow:

    def test_trigger_download(self, client):
        mock_app = MagicMock()
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.post("/api/run-getcomics-now")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True


class TestWeeklyPacksConfig:

    @patch("core.database.get_weekly_packs_config", return_value=None)
    def test_get_config_default(self, mock_config, client):
        resp = client.get("/api/get-weekly-packs-config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["config"]["enabled"] is False

    @patch("core.database.save_weekly_packs_config", return_value=True)
    def test_save_config(self, mock_save, client):
        mock_app = MagicMock()
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.post("/api/save-weekly-packs-config", json={
                "enabled": True,
                "format": "JPG",
                "publishers": ["DC", "Marvel"],
                "weekday": 2,
                "time": "10:00",
            })
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_invalid_format(self, client):
        with patch.dict("sys.modules", {"app": MagicMock()}):
            resp = client.post("/api/save-weekly-packs-config", json={
                "format": "PNG",
                "publishers": ["DC"],
            })
        assert resp.status_code == 400

    def test_invalid_publisher(self, client):
        with patch.dict("sys.modules", {"app": MagicMock()}):
            resp = client.post("/api/save-weekly-packs-config", json={
                "format": "JPG",
                "publishers": ["FakePublisher"],
            })
        assert resp.status_code == 400


class TestWeeklyPacksHistory:

    @patch("core.database.get_weekly_packs_history", return_value=[
        {"pack_date": "2024-01-01", "publisher": "DC", "status": "completed"},
    ])
    def test_get_history(self, mock_hist, client):
        resp = client.get("/api/weekly-packs-history")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["history"]) == 1


class TestRunWeeklyPacksNow:

    def test_trigger(self, client):
        mock_app = MagicMock()
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.post("/api/run-weekly-packs-now")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
