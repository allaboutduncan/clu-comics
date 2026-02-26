"""Tests for favorites.py blueprint -- /api/favorites endpoints."""
import pytest
from unittest.mock import patch


class TestPublishersEndpoints:

    @patch("favorites.get_favorite_publishers", return_value=[
        {"path": "/data/DC Comics", "created_at": "2024-01-01"},
    ])
    def test_get_publishers(self, mock_get, client):
        resp = client.get("/api/favorites/publishers")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["publishers"]) == 1

    @patch("favorites.get_favorite_publishers", side_effect=Exception("db error"))
    def test_get_publishers_error(self, mock_get, client):
        resp = client.get("/api/favorites/publishers")
        assert resp.status_code == 500
        assert resp.get_json()["success"] is False

    @patch("favorites.is_favorite_publisher", return_value=True)
    def test_check_publisher_favorited(self, mock_check, client):
        resp = client.get("/api/favorites/publishers/check?path=/data/DC")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["is_favorite"] is True

    def test_check_publisher_missing_param(self, client):
        resp = client.get("/api/favorites/publishers/check")
        assert resp.status_code == 400

    @patch("favorites.add_favorite_publisher", return_value=True)
    def test_add_publisher(self, mock_add, client):
        resp = client.post("/api/favorites/publishers",
                           json={"path": "/data/DC Comics"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        mock_add.assert_called_once_with("/data/DC Comics")

    def test_add_publisher_missing_path(self, client):
        resp = client.post("/api/favorites/publishers", json={})
        assert resp.status_code == 400

    @patch("favorites.add_favorite_publisher", return_value=False)
    def test_add_publisher_failure(self, mock_add, client):
        resp = client.post("/api/favorites/publishers",
                           json={"path": "/data/DC"})
        assert resp.status_code == 500

    @patch("favorites.remove_favorite_publisher", return_value=True)
    def test_remove_publisher(self, mock_rm, client):
        resp = client.delete("/api/favorites/publishers",
                             json={"path": "/data/DC Comics"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_remove_publisher_missing_path(self, client):
        resp = client.delete("/api/favorites/publishers", json={})
        assert resp.status_code == 400


class TestIssuesReadEndpoints:

    @patch("favorites.get_issues_read", return_value=[
        {"issue_path": "/data/DC/Batman 001.cbz", "read_at": "2024-01-01"},
    ])
    def test_get_issues(self, mock_get, client):
        resp = client.get("/api/favorites/issues")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["issues"]) == 1

    @patch("favorites.is_issue_read", return_value=True)
    @patch("favorites.get_issue_read_date", return_value="2024-01-01")
    def test_check_issue_read(self, mock_date, mock_check, client):
        resp = client.get("/api/favorites/issues/check?path=/data/DC/Batman.cbz")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["is_read"] is True
        assert data["read_at"] == "2024-01-01"

    @patch("favorites.is_issue_read", return_value=False)
    def test_check_issue_not_read(self, mock_check, client):
        resp = client.get("/api/favorites/issues/check?path=/data/DC/Batman.cbz")
        data = resp.get_json()
        assert data["is_read"] is False
        assert data["read_at"] is None

    def test_check_issue_missing_param(self, client):
        resp = client.get("/api/favorites/issues/check")
        assert resp.status_code == 400

    @patch("favorites.clear_stats_cache_keys")
    @patch("favorites.mark_issue_read", return_value=True)
    def test_mark_read(self, mock_mark, mock_cache, client):
        resp = client.post("/api/favorites/issues",
                           json={"path": "/data/DC/Batman.cbz"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        mock_cache.assert_called_once()

    def test_mark_read_missing_path(self, client):
        resp = client.post("/api/favorites/issues", json={})
        assert resp.status_code == 400

    @patch("favorites.clear_stats_cache_keys")
    @patch("favorites.unmark_issue_read", return_value=True)
    def test_unmark_read(self, mock_unmark, mock_cache, client):
        resp = client.delete("/api/favorites/issues",
                             json={"path": "/data/DC/Batman.cbz"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True


class TestToReadEndpoints:

    @patch("favorites.get_to_read_items", return_value=[
        {"path": "/data/DC/Batman.cbz", "type": "file"},
    ])
    def test_get_to_read(self, mock_get, client):
        resp = client.get("/api/favorites/to-read")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["items"]) == 1

    @patch("favorites.is_to_read", return_value=True)
    def test_check_to_read(self, mock_check, client):
        resp = client.get("/api/favorites/to-read/check?path=/data/DC/Batman.cbz")
        data = resp.get_json()
        assert data["is_to_read"] is True

    def test_check_to_read_missing_param(self, client):
        resp = client.get("/api/favorites/to-read/check")
        assert resp.status_code == 400

    @patch("favorites.add_to_read", return_value=True)
    def test_add_to_read(self, mock_add, client):
        resp = client.post("/api/favorites/to-read",
                           json={"path": "/data/DC/Batman.cbz", "type": "file"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_add_to_read_missing_path(self, client):
        resp = client.post("/api/favorites/to-read", json={})
        assert resp.status_code == 400

    @patch("favorites.remove_to_read", return_value=True)
    def test_remove_to_read(self, mock_rm, client):
        resp = client.delete("/api/favorites/to-read",
                             json={"path": "/data/DC/Batman.cbz"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_remove_to_read_missing_path(self, client):
        resp = client.delete("/api/favorites/to-read", json={})
        assert resp.status_code == 400
