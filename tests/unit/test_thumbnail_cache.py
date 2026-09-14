"""Tests for core/thumbnail_cache.py -- the per-comic thumbnail cache.

This module exists because the same twenty lines were copy-pasted into nine
files and one of them (cbz_ops/rebuild.py) was missed, so rebuilding a series
never refreshed its covers (#548). The properties worth pinning are therefore
the ones that made that bug invisible and the ones that would make a future
consolidation silently destructive:

* the path formula, which addresses every JPEG already on every install;
* staleness, which is the only thing that notices a comic was rewritten under
  an unchanged path;
* the fact that a failure is *recorded* rather than swallowed.
"""
import io
import os
import zipfile

import pytest
from PIL import Image

from core import thumbnail_cache
from core.thumbnail_cache import (
    file_changed_since,
    is_thumbnail_stale,
    regenerate_thumbnail,
    thumbnail_cache_path,
    thumbnails_root,
)


class _CacheDirOnly:
    """Stand-in for core.config.config, swapped in on the thumbnail_cache module
    only. Patching `.get` on the real ConfigParser would mutate the object every
    other module shares -- config.getint("LARGE_FILE_THRESHOLD") included.
    """

    def __init__(self, root):
        self._root = root

    def get(self, section, option, fallback=None, **kwargs):
        if (section, option) == ("SETTINGS", "CACHE_DIR"):
            return self._root
        return fallback


def _make_cbz(path, pages=("001.jpg",), color=(200, 10, 10)):
    """Write a real CBZ whose pages are distinguishable by colour."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in pages:
            buf = io.BytesIO()
            # Comfortably larger than THUMBNAIL_HEIGHT: thumbnail() never
            # upscales, so a small page would be copied through unresized.
            Image.new("RGB", (400, 600), color).save(buf, format="JPEG")
            zf.writestr(name, buf.getvalue())


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    """Point the cache at a temp dir for the duration of a test."""
    root = tmp_path / "cache"
    root.mkdir()
    monkeypatch.setattr(thumbnail_cache, "config", _CacheDirOnly(str(root)))
    return root


@pytest.fixture(autouse=True)
def no_db(monkeypatch):
    """thumbnail_jobs bookkeeping is exercised separately; the default is a
    no-op connection so these tests do not depend on a database."""
    monkeypatch.setattr(
        "core.database.get_db_connection", lambda *a, **kw: None, raising=False
    )


class TestCachePath:
    """The formula is a compatibility surface, not an implementation detail."""

    def test_matches_the_historical_formula(self, cache_dir):
        # md5("/data/Batman/Batman 001.cbz") -- the value every already-cached
        # thumbnail on every install is filed under.
        import hashlib

        path = "/data/Batman/Batman 001.cbz"
        digest = hashlib.md5(path.encode("utf-8")).hexdigest()

        result = thumbnail_cache_path(path)

        assert os.path.basename(result) == f"{digest}.jpg"
        assert os.path.basename(os.path.dirname(result)) == digest[:2]
        assert result.startswith(thumbnails_root())

    def test_shard_is_two_characters(self, cache_dir):
        shard = os.path.basename(os.path.dirname(thumbnail_cache_path("/x/y.cbz")))
        assert len(shard) == 2

    def test_differs_by_path_not_content(self, cache_dir, tmp_path):
        a = thumbnail_cache_path("/data/A.cbz")
        b = thumbnail_cache_path("/data/B.cbz")
        assert a != b

    def test_honours_an_explicit_cache_dir(self, tmp_path):
        result = thumbnail_cache_path("/data/A.cbz", cache_dir=str(tmp_path))
        assert result.startswith(os.path.join(str(tmp_path), "thumbnails"))


class TestRegenerate:

    def test_writes_a_thumbnail_for_a_real_cbz(self, cache_dir, tmp_path):
        cbz = tmp_path / "Book 001.cbz"
        _make_cbz(str(cbz))

        assert regenerate_thumbnail(str(cbz)) is True

        cached = thumbnail_cache_path(str(cbz))
        assert os.path.exists(cached)
        with Image.open(cached) as img:
            assert img.height == thumbnail_cache.THUMBNAIL_HEIGHT

    def test_overwrites_an_existing_thumbnail(self, cache_dir, tmp_path):
        """The #548 case: same path, new contents."""
        cbz = tmp_path / "Book 001.cbz"
        _make_cbz(str(cbz), color=(255, 0, 0))
        regenerate_thumbnail(str(cbz))
        cached = thumbnail_cache_path(str(cbz))
        before = open(cached, "rb").read()

        _make_cbz(str(cbz), color=(0, 0, 255))
        assert regenerate_thumbnail(str(cbz)) is True

        assert open(cached, "rb").read() != before

    def test_picks_the_first_page_case_insensitively(self, cache_dir, tmp_path):
        """The cbz_ops copies sorted case-sensitively and the app.py copies did
        not, so the two could disagree about which page is the cover."""
        cbz = tmp_path / "Book 002.cbz"
        _make_cbz(str(cbz), pages=("b.jpg", "A.jpg"))

        with zipfile.ZipFile(str(cbz)) as zf:
            pages = thumbnail_cache._sorted_page_names(zf.namelist())
        assert pages[0] == "A.jpg"

    def test_skips_macosx_and_dotfiles(self, cache_dir):
        names = ["__MACOSX/._001.jpg", ".hidden.jpg", "002.jpg"]
        assert thumbnail_cache._sorted_page_names(names) == ["002.jpg"]

    def test_reports_failure_for_an_archive_with_no_pages(self, cache_dir, tmp_path):
        cbz = tmp_path / "Empty.cbz"
        with zipfile.ZipFile(str(cbz), "w") as zf:
            zf.writestr("ComicInfo.xml", "<ComicInfo/>")

        assert regenerate_thumbnail(str(cbz)) is False
        assert not os.path.exists(thumbnail_cache_path(str(cbz)))

    def test_reports_failure_for_a_missing_file(self, cache_dir, tmp_path):
        assert regenerate_thumbnail(str(tmp_path / "nope.cbz")) is False

    def test_reports_failure_for_an_unsupported_type(self, cache_dir, tmp_path):
        pdf = tmp_path / "Book.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        assert regenerate_thumbnail(str(pdf)) is False

    def test_leaves_no_temp_files_behind_on_failure(self, cache_dir, tmp_path):
        """A failed write must not litter the shard dir with .jpg.tmp files —
        os.path.exists() on the real name would still miss them, but they
        accumulate forever."""
        cbz = tmp_path / "Bad.cbz"
        cbz.write_bytes(b"not a zip at all")
        cache_path = thumbnail_cache_path(str(cbz))
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)

        regenerate_thumbnail(str(cbz))

        assert os.listdir(os.path.dirname(cache_path)) == []


