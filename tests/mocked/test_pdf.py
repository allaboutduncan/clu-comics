"""Tests for cbz_ops/pdf.py -- mocked pdf2image."""
import os
import zipfile
import pytest
from unittest.mock import patch, MagicMock
from PIL import Image


class TestProcessSinglePage:

    def test_saves_page(self, tmp_path):
        from cbz_ops.pdf import process_single_page

        page = Image.new("RGB", (800, 1200), "white")
        output_folder = str(tmp_path)

        process_single_page(page, 1, "TestComic", output_folder)

        saved = os.path.join(output_folder, "TestComic page_1.jpg")
        assert os.path.exists(saved)

    def test_resizes_large_page(self, tmp_path):
        from cbz_ops.pdf import process_single_page

        # Create an oversized image (wider than 50MP limit)
        page = Image.new("RGB", (10000, 10000), "white")  # 100MP
        output_folder = str(tmp_path)

        process_single_page(page, 1, "LargeComic", output_folder)

        saved = os.path.join(output_folder, "LargeComic page_1.jpg")
        assert os.path.exists(saved)

        # Verify it was resized
        with Image.open(saved) as img:
            assert img.width * img.height <= 50_000_000 + 1000  # Allow small rounding

    def test_multiple_pages(self, tmp_path):
        from cbz_ops.pdf import process_single_page

        output_folder = str(tmp_path)
        for i in range(1, 4):
            page = Image.new("RGB", (600, 900), "white")
            process_single_page(page, i, "MultiPage", output_folder)

        files = os.listdir(output_folder)
        assert len(files) == 3


class TestCreateCbzFile:

    def test_creates_valid_cbz(self, tmp_path):
        from cbz_ops.pdf import create_cbz_file

        source_dir = str(tmp_path / "pages")
        os.makedirs(source_dir)

        for i in range(3):
            img = Image.new("RGB", (100, 150), "blue")
            img.save(os.path.join(source_dir, f"page_{i+1}.jpg"))

        cbz_path = str(tmp_path / "output.cbz")
        create_cbz_file(source_dir, cbz_path)

        assert os.path.exists(cbz_path)
        with zipfile.ZipFile(cbz_path, "r") as zf:
            assert len(zf.namelist()) == 3

    def test_empty_folder(self, tmp_path):
        from cbz_ops.pdf import create_cbz_file

        source_dir = str(tmp_path / "empty")
        os.makedirs(source_dir)
        cbz_path = str(tmp_path / "empty.cbz")

        create_cbz_file(source_dir, cbz_path)
        assert os.path.exists(cbz_path)


class TestCleanupTempFolder:

    def test_removes_folder(self, tmp_path):
        from cbz_ops.pdf import cleanup_temp_folder

        target = tmp_path / "cleanup_target"
        target.mkdir()
        (target / "file.txt").write_text("data")

        cleanup_temp_folder(str(target))
        assert not target.exists()

    def test_nonexistent_folder(self, tmp_path):
        from cbz_ops.pdf import cleanup_temp_folder
        # Should not crash
        cleanup_temp_folder(str(tmp_path / "nonexistent"))


class TestProcessPdfFile:

    @patch("cbz_ops.pdf.cleanup_temp_folder")
    @patch("cbz_ops.pdf.create_cbz_file")
    @patch("cbz_ops.pdf.convert_from_path")
    @patch("cbz_ops.pdf.pdfinfo_from_path", return_value={"Pages": 3})
    def test_processes_pdf(self, mock_info, mock_convert, mock_create_cbz,
                           mock_cleanup, tmp_path):
        from cbz_ops.pdf import process_pdf_file

        pdf_path = str(tmp_path / "comic.pdf")
        with open(pdf_path, "w") as f:
            f.write("fake pdf")

        # Mock convert_from_path to return mock page images
        mock_pages = []
        for _ in range(2):
            page = MagicMock()
            page.size = (800, 1200)
            page.close = MagicMock()
            mock_pages.append(page)

        mock_convert.return_value = mock_pages

        process_pdf_file(pdf_path)

        mock_info.assert_called_once_with(pdf_path)
        assert mock_convert.call_count >= 1
        mock_create_cbz.assert_called_once()

    @patch("cbz_ops.pdf.pdfinfo_from_path", side_effect=Exception("corrupt PDF"))
    def test_handles_corrupt_pdf(self, mock_info, tmp_path):
        from cbz_ops.pdf import process_pdf_file

        pdf_path = str(tmp_path / "corrupt.pdf")
        with open(pdf_path, "w") as f:
            f.write("not a pdf")

        # Should not crash
        process_pdf_file(pdf_path)


class TestScanAndConvert:

    @patch("cbz_ops.pdf.process_pdf_file")
    def test_finds_pdfs(self, mock_process, tmp_path):
        from cbz_ops.pdf import scan_and_convert

        (tmp_path / "comic1.pdf").write_text("fake")
        (tmp_path / "comic2.pdf").write_text("fake")
        (tmp_path / "readme.txt").write_text("not pdf")

        scan_and_convert(str(tmp_path))
        assert mock_process.call_count == 2

    @patch("cbz_ops.pdf.process_pdf_file")
    def test_skips_hidden(self, mock_process, tmp_path):
        from cbz_ops.pdf import scan_and_convert

        (tmp_path / ".hidden.pdf").write_text("fake")
        (tmp_path / "visible.pdf").write_text("fake")

        scan_and_convert(str(tmp_path))
        assert mock_process.call_count == 1

    @patch("cbz_ops.pdf.process_pdf_file")
    def test_recursive_scan(self, mock_process, tmp_path):
        from cbz_ops.pdf import scan_and_convert

        sub = tmp_path / "subdir"
        sub.mkdir()
        (tmp_path / "root.pdf").write_text("fake")
        (sub / "nested.pdf").write_text("fake")

        scan_and_convert(str(tmp_path))
        assert mock_process.call_count == 2

    @patch("cbz_ops.pdf.process_pdf_file")
    def test_empty_directory(self, mock_process, tmp_path):
        from cbz_ops.pdf import scan_and_convert

        scan_and_convert(str(tmp_path))
        mock_process.assert_not_called()
