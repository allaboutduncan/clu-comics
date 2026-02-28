"""Tests for routes/collection.py -- collection browse and search endpoints."""
import os
import pytest
from unittest.mock import patch, MagicMock


class TestFilesPage:

    @patch("routes.collection.config")
    def test_files_page(self, mock_config, client):
        mock_config.get.side_effect = lambda s, k, fallback="": fallback
        resp = client.get("/files")
        assert resp.status_code == 200


class TestCollectionPage:

    @patch("routes.collection.get_dashboard_sections", return_value=[])
    @patch("routes.collection.config")
    def test_collection_root(self, mock_config, mock_sections, client):
        mock_config.get.return_value = "True"
        resp = client.get("/collection")
        assert resp.status_code == 200

    @patch("routes.collection.get_dashboard_sections", return_value=[])
    @patch("routes.collection.config")
    def test_collection_with_subpath(self, mock_config, mock_sections, client):
        mock_config.get.return_value = "True"
        resp = client.get("/collection/DC%20Comics/Batman")
        assert resp.status_code == 200


class TestToReadPage:

    def test_to_read_page(self, client):
        resp = client.get("/to-read")
        assert resp.status_code == 200


class TestApiBrowse:

    @patch("routes.collection.get_directory_children")
    def test_browse_root(self, mock_children, client, app, tmp_path):
        data_dir = str(tmp_path / "data")
        mock_children.return_value = ([], [])

        with patch.dict("sys.modules", {"app": MagicMock(DATA_DIR=data_dir)}):
            resp = client.get("/api/browse")

        assert resp.status_code == 200
        data = resp.get_json()
        assert "directories" in data
        assert "files" in data

    @patch("routes.collection.get_directory_children")
    def test_browse_with_path(self, mock_children, client, tmp_path):
        path = str(tmp_path / "data")
        os.makedirs(path, exist_ok=True)
        mock_children.return_value = (
            [{"name": "DC Comics", "path": os.path.join(path, "DC Comics"),
              "has_thumbnail": False}],
            [{"name": "comic.cbz", "path": os.path.join(path, "comic.cbz"),
              "size": 1000, "has_comicinfo": True}],
        )

        with patch.dict("sys.modules", {"app": MagicMock(DATA_DIR=path)}):
            resp = client.get(f"/api/browse?path={path}")

        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data["directories"]) == 1
        assert len(data["files"]) == 1

    @patch("routes.collection.get_directory_children",
           side_effect=Exception("DB error"))
    def test_browse_error(self, mock_children, client, tmp_path):
        with patch.dict("sys.modules", {"app": MagicMock(DATA_DIR=str(tmp_path))}):
            resp = client.get("/api/browse")
        assert resp.status_code == 500


class TestApiMissingXml:

    @patch("database.get_files_missing_comicinfo",
           return_value=[{"name": "x.cbz", "path": "/data/x.cbz",
                          "size": 100, "has_comicinfo": False,
                          "has_thumbnail": False}])
    def test_missing_xml(self, mock_fn, client):
        resp = client.get("/api/missing-xml")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["total"] >= 0


class TestApiIssuesReadPaths:

    @patch("database.get_issues_read", return_value=[
        {"issue_path": "/data/Batman.cbz"},
    ])
    def test_issues_read_paths(self, mock_read, client):
        resp = client.get("/api/issues-read-paths")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "paths" in data


class TestSearchFiles:

    @patch("routes.collection.search_file_index", return_value=[
        {"name": "Batman 001.cbz", "path": "/data/Batman 001.cbz", "type": "file"},
    ])
    def test_search_files(self, mock_search, client):
        with patch.dict("sys.modules", {"app": MagicMock(index_built=True)}):
            resp = client.get("/search-files?query=batman")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["results"]) == 1

    def test_search_empty_query(self, client):
        with patch.dict("sys.modules", {"app": MagicMock(index_built=True)}):
            resp = client.get("/search-files?query=")
        assert resp.status_code == 400

    def test_search_too_short(self, client):
        with patch.dict("sys.modules", {"app": MagicMock(index_built=True)}):
            resp = client.get("/search-files?query=a")
        assert resp.status_code == 400


