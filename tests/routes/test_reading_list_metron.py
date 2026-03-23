"""Tests for reading list Metron browse and import endpoints."""
import pytest
from unittest.mock import patch, MagicMock


class TestMetronBrowse:

    @patch("routes.reading_lists.is_metron_configured", return_value=False)
    def test_browse_not_configured(self, mock_configured, client):
        resp = client.get("/api/reading-lists/metron-browse")
        data = resp.get_json()
        assert data["success"] is False
        assert "not configured" in data["message"]

    @patch("routes.reading_lists.fetch_reading_lists")
    @patch("routes.reading_lists.get_flask_api")
    @patch("routes.reading_lists.is_metron_configured", return_value=True)
    def test_browse_returns_lists(self, mock_configured, mock_api, mock_fetch, client):
        mock_api.return_value = MagicMock()
        mock_fetch.return_value = [
            {"id": 1, "name": "Crisis on Infinite Earths", "user": "testuser"},
            {"id": 2, "name": "Secret Wars", "user": "anotheruser"},
        ]

        resp = client.get("/api/reading-lists/metron-browse")
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["lists"]) == 2
        assert data["lists"][0]["name"] == "Crisis on Infinite Earths"

    @patch("routes.reading_lists.fetch_reading_lists")
    @patch("routes.reading_lists.get_flask_api")
    @patch("routes.reading_lists.is_metron_configured", return_value=True)
    def test_browse_with_search(self, mock_configured, mock_api, mock_fetch, client):
        mock_api.return_value = MagicMock()
        mock_fetch.return_value = [
            {"id": 1, "name": "Crisis on Infinite Earths"},
        ]

        resp = client.get("/api/reading-lists/metron-browse?search=crisis")
        data = resp.get_json()
        assert data["success"] is True

        # Verify search param was forwarded
        call_args = mock_fetch.call_args
        assert call_args[0][1] == {"name": "crisis"}

    @patch("routes.reading_lists.fetch_reading_lists")
    @patch("routes.reading_lists.get_flask_api")
    @patch("routes.reading_lists.is_metron_configured", return_value=True)
    def test_browse_api_failure(self, mock_configured, mock_api, mock_fetch, client):
        mock_api.return_value = MagicMock()
        mock_fetch.return_value = []

        resp = client.get("/api/reading-lists/metron-browse")
        data = resp.get_json()
        assert data["success"] is True
        assert data["lists"] == []

    @patch("routes.reading_lists.get_flask_api", return_value=None)
    @patch("routes.reading_lists.is_metron_configured", return_value=True)
    def test_browse_api_connect_failure(self, mock_configured, mock_api, client):
        resp = client.get("/api/reading-lists/metron-browse")
        data = resp.get_json()
        assert data["success"] is False
        assert "Failed to connect" in data["message"]


class TestMetronImport:

    @patch("routes.reading_lists.threading.Thread")
    @patch("routes.reading_lists.is_metron_configured", return_value=True)
    def test_import_creates_tasks(self, mock_configured, mock_thread, client):
        resp = client.post(
            "/api/reading-lists/metron-import",
            json={"list_ids": [1, 2, 3]},
        )
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["tasks"]) == 3
        assert data["tasks"][0]["list_id"] == 1
        assert data["tasks"][1]["list_id"] == 2
        assert data["tasks"][2]["list_id"] == 3

    @patch("routes.reading_lists.is_metron_configured", return_value=True)
    def test_import_no_list_ids(self, mock_configured, client):
        resp = client.post(
            "/api/reading-lists/metron-import",
            json={"list_ids": []},
        )
        data = resp.get_json()
        assert data["success"] is False
        assert "No lists selected" in data["message"]

    @patch("routes.reading_lists.is_metron_configured", return_value=False)
    def test_import_not_configured(self, mock_configured, client):
        resp = client.post(
            "/api/reading-lists/metron-import",
            json={"list_ids": [1]},
        )
        data = resp.get_json()
        assert data["success"] is False
        assert "not configured" in data["message"]

    @patch("routes.reading_lists.is_metron_configured", return_value=True)
    def test_import_no_body(self, mock_configured, client):
        resp = client.post(
            "/api/reading-lists/metron-import",
            content_type="application/json",
            data="{}",
        )
        data = resp.get_json()
        assert data["success"] is False


