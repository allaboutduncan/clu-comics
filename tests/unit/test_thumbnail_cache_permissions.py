"""Regression tests for the write that issue #548 reported failing:

    ERROR - Error regenerating thumbnail: [Errno 13] Permission denied:
            '/cache/thumbnails/b9/b94f0bf1c83ac42ddfb8e3bb49c9606f.jpg'

`/cache` was missing from every ownership pass in entrypoint.sh, so thumbnails
written while the container ran as root (its documented non-writable-mount
fallback) stayed root-owned. The later unprivileged process then could not
reopen them for writing -- and because the old code saved *onto* the cache path,
that was fatal even though the shard directory itself was perfectly writable.

Writing via a temp file plus os.replace fixes it: rename needs permission on the
directory, not on the file being replaced. These tests pin that, and that the
result stays as accessible as its directory so the next writer (a second
container, a NAS account) is not locked out in turn.

POSIX only -- Windows has no comparable mode semantics, matching
tests/unit/test_cbz_permissions.py.
"""
import io
import os
import stat
import zipfile

import pytest
from PIL import Image

from core import thumbnail_cache
from core.thumbnail_cache import (
    regenerate_thumbnail,
    thumbnail_cache_path,
    write_cached_thumbnail,
)

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX chmod semantics")


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


def _make_cbz(path, color=(200, 10, 10)):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        buf = io.BytesIO()
        Image.new("RGB", (400, 600), color).save(buf, format="JPEG")
        zf.writestr("001.jpg", buf.getvalue())


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


def test_replaces_a_read_only_cached_thumbnail(cache_dir, tmp_path):
    """The reported failure, reproduced: an unwritable file in a writable dir."""
    cbz = tmp_path / "Book 001.cbz"
    _make_cbz(str(cbz), color=(255, 0, 0))
    regenerate_thumbnail(str(cbz))

    cached = thumbnail_cache_path(str(cbz))
    os.chmod(os.path.dirname(cached), 0o777)
    os.chmod(cached, 0o444)  # what a root-written thumbnail looks like to us
    before = open(cached, "rb").read()

    _make_cbz(str(cbz), color=(0, 0, 255))
    assert regenerate_thumbnail(str(cbz)) is True

    assert open(cached, "rb").read() != before


def test_saving_directly_would_have_failed(cache_dir, tmp_path):
    """Guards the premise of the test above -- if a plain save started working,
    the regression test would pass for the wrong reason and stop protecting
    anything."""
    target = tmp_path / "readonly.jpg"
    Image.new("RGB", (10, 10)).save(str(target), format="JPEG")
    os.chmod(str(target), 0o444)

    with pytest.raises(PermissionError):
        Image.new("RGB", (10, 10)).save(str(target), format="JPEG")


def test_result_is_as_accessible_as_its_directory(cache_dir, tmp_path):
    """mkstemp creates 0600. Left alone, every thumbnail would be private to
    whichever account happened to write it."""
    cbz = tmp_path / "Book 002.cbz"
    _make_cbz(str(cbz))
    cache_path = thumbnail_cache_path(str(cbz))
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    os.chmod(os.path.dirname(cache_path), 0o777)

    assert regenerate_thumbnail(str(cbz)) is True

    mode = stat.S_IMODE(os.stat(cache_path).st_mode)
    assert mode & 0o066, f"thumbnail is not group/other accessible: {oct(mode)}"


def test_reports_failure_when_the_shard_directory_is_unwritable(cache_dir, tmp_path):
    """No shard dir access means no temp file and no rename -- the one case the
    code genuinely cannot work around. It must report, not pretend."""
    if os.geteuid() == 0:
        pytest.skip("root ignores directory permissions")

    cbz = tmp_path / "Book 003.cbz"
    _make_cbz(str(cbz))
    cache_path = thumbnail_cache_path(str(cbz))
    shard = os.path.dirname(cache_path)
    os.makedirs(shard, exist_ok=True)
    os.chmod(shard, 0o555)
    try:
        img = Image.new("RGB", (10, 10))
        assert write_cached_thumbnail(img, cache_path) is False
    finally:
        os.chmod(shard, 0o777)
