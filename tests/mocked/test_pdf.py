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

    def test_saves_page_as_webp(self, tmp_path):
        from cbz_ops.pdf import process_single_page

        page = Image.new("RGB", (800, 1200), "white")
        output_folder = str(tmp_path)

        process_single_page(page, 1, "WebComic", output_folder, image_format="webp")

        saved = os.path.join(output_folder, "WebComic page_1.webp")
        assert os.path.exists(saved)
        # A .jpg must NOT have been written when webp is requested.
        assert not os.path.exists(os.path.join(output_folder, "WebComic page_1.jpg"))
        with Image.open(saved) as img:
            assert img.format == "WEBP"

    def test_invalid_format_falls_back_to_jpeg(self, tmp_path):
        from cbz_ops.pdf import process_single_page

        page = Image.new("RGB", (800, 1200), "white")
        output_folder = str(tmp_path)

        process_single_page(page, 1, "FallbackComic", output_folder, image_format="gif")

        saved = os.path.join(output_folder, "FallbackComic page_1.jpg")
        assert os.path.exists(saved)


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


def _render_all(pdf_path, first_page, last_page, **kwargs):
    """convert_from_path stand-in: one real image per requested page."""
    return [Image.new("RGB", (80, 120), "white")
            for _ in range(first_page, last_page + 1)]


