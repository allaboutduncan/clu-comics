"""Tests for cbz_ops/enhance_single.py and enhance_dir.py."""
import os
import zipfile
import pytest
from unittest.mock import patch, MagicMock
from PIL import Image


class TestEnhanceComic:

    def test_skips_hidden_files(self, tmp_path):
        from cbz_ops.enhance_single import enhance_comic

        hidden = tmp_path / ".hidden.cbz"
        hidden.write_bytes(b"fake")
        # Should skip without error
        enhance_comic(str(hidden))

    @patch("cbz_ops.enhance_single.enhance_cbz_file")
    def test_dispatches_cbz(self, mock_enhance_cbz, tmp_path):
        from cbz_ops.enhance_single import enhance_comic

        cbz = tmp_path / "test.cbz"
        cbz.write_bytes(b"fake")
        enhance_comic(str(cbz))
        mock_enhance_cbz.assert_called_once_with(str(cbz))

    @patch("cbz_ops.enhance_single.enhance_single_image")
    def test_dispatches_single_image(self, mock_enhance_img, tmp_path):
        from cbz_ops.enhance_single import enhance_comic

        img = tmp_path / "page.jpg"
        img.write_bytes(b"fake")
        enhance_comic(str(img))
        mock_enhance_img.assert_called_once_with(str(img))


class TestEnhanceSingleImage:

    @patch("cbz_ops.enhance_single.enhance_image")
    def test_enhances_small_image(self, mock_enhance, tmp_path):
        from cbz_ops.enhance_single import enhance_single_image

        img_path = tmp_path / "page.jpg"
        # Create a small image
        img = Image.new("RGB", (100, 100), "red")
        img.save(str(img_path))

        # Mock enhance_image to return an enhanced image
        mock_result = Image.new("RGB", (100, 100), "blue")
        mock_enhance.return_value = mock_result

        enhance_single_image(str(img_path))
        mock_enhance.assert_called_once()

    @patch("cbz_ops.enhance_single.enhance_image", return_value=None)
    def test_handles_enhancement_failure(self, mock_enhance, tmp_path):
        from cbz_ops.enhance_single import enhance_single_image

        img_path = tmp_path / "bad.jpg"
        img_path.write_bytes(b"fake")

        # Should not crash
        enhance_single_image(str(img_path))

    @patch("cbz_ops.enhance_single.enhance_image_streaming", return_value=True)
    @patch("os.path.getsize", return_value=200 * 1024 * 1024)  # 200MB
    def test_streaming_for_large_files(self, mock_size, mock_streaming, tmp_path):
        from cbz_ops.enhance_single import enhance_single_image

        img_path = tmp_path / "large.jpg"
        img_path.write_bytes(b"fake")

        enhance_single_image(str(img_path))
        mock_streaming.assert_called_once()


class TestEnhanceCbzFile:

    @patch("cbz_ops.enhance_single.get_db_connection", create=True, return_value=None)
    @patch("cbz_ops.enhance_single.enhance_image")
    @patch("cbz_ops.enhance_single.unzip_file")
    def test_enhances_cbz(self, mock_unzip, mock_enhance, mock_db, create_cbz, tmp_path):
        from cbz_ops.enhance_single import enhance_cbz_file

        cbz_path = create_cbz("test.cbz", num_images=2)

        # Setup: unzip returns a temp directory with images
        extract_dir = str(tmp_path / "extracted")
        os.makedirs(extract_dir)
        for i in range(2):
            img = Image.new("RGB", (50, 50), "red")
            img.save(os.path.join(extract_dir, f"page{i}.jpg"))

        mock_unzip.return_value = extract_dir
        mock_result = Image.new("RGB", (50, 50), "blue")
        mock_enhance.return_value = mock_result

        enhance_cbz_file(cbz_path)

        mock_unzip.assert_called_once()
        assert mock_enhance.call_count == 2


class TestCreateEnhancedCbz:

    @patch("cbz_ops.enhance_single.get_db_connection", create=True, return_value=None)
    def test_creates_valid_cbz(self, mock_db, tmp_path):
        from cbz_ops.enhance_single import create_enhanced_cbz

        source_dir = str(tmp_path / "source")
        os.makedirs(source_dir)
        for i in range(3):
            img = Image.new("RGB", (50, 50), "green")
            img.save(os.path.join(source_dir, f"page{i:03d}.jpg"))

        cbz_path = str(tmp_path / "output.cbz")
        create_enhanced_cbz(source_dir, cbz_path)

        assert os.path.exists(cbz_path)
        with zipfile.ZipFile(cbz_path, "r") as zf:
            assert len(zf.namelist()) == 3


class TestCleanupExtractedDir:

    def test_removes_directory(self, tmp_path):
        from cbz_ops.enhance_single import cleanup_extracted_dir

        target = tmp_path / "to_clean"
        target.mkdir()
        (target / "file.txt").write_text("data")

        cleanup_extracted_dir(str(target))
        assert not target.exists()

    def test_nonexistent_dir(self, tmp_path):
        from cbz_ops.enhance_single import cleanup_extracted_dir
        # Should not crash
        cleanup_extracted_dir(str(tmp_path / "nonexistent"))


class TestEnhanceDirectory:

    @patch("cbz_ops.enhance_dir.enhance_comic")
    def test_processes_files(self, mock_enhance, tmp_path):
        from cbz_ops.enhance_dir import enhance_directory

        # Create test files
        (tmp_path / "comic1.cbz").write_bytes(b"fake1")
        (tmp_path / "comic2.cbz").write_bytes(b"fake2")
        (tmp_path / ".hidden.cbz").write_bytes(b"hidden")

        enhance_directory(str(tmp_path))

        # Should process 2 visible files, skip hidden
        assert mock_enhance.call_count == 2

    @patch("cbz_ops.enhance_dir.enhance_comic")
    def test_skips_directories(self, mock_enhance, tmp_path):
        from cbz_ops.enhance_dir import enhance_directory

        (tmp_path / "subdir").mkdir()
        (tmp_path / "file.cbz").write_bytes(b"fake")

        enhance_directory(str(tmp_path))
        assert mock_enhance.call_count == 1

    @patch("cbz_ops.enhance_dir.enhance_comic")
    def test_empty_directory(self, mock_enhance, tmp_path):
        from cbz_ops.enhance_dir import enhance_directory

        enhance_directory(str(tmp_path))
        mock_enhance.assert_not_called()