class TestCountFiles:

    def test_count_files(self, client, tmp_path):
        # Create some files in the tmp dir
        d = tmp_path / "comics"
        d.mkdir()
        (d / "a.cbz").write_bytes(b"fake")
        (d / "b.cbz").write_bytes(b"fake")

        resp = client.get(f"/count-files?path={d}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["file_count"] == 2

    def test_count_invalid_path(self, client):
        resp = client.get("/count-files?path=/nonexistent/path")
        assert resp.status_code == 400


class TestApiBrowseMetadata:

    @patch("routes.collection.get_path_counts_batch", return_value={
        "/data/DC": (5, 20),
    })
    def test_browse_metadata(self, mock_counts, client):
        resp = client.post("/api/browse-metadata",
                           json={"paths": ["/data/DC"]})
        assert resp.status_code == 200
        data = resp.get_json()
        assert "/data/DC" in data["metadata"]
        assert data["metadata"]["/data/DC"]["folder_count"] == 5

    def test_no_paths(self, client):
        resp = client.post("/api/browse-metadata", json={"paths": []})
        assert resp.status_code == 400

    def test_too_many_paths(self, client):
        resp = client.post("/api/browse-metadata",
                           json={"paths": [f"/data/{i}" for i in range(101)]})
        assert resp.status_code == 400


class TestApiClearBrowseCache:

    @patch("routes.collection.invalidate_browse_cache")
    def test_clear_specific_path(self, mock_inv, client):
        with patch.dict("sys.modules", {"app": MagicMock()}):
            resp = client.post("/api/clear-browse-cache",
                               json={"path": "/data/DC"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_clear_all(self, client):
        mock_clear = MagicMock()
        with patch.dict("sys.modules", {"app": MagicMock(clear_browse_cache=mock_clear)}):
            resp = client.post("/api/clear-browse-cache", json={})
        assert resp.status_code == 200


class TestListRecentFiles:

    @patch("routes.collection.get_recent_files", return_value=[
        {"name": "Batman.cbz", "path": "/data/Batman.cbz", "added_at": "2024-01-01"},
    ])
    def test_list_recent(self, mock_recent, client):
        resp = client.get("/list-recent-files")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["total_count"] == 1

    @patch("routes.collection.get_recent_files", return_value=[])
    def test_list_recent_empty(self, mock_recent, client):
        resp = client.get("/list-recent-files")
        data = resp.get_json()
        assert data["total_count"] == 0
        assert data["date_range"] is None


class TestFolderThumbnail:

    def test_missing_path(self, client):
        resp = client.get("/api/folder-thumbnail")
        assert resp.status_code == 200  # Returns error.svg

    def test_nonexistent_path(self, client):
        resp = client.get("/api/folder-thumbnail?path=/nonexistent/image.png")
        assert resp.status_code == 200  # Returns error.svg

    def test_valid_image(self, client, tmp_path):
        from PIL import Image
        img_path = tmp_path / "folder.png"
        Image.new("RGB", (10, 10), "red").save(str(img_path))

        resp = client.get(f"/api/folder-thumbnail?path={img_path}")
        assert resp.status_code == 200
        assert resp.content_type == "image/png"


class TestCbzPreview:

    def test_invalid_path(self, client):
        resp = client.get("/cbz-preview?path=/nonexistent.cbz")
        assert resp.status_code == 400

    def test_non_cbz_file(self, client, tmp_path):
        txt = tmp_path / "test.txt"
        txt.write_text("hello")
        resp = client.get(f"/cbz-preview?path={txt}")
        assert resp.status_code == 400

    def test_valid_cbz(self, client, create_cbz):
        cbz_path = create_cbz("preview.cbz", num_images=2)
        resp = client.get(f"/cbz-preview?path={cbz_path}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["total_images"] == 2
        assert "preview" in data


class TestApiOnTheStack:

    @patch("database.get_on_the_stack_items", return_value=[])
    def test_empty_response(self, mock_items, client):
        resp = client.get("/api/on-the-stack")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["items"] == []
        assert data["total_count"] == 0

    @patch("database.get_on_the_stack_items")
    def test_with_items(self, mock_items, client):
        mock_items.return_value = [{
            "series_id": 100,
            "series_name": "Absolute Batman",
            "issue_number": "4",
            "file_path": "/data/DC/Absolute Batman/Absolute Batman 004.cbz",
            "file_name": "Absolute Batman 004.cbz",
            "cover_image": "https://example.com/cover.jpg",
            "last_read_at": "2024-12-01 10:00:00",
            "series_status": "Ongoing",
        }]
        resp = client.get("/api/on-the-stack")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["items"]) == 1
        assert data["items"][0]["series_name"] == "Absolute Batman"

    @patch("database.get_on_the_stack_items")
    def test_limit_param(self, mock_items, client):
        mock_items.return_value = []
        resp = client.get("/api/on-the-stack?limit=5")
        assert resp.status_code == 200
        mock_items.assert_called_once_with(limit=5)
