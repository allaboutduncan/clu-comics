"""Tests for cbz_ops/single_file.py -- single file conversion/rebuild."""
import os
import zipfile
import pytest
from unittest.mock import patch, MagicMock


class TestGetFileSizeMb:

    def test_returns_size(self, tmp_path):
        from cbz_ops.single_file import get_file_size_mb

        f = tmp_path / "test.cbz"
        f.write_bytes(b"x" * (1024 * 1024))  # 1 MB
        result = get_file_size_mb(str(f))
        assert abs(result - 1.0) < 0.01

    def test_missing_file(self):
        from cbz_ops.single_file import get_file_size_mb
        assert get_file_size_mb("/nonexistent/file") == 0


class TestConvertSingleRarFile:

    @patch("cbz_ops.single_file.get_db_connection", create=True, return_value=None)
    @patch("cbz_ops.single_file.extract_rar_with_unar")
    def test_successful_conversion(self, mock_extract, mock_db, tmp_path):
        from cbz_ops.single_file import convert_single_rar_file

        rar_path = str(tmp_path / "comic.rar")
        cbz_path = str(tmp_path / "comic.cbz")
        temp_dir = str(tmp_path / "temp_comic")

        def fake_extract(rar, dest):
            os.makedirs(dest, exist_ok=True)
            for i in range(3):
                with open(os.path.join(dest, f"page{i}.jpg"), "w") as f:
                    f.write("fake image data")
            return True

        mock_extract.side_effect = fake_extract

        result = convert_single_rar_file(rar_path, cbz_path, temp_dir)
        assert result is True
        assert os.path.exists(cbz_path)

    @patch("cbz_ops.single_file.extract_rar_with_unar", return_value=False)
    def test_extraction_failure(self, mock_extract, tmp_path):
        from cbz_ops.single_file import convert_single_rar_file

        result = convert_single_rar_file(
            str(tmp_path / "bad.rar"),
            str(tmp_path / "bad.cbz"),
            str(tmp_path / "temp"),
        )
        assert result is False


class TestRebuildSingleCbzFile:

    @patch("cbz_ops.single_file.get_db_connection", create=True, return_value=None)
    def test_rebuilds_cbz(self, mock_db, create_cbz):
        from cbz_ops.single_file import rebuild_single_cbz_file

        cbz_path = create_cbz("rebuild.cbz", num_images=3)
        result = rebuild_single_cbz_file(cbz_path)

        assert result is True
        assert os.path.exists(cbz_path)
        assert zipfile.is_zipfile(cbz_path)

    @patch("cbz_ops.single_file.get_db_connection", create=True, return_value=None)
    def test_cleanup_after_rebuild(self, mock_db, create_cbz):
        from cbz_ops.single_file import rebuild_single_cbz_file

        cbz_path = create_cbz("cleanup.cbz", num_images=2)
        base = os.path.splitext(cbz_path)[0]

        rebuild_single_cbz_file(cbz_path)

        assert not os.path.exists(base + "_folder")
        assert not os.path.exists(base + ".zip.bak")


class TestHandleCbzFile:

    def test_rejects_non_cbz(self, tmp_path):
        from cbz_ops.single_file import handle_cbz_file

        txt = tmp_path / "test.txt"
        txt.write_text("not a cbz")
        handle_cbz_file(str(txt))

    @patch("cbz_ops.single_file.rebuild_single_cbz_file", return_value=True)
    def test_delegates_to_rebuild(self, mock_rebuild, tmp_path):
        from cbz_ops.single_file import handle_cbz_file

        cbz = tmp_path / "test.cbz"
        cbz.write_bytes(b"fake")
        handle_cbz_file(str(cbz))
        mock_rebuild.assert_called_once_with(str(cbz))


class TestConvertToCbz:

    def test_nonexistent_file(self):
        from cbz_ops.single_file import convert_to_cbz
        # Should not crash
        convert_to_cbz("/nonexistent/file.rar")

    def test_unrecognized_extension(self, tmp_path):
        from cbz_ops.single_file import convert_to_cbz

        txt = tmp_path / "test.txt"
        txt.write_text("not a comic")
        convert_to_cbz(str(txt))

    @patch("database.add_file_index_entry")
    @patch("database.delete_file_index_entry")
    @patch("database.invalidate_browse_cache")
    @patch("cbz_ops.single_file.convert_single_rar_file", return_value=True)
    def test_converts_cbr(self, mock_convert, mock_cache, mock_delete, mock_add, tmp_path):
        from cbz_ops.single_file import convert_to_cbz

        cbr = tmp_path / "comic.cbr"
        cbr.write_bytes(b"fake rar")

        # Create the expected CBZ file so os.remove and os.path.getsize work
        cbz = tmp_path / "comic.cbz"
        cbz.write_bytes(b"fake cbz")

        convert_to_cbz(str(cbr))

        mock_convert.assert_called_once()
        mock_cache.assert_called_once()

    @patch("cbz_ops.single_file.handle_cbz_file")
    def test_dispatches_cbz(self, mock_handle, tmp_path):
        from cbz_ops.single_file import convert_to_cbz

        cbz = tmp_path / "comic.cbz"
        cbz.write_bytes(b"fake cbz")

        convert_to_cbz(str(cbz))
        mock_handle.assert_called_once_with(str(cbz))