class TestStaleness:

    def test_true_when_the_comic_is_newer(self, cache_dir, tmp_path):
        cbz = tmp_path / "Book.cbz"
        _make_cbz(str(cbz))
        regenerate_thumbnail(str(cbz))
        cached = thumbnail_cache_path(str(cbz))

        os.utime(str(cbz), (os.path.getmtime(cached) + 10,) * 2)

        assert is_thumbnail_stale(str(cbz), cached) is True

    def test_false_when_the_thumbnail_is_newer(self, cache_dir, tmp_path):
        cbz = tmp_path / "Book.cbz"
        _make_cbz(str(cbz))
        regenerate_thumbnail(str(cbz))
        cached = thumbnail_cache_path(str(cbz))

        os.utime(cached, (os.path.getmtime(str(cbz)) + 10,) * 2)

        assert is_thumbnail_stale(str(cbz), cached) is False

    def test_false_when_either_side_is_missing(self, cache_dir, tmp_path):
        """'I cannot tell' must mean 'serve what we have'. Returning True for a
        comic that has been moved or unmounted would regenerate on every single
        request for it."""
        cbz = tmp_path / "Gone.cbz"
        assert is_thumbnail_stale(str(cbz)) is False

        _make_cbz(str(cbz))
        assert is_thumbnail_stale(str(cbz)) is False  # no cached file yet


class TestFileChangedSince:

    def test_true_when_rewritten_after_the_recorded_mtime(self, tmp_path):
        cbz = tmp_path / "Book.cbz"
        _make_cbz(str(cbz))
        assert file_changed_since(str(cbz), os.path.getmtime(str(cbz)) - 10) is True

    def test_false_when_unchanged(self, tmp_path):
        cbz = tmp_path / "Book.cbz"
        _make_cbz(str(cbz))
        assert file_changed_since(str(cbz), os.path.getmtime(str(cbz))) is False

    def test_true_for_a_null_mtime(self, tmp_path):
        """Rows predating the file_mtime column cannot answer the question, and
        the useful reading of that is 'retry once' — after which the row
        carries a real mtime and settles."""
        assert file_changed_since(str(tmp_path / "any.cbz"), None) is True

    def test_false_when_the_file_is_gone(self, tmp_path):
        assert file_changed_since(str(tmp_path / "gone.cbz"), 1.0) is False


class TestInvalidate:

    def test_removes_the_cached_file(self, cache_dir, tmp_path):
        cbz = tmp_path / "Book.cbz"
        _make_cbz(str(cbz))
        regenerate_thumbnail(str(cbz))
        cached = thumbnail_cache_path(str(cbz))
        assert os.path.exists(cached)

        thumbnail_cache.invalidate_thumbnail(str(cbz))

        assert not os.path.exists(cached)

    def test_tolerates_a_path_with_no_cached_thumbnail(self, cache_dir, tmp_path):
        thumbnail_cache.invalidate_thumbnail(str(tmp_path / "never-cached.cbz"))
