"""Tests for cbz_ops/remove.py -- remove first image from CBZ."""
import os
import zipfile
import pytest
from unittest.mock import patch, MagicMock
from PIL import Image


class TestNaturalSortKey:

    def test_numeric_sorting(self):
        from cbz_ops.remove import natural_sort_key

        files = ["page10.jpg", "page2.jpg", "page1.jpg"]
        sorted_files = sorted(files, key=natural_sort_key)
        assert sorted_files == ["page1.jpg", "page2.jpg", "page10.jpg"]

    def test_special_chars_first(self):
        from cbz_ops.remove import natural_sort_key

        files = ["page1.jpg", "_cover.jpg"]
        sorted_files = sorted(files, key=natural_sort_key)
        assert sorted_files[0] == "_cover.jpg"  # Special char sorts first

    def test_case_insensitive(self):
        from cbz_ops.remove import natural_sort_key

        files = ["Page1.jpg", "page2.jpg"]
        sorted_files = sorted(files, key=natural_sort_key)
        assert sorted_files[0] == "Page1.jpg"


class TestRemoveFirstImageFile:

    def test_removes_first_image(self, tmp_path):
        from cbz_ops.remove import remove_first_image_file

        # Create test images
        for name in ["page01.jpg", "page02.jpg", "page03.jpg"]:
            img = Image.new("RGB", (10, 10), "white")
            img.save(str(tmp_path / name))

        remove_first_image_file(str(tmp_path))

        remaining = sorted(os.listdir(tmp_path))
        assert "page01.jpg" not in remaining
        assert len(remaining) == 2

    def test_no_images(self, tmp_path):
        from cbz_ops.remove import remove_first_image_file

        (tmp_path / "readme.txt").write_text("not an image")
        # Should not crash
        remove_first_image_file(str(tmp_path))

    def test_nonexistent_dir(self):
        from cbz_ops.remove import remove_first_image_file
        # Should not crash
        remove_first_image_file("/nonexistent/directory")

    def test_special_char_removed_first(self, tmp_path):
        from cbz_ops.remove import remove_first_image_file

        # Create images where special char file should sort first
        for name in ["page01.jpg", "_cover.jpg"]:
            img = Image.new("RGB", (10, 10), "white")
            img.save(str(tmp_path / name))

        remove_first_image_file(str(tmp_path))

        remaining = os.listdir(tmp_path)
        assert "_cover.jpg" not in remaining
        assert "page01.jpg" in remaining


class TestHandleCbzFile:

    def test_rejects_non_cbz(self, tmp_path):
        from cbz_ops.remove import handle_cbz_file

        txt = tmp_path / "test.txt"
        txt.write_text("not a cbz")
        handle_cbz_file(str(txt))
        # Should return early

    @patch("database.get_db_connection", return_value=None)
    def test_removes_first_image_from_cbz(self, mock_db, create_cbz):
        from cbz_ops.remove import handle_cbz_file

        cbz_path = create_cbz("test.cbz", num_images=3)

        with zipfile.ZipFile(cbz_path, "r") as zf:
            original_count = len([n for n in zf.namelist()
                                  if n.lower().endswith(('.jpg', '.png'))])

        handle_cbz_file(cbz_path)

        assert os.path.exists(cbz_path)
        with zipfile.ZipFile(cbz_path, "r") as zf:
            new_count = len([n for n in zf.namelist()
                             if n.lower().endswith(('.jpg', '.png'))])
        assert new_count == original_count - 1

    @patch("database.get_db_connection", return_value=None)
    def test_cleanup_temp_files(self, mock_db, create_cbz):
        from cbz_ops.remove import handle_cbz_file

        cbz_path = create_cbz("cleanup.cbz", num_images=2)
        base = os.path.splitext(cbz_path)[0]

        handle_cbz_file(cbz_path)

        assert not os.path.exists(base + "_folder")
        assert not os.path.exists(base + ".zip.bak")
