"""Tests for reading list GitHub tree browser, batch import, and sync endpoints."""
import pytest
from unittest.mock import patch, MagicMock
import hashlib


class TestGithubTree:

    @patch("routes.reading_lists.requests.get")
    def test_github_tree_returns_tree(self, mock_get, client):
        # Reset cache
        import routes.reading_lists as rl
        rl._github_tree_cache = {"tree": None, "fetched_at": 0}

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "tree": [
                {"path": "DC/Events/Crisis.cbl", "type": "blob"},
                {"path": "DC/Events", "type": "tree"},
                {"path": "DC", "type": "tree"},
                {"path": "README.md", "type": "blob"},
            ]
        }
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        resp = client.get("/api/reading-lists/github-tree")
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["tree"]) > 0

    @patch("routes.reading_lists.requests.get")
    def test_github_tree_filters_cbl_files(self, mock_get, client):
        import routes.reading_lists as rl
        rl._github_tree_cache = {"tree": None, "fetched_at": 0}

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "tree": [
                {"path": "DC/Events/Crisis.cbl", "type": "blob"},
                {"path": "DC/Events/README.md", "type": "blob"},
                {"path": "Marvel/X-Men.cbl", "type": "blob"},
            ]
        }
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        resp = client.get("/api/reading-lists/github-tree")
        data = resp.get_json()
        assert data["success"] is True
        # Should have folders + 2 CBL files (not README.md)
        blob_items = [i for i in data["tree"] if i["type"] == "blob"]
        assert len(blob_items) == 2
        paths = [i["path"] for i in blob_items]
        assert "DC/Events/README.md" not in paths

    def test_github_tree_returns_cached_tree(self, client):
        import routes.reading_lists as rl
        import time
        rl._github_tree_cache = {
            "tree": [{"path": "cached.cbl", "type": "blob"}],
            "fetched_at": time.time(),
        }

        resp = client.get("/api/reading-lists/github-tree")
        data = resp.get_json()
        assert data["success"] is True
        assert data["tree"][0]["path"] == "cached.cbl"


class TestImportBatch:

    @patch("routes.reading_lists.threading.Thread")
    def test_import_batch_creates_tasks(self, mock_thread, client):
        resp = client.post(
            "/api/reading-lists/import-batch",
            json={"files": ["DC/Crisis.cbl", "Marvel/X-Men.cbl"]},
        )
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["tasks"]) == 2
        assert data["tasks"][0]["filename"] == "Crisis.cbl"
        assert data["tasks"][1]["filename"] == "X-Men.cbl"

    def test_import_batch_no_files(self, client):
        resp = client.post("/api/reading-lists/import-batch", json={"files": []})
        data = resp.get_json()
        assert data["success"] is False


class TestSyncList:

    @patch("routes.reading_lists.requests.get")
    @patch("routes.reading_lists.get_reading_list")
    def test_sync_unchanged_list(self, mock_get_list, mock_requests_get, client):
        content = "<ReadingList><Name>Test</Name><Books></Books></ReadingList>"
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        mock_get_list.return_value = {
            "id": 1,
            "name": "Test",
            "source": "https://raw.githubusercontent.com/DieselTech/CBL-ReadingLists/main/test.cbl",
            "source_hash": content_hash,
            "entries": [],
        }

        mock_response = MagicMock()
        mock_response.text = content
        mock_response.raise_for_status = MagicMock()
        mock_requests_get.return_value = mock_response

        resp = client.post("/api/reading-lists/1/sync")
        data = resp.get_json()
        assert data["success"] is True
        assert data["changed"] is False

    @patch("routes.reading_lists.update_reading_list_source_hash", return_value=True)
    @patch("routes.reading_lists.sync_reading_list_entries", return_value={"added": 2, "removed": 1})
    @patch("routes.reading_lists.requests.get")
    @patch("routes.reading_lists.get_reading_list")
    def test_sync_changed_list(self, mock_get_list, mock_requests_get, mock_sync, mock_update_hash, client):
        mock_get_list.return_value = {
            "id": 1,
            "name": "Test",
            "source": "https://raw.githubusercontent.com/DieselTech/CBL-ReadingLists/main/test.cbl",
            "source_hash": "oldhash",
            "entries": [],
        }

        new_content = """<?xml version="1.0"?>
        <ReadingList><Name>Test</Name><Books>
            <Book Series="Batman" Number="1" Volume="2016" Year="2016"/>
        </Books></ReadingList>"""

        mock_response = MagicMock()
        mock_response.text = new_content
        mock_response.raise_for_status = MagicMock()
        mock_requests_get.return_value = mock_response

        resp = client.post("/api/reading-lists/1/sync")
        data = resp.get_json()
        assert data["success"] is True
        assert data["changed"] is True
        assert data["added"] == 2
        assert data["removed"] == 1

    @patch("routes.reading_lists.get_reading_list")
    def test_sync_returns_error_for_non_github_source(self, mock_get_list, client):
        mock_get_list.return_value = {
            "id": 1,
            "name": "Test",
            "source": "uploaded_file.cbl",
            "source_hash": None,
            "entries": [],
        }

        resp = client.post("/api/reading-lists/1/sync")
        data = resp.get_json()
        assert data["success"] is False
        assert resp.status_code == 400

    @patch("routes.reading_lists.get_reading_list", return_value=None)
    def test_sync_not_found(self, mock_get_list, client):
        resp = client.post("/api/reading-lists/999/sync")
        assert resp.status_code == 404