class TestMetronArcBrowse:

    @patch("routes.reading_lists.is_metron_configured", return_value=False)
    def test_arc_browse_not_configured(self, mock_configured, client):
        resp = client.get("/api/reading-lists/metron-browse-arcs")
        data = resp.get_json()
        assert data["success"] is False
        assert "not configured" in data["message"]

    @patch("routes.reading_lists.fetch_arcs_page")
    @patch("routes.reading_lists.get_flask_api")
    @patch("routes.reading_lists.is_metron_configured", return_value=True)
    def test_arc_browse_returns_arcs(self, mock_configured, mock_api, mock_fetch, client):
        mock_api.return_value = MagicMock()
        mock_fetch.return_value = {
            "results": [
                {"id": 10, "name": "Knightfall"},
                {"id": 20, "name": "No Man's Land"},
            ],
            "has_next": True,
            "count": 100,
            "page": 1,
        }

        resp = client.get("/api/reading-lists/metron-browse-arcs")
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["results"]) == 2
        assert data["results"][0]["name"] == "Knightfall"
        assert data["has_next"] is True
        assert data["count"] == 100
        assert data["page"] == 1

    @patch("routes.reading_lists.fetch_arcs_page")
    @patch("routes.reading_lists.get_flask_api")
    @patch("routes.reading_lists.is_metron_configured", return_value=True)
    def test_arc_browse_with_search(self, mock_configured, mock_api, mock_fetch, client):
        mock_api.return_value = MagicMock()
        mock_fetch.return_value = {
            "results": [{"id": 10, "name": "Knightfall"}],
            "has_next": False,
            "count": 1,
            "page": 1,
        }

        resp = client.get("/api/reading-lists/metron-browse-arcs?search=knight")
        data = resp.get_json()
        assert data["success"] is True

        # Verify search param was forwarded
        call_args = mock_fetch.call_args
        assert call_args[0][1] == {"name": "knight"}

    @patch("routes.reading_lists.fetch_arcs_page")
    @patch("routes.reading_lists.get_flask_api")
    @patch("routes.reading_lists.is_metron_configured", return_value=True)
    def test_arc_browse_page_param(self, mock_configured, mock_api, mock_fetch, client):
        mock_api.return_value = MagicMock()
        mock_fetch.return_value = {
            "results": [{"id": 30, "name": "War of the Realms"}],
            "has_next": False,
            "count": 50,
            "page": 2,
        }

        resp = client.get("/api/reading-lists/metron-browse-arcs?page=2")
        data = resp.get_json()
        assert data["success"] is True
        assert data["page"] == 2

        # Verify page param was forwarded
        call_args = mock_fetch.call_args
        assert call_args[1]["page"] == 2

    @patch("routes.reading_lists.fetch_arcs_page")
    @patch("routes.reading_lists.get_flask_api")
    @patch("routes.reading_lists.is_metron_configured", return_value=True)
    def test_arc_browse_api_failure(self, mock_configured, mock_api, mock_fetch, client):
        mock_api.return_value = MagicMock()
        mock_fetch.return_value = {
            "results": [],
            "has_next": False,
            "count": 0,
            "page": 1,
        }

        resp = client.get("/api/reading-lists/metron-browse-arcs")
        data = resp.get_json()
        assert data["success"] is True
        assert data["results"] == []

    @patch("routes.reading_lists.get_flask_api", return_value=None)
    @patch("routes.reading_lists.is_metron_configured", return_value=True)
    def test_arc_browse_api_connect_failure(self, mock_configured, mock_api, client):
        resp = client.get("/api/reading-lists/metron-browse-arcs")
        data = resp.get_json()
        assert data["success"] is False
        assert "Failed to connect" in data["message"]


class TestMetronArcImport:

    @patch("routes.reading_lists.threading.Thread")
    @patch("routes.reading_lists.is_metron_configured", return_value=True)
    def test_arc_import_creates_tasks(self, mock_configured, mock_thread, client):
        resp = client.post(
            "/api/reading-lists/metron-import-arcs",
            json={"arc_ids": [10, 20, 30]},
        )
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["tasks"]) == 3
        assert data["tasks"][0]["arc_id"] == 10
        assert data["tasks"][1]["arc_id"] == 20
        assert data["tasks"][2]["arc_id"] == 30

    @patch("routes.reading_lists.is_metron_configured", return_value=True)
    def test_arc_import_no_arc_ids(self, mock_configured, client):
        resp = client.post(
            "/api/reading-lists/metron-import-arcs",
            json={"arc_ids": []},
        )
        data = resp.get_json()
        assert data["success"] is False
        assert "No arcs selected" in data["message"]

    @patch("routes.reading_lists.is_metron_configured", return_value=False)
    def test_arc_import_not_configured(self, mock_configured, client):
        resp = client.post(
            "/api/reading-lists/metron-import-arcs",
            json={"arc_ids": [10]},
        )
        data = resp.get_json()
        assert data["success"] is False
        assert "not configured" in data["message"]

    @patch("routes.reading_lists.is_metron_configured", return_value=True)
    def test_arc_import_no_body(self, mock_configured, client):
        resp = client.post(
            "/api/reading-lists/metron-import-arcs",
            content_type="application/json",
            data="{}",
        )
        data = resp.get_json()
        assert data["success"] is False
