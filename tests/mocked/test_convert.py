"""Tests for cbz_ops/convert.py -- mocked subprocess/unar."""
import os
import zipfile
import pytest
from unittest.mock import patch, MagicMock


class TestGetFileSizeMb:

    def test_returns_size(self, tmp_path):
        from cbz_ops.convert import get_file_size_mb

        f = tmp_path / "test.cbz"
        f.write_bytes(b"x" * (2 * 1024 * 1024))  # 2 MB
        result = get_file_size_mb(str(f))
        assert abs(result - 2.0) < 0.01

    def test_missing_file(self):
        from cbz_ops.convert import get_file_size_mb
        assert get_file_size_mb("/nonexistent/file.cbz") == 0


class TestCountConvertableFiles:

    @patch("cbz_ops.convert.convertSubdirectories", False)
    def test_counts_rar_cbr(self, tmp_path):
        from cbz_ops.convert import count_convertable_files

        (tmp_path / "comic1.cbr").write_bytes(b"fake")
        (tmp_path / "comic2.rar").write_bytes(b"fake")
        (tmp_path / "comic3.cbz").write_bytes(b"fake")  # Not counted
        (tmp_path / "readme.txt").write_bytes(b"fake")   # Not counted

        assert count_convertable_files(str(tmp_path)) == 2

    @patch("cbz_ops.convert.convertSubdirectories", False)
    def test_empty_dir(self, tmp_path):
        from cbz_ops.convert import count_convertable_files
        assert count_convertable_files(str(tmp_path)) == 0

    @patch("cbz_ops.convert.convertSubdirectories", True)
    def test_recursive_count(self, tmp_path):
        from cbz_ops.convert import count_convertable_files

        sub = tmp_path / "subdir"
        sub.mkdir()
        (tmp_path / "a.cbr").write_bytes(b"fake")
        (sub / "b.rar").write_bytes(b"fake")

        assert count_convertable_files(str(tmp_path)) == 2


class TestConvertSingleRarFile:

    @patch("cbz_ops.convert.extract_rar_with_unar")
    def test_successful_conversion(self, mock_extract, tmp_path):
        from cbz_ops.convert import convert_single_rar_file

        rar_path = str(tmp_path / "comic.rar")
        cbz_path = str(tmp_path / "comic.cbz")
        temp_dir = str(tmp_path / "temp_comic")

        # Simulate extraction: create some files in temp_dir
        def fake_extract(rar, dest):
            os.makedirs(dest, exist_ok=True)
            for i in range(3):
                with open(os.path.join(dest, f"page{i}.jpg"), "w") as f:
                    f.write("fake image data")
            return True, 0

        mock_extract.side_effect = fake_extract

        result = convert_single_rar_file(rar_path, cbz_path, temp_dir)
        assert result is True
        assert os.path.exists(cbz_path)

        # Verify CBZ is a valid zip with 3 files
        with zipfile.ZipFile(cbz_path, "r") as zf:
            assert len(zf.namelist()) == 3

    @patch("cbz_ops.convert.extract_rar_with_unar", return_value=(False, 0))
    def test_extraction_failure(self, mock_extract, tmp_path):
        from cbz_ops.convert import convert_single_rar_file

        result = convert_single_rar_file(
            str(tmp_path / "bad.rar"),
            str(tmp_path / "bad.cbz"),
            str(tmp_path / "temp_bad"),
        )
        assert result is False


class TestConvertRarDirectory:

    @patch("cbz_ops.convert.convertSubdirectories", False)
    @patch("cbz_ops.convert.convert_single_rar_file")
    def test_converts_files_in_dir(self, mock_convert, tmp_path):
        from cbz_ops.convert import convert_rar_directory

        # Create fake CBR file
        cbr = tmp_path / "issue1.cbr"
        cbr.write_bytes(b"fake rar data")

        mock_convert.return_value = True

        result = convert_rar_directory(str(tmp_path))
        assert len(result) == 1
        assert result[0] == "issue1"
        mock_convert.assert_called_once()

    @patch("cbz_ops.convert.convertSubdirectories", False)
    def test_empty_directory(self, tmp_path):
        from cbz_ops.convert import convert_rar_directory

        result = convert_rar_directory(str(tmp_path))
        assert result == []


class TestConvertDirectoryScratchDirs:
    """cbz_ops/convert.py must use the same hidden scratch-dir name."""

    def test_scratch_dir_is_hidden(self, tmp_path):
        from unittest.mock import patch
        from helpers import is_hidden
        from cbz_ops.convert import convert_rar_directory

        (tmp_path / "Batman 001.cbr").write_bytes(b"fake rar")
        seen = {}

        def fake_convert(rar_path, zip_path, temp_extraction_dir):
            seen["dir"] = temp_extraction_dir
            with open(zip_path, "wb") as f:
                f.write(b"fake cbz")
            return True

        with patch("cbz_ops.convert.convert_single_rar_file", side_effect=fake_convert):
            convert_rar_directory(str(tmp_path))

        assert seen.get("dir") is not None, "convert_single_rar_file was never called"
        assert os.path.basename(seen["dir"]).startswith(".")
        assert is_hidden(seen["dir"]) is True


class TestConversionWorksFromAHiddenExtractionRoot:
    """The zip-building walk must still see pages under a dot-prefixed root.

    Both walks in convert_single_rar_file prune hidden *sub*directories. The
    extraction root is now dot-prefixed itself, and os.walk never applies that
    filter to its own root -- but if that ever changed, every conversion would
    silently produce an empty CBZ. This pins it down with the real walk/zip code
    (only the unar call is faked).
    """

    def test_pages_under_a_hidden_root_are_zipped(self, tmp_path):
        from unittest.mock import patch
        from cbz_ops.convert import convert_single_rar_file

        rar = tmp_path / "Batman 001.cbr"
        rar.write_bytes(b"fake rar")
        cbz = tmp_path / "Batman 001.cbz"
        scratch = tmp_path / ".temp_Batman 001"

        def fake_extract(rar_path, output_dir):
            os.makedirs(output_dir, exist_ok=True)
            for n in (1, 2, 3):
                with open(os.path.join(output_dir, f"page{n:03}.jpg"), "wb") as f:
                    f.write(b"jpeg-bytes")
            return True, 0

        with patch("cbz_ops.convert.extract_rar_with_unar", side_effect=fake_extract):
            ok = convert_single_rar_file(str(rar), str(cbz), str(scratch))

        assert ok is True
        assert cbz.exists(), "conversion must produce a CBZ"
        with zipfile.ZipFile(str(cbz)) as zf:
            names = sorted(zf.namelist())
        assert names == ["page001.jpg", "page002.jpg", "page003.jpg"], \
            f"all pages must be archived, got {names}"
