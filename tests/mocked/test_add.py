"""Tests for cbz_ops/add.py -- add blank image to CBZ."""
import os
import zipfile
import pytest
from unittest.mock import patch


class TestAddImageToFolder:

    @patch("cbz_ops.add.shutil.copy")
    def test_copies_image(self, mock_copy, tmp_path):
        from cbz_ops.add import add_image_to_folder

        with patch("os.path.exists", return_value=True):
            add_image_to_folder(str(tmp_path))

        mock_copy.assert_called_once()
        dest = mock_copy.call_args[0][1]
        assert dest.endswith("zzzz9999.png")

    def test_source_not_found(self, tmp_path):
        from cbz_ops.add import add_image_to_folder

        # Source image doesn't exist, function should log error and return
        add_image_to_folder(str(tmp_path))
        # No crash expected


class TestHandleCbzFile:

    def test_rejects_non_cbz(self, tmp_path):
        from cbz_ops.add import handle_cbz_file

        txt_file = tmp_path / "test.txt"
        txt_file.write_text("not a cbz")
        handle_cbz_file(str(txt_file))
        # Should return early, no crash

    @patch("cbz_ops.add.add_image_to_folder")
    def test_processes_cbz(self, mock_add_image, create_cbz):
        from cbz_ops.add import handle_cbz_file

        cbz_path = create_cbz("test.cbz", num_images=2)

        # Verify initial state
        with zipfile.ZipFile(cbz_path, "r") as zf:
            original_count = len(zf.namelist())

        handle_cbz_file(cbz_path)

        # add_image_to_folder should have been called
        mock_add_image.assert_called_once()
        # File should still exist as CBZ
        assert os.path.exists(cbz_path)

    @patch("cbz_ops.add.add_image_to_folder")
    def test_cleanup_on_success(self, mock_add_image, create_cbz):
        from cbz_ops.add import handle_cbz_file

        cbz_path = create_cbz("cleanup_test.cbz", num_images=1)
        base = os.path.splitext(cbz_path)[0]

        handle_cbz_file(cbz_path)

        # Temp folder should be cleaned up
        assert not os.path.exists(base + "_folder")
        # Backup should be cleaned up
        assert not os.path.exists(base + ".zip.bak")
