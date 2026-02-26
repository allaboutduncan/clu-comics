"""Tests for cbz_ops/rebuild.py -- rebuild CBZ and convert RAR."""
import os
import zipfile
import pytest
from unittest.mock import patch, MagicMock


class TestGetFileSizeMb:

    def test_returns_size(self, tmp_path):
        from cbz_ops.rebuild import get_file_size_mb

        f = tmp_path / "test.cbz"
        f.write_bytes(b"x" * (3 * 1024 * 1024))  # 3 MB
        result = get_file_size_mb(str(f))
        assert abs(result - 3.0) < 0.01

    def test_missing_file(self):
        from cbz_ops.rebuild import get_file_size_mb
        assert get_file_size_mb("/nonexistent/file") == 0


class TestCountRebuildableFiles:

    def test_counts_all_comic_types(self, tmp_path):
        from cbz_ops.rebuild import count_rebuildable_files

        (tmp_path / "comic1.cbr").write_bytes(b"fake")
        (tmp_path / "comic2.rar").write_bytes(b"fake")
        (tmp_path / "comic3.cbz").write_bytes(b"fake")
        (tmp_path / "readme.txt").write_bytes(b"fake")

        assert count_rebuildable_files(str(tmp_path)) == 3

    def test_skips_hidden(self, tmp_path):
        from cbz_ops.rebuild import count_rebuildable_files

        (tmp_path / ".hidden.cbz").write_bytes(b"fake")
        (tmp_path / "visible.cbz").write_bytes(b"fake")

        assert count_rebuildable_files(str(tmp_path)) == 1

    def test_empty_dir(self, tmp_path):
        from cbz_ops.rebuild import count_rebuildable_files
        assert count_rebuildable_files(str(tmp_path)) == 0


class TestConvertSingleRarFile:

    @patch("cbz_ops.rebuild.extract_rar_with_unar")
    def test_successful_conversion(self, mock_extract, tmp_path):
        from cbz_ops.rebuild import convert_single_rar_file

        rar_path = str(tmp_path / "comic.rar")
        cbz_path = str(tmp_path / "comic.cbz")
        temp_dir = str(tmp_path / "temp_comic")

        def fake_extract(rar, dest):
            os.makedirs(dest, exist_ok=True)
            for i in range(2):
                with open(os.path.join(dest, f"page{i}.jpg"), "w") as f:
                    f.write("fake image")
            return True

        mock_extract.side_effect = fake_extract

        result = convert_single_rar_file(rar_path, cbz_path, temp_dir)
        assert result is True
        assert os.path.exists(cbz_path)

    @patch("cbz_ops.rebuild.extract_rar_with_unar", return_value=False)
    def test_extraction_failure(self, mock_extract, tmp_path):
        from cbz_ops.rebuild import convert_single_rar_file

        result = convert_single_rar_file(
            str(tmp_path / "bad.rar"),
            str(tmp_path / "bad.cbz"),
            str(tmp_path / "temp"),
        )
        assert result is False


class TestRebuildSingleCbzFile:

    @patch("database.get_db_connection", return_value=None)
    def test_rebuilds_cbz(self, mock_db, create_cbz):
        from cbz_ops.rebuild import rebuild_single_cbz_file

        cbz_path = create_cbz("rebuild_test.cbz", num_images=3)
        directory = os.path.dirname(cbz_path)
        result = rebuild_single_cbz_file(cbz_path, directory)

        assert result is True
        assert os.path.exists(cbz_path)
        assert zipfile.is_zipfile(cbz_path)

        # Verify contents preserved
        with zipfile.ZipFile(cbz_path, "r") as zf:
            assert len(zf.namelist()) == 3

    @patch("database.get_db_connection", return_value=None)
    def test_cleanup_after_rebuild(self, mock_db, create_cbz):
        from cbz_ops.rebuild import rebuild_single_cbz_file

        cbz_path = create_cbz("cleanup_test.cbz", num_images=2)
        directory = os.path.dirname(cbz_path)
        base = os.path.join(directory, os.path.splitext(os.path.basename(cbz_path))[0])

        rebuild_single_cbz_file(cbz_path, directory)

        # Temp files should be cleaned up
        assert not os.path.exists(base)
        assert not os.path.exists(base + ".bak")


class TestRebuildTask:

    @patch("cbz_ops.rebuild.rebuild_single_cbz_file", return_value=True)
    @patch("cbz_ops.rebuild.convert_single_rar_file", return_value=True)
    def test_processes_mixed_files(self, mock_convert, mock_rebuild, tmp_path):
        from cbz_ops.rebuild import rebuild_task

        (tmp_path / "issue1.cbz").write_bytes(b"PK\x03\x04fake")
        (tmp_path / "issue2.cbr").write_bytes(b"Rar!")

        rebuild_task(str(tmp_path))

        mock_rebuild.assert_called_once()
        mock_convert.assert_called_once()

    def test_empty_directory(self, tmp_path):
        from cbz_ops.rebuild import rebuild_task
        # Should not crash
        rebuild_task(str(tmp_path))
