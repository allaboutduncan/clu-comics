"""Tests for routes/series.py -- series management endpoints."""
import pytest
from unittest.mock import patch, MagicMock


class TestSeriesSearch:

    def test_empty_query(self, client):
        resp = client.get("/api/series/search?q=")
        assert resp.status_code == 400

    def test_no_metron_creds(self, client, app):
        # Config already has empty METRON_USERNAME/PASSWORD from conftest
        resp = client.get("/api/series/search?q=batman")
        assert resp.status_code == 400

    @patch("routes.series.metron")
    def test_search_success(self, mock_metron, client, app):
        app.config["METRON_USERNAME"] = "user"
        app.config["METRON_PASSWORD"] = "pass"

        mock_series = MagicMock()
        mock_series.id = 100
        mock_series.display_name = "Batman"
        mock_series.name = "Batman"
        mock_series.volume = 2020
        mock_series.year_began = 2020
        mock_series.issue_count = 50
        mock_series.status = "Ongoing"
        mock_series.publisher = MagicMock(name="DC Comics")
        mock_series.publisher.name = "DC Comics"

        mock_api = MagicMock()
        mock_api.series_list.return_value = [mock_series]
        mock_metron.get_api.return_value = mock_api
        mock_metron.is_connection_error.return_value = False

        mock_app = MagicMock()
        mock_app.generate_series_slug.return_value = "batman-v2020-100"
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.get("/api/series/search?q=batman")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["count"] == 1


class TestMapSeries:

    @patch("database.save_publisher")
    @patch("database.save_series_mapping", return_value=True)
    def test_map_success(self, mock_save, mock_pub, client):
        resp = client.post("/api/series/100/map", json={
            "mapped_path": "/data/DC/Batman",
            "series": {
                "id": 100, "name": "Batman",
                "publisher": {"id": 10, "name": "DC Comics"},
            },
        })
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_no_data(self, client):
        resp = client.post("/api/series/100/map",
                           content_type="application/json",
                           data="{}")
        assert resp.status_code == 400

    def test_missing_fields(self, client):
        resp = client.post("/api/series/100/map", json={"mapped_path": "/x"})
        assert resp.status_code == 400


class TestGetSeriesMapping:

    @patch("database.get_series_mapping", return_value="/data/DC/Batman")
    def test_get_mapping(self, mock_get, client):
        resp = client.get("/api/series/100/mapping")
        assert resp.status_code == 200
        assert resp.get_json()["mapped_path"] == "/data/DC/Batman"

    @patch("database.get_series_mapping", return_value=None)
    def test_no_mapping(self, mock_get, client):
        resp = client.get("/api/series/100/mapping")
        assert resp.get_json()["mapped_path"] is None


class TestDeleteSeriesMapping:

    @patch("database.remove_series_mapping", return_value=True)
    def test_delete_success(self, mock_rm, client):
        resp = client.delete("/api/series/100/mapping")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    @patch("database.remove_series_mapping", return_value=False)
    def test_delete_failure(self, mock_rm, client):
        resp = client.delete("/api/series/100/mapping")
        assert resp.status_code == 500


