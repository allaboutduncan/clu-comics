"""regenerate_thumbnail's hand-off to the problem-files worklist.

Two properties matter beyond "it records something":

* the log line and the stored message go through helpers.archive_error_detail,
  because zipfile embeds several KB of raw binary in its header-mismatch
  message and the thumbnail path was the one reader that logged it verbatim;
* `skipped` records nothing. A PDF having no reader is a fact about the format,
  and recording it would put every PDF in the library on the page.
"""

import io
import zipfile

import pytest
from PIL import Image

from core import thumbnail_cache
from core.problem_files import CLASS_CACHE_WRITE, CLASS_NO_PAGES, SOURCE_THUMBNAIL
from core.thumbnail_cache import regenerate_thumbnail


class _CacheDirOnly:
    def __init__(self, root):
        self._root = root

    def get(self, section, option, fallback=None, **kwargs):
        if (section, option) == ("SETTINGS", "CACHE_DIR"):
            return self._root
        return fallback


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    root.mkdir()
    monkeypatch.setattr(thumbnail_cache, "config", _CacheDirOnly(str(root)))
    return root


@pytest.fixture(autouse=True)
def no_db(monkeypatch):
    monkeypatch.setattr(
        "core.database.get_db_connection", lambda *a, **kw: None, raising=False
    )


@pytest.fixture
def problems(monkeypatch):
    """Capture the module-level record/clear names."""
    recorded, cleared = [], []
    monkeypatch.setattr(
        thumbnail_cache,
        "record_problem",
        lambda path, source, exc=None, error_class=None, error_message=None: recorded.append(
            {
                "path": path,
                "source": source,
                "exc": exc,
                "error_class": error_class,
                "error_message": error_message,
            }
        ),
    )
    monkeypatch.setattr(
        thumbnail_cache, "clear_problem", lambda path, source: cleared.append((path, source))
    )
    return recorded, cleared


def _make_cbz(path, pages=("001.jpg",)):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in pages:
            buf = io.BytesIO()
            Image.new("RGB", (400, 600), (10, 20, 30)).save(buf, format="JPEG")
            zf.writestr(name, buf.getvalue())


class TestRecording:
    def test_a_damaged_archive_is_recorded_with_the_exception(
        self, cache_dir, tmp_path, problems
    ):
        recorded, _cleared = problems
        cbz = tmp_path / "Bad.cbz"
        cbz.write_bytes(b"not a zip at all")

        assert regenerate_thumbnail(str(cbz)) is False

        assert len(recorded) == 1
        assert recorded[0]["path"] == str(cbz)
        assert recorded[0]["source"] == SOURCE_THUMBNAIL
        # The exception itself is handed over, so the store does the splitting
        # and sanitising in one place.
        assert isinstance(recorded[0]["exc"], Exception)

    def test_an_empty_archive_records_no_pages_not_corruption(
        self, cache_dir, tmp_path, problems
    ):
        recorded, _cleared = problems
        cbz = tmp_path / "Empty.cbz"
        with zipfile.ZipFile(cbz, "w") as zf:
            zf.writestr("ComicInfo.xml", "<ComicInfo/>")

        assert regenerate_thumbnail(str(cbz)) is False

        assert recorded[0]["error_class"] == CLASS_NO_PAGES

    def test_an_unwritable_cache_is_not_blamed_on_the_comic(
        self, cache_dir, tmp_path, problems, monkeypatch
    ):
        """The #548 permissions failure. A distinct class, because the page must
        never advise deleting a perfectly good file."""
        recorded, _cleared = problems
        cbz = tmp_path / "Fine.cbz"
        _make_cbz(cbz)
        monkeypatch.setattr(thumbnail_cache, "write_cached_thumbnail", lambda *a: False)

        assert regenerate_thumbnail(str(cbz)) is False

        assert recorded[0]["error_class"] == CLASS_CACHE_WRITE

    def test_success_clears_the_entry(self, cache_dir, tmp_path, problems):
        recorded, cleared = problems
        cbz = tmp_path / "Good.cbz"
        _make_cbz(cbz)

        assert regenerate_thumbnail(str(cbz)) is True

        assert recorded == []
        assert cleared == [(str(cbz), SOURCE_THUMBNAIL)]

    def test_a_skipped_type_records_nothing(self, cache_dir, tmp_path, problems):
        """A PDF has no reader; that is not a damaged file."""
        recorded, cleared = problems
        pdf = tmp_path / "Book.pdf"
        pdf.write_bytes(b"%PDF-1.4")

        assert regenerate_thumbnail(str(pdf)) is False

        assert recorded == []
        assert cleared == []

    def test_diagnostics_are_not_gated_on_record_job(
        self, cache_dir, tmp_path, problems
    ):
        """`record_job=False` suppresses thumbnail_jobs rows, which drive
        re-queue storms. It says nothing about diagnostics: a comic that cannot
        be rendered for folder art is exactly as broken."""
        recorded, _cleared = problems
        cbz = tmp_path / "Bad.cbz"
        cbz.write_bytes(b"not a zip at all")

        regenerate_thumbnail(str(cbz), record_job=False)

        assert len(recorded) == 1


class TestLogging:
    def test_the_binary_blob_never_reaches_the_log(
        self, cache_dir, tmp_path, problems, monkeypatch, caplog
    ):
        """The exact complaint that started this: one bad comic dumped several
        KB of raw header bytes into the log."""
        cbz = tmp_path / "Bad.cbz"
        _make_cbz(cbz)

        blob = b"\x00+\x00\x00\x00rubbish" * 200

        def explode(_path):
            raise zipfile.BadZipFile(
                f"File name in directory 'x.jpg' and header {blob!r} differ"
            )

        monkeypatch.setattr(thumbnail_cache, "_first_page", explode)

        with caplog.at_level("ERROR"):
            assert regenerate_thumbnail(str(cbz)) is False

        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "rubbish" not in logged
        assert "<binary>" in logged
        # And it stays one short line, not a multi-KB wall.
        assert len(logged) < 600
