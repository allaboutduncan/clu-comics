"""Tests for reading_lists.py blueprint -- reading list API endpoints."""
import pytest
from unittest.mock import patch, MagicMock
from io import BytesIO


class TestReadingListIndex:

    @patch("routes.reading_lists.get_reading_lists", return_value=[])
    def test_index_page(self, mock_get, client):
        resp = client.get("/reading-lists")
        assert resp.status_code == 200

    @patch("routes.reading_lists.get_reading_list", return_value=None)
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

    @patch("routes.reading_lists.threading.Thread")
    @patch("routes.reading_lists.uuid.uuid4", return_value="test-uuid-1234")
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

    @patch("routes.reading_lists.threading.Thread")
    @patch("routes.reading_lists.uuid.uuid4", return_value="test-uuid-5678")
    @patch("routes.reading_lists.requests.get")
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

    @patch("routes.reading_lists.requests.get", side_effect=Exception("Connection error"))
    def test_import_network_error(self, mock_get, client):
        resp = client.post("/api/reading-lists/import",
                           json={"url": "https://bad.example.com/list.cbl"})
        data = resp.get_json()
        assert data["success"] is False


class TestMapEntry:

    @patch("routes.reading_lists.update_reading_list_entry_match", return_value=True)
    def test_map_entry_success(self, mock_update, client):
        resp = client.post("/api/reading-lists/1/map",
                           json={"entry_id": 42, "file_path": "/data/comic.cbz"})
        data = resp.get_json()
        assert data["success"] is True

    @patch("routes.reading_lists.update_reading_list_entry_match", return_value=True)
    @patch("routes.reading_lists.clear_thumbnail_if_matches_entry")
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

    @patch("routes.reading_lists.delete_reading_list", return_value=True)
    def test_delete_success(self, mock_del, client):
        resp = client.delete("/api/reading-lists/1")
        assert resp.get_json()["success"] is True

    @patch("routes.reading_lists.delete_reading_list", return_value=False)
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
        from routes.reading_lists import import_tasks
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

    @patch("routes.reading_lists.search_file_index", return_value=[
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

    @patch("routes.reading_lists.update_reading_list_thumbnail", return_value=True)
    def test_set_thumbnail(self, mock_update, client):
        resp = client.post("/api/reading-lists/1/thumbnail",
                           json={"file_path": "/data/Batman.cbz"})
        assert resp.get_json()["success"] is True

    def test_missing_file_path(self, client):
        resp = client.post("/api/reading-lists/1/thumbnail", json={})
        assert resp.get_json()["success"] is False


class TestUpdateName:

    @patch("routes.reading_lists.update_reading_list_name", return_value=True)
    def test_update_name(self, mock_update, client):
        resp = client.post("/api/reading-lists/1/name",
                           json={"name": "New Name"})
        assert resp.get_json()["success"] is True

    def test_empty_name(self, client):
        resp = client.post("/api/reading-lists/1/name", json={"name": ""})
        assert resp.get_json()["success"] is False


class TestUpdateTags:

    @patch("routes.reading_lists.update_reading_list_tags", return_value=True)
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

    @patch("routes.reading_lists.get_all_reading_list_tags", return_value=["dc", "marvel"])
    def test_get_tags(self, mock_get, client):
        resp = client.get("/api/reading-lists/tags")
        data = resp.get_json()
        assert data["tags"] == ["dc", "marvel"]


class TestCreateList:

    @patch("routes.reading_lists.create_reading_list", return_value=42)
    def test_create_success(self, mock_create, client):
        resp = client.post("/api/reading-lists/create", json={"name": "My List"})
        data = resp.get_json()
        assert data["success"] is True
        assert data["list_id"] == 42
        mock_create.assert_called_once_with("My List")

    def test_create_empty_name(self, client):
        resp = client.post("/api/reading-lists/create", json={"name": ""})
        data = resp.get_json()
        assert data["success"] is False

    def test_create_no_body(self, client):
        resp = client.post("/api/reading-lists/create",
                           content_type="application/json", data="{}")
        data = resp.get_json()
        assert data["success"] is False

    @patch("routes.reading_lists.create_reading_list", return_value=None)
    def test_create_failure(self, mock_create, client):
        resp = client.post("/api/reading-lists/create", json={"name": "Fail"})
        data = resp.get_json()
        assert data["success"] is False


class TestAddEntry:

    @patch("routes.reading_lists.add_reading_list_entry", return_value=99)
    @patch("routes.reading_lists.get_file_metadata_for_reading_list", return_value={
        "ci_series": "Batman", "ci_number": "1", "ci_volume": "2016", "ci_year": "2016"
    })
    def test_add_entry_with_metadata(self, mock_meta, mock_add, client):
        resp = client.post("/api/reading-lists/1/add-entry",
                           json={"file_path": "/data/Batman 001.cbz"})
        data = resp.get_json()
        assert data["success"] is True
        assert data["entry_id"] == 99

    @patch("routes.reading_lists.add_reading_list_entry", return_value=100)
    @patch("routes.reading_lists.get_file_metadata_for_reading_list", return_value=None)
    def test_add_entry_no_metadata_fallback(self, mock_meta, mock_add, client):
        resp = client.post("/api/reading-lists/1/add-entry",
                           json={"file_path": "/data/Batman 001.cbz"})
        data = resp.get_json()
        assert data["success"] is True
        # Verify fallback uses filename as series
        call_args = mock_add.call_args[0][1]
        assert call_args["series"] == "Batman 001"

    def test_add_entry_missing_path(self, client):
        resp = client.post("/api/reading-lists/1/add-entry", json={})
        data = resp.get_json()
        assert data["success"] is False

    @patch("routes.reading_lists.add_reading_list_entry", return_value=None)
    @patch("routes.reading_lists.get_file_metadata_for_reading_list", return_value=None)
    def test_add_entry_db_failure(self, mock_meta, mock_add, client):
        resp = client.post("/api/reading-lists/1/add-entry",
                           json={"file_path": "/data/comic.cbz"})
        data = resp.get_json()
        assert data["success"] is False


class TestRemoveEntry:

    @patch("routes.reading_lists.delete_reading_list_entry", return_value=True)
    def test_remove_success(self, mock_del, client):
        resp = client.delete("/api/reading-lists/1/entry/42")
        data = resp.get_json()
        assert data["success"] is True

    @patch("routes.reading_lists.delete_reading_list_entry", return_value=False)
    def test_remove_failure(self, mock_del, client):
        resp = client.delete("/api/reading-lists/1/entry/999")
        data = resp.get_json()
        assert data["success"] is False


class TestReorderEntries:

    @patch("routes.reading_lists.reorder_reading_list_entries", return_value=True)
    def test_reorder_success(self, mock_reorder, client):
        resp = client.post("/api/reading-lists/1/reorder",
                           json={"entry_ids": [3, 1, 2]})
        data = resp.get_json()
        assert data["success"] is True
        mock_reorder.assert_called_once_with(1, [3, 1, 2])

    def test_reorder_empty_ids(self, client):
        resp = client.post("/api/reading-lists/1/reorder",
                           json={"entry_ids": []})
        data = resp.get_json()
        assert data["success"] is False

    @patch("routes.reading_lists.reorder_reading_list_entries", return_value=False)
    def test_reorder_failure(self, mock_reorder, client):
        resp = client.post("/api/reading-lists/1/reorder",
                           json={"entry_ids": [1, 2]})
        data = resp.get_json()
        assert data["success"] is False


class TestExportCBL:

    @patch("routes.reading_lists.get_reading_list", return_value={
        "name": "Test List",
        "entries": [
            {"series": "Batman", "issue_number": "1", "volume": "2016", "year": "2016"},
            {"series": "Superman", "issue_number": "5", "volume": None, "year": "2018"},
        ]
    })
    def test_export_success(self, mock_get, client):
        resp = client.get("/api/reading-lists/1/export")
        assert resp.status_code == 200
        assert "application/xml" in resp.content_type
        assert b"Test List" in resp.data
        assert b'Series="Batman"' in resp.data
        assert b'Number="1"' in resp.data
        assert b'Series="Superman"' in resp.data
        assert ".cbl" in resp.headers["Content-Disposition"]

    @patch("routes.reading_lists.get_reading_list", return_value=None)
    def test_export_not_found(self, mock_get, client):
        resp = client.get("/api/reading-lists/999/export")
        assert resp.status_code == 404


class TestSummary:

    @patch("routes.reading_lists.get_user_reading_lists_summary", return_value=[
        {"id": 1, "name": "Batman"},
        {"id": 2, "name": "X-Men"},
    ])
    def test_summary(self, mock_get, client):
        resp = client.get("/api/reading-lists/summary")
        data = resp.get_json()
        assert len(data) == 2
        assert data[0]["name"] == "Batman"
