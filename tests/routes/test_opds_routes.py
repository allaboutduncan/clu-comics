"""Tests for opds.py -- OPDS feed endpoints."""
import os
import pytest
from unittest.mock import patch, MagicMock


class TestOpdsFeedId:

    def test_generate_feed_id(self):
        from opds import generate_feed_id
        result = generate_feed_id("/opds")
        assert result.startswith("urn:uuid:")
        assert len(result) == len("urn:uuid:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")

    def test_deterministic(self):
        from opds import generate_feed_id
        a = generate_feed_id("/some/path")
        b = generate_feed_id("/some/path")
        assert a == b

    def test_different_paths(self):
        from opds import generate_feed_id
        a = generate_feed_id("/a")
        b = generate_feed_id("/b")
        assert a != b


class TestGetTimestamp:

    def test_format(self):
        from opds import get_timestamp
        ts = get_timestamp()
        assert ts.endswith("Z")
        assert "T" in ts


class TestIsValidLibraryPath:

    @patch("opds.get_library_roots", return_value=["/data"])
    def test_valid_path(self, mock_roots):
        from opds import is_valid_library_path
        assert is_valid_library_path("/data/Comics/Batman") is True

    @patch("opds.get_library_roots", return_value=["/data"])
    def test_exact_root(self, mock_roots):
        from opds import is_valid_library_path
        assert is_valid_library_path("/data") is True

    @patch("opds.get_library_roots", return_value=["/data"])
    def test_invalid_path(self, mock_roots):
        from opds import is_valid_library_path
        assert is_valid_library_path("/etc/passwd") is False

    @patch("opds.get_library_roots", return_value=["/data"])
    def test_empty_path(self, mock_roots):
        from opds import is_valid_library_path
        assert is_valid_library_path("") is False

    @patch("opds.get_library_roots", return_value=["/data"])
    def test_none_path(self, mock_roots):
        from opds import is_valid_library_path
        assert is_valid_library_path(None) is False


class TestCheckFolderThumbnail:

    def test_no_thumbnail(self, tmp_path):
        from opds import check_folder_thumbnail
        assert check_folder_thumbnail(str(tmp_path)) is None

    def test_png_thumbnail(self, tmp_path):
        from opds import check_folder_thumbnail
        from PIL import Image
        thumb = tmp_path / "folder.png"
        Image.new("RGB", (10, 10), "red").save(str(thumb))
        result = check_folder_thumbnail(str(tmp_path))
        assert result == str(thumb)

    def test_jpg_thumbnail(self, tmp_path):
        from opds import check_folder_thumbnail
        from PIL import Image
        thumb = tmp_path / "folder.jpg"
        Image.new("RGB", (10, 10), "red").save(str(thumb))
        result = check_folder_thumbnail(str(tmp_path))
        assert result == str(thumb)


class TestGetDirectoryListingForOpds:

    def test_empty_directory(self, tmp_path):
        from opds import get_directory_listing_for_opds
        dirs, files = get_directory_listing_for_opds(str(tmp_path))
        assert dirs == []
        assert files == []

    def test_directories_and_files(self, tmp_path):
        from opds import get_directory_listing_for_opds
        (tmp_path / "Batman").mkdir()
        (tmp_path / "comic.cbz").write_bytes(b"PK\x03\x04")
        (tmp_path / "readme.txt").write_text("hello")

        dirs, files = get_directory_listing_for_opds(str(tmp_path))
        assert len(dirs) == 1
        assert dirs[0]["name"] == "Batman"
        assert len(files) == 1
        assert files[0]["name"] == "comic.cbz"

    def test_skips_hidden(self, tmp_path):
        from opds import get_directory_listing_for_opds
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "_macosx").mkdir()
        (tmp_path / "visible").mkdir()

        dirs, files = get_directory_listing_for_opds(str(tmp_path))
        assert len(dirs) == 1
        assert dirs[0]["name"] == "visible"

    def test_comic_extensions(self, tmp_path):
        from opds import get_directory_listing_for_opds
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
        from opds import get_directory_listing_for_opds
        dirs, files = get_directory_listing_for_opds("/nonexistent")
        assert dirs == []
        assert files == []


class TestOpdsMimeTypes:

    def test_comic_mime_types(self):
        from opds import COMIC_MIME_TYPES
        assert COMIC_MIME_TYPES[".cbz"] == "application/vnd.comicbook+zip"
        assert COMIC_MIME_TYPES[".cbr"] == "application/vnd.comicbook-rar"
        assert COMIC_MIME_TYPES[".pdf"] == "application/pdf"


class TestOpdsRoot:

    @patch("opds.get_libraries", return_value=[{"path": "/data", "name": "Library"}])
    @patch("opds.get_to_read_items", return_value=[])
    def test_root_feed(self, mock_read, mock_libs, client):
        resp = client.get("/opds/")
        assert resp.status_code == 200
        assert "application/atom+xml" in resp.content_type


class TestOpdsToRead:

    @patch("opds.get_to_read_items", return_value=[])
    def test_empty_to_read(self, mock_items, client):
        resp = client.get("/opds/to-read")
        assert resp.status_code == 200
        assert "application/atom+xml" in resp.content_type
