"""Issue #548: rebuilding a whole series left every cover stale.

The reporter's words: "I can 'rebuild' issues one at a time and it updates the
thumbnails, but I try to 'rebuild' a series at the File Manager, they do NOT
update thumbnails."

That is not a UI difference -- the two buttons run different modules.
Single-issue rebuild is cbz_ops/single_file.py, which regenerated the thumbnail;
series rebuild is cbz_ops/rebuild.py, which was the only mutating op in
cbz_ops/ that did not. Since the cache is keyed on the file *path* and a rebuild
preserves it, nothing downstream could tell the archive had changed.

rebuild.py is importable (unlike app.py), so the main path is tested for real.
The RAR-masquerade fallback needs a working `unar` binary, so that one is
asserted structurally.
"""
import ast
import io
import os
import zipfile

import pytest
from PIL import Image

from core import thumbnail_cache
from core.thumbnail_cache import thumbnail_cache_path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
REBUILD_PATH = os.path.join(PROJECT_ROOT, "cbz_ops", "rebuild.py")


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


def _make_cbz(path, color=(200, 10, 10), pages=2):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(pages):
            buf = io.BytesIO()
            Image.new("RGB", (400, 600), color).save(buf, format="JPEG")
            zf.writestr(f"{i + 1:03d}.jpg", buf.getvalue())


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


class TestSeriesRebuildRefreshesThumbnails:

    def test_rebuild_rewrites_the_cached_thumbnail(self, cache_dir, tmp_path):
        """The reported bug, end to end."""
        from cbz_ops.rebuild import rebuild_single_cbz_file

        folder = tmp_path / "Batman"
        folder.mkdir()
        cbz = folder / "Batman 001.cbz"

        _make_cbz(str(cbz), color=(255, 0, 0))
        assert thumbnail_cache.regenerate_thumbnail(str(cbz)) is True
        cached = thumbnail_cache_path(str(cbz))
        stale_bytes = open(cached, "rb").read()

        # The cover changes, under the same path -- which is what a rebuild
        # after an edit/crop looks like to the cache.
        _make_cbz(str(cbz), color=(0, 0, 255))

        assert rebuild_single_cbz_file(str(cbz), str(folder)) is True

        assert open(cached, "rb").read() != stale_bytes, (
            "series rebuild left the previous cover cached (#548)"
        )

    def test_rebuild_creates_a_thumbnail_that_was_never_cached(self, cache_dir, tmp_path):
        from cbz_ops.rebuild import rebuild_single_cbz_file

        folder = tmp_path / "Batman"
        folder.mkdir()
        cbz = folder / "Batman 002.cbz"
        _make_cbz(str(cbz))

        assert rebuild_single_cbz_file(str(cbz), str(folder)) is True

        assert os.path.exists(thumbnail_cache_path(str(cbz)))

    def test_a_failed_thumbnail_does_not_fail_the_rebuild(self, cache_dir, tmp_path, monkeypatch):
        """The archive is the artifact; its thumbnail is a convenience. A cache
        that cannot be written must not make the rebuild report failure and
        leave the user thinking their file is damaged."""
        import cbz_ops.rebuild as rebuild_mod

        folder = tmp_path / "Batman"
        folder.mkdir()
        cbz = folder / "Batman 003.cbz"
        _make_cbz(str(cbz))

        def _boom(*a, **kw):
            raise OSError("cache volume offline")

        monkeypatch.setattr(rebuild_mod, "regenerate_thumbnail", _boom)

        assert rebuild_mod.rebuild_single_cbz_file(str(cbz), str(folder)) is True

        # ...and the archive it just wrote is intact, not half-rebuilt.
        with zipfile.ZipFile(str(cbz)) as zf:
            assert zf.namelist()

    def test_rebuilt_archive_still_holds_its_pages(self, cache_dir, tmp_path):
        """Guards the premise of the tests above: a rebuild that quietly emptied
        the archive would also 'refresh' the thumbnail."""
        from cbz_ops.rebuild import rebuild_single_cbz_file

        folder = tmp_path / "Batman"
        folder.mkdir()
        cbz = folder / "Batman 004.cbz"
        _make_cbz(str(cbz), pages=3)

        assert rebuild_single_cbz_file(str(cbz), str(folder)) is True

        with zipfile.ZipFile(str(cbz)) as zf:
            assert len(zf.namelist()) == 3


class TestStructure:
    """Properties that need a RAR toolchain to exercise, asserted on the AST."""

    @pytest.fixture(scope="class")
    def tree(self):
        with open(REBUILD_PATH, encoding="utf-8") as fh:
            return ast.parse(fh.read())

    def _func(self, tree, name):
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        pytest.fail(f"{name} not found in cbz_ops/rebuild.py")

    def _calls(self, node):
        return [
            n.func.id
            for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        ]

    def test_rar_masquerade_fallback_regenerates(self, tree):
        """A .cbz that is really a RAR fails thumbnail generation, so its job row
        is already 'error'. Converting it to a real CBZ without regenerating
        would leave it showing error.svg until the container restarts."""
        func = self._func(tree, "rebuild_single_cbz_file")
        handlers = [n for n in ast.walk(func) if isinstance(n, ast.ExceptHandler)]
        fallback = [h for h in handlers if "_refresh_thumbnail" in self._calls(h)]
        assert fallback, (
            "the BadZipFile/RAR fallback rebuilds the archive but never "
            "refreshes its thumbnail"
        )

    def test_directory_rar_conversion_regenerates(self, tree):
        func = self._func(tree, "convert_rar_to_zip_in_directory")
        assert "_refresh_thumbnail" in self._calls(func)

    def test_sweep_invalidates_the_browse_cache(self, tree):
        """Sizes and mtimes in the cached listing are wrong for every file the
        sweep touched."""
        func = self._func(tree, "rebuild_task")
        assert "invalidate_browse_cache" in self._calls(func)
