"""Tests for cbz_ops/crop.py -- crop cover image from CBZ."""
import os
import zipfile
import pytest
from unittest.mock import patch, MagicMock
from PIL import Image


class TestProcessImage:

    def test_crops_right_half(self, tmp_path):
        from cbz_ops.crop import process_image

        # Create a 200x100 test image
        img = Image.new("RGB", (200, 100), "red")
        img.save(str(tmp_path / "cover.jpg"))

        process_image(str(tmp_path))

        # Original should be deleted
        assert not os.path.exists(str(tmp_path / "cover.jpg"))
        # Backup (b suffix) and cropped (a suffix) should exist
        assert os.path.exists(str(tmp_path / "covera.jpg"))
        assert os.path.exists(str(tmp_path / "coverb.jpg"))

        # Cropped image should be right half (100x100)
        with Image.open(str(tmp_path / "covera.jpg")) as cropped:
            assert cropped.width == 100
            assert cropped.height == 100

    def test_no_files(self, tmp_path):
        from cbz_ops.crop import process_image
        # Should not crash on empty directory
        process_image(str(tmp_path))

    def test_nonexistent_dir(self):
        from cbz_ops.crop import process_image
        process_image("/nonexistent/path")

    def test_skips_comicinfo(self, tmp_path):
        from cbz_ops.crop import process_image

        # ComicInfo.xml should be skipped
        (tmp_path / "ComicInfo.xml").write_text("<ComicInfo/>")
        img = Image.new("RGB", (200, 100), "blue")
        img.save(str(tmp_path / "page01.jpg"))

        process_image(str(tmp_path))

        # ComicInfo.xml should remain untouched
        assert os.path.exists(str(tmp_path / "ComicInfo.xml"))


class TestHandleCbzFile:

    def test_rejects_non_cbz(self, tmp_path):
        from cbz_ops.crop import handle_cbz_file

        txt = tmp_path / "test.txt"
        txt.write_text("not a cbz")
        handle_cbz_file(str(txt))

    @patch("database.get_db_connection", return_value=None)
    def test_processes_cbz(self, mock_db, create_cbz):
        from cbz_ops.crop import handle_cbz_file

        cbz_path = create_cbz("test.cbz", num_images=2)
        handle_cbz_file(cbz_path)

        # File should still exist
        assert os.path.exists(cbz_path)
        assert zipfile.is_zipfile(cbz_path)

    @patch("database.get_db_connection", return_value=None)
    def test_cleanup(self, mock_db, create_cbz):
        from cbz_ops.crop import handle_cbz_file

        cbz_path = create_cbz("cleanup.cbz", num_images=1)
        base = os.path.splitext(cbz_path)[0]

        handle_cbz_file(cbz_path)

        assert not os.path.exists(base + "_folder")
        assert not os.path.exists(base + ".zip.bak")
