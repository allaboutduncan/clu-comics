"""Tests for routes/source_wall.py -- Source Wall metadata table editor."""
import json
import pytest
from unittest.mock import patch, MagicMock


class TestSourceWallPage:

    @patch("routes.source_wall.config")
    def test_page_loads(self, mock_config, client):
        mock_config.get.return_value = ""
        resp = client.get("/source-wall")
        assert resp.status_code == 200


class TestSourceWallFiles:

    @patch("routes.source_wall.get_source_wall_files")
    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_list_files_returns_metadata(self, mock_valid, mock_files, client):
        mock_files.return_value = (
            [{"id": 1, "name": "SubDir", "path": "/data/SubDir", "type": "directory"}],
            [{"id": 2, "name": "Issue 001.cbz", "path": "/data/Issue 001.cbz",
              "type": "file", "ci_series": "Batman", "ci_volume": "2020"}],
        )

        resp = client.get("/api/source-wall/files?path=/data")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["directories"]) == 1
        assert len(data["files"]) == 1
        assert data["files"][0]["ci_series"] == "Batman"

    def test_missing_path_returns_403(self, client):
        resp = client.get("/api/source-wall/files")
        assert resp.status_code == 403

    @patch("routes.source_wall.is_valid_library_path", return_value=False)
    def test_invalid_path_returns_403(self, mock_valid, client):
        resp = client.get("/api/source-wall/files?path=/etc/passwd")
        assert resp.status_code == 403


class TestSourceWallUpdateField:

    @patch("routes.source_wall.threading")
    @patch("routes.source_wall.update_file_index_ci_field", return_value=True)
    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_single_field_update_succeeds(self, mock_valid, mock_update, mock_thread, client):
        mock_thread.Thread.return_value = MagicMock()

        resp = client.post("/api/source-wall/update-field",
                           data=json.dumps({"path": "/data/comic.cbz",
                                            "field": "ci_series",
                                            "value": "Spider-Man"}),
                           content_type="application/json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        mock_update.assert_called_once_with("/data/comic.cbz", "ci_series", "Spider-Man")

    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_invalid_field_rejected(self, mock_valid, client):
        resp = client.post("/api/source-wall/update-field",
                           data=json.dumps({"path": "/data/comic.cbz",
                                            "field": "invalid_field",
                                            "value": "test"}),
                           content_type="application/json")
        assert resp.status_code == 400

    @patch("routes.source_wall.is_valid_library_path", return_value=False)
    def test_invalid_path_rejected(self, mock_valid, client):
        resp = client.post("/api/source-wall/update-field",
                           data=json.dumps({"path": "/etc/bad",
                                            "field": "ci_series",
                                            "value": "test"}),
                           content_type="application/json")
        assert resp.status_code == 403


class TestSourceWallBulkUpdate:

    @patch("core.app_state.register_operation", return_value="op-123")
    @patch("routes.source_wall.threading")
    @patch("routes.source_wall.bulk_update_file_index_ci_field", return_value=3)
    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_bulk_update_registers_operation(self, mock_valid, mock_bulk, mock_thread,
                                              mock_register, client):
        mock_thread.Thread.return_value = MagicMock()

        resp = client.post("/api/source-wall/bulk-update",
                           data=json.dumps({
                               "paths": ["/data/a.cbz", "/data/b.cbz", "/data/c.cbz"],
                               "field": "ci_writer",
                               "value": "Alan Moore",
                           }),
                           content_type="application/json")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["op_id"] == "op-123"
        assert data["affected"] == 3

    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_bulk_update_empty_paths_rejected(self, mock_valid, client):
        resp = client.post("/api/source-wall/bulk-update",
                           data=json.dumps({"paths": [], "field": "ci_writer", "value": "x"}),
                           content_type="application/json")
        assert resp.status_code == 400

    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_bulk_update_invalid_field_rejected(self, mock_valid, client):
        resp = client.post("/api/source-wall/bulk-update",
                           data=json.dumps({"paths": ["/data/a.cbz"],
                                            "field": "bad",
                                            "value": "x"}),
                           content_type="application/json")
        assert resp.status_code == 400


class TestSourceWallColumns:

    @patch("routes.source_wall.get_user_preference", return_value=["name", "ci_volume"])
    def test_get_defaults(self, mock_pref, client):
        resp = client.get("/api/source-wall/columns")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "name" in data["columns"]

    @patch("routes.source_wall.set_user_preference", return_value=True)
    def test_save_and_load_round_trip(self, mock_set, client):
        cols = ["name", "ci_series", "ci_writer", "ci_year"]
        resp = client.post("/api/source-wall/columns",
                           data=json.dumps({"columns": cols}),
                           content_type="application/json")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        mock_set.assert_called_once_with("source_wall_columns", cols, category="source_wall")


class TestSourceWallSuggest:

    @patch("routes.source_wall.get_distinct_ci_values",
           return_value=["Alan Moore", "Alan Grant"])
    def test_suggest_returns_values(self, mock_distinct, client):
        resp = client.get("/api/source-wall/suggest?field=ci_writer&q=Ala&path=/data/Comics")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["values"]) == 2
        assert "Alan Moore" in data["values"]
        mock_distinct.assert_called_once_with("ci_writer", "Ala", parent_path="/data/Comics", limit=20)

    def test_suggest_rejects_invalid_field(self, client):
        resp = client.get("/api/source-wall/suggest?field=bad_field&q=test")
        assert resp.status_code == 400

    def test_suggest_short_query_returns_empty(self, client):
        resp = client.get("/api/source-wall/suggest?field=ci_writer&q=Al")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["values"] == []