class TestProcessPdfFileOutcome:
    """process_pdf_file reports its own outcome and is all or nothing (#834).

    It returned None and swallowed everything, so its one caller tested
    ``os.path.exists(<base>.cbz)`` instead. A failed CBZ write still deleted
    the PDF, and a page that failed to save was simply missing from the CBZ.
    """

    @pytest.fixture
    def pdf(self, tmp_path):
        path = tmp_path / "Series 001 (2011).pdf"
        path.write_bytes(b"%PDF-1.4 fake")
        return str(path)

    @pytest.fixture(autouse=True)
    def _pdf2image(self):
        with patch("cbz_ops.pdf.get_user_preference", return_value="jpeg"), \
             patch("cbz_ops.pdf.pdfinfo_from_path", return_value={"Pages": 3}), \
             patch("cbz_ops.pdf.convert_from_path", side_effect=_render_all) as conv:
            self.convert = conv
            yield

    @staticmethod
    def _cbz(pdf):
        return os.path.splitext(pdf)[0] + ".cbz"

    def test_success_writes_every_page_and_removes_the_pdf(self, pdf, tmp_path):
        from cbz_ops.pdf import process_pdf_file

        assert process_pdf_file(pdf) is True

        assert not os.path.exists(pdf)
        with zipfile.ZipFile(self._cbz(pdf)) as zf:
            assert len(zf.namelist()) == 3
        assert os.listdir(tmp_path) == ["Series 001 (2011).cbz"], \
            "the scratch folder is removed"

    def test_pages_are_rendered_into_a_hidden_scratch_folder(self, pdf, tmp_path):
        """A bare <name>/ in WATCH is a folder of loose pages to the monitor."""
        from cbz_ops import pdf as pdf_mod

        seen = []

        def spy(page, page_number, pdf_name, output_folder, image_format="jpeg"):
            seen.append(output_folder)
            return True

        with patch("cbz_ops.pdf.process_single_page", side_effect=spy):
            pdf_mod.process_pdf_file(pdf)

        assert set(seen) == {str(tmp_path / ".temp_Series 001 (2011)")}
        assert seen[0] == pdf_mod.scratch_dir_for(pdf)

    def test_failed_cbz_write_keeps_the_pdf(self, pdf, tmp_path):
        """It used to log the error and delete the PDF on the next line."""
        from cbz_ops.pdf import process_pdf_file

        with patch("cbz_ops.pdf.open_zip_for_write", side_effect=OSError("disk full")):
            assert process_pdf_file(pdf) is False

        assert os.listdir(tmp_path) == ["Series 001 (2011).pdf"]

    def test_a_page_that_fails_to_save_fails_the_conversion(self, pdf, tmp_path):
        from cbz_ops import pdf as pdf_mod

        real = pdf_mod.process_single_page

        def second_page_fails(page, page_number, *args, **kwargs):
            if page_number == 2:
                return False
            return real(page, page_number, *args, **kwargs)

        with patch("cbz_ops.pdf.process_single_page", side_effect=second_page_fails):
            assert pdf_mod.process_pdf_file(pdf) is False

        assert os.listdir(tmp_path) == ["Series 001 (2011).pdf"], \
            "no short CBZ, no scratch folder, PDF kept"

    def test_a_page_the_renderer_drops_fails_the_conversion(self, pdf, tmp_path):
        from cbz_ops.pdf import process_pdf_file

        self.convert.side_effect = lambda *a, **k: _render_all(*a, **k)[:1]

        assert process_pdf_file(pdf) is False
        assert os.listdir(tmp_path) == ["Series 001 (2011).pdf"]

    def test_a_pdf_that_cannot_be_removed_leaves_no_cbz_beside_it(self, pdf, tmp_path):
        """A PDF beside its own CBZ would be converted again on the next pass."""
        from cbz_ops import pdf as pdf_mod

        real_remove = os.remove

        def remove(path):
            if path == pdf:
                raise PermissionError("in use")
            real_remove(path)

        with patch.object(pdf_mod.os, "remove", side_effect=remove):
            assert pdf_mod.process_pdf_file(pdf) is False

        assert os.listdir(tmp_path) == ["Series 001 (2011).pdf"]

    def test_an_existing_cbz_of_the_same_name_is_never_overwritten(self, pdf, tmp_path):
        from cbz_ops.pdf import process_pdf_file

        with open(self._cbz(pdf), "wb") as f:
            f.write(b"a different comic")

        assert process_pdf_file(pdf) is False

        self.convert.assert_not_called()
        assert os.path.exists(pdf)
        with open(self._cbz(pdf), "rb") as f:
            assert f.read() == b"a different comic"

    def test_unreadable_pdf_returns_false(self, pdf):
        from cbz_ops.pdf import process_pdf_file

        with patch("cbz_ops.pdf.pdfinfo_from_path", side_effect=Exception("corrupt PDF")):
            assert process_pdf_file(pdf) is False
        assert os.path.exists(pdf)

    def test_single_page_and_cbz_writer_report_their_outcome(self, tmp_path):
        from cbz_ops.pdf import create_cbz_file, process_single_page

        pages = tmp_path / "pages"
        pages.mkdir()
        assert process_single_page(
            Image.new("RGB", (80, 120), "white"), 1, "X", str(pages)) is True
        broken = MagicMock()
        broken.size = (80, 120)
        broken.save.side_effect = OSError("no space")
        assert process_single_page(broken, 2, "X", str(pages)) is False

        assert create_cbz_file(str(pages), str(tmp_path / "X.cbz")) is True
        with patch("cbz_ops.pdf.open_zip_for_write", side_effect=OSError("disk full")):
            assert create_cbz_file(str(pages), str(tmp_path / "Y.cbz")) is False


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

        # Mock convert_from_path to return one mock image per requested page
        mock_convert.side_effect = _render_all

        assert process_pdf_file(pdf_path) is True

        mock_info.assert_called_once_with(pdf_path)
        assert mock_convert.call_count >= 1
        # Render the CropBox, not the MediaBox, so pages with a tight crop box
        # don't get large white margins baked into every image.
        assert mock_convert.call_args.kwargs.get("use_cropbox") is True
        mock_create_cbz.assert_called_once()

    @patch("cbz_ops.pdf.get_user_preference", return_value="webp")
    @patch("cbz_ops.pdf.process_single_page")
    @patch("cbz_ops.pdf.cleanup_temp_folder")
    @patch("cbz_ops.pdf.create_cbz_file")
    @patch("cbz_ops.pdf.convert_from_path")
    @patch("cbz_ops.pdf.pdfinfo_from_path", return_value={"Pages": 1})
    def test_threads_webp_format_from_preference(self, mock_info, mock_convert,
                                                 mock_create_cbz, mock_cleanup,
                                                 mock_single_page, mock_pref,
                                                 tmp_path):
        from cbz_ops.pdf import process_pdf_file

        pdf_path = str(tmp_path / "comic.pdf")
        with open(pdf_path, "w") as f:
            f.write("fake pdf")

        page = MagicMock()
        page.size = (800, 1200)
        page.close = MagicMock()
        mock_convert.return_value = [page]

        process_pdf_file(pdf_path)

        mock_pref.assert_called_once_with("pdf_image_format", default="jpeg")
        assert mock_single_page.call_count >= 1
        assert mock_single_page.call_args.kwargs.get("image_format") == "webp"

    @patch("cbz_ops.pdf.pdfinfo_from_path", side_effect=Exception("corrupt PDF"))
    def test_handles_corrupt_pdf(self, mock_info, tmp_path):
        from cbz_ops.pdf import process_pdf_file

        pdf_path = str(tmp_path / "corrupt.pdf")
        with open(pdf_path, "w") as f:
            f.write("not a pdf")

        # Should not crash, and must say it failed
        assert process_pdf_file(pdf_path) is False


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
