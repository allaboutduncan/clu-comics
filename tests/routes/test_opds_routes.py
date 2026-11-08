"""Tests for opds.py -- OPDS feed endpoints."""
import os
import pytest
from unittest.mock import patch, MagicMock


class TestOpdsFeedId:

    def test_generate_feed_id(self):
        from routes.opds import generate_feed_id
        result = generate_feed_id("/opds")
        assert result.startswith("urn:uuid:")
        assert len(result) == len("urn:uuid:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")

    def test_deterministic(self):
        from routes.opds import generate_feed_id
        a = generate_feed_id("/some/path")
        b = generate_feed_id("/some/path")
        assert a == b

    def test_different_paths(self):
        from routes.opds import generate_feed_id
        a = generate_feed_id("/a")
        b = generate_feed_id("/b")
        assert a != b


class TestGetTimestamp:

    def test_format(self):
        from routes.opds import get_timestamp
        ts = get_timestamp()
        assert ts.endswith("Z")
        assert "T" in ts


class TestIsValidLibraryPath:

    @patch("routes.opds.get_library_roots", return_value=["/data"])
    def test_valid_path(self, mock_roots):
        from routes.opds import is_valid_library_path
        assert is_valid_library_path("/data/Comics/Batman") is True

    @patch("routes.opds.get_library_roots", return_value=["/data"])
    def test_exact_root(self, mock_roots):
        from routes.opds import is_valid_library_path
        assert is_valid_library_path("/data") is True

    @patch("routes.opds.get_library_roots", return_value=["/data"])
    def test_invalid_path(self, mock_roots):
        from routes.opds import is_valid_library_path
        assert is_valid_library_path("/etc/passwd") is False

    @patch("routes.opds.get_library_roots", return_value=["/data"])
    def test_empty_path(self, mock_roots):
        from routes.opds import is_valid_library_path
        assert is_valid_library_path("") is False

    @patch("routes.opds.get_library_roots", return_value=["/data"])
    def test_none_path(self, mock_roots):
        from routes.opds import is_valid_library_path
        assert is_valid_library_path(None) is False


class TestFolderThumbnailUrl:
    """OPDS covers go through core.folder_art like the web grid (#720)."""

    def test_no_thumbnail(self, app, tmp_path):
        from routes.opds import folder_thumbnail_url
        with app.test_request_context():
            assert folder_thumbnail_url(str(tmp_path)) is None

    @pytest.mark.parametrize("name", ["folder.png", "folder.jpg", "folder.gif", "folder.webp"])
    def test_legacy_disk_thumbnail(self, app, tmp_path, name):
        from routes.opds import folder_thumbnail_url
        from PIL import Image
        Image.new("RGB", (10, 10), "red").save(str(tmp_path / name))
        with app.test_request_context():
            url = folder_thumbnail_url(str(tmp_path))
        assert url.startswith("http")
        assert "kind=folder" in url
        assert name not in url

    def test_cached_thumbnail(self, app, tmp_path):
        from core import folder_art
        from routes.opds import folder_thumbnail_url
        from PIL import Image
        assert folder_art.put_art(
            str(tmp_path), "folder", Image.new("RGB", (10, 10), "red"), ".png", "generated")
        with app.test_request_context():
            url = folder_thumbnail_url(str(tmp_path))
        assert "kind=folder" in url


class TestGetDirectoryListingForOpds:

    def test_empty_directory(self, tmp_path):
        from routes.opds import get_directory_listing_for_opds
        dirs, files = get_directory_listing_for_opds(str(tmp_path))
        assert dirs == []
        assert files == []

    def test_directories_and_files(self, tmp_path):
        from routes.opds import get_directory_listing_for_opds
        (tmp_path / "Batman").mkdir()
        (tmp_path / "comic.cbz").write_bytes(b"PK\x03\x04")
        (tmp_path / "readme.txt").write_text("hello")

        dirs, files = get_directory_listing_for_opds(str(tmp_path))
        assert len(dirs) == 1
        assert dirs[0]["name"] == "Batman"
        assert len(files) == 1
        assert files[0]["name"] == "comic.cbz"

    def test_skips_hidden(self, tmp_path):
        from routes.opds import get_directory_listing_for_opds
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "_macosx").mkdir()
        (tmp_path / "visible").mkdir()

        dirs, files = get_directory_listing_for_opds(str(tmp_path))
        assert len(dirs) == 1
        assert dirs[0]["name"] == "visible"

    def test_comic_extensions(self, tmp_path):
        from routes.opds import get_directory_listing_for_opds
        (tmp_path / "comic.cbz").write_bytes(b"fake")
        (tmp_path / "comic.cbr").write_bytes(b"fake")
        (tmp_path / "comic.pdf").write_bytes(b"fake")
        (tmp_path / "comic.epub").write_bytes(b"fake")
        (tmp_path / "notes.txt").write_text("text")

        dirs, files = get_directory_listing_for_opds(str(tmp_path))
        assert len(files) == 4
        names = {f["name"] for f in files}
        assert "comic.cbz" in names
        assert "notes.txt" not in names

    def test_nonexistent_directory(self):
        from routes.opds import get_directory_listing_for_opds
        dirs, files = get_directory_listing_for_opds("/nonexistent")
        assert dirs == []
        assert files == []


class TestOpdsMimeTypes:

    def test_comic_mime_types(self):
        from routes.opds import COMIC_MIME_TYPES
        assert COMIC_MIME_TYPES[".cbz"] == "application/vnd.comicbook+zip"
        assert COMIC_MIME_TYPES[".cbr"] == "application/vnd.comicbook-rar"
        assert COMIC_MIME_TYPES[".pdf"] == "application/pdf"


class TestOpdsRoot:

    @patch("routes.opds.get_libraries", return_value=[{"path": "/data", "name": "Library"}])
    @patch("routes.opds.get_to_read_items", return_value=[])
    def test_root_feed(self, mock_read, mock_libs, client):
        resp = client.get("/opds/")
        assert resp.status_code == 200
        assert "application/atom+xml" in resp.content_type


class TestOpdsToRead:

    @patch("routes.opds.get_to_read_items", return_value=[])
    def test_empty_to_read(self, mock_items, client):
        resp = client.get("/opds/to-read")
        assert resp.status_code == 200
        assert "application/atom+xml" in resp.content_type
