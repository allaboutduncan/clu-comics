"""Tests for reading_lists.py blueprint -- reading list API endpoints."""
import pytest
from unittest.mock import patch, MagicMock
from io import BytesIO


class TestReadingListIndex:

    @patch("reading_lists.get_reading_lists", return_value=[])
    def test_index_page(self, mock_get, client):
        resp = client.get("/reading-lists")
        assert resp.status_code == 200

    @patch("reading_lists.get_reading_list", return_value=None)
    def test_view_nonexistent_list(self, mock_get, client):
        resp = client.get("/reading-lists/999")
        # Should redirect when list not found
        assert resp.status_code == 302


class TestUploadList:

    def test_upload_no_file(self, client):
        resp = client.post("/api/reading-lists/upload")
        data = resp.get_json()
        assert data["success"] is False
        assert "No file part" in data["message"]

    def test_upload_empty_filename(self, client):
        data = {"file": (BytesIO(b""), "")}
        resp = client.post("/api/reading-lists/upload",
                           content_type="multipart/form-data", data=data)
        json_data = resp.get_json()
        assert json_data["success"] is False

    @patch("reading_lists.threading.Thread")
    @patch("reading_lists.uuid.uuid4", return_value="test-uuid-1234")
    def test_upload_valid_cbl(self, mock_uuid, mock_thread, client):
        cbl_content = b"""<?xml version="1.0"?>
        <ReadingList><Name>Test</Name><Books></Books></ReadingList>"""
        data = {"file": (BytesIO(cbl_content), "test.cbl")}
        resp = client.post("/api/reading-lists/upload",
                           content_type="multipart/form-data", data=data)
        json_data = resp.get_json()
        assert json_data["success"] is True
        assert json_data["background"] is True
        assert json_data["task_id"] == "test-uuid-1234"


class TestImportList:

    def test_import_no_url(self, client):
        resp = client.post("/api/reading-lists/import", json={})
        data = resp.get_json()
        assert data["success"] is False

    @patch("reading_lists.threading.Thread")
    @patch("reading_lists.uuid.uuid4", return_value="test-uuid-5678")
    @patch("reading_lists.requests.get")
    def test_import_from_url(self, mock_get, mock_uuid, mock_thread, client):
        mock_response = MagicMock()
        mock_response.text = "<ReadingList><Name>Test</Name><Books></Books></ReadingList>"
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        resp = client.post("/api/reading-lists/import",
                           json={"url": "https://example.com/list.cbl"})
        data = resp.get_json()
        assert data["success"] is True
        assert data["task_id"] == "test-uuid-5678"

    @patch("reading_lists.requests.get", side_effect=Exception("Connection error"))
    def test_import_network_error(self, mock_get, client):
        resp = client.post("/api/reading-lists/import",
                           json={"url": "https://bad.example.com/list.cbl"})
        data = resp.get_json()
        assert data["success"] is False


class TestMapEntry:

    @patch("reading_lists.update_reading_list_entry_match", return_value=True)
    def test_map_entry_success(self, mock_update, client):
        resp = client.post("/api/reading-lists/1/map",
                           json={"entry_id": 42, "file_path": "/data/comic.cbz"})
        data = resp.get_json()
        assert data["success"] is True

    @patch("reading_lists.update_reading_list_entry_match", return_value=True)
    @patch("reading_lists.clear_thumbnail_if_matches_entry")
    def test_clear_mapping(self, mock_clear, mock_update, client):
        resp = client.post("/api/reading-lists/1/map",
                           json={"entry_id": 42, "file_path": None})
        data = resp.get_json()
        assert data["success"] is True
        mock_clear.assert_called_once_with(1, 42)

    def test_map_entry_missing_entry_id(self, client):
        resp = client.post("/api/reading-lists/1/map", json={})
        data = resp.get_json()
        assert data["success"] is False


class TestDeleteList:

    @patch("reading_lists.delete_reading_list", return_value=True)
    def test_delete_success(self, mock_del, client):
        resp = client.delete("/api/reading-lists/1")
        assert resp.get_json()["success"] is True

    @patch("reading_lists.delete_reading_list", return_value=False)
    def test_delete_failure(self, mock_del, client):
        resp = client.delete("/api/reading-lists/1")
        assert resp.get_json()["success"] is False


class TestImportStatus:

    def test_unknown_task(self, client):
        resp = client.get("/api/reading-lists/import-status/nonexistent")
        data = resp.get_json()
        assert data["success"] is False

    def test_known_task(self, client):
        # Inject a task into the in-memory store
        from reading_lists import import_tasks
        import_tasks["test-task"] = {
            "status": "complete",
            "message": "Done",
            "processed": 10,
            "total": 10,
            "list_id": 1,
            "list_name": "Test",
        }
        resp = client.get("/api/reading-lists/import-status/test-task")
        data = resp.get_json()
        assert data["success"] is True
        assert data["status"] == "complete"
        assert data["processed"] == 10
        # Clean up
        del import_tasks["test-task"]


class TestSearchFile:

    @patch("reading_lists.search_file_index", return_value=[
        {"name": "Batman 001.cbz", "path": "/data/Batman 001.cbz"},
    ])
    def test_search(self, mock_search, client):
        resp = client.get("/api/reading-lists/search-file?q=batman")
        data = resp.get_json()
        assert len(data) == 1

    def test_search_empty_query(self, client):
        resp = client.get("/api/reading-lists/search-file?q=")
        data = resp.get_json()
        assert data == []


class TestSetThumbnail:

    @patch("reading_lists.update_reading_list_thumbnail", return_value=True)
    def test_set_thumbnail(self, mock_update, client):
        resp = client.post("/api/reading-lists/1/thumbnail",
                           json={"file_path": "/data/Batman.cbz"})
        assert resp.get_json()["success"] is True

    def test_missing_file_path(self, client):
        resp = client.post("/api/reading-lists/1/thumbnail", json={})
        assert resp.get_json()["success"] is False


class TestUpdateName:

    @patch("reading_lists.update_reading_list_name", return_value=True)
    def test_update_name(self, mock_update, client):
        resp = client.post("/api/reading-lists/1/name",
                           json={"name": "New Name"})
        assert resp.get_json()["success"] is True

    def test_empty_name(self, client):
        resp = client.post("/api/reading-lists/1/name", json={"name": ""})
        assert resp.get_json()["success"] is False


class TestUpdateTags:

    @patch("reading_lists.update_reading_list_tags", return_value=True)
    def test_update_tags(self, mock_update, client):
        resp = client.post("/api/reading-lists/1/tags",
                           json={"tags": ["dc", "batman"]})
        assert resp.get_json()["success"] is True
        mock_update.assert_called_once_with(1, ["dc", "batman"])

    def test_invalid_tags_type(self, client):
        resp = client.post("/api/reading-lists/1/tags",
                           json={"tags": "not-a-list"})
        assert resp.get_json()["success"] is False


class TestGetTags:

    @patch("reading_lists.get_all_reading_list_tags", return_value=["dc", "marvel"])
    def test_get_tags(self, mock_get, client):
        resp = client.get("/api/reading-lists/tags")
        data = resp.get_json()
        assert data["tags"] == ["dc", "marvel"]