class TestManualStatus:

    @patch("database.get_manual_status_for_series", return_value={"1": {"status": "owned"}})
    def test_get_manual_status(self, mock_get, client):
        resp = client.get("/api/series/100/manual-status")
        data = resp.get_json()
        assert data["success"] is True
        assert "1" in data["manual_status"]

    @patch("database.set_manual_status", return_value=True)
    def test_set_status(self, mock_set, client):
        resp = client.post("/api/series/100/issue/1/manual-status",
                           json={"status": "owned", "notes": "hardcover"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_invalid_status(self, client):
        resp = client.post("/api/series/100/issue/1/manual-status",
                           json={"status": "invalid"})
        assert resp.status_code == 400

    @patch("database.clear_manual_status", return_value=True)
    def test_delete_status(self, mock_clear, client):
        resp = client.delete("/api/series/100/issue/1/manual-status")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True


class TestBulkManualStatus:

    @patch("database.bulk_set_manual_status", return_value=3)
    def test_bulk_set(self, mock_bulk, client):
        resp = client.post("/api/series/100/bulk-manual-status", json={
            "issue_numbers": ["1", "2", "3"],
            "status": "owned",
        })
        assert resp.status_code == 200
        assert resp.get_json()["count"] == 3

    def test_empty_issues(self, client):
        resp = client.post("/api/series/100/bulk-manual-status", json={
            "issue_numbers": [],
            "status": "owned",
        })
        assert resp.status_code == 400

    @patch("database.bulk_clear_manual_status", return_value=2)
    def test_bulk_delete(self, mock_clear, client):
        resp = client.delete("/api/series/100/bulk-manual-status", json={
            "issue_numbers": ["1", "2"],
        })
        assert resp.status_code == 200
        assert resp.get_json()["count"] == 2


class TestWantedApi:

    @patch("routes.series.get_wanted_issues", return_value=[
        {"issue_id": 1, "series_name": "Batman"},
    ])
    def test_get_wanted(self, mock_wanted, client):
        resp = client.get("/api/wanted")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["count"] == 1


class TestRefreshWanted:

    @patch("routes.series.app_state")
    def test_refresh_started(self, mock_state, client):
        mock_state.wanted_refresh_in_progress = False
        mock_app = MagicMock()
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.post("/api/refresh-wanted")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    @patch("routes.series.app_state")
    def test_already_refreshing(self, mock_state, client):
        mock_state.wanted_refresh_in_progress = True
        resp = client.post("/api/refresh-wanted")
        assert resp.status_code == 200
        assert "already" in resp.get_json()["message"].lower()


class TestWantedStatus:

    @patch("routes.series.app_state")
    @patch("database.get_wanted_cache_age", return_value="5 minutes")
    @patch("database.get_cached_wanted_issues", return_value=[{"id": 1}])
    def test_wanted_status(self, mock_cached, mock_age, mock_state, client):
        mock_state.wanted_refresh_in_progress = False
        resp = client.get("/api/wanted-status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["count"] == 1
        assert data["refreshing"] is False


class TestLibrariesApi:

    @patch("database.get_libraries", return_value=[
        {"id": 1, "name": "Comics", "path": "/data/comics", "enabled": True},
    ])
    def test_get_libraries(self, mock_libs, client):
        resp = client.get("/api/libraries")
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["libraries"]) == 1

    @patch("database.add_library", return_value=1)
    def test_add_library(self, mock_add, client, tmp_path):
        lib_path = str(tmp_path / "comics")
        import os
        os.makedirs(lib_path)

        mock_app = MagicMock()
        with patch.dict("sys.modules", {"app": mock_app}), \
             patch("database.sync_file_index_incremental"), \
             patch("database.invalidate_browse_cache"):
            resp = client.post("/api/libraries", json={
                "name": "Comics", "path": lib_path,
            })
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_add_library_missing_name(self, client):
        with patch.dict("sys.modules", {"app": MagicMock()}):
            resp = client.post("/api/libraries", json={"path": "/tmp"})
        assert resp.status_code == 400

    @patch("database.get_library_by_id", return_value={"id": 1, "name": "Old"})
    @patch("database.update_library", return_value=True)
    def test_update_library(self, mock_update, mock_get, client):
        resp = client.put("/api/libraries/1", json={"name": "New Name"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    @patch("database.get_library_by_id", return_value=None)
    def test_update_nonexistent(self, mock_get, client):
        resp = client.put("/api/libraries/999", json={"name": "X"})
        assert resp.status_code == 404

    @patch("database.get_library_by_id", return_value={"id": 1, "name": "Comics"})
    @patch("database.delete_library", return_value=True)
    def test_delete_library(self, mock_del, mock_get, client):
        resp = client.delete("/api/libraries/1")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True


class TestPublishersApi:

    @patch("database.get_all_publishers", return_value=[
        {"id": 10, "name": "DC Comics"},
    ])
    def test_get_publishers(self, mock_get, client):
        resp = client.get("/api/publishers")
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["publishers"]) == 1

    @patch("database.get_db_connection")
    @patch("database.save_publisher", return_value=True)
    def test_add_publisher(self, mock_save, mock_conn, client):
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = [None]
        mock_db = MagicMock()
        mock_db.cursor.return_value = mock_cursor
        mock_conn.return_value = mock_db

        resp = client.post("/api/publishers", json={"name": "Test Pub"})
        assert resp.status_code == 200

    def test_add_publisher_no_name(self, client):
        resp = client.post("/api/publishers", json={})
        assert resp.status_code == 400

    @patch("database.delete_publisher", return_value=True)
    def test_delete_publisher(self, mock_del, client):
        resp = client.delete("/api/publishers/10")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    @patch("database.delete_publisher", return_value=True)
    def test_delete_negative_publisher(self, mock_del, client):
        resp = client.delete("/api/publishers/-1")
        assert resp.status_code == 200
