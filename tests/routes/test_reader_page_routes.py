"""The reader's page routes: /api/read/<path>/page/<n>, .../page/<n>/info, .../info.

They lived in app.py, which cannot be imported in tests, so the only coverage
they had was an AST check that a corrupt-archive handler existed. They are in
routes/reading.py now and these run them against real archives.
"""
import os
import zipfile
from unittest.mock import patch

import pytest


def _url_path(path):
    """A filesystem path as the reader puts it in the URL.

    The route restores the leading slash. On Windows the drive letter is
    dropped, which leaves a path relative to the current drive's root.
    """
    return os.path.splitdrive(str(path))[1].replace("\\", "/").lstrip("/")


def _cbz(path, members):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members:
            zf.writestr(name, data)
    return path


@pytest.fixture
def comic(tmp_path):
    return _cbz(tmp_path / "Batman 001.cbz", [
        ("10.jpg", b"ten"),
        ("2.jpg", b"two"),
        ("1.png", b"one"),
        ("ComicInfo.xml", b"<ComicInfo/>"),
        ("__MACOSX/1.jpg", b"resource fork"),
        ("sub/.hidden.jpg", b"hidden"),
    ])


def _page(client, path, n):
    return client.get(f"/api/read/{_url_path(path)}/page/{n}")


def _is_error_image(resp):
    return resp.status_code == 200 and resp.mimetype == "image/svg+xml"


class TestPage:
    def test_serves_the_page_bytes_with_its_type(self, client, comic):
        resp = _page(client, comic, 0)
        assert resp.status_code == 200
        assert resp.data == b"one"
        assert resp.mimetype == "image/png"

    def test_pages_are_in_natural_order(self, client, comic):
        """2.jpg comes before 10.jpg; a plain sort would serve them reversed."""
        assert _page(client, comic, 1).data == b"two"
        assert _page(client, comic, 2).data == b"ten"
        assert _page(client, comic, 1).mimetype == "image/jpeg"

    def test_metadata_and_junk_entries_are_not_pages(self, client, comic):
        """ComicInfo.xml, __MACOSX and dot-files must not shift the page numbers."""
        assert _page(client, comic, 3).status_code == 400

    @pytest.mark.parametrize("n", [3, 99])
    def test_a_page_past_the_end_is_400(self, client, comic, n):
        resp = _page(client, comic, n)
        assert resp.status_code == 400
        assert resp.get_json() == {"error": "Invalid page number"}

    def test_a_missing_comic_answers_with_the_error_image(self, client, tmp_path):
        """It is an <img> src: a 404 would show a broken-image icon."""
        assert _is_error_image(_page(client, tmp_path / "gone.cbz", 0))

    def test_an_unsupported_format_is_400(self, client, tmp_path):
        pdf = tmp_path / "book.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        resp = _page(client, pdf, 0)
        assert resp.status_code == 400
        assert resp.get_json() == {"error": "Unsupported file format"}

    def test_a_damaged_archive_answers_with_the_error_image(self, client, tmp_path):
        bad = tmp_path / "broken.cbz"
        bad.write_bytes(b"this is not a zip archive at all")
        assert _is_error_image(_page(client, bad, 0))

    def test_a_damaged_archive_is_a_warning_without_a_traceback(self, client, tmp_path):
        """A corrupt comic is the user's file, not a bug: one WARNING line."""
        bad = tmp_path / "broken.cbz"
        bad.write_bytes(b"this is not a zip archive at all")
        with patch("routes.reading.app_logger") as log:
            _page(client, bad, 0)
        assert log.warning.call_count == 1
        assert "Damaged archive" in log.warning.call_args[0][0]
        log.error.assert_not_called()

    def test_a_zip_extension_is_read_too(self, client, tmp_path):
        z = _cbz(tmp_path / "pack.zip", [("a.jpg", b"aaa")])
        assert _page(client, z, 0).data == b"aaa"


