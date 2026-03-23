"""Tests for reading list ComicVine browse and import endpoints."""
import pytest
from unittest.mock import patch, MagicMock


class TestCVArcBrowse:

    @patch("routes.reading_lists.is_comicvine_configured", return_value=False)
    def test_cv_arc_browse_not_configured(self, mock_configured, client):
        resp = client.get("/api/reading-lists/cv-browse-arcs")
        data = resp.get_json()
        assert data["success"] is False
        assert "not configured" in data["message"]

    @patch("routes.reading_lists.fetch_cv_arcs")
    @patch("routes.reading_lists.get_cv_api_key", return_value="test-key")
    @patch("routes.reading_lists.is_comicvine_configured", return_value=True)
    def test_cv_arc_browse_returns_arcs(self, mock_configured, mock_key, mock_fetch, client):
        mock_fetch.return_value = [
            {"id": 10, "name": "Knightfall", "description": None, "issue_count": 20, "publisher": "DC Comics"},
            {"id": 20, "name": "No Man's Land", "description": "Gotham...", "issue_count": 80, "publisher": "DC Comics"},
        ]

        resp = client.get("/api/reading-lists/cv-browse-arcs")
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["arcs"]) == 2
        assert data["arcs"][0]["name"] == "Knightfall"

    @patch("routes.reading_lists.fetch_cv_arcs")
    @patch("routes.reading_lists.get_cv_api_key", return_value="test-key")
    @patch("routes.reading_lists.is_comicvine_configured", return_value=True)
    def test_cv_arc_browse_with_search(self, mock_configured, mock_key, mock_fetch, client):
        mock_fetch.return_value = [
            {"id": 10, "name": "Knightfall"},
        ]

        resp = client.get("/api/reading-lists/cv-browse-arcs?search=knight")
        data = resp.get_json()
        assert data["success"] is True

        # Verify search param was forwarded
        call_args = mock_fetch.call_args
        assert call_args[1]["search"] == "knight"

    @patch("routes.reading_lists.fetch_cv_arcs")
    @patch("routes.reading_lists.get_cv_api_key", return_value="test-key")
    @patch("routes.reading_lists.is_comicvine_configured", return_value=True)
    def test_cv_arc_browse_empty(self, mock_configured, mock_key, mock_fetch, client):
        mock_fetch.return_value = []

        resp = client.get("/api/reading-lists/cv-browse-arcs")
        data = resp.get_json()
        assert data["success"] is True
        assert data["arcs"] == []

    @patch("routes.reading_lists.get_cv_api_key", return_value=None)
    @patch("routes.reading_lists.is_comicvine_configured", return_value=True)
    def test_cv_arc_browse_no_api_key(self, mock_configured, mock_key, client):
        resp = client.get("/api/reading-lists/cv-browse-arcs")
        data = resp.get_json()
        assert data["success"] is False
        assert "not found" in data["message"]


class TestCVArcImport:

    @patch("routes.reading_lists.threading.Thread")
    @patch("routes.reading_lists.is_comicvine_configured", return_value=True)
    def test_cv_arc_import_creates_tasks(self, mock_configured, mock_thread, client):
        resp = client.post(
            "/api/reading-lists/cv-import-arcs",
            json={"arc_ids": [10, 20, 30]},
        )
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["tasks"]) == 3
        assert data["tasks"][0]["arc_id"] == 10
        assert data["tasks"][1]["arc_id"] == 20
        assert data["tasks"][2]["arc_id"] == 30

    @patch("routes.reading_lists.is_comicvine_configured", return_value=True)
    def test_cv_arc_import_no_arc_ids(self, mock_configured, client):
        resp = client.post(
            "/api/reading-lists/cv-import-arcs",
            json={"arc_ids": []},
        )
        data = resp.get_json()
        assert data["success"] is False
        assert "No arcs selected" in data["message"]

    @patch("routes.reading_lists.is_comicvine_configured", return_value=False)
    def test_cv_arc_import_not_configured(self, mock_configured, client):
        resp = client.post(
            "/api/reading-lists/cv-import-arcs",
            json={"arc_ids": [10]},
        )
        data = resp.get_json()
        assert data["success"] is False
        assert "not configured" in data["message"]

    @patch("routes.reading_lists.is_comicvine_configured", return_value=True)
    def test_cv_arc_import_no_body(self, mock_configured, client):
        resp = client.post(
            "/api/reading-lists/cv-import-arcs",
            content_type="application/json",
            data="{}",
        )
        data = resp.get_json()
        assert data["success"] is False