class TestCbrFallback:
    """A .cbr page rarfile cannot read falls back to the command-line tools."""

    class _Rar:
        def __init__(self, data=None, raises=False):
            self._data, self._raises, self.closed = data, raises, False

        def namelist(self):
            return ["1.jpg"]

        def read(self, name):
            if self._raises:
                raise RuntimeError("bad header")
            return self._data

        def close(self):
            self.closed = True

    JPEG = b"\xff\xd8" + b"x" * 20 + b"\xff\xd9"

    @pytest.fixture
    def cbr(self, tmp_path):
        path = tmp_path / "Batman 001.cbr"
        path.write_bytes(b"Rar!\x1a\x07\x00")
        return path

    def test_a_complete_page_is_served_from_rarfile(self, client, cbr):
        with patch("routes.reading.rarfile.RarFile", return_value=self._Rar(self.JPEG)), \
                patch("routes.reading._extract_single_rar_entry") as fallback:
            resp = _page(client, cbr, 0)
        assert resp.data == self.JPEG
        fallback.assert_not_called()

    def test_truncated_data_goes_to_the_fallback(self, client, cbr):
        truncated = self.JPEG[:-2]
        archive = self._Rar(truncated)
        with patch("routes.reading.rarfile.RarFile", return_value=archive), \
                patch("routes.reading._extract_single_rar_entry", return_value=self.JPEG) as fallback:
            resp = _page(client, cbr, 0)
        assert resp.data == self.JPEG
        assert fallback.call_args[0][1] == "1.jpg"
        assert archive.closed, "the archive is released before a tool opens it"

    def test_a_read_error_goes_to_the_fallback(self, client, cbr):
        with patch("routes.reading.rarfile.RarFile", return_value=self._Rar(raises=True)), \
                patch("routes.reading._extract_single_rar_entry", return_value=self.JPEG):
            assert _page(client, cbr, 0).data == self.JPEG

    def test_no_tool_can_read_it_answers_with_the_error_image(self, client, cbr):
        with patch("routes.reading.rarfile.RarFile", return_value=self._Rar(raises=True)), \
                patch("routes.reading._extract_single_rar_entry", return_value=None):
            assert _is_error_image(_page(client, cbr, 0))


class TestPageInfo:
    def _info(self, client, path, n):
        return client.get(f"/api/read/{_url_path(path)}/page/{n}/info")

    def test_reports_the_entry_behind_a_page(self, client, comic):
        body = self._info(client, comic, 2).get_json()
        assert body == {
            "success": True,
            "page_num": 2,
            "file_name": "10.jpg",
            "file_size": 3,
            "archive_path": "10.jpg",
        }

    def test_a_page_past_the_end_is_400(self, client, comic):
        resp = self._info(client, comic, 3)
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False

    def test_a_missing_comic_is_404(self, client, tmp_path):
        assert self._info(client, tmp_path / "gone.cbz", 0).status_code == 404

    def test_an_unsupported_format_is_400(self, client, tmp_path):
        pdf = tmp_path / "book.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        assert self._info(client, pdf, 0).status_code == 400

    def test_a_damaged_archive_is_a_500_with_a_short_reason(self, client, tmp_path):
        bad = tmp_path / "broken.cbz"
        bad.write_bytes(b"this is not a zip archive at all")
        resp = self._info(client, bad, 0)
        assert resp.status_code == 500
        body = resp.get_json()
        assert body["success"] is False and body["error"]
        assert len(body["error"]) < 300, "the reason is sanitised, not a header dump"


class TestComicInfo:
    def _info(self, client, path):
        return client.get(f"/api/read/{_url_path(path)}/info")

    def test_counts_pages_only(self, client, comic):
        assert self._info(client, comic).get_json() == {
            "success": True,
            "page_count": 3,
            "filename": "Batman 001.cbz",
        }

    def test_a_missing_comic_is_404(self, client, tmp_path):
        assert self._info(client, tmp_path / "gone.cbz").status_code == 404

    def test_an_unsupported_format_is_400(self, client, tmp_path):
        pdf = tmp_path / "book.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        assert self._info(client, pdf).status_code == 400

    def test_a_damaged_archive_is_500(self, client, tmp_path):
        bad = tmp_path / "broken.cbz"
        bad.write_bytes(b"this is not a zip archive at all")
        assert self._info(client, bad).status_code == 500


class TestAccessIsCheckedFirst:
    """A path the user may not read is refused before the file is touched."""

    @pytest.mark.parametrize("suffix", ["/page/0", "/page/0/info", "/info"])
    def test_a_denied_path_is_never_opened(self, client, comic, suffix):
        with patch("routes.reading.enforce_path_access", return_value=("denied", 403)) as check, \
                patch("routes.reading.zipfile.ZipFile") as opened:
            resp = client.get(f"/api/read/{_url_path(comic)}{suffix}")
        assert resp.status_code == 403
        opened.assert_not_called()
        checked = check.call_args[0][0]
        assert checked.startswith("/"), "the check sees the restored absolute path"
        assert checked.endswith("Batman 001.cbz")
