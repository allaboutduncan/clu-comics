"""Containment decisions compare realpaths on a separator boundary.

``helpers.library.is_valid_library_path`` / ``is_allowed_path`` /
``is_within_roots`` used to compare ``normpath`` strings, so a symlink inside a
library that pointed out of it counted as inside, and a few call sites used a
bare ``startswith(root)``, which puts ``/data2`` inside ``/data``. Only the
decision changes: callers keep the path string exactly as given.
"""
import os
from unittest.mock import patch

import pytest

from helpers import library as lib


def _symlink_or_skip(src, dst):
    try:
        os.symlink(src, dst, target_is_directory=os.path.isdir(src))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")


@pytest.fixture
def roots(tmp_path):
    root = tmp_path / "data"
    (root / "Batman").mkdir(parents=True)
    sibling = tmp_path / "data2"            # "data2" starts with "data"
    (sibling / "Robin").mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with patch.object(lib, "get_library_roots", return_value=[str(root)]):
        yield root, sibling, outside


class TestIsValidLibraryPath:

    def test_inside_and_root_itself(self, roots):
        root, _sibling, _outside = roots
        assert lib.is_valid_library_path(str(root))
        assert lib.is_valid_library_path(str(root / "Batman"))

    def test_name_prefix_sibling_is_outside(self, roots):
        _root, sibling, _outside = roots
        assert not lib.is_valid_library_path(str(sibling / "Robin"))

    def test_dotdot_escape_is_outside(self, roots):
        root, _sibling, _outside = roots
        assert not lib.is_valid_library_path(str(root / ".." / "elsewhere"))

    def test_path_that_does_not_exist_yet(self, roots):
        """The destination of a move is judged by its nearest existing parent."""
        root, sibling, _outside = roots
        assert lib.is_valid_library_path(str(root / "Batman" / "New" / "x.cbz"))
        assert not lib.is_valid_library_path(str(sibling / "New" / "x.cbz"))

    def test_symlink_out_of_the_library_is_outside(self, roots):
        root, _sibling, outside = roots
        link = root / "Linked"
        _symlink_or_skip(str(outside), str(link))
        assert not lib.is_valid_library_path(str(link))
        assert not lib.is_valid_library_path(str(link / "x.cbz"))

    def test_symlinked_library_root_still_contains_its_paths(self, tmp_path):
        real = tmp_path / "real"
        (real / "Batman").mkdir(parents=True)
        link = tmp_path / "linked-root"
        _symlink_or_skip(str(real), str(link))
        with patch.object(lib, "get_library_roots", return_value=[str(link)]):
            assert lib.is_valid_library_path(str(link / "Batman"))
            assert lib.is_valid_library_path(str(real / "Batman"))

    def test_garbage_input(self, roots):
        assert not lib.is_valid_library_path("")
        assert not lib.is_valid_library_path(None)
        assert not lib.is_valid_library_path("bad" + chr(0) + "path")


class TestIsWithinRoots:

    def test_separator_bounded(self, tmp_path):
        a = tmp_path / "temp"
        a.mkdir()
        b = tmp_path / "temp2"
        b.mkdir()
        assert lib.is_within_roots(str(a / "x.cbz"), [str(a)])
        assert not lib.is_within_roots(str(b / "x.cbz"), [str(a)])

    def test_none_roots_are_ignored(self, tmp_path):
        assert not lib.is_within_roots(str(tmp_path), [None, ""])
        assert lib.is_within_roots(str(tmp_path), [None, str(tmp_path)])

    def test_path_within_on_filesystem_root(self):
        top = os.path.abspath(os.sep)
        assert lib.path_within(os.path.join(top, "x"), top)


class TestIsAllowedPath:

    def test_watch_sibling_is_not_allowed(self, tmp_path):
        watch = tmp_path / "temp"
        watch.mkdir()
        sibling = tmp_path / "temp2"
        sibling.mkdir()
        with patch.object(lib, "get_library_roots", return_value=[]), \
                patch("core.config.get_watch_dir", return_value=str(watch)), \
                patch("core.config.get_target_dir", return_value=None), \
                patch("tempfile.gettempdir", return_value=str(tmp_path / "tmp-root")):
            assert lib.is_allowed_path(str(watch / "a.cbz"))
            assert not lib.is_allowed_path(str(sibling / "a.cbz"))


class TestSmallSites:

    def test_dcpp_translate_is_separator_bounded(self):
        from models.dcpp import translate_remote_path

        assert translate_remote_path("/downloads/x.cbz", "/downloads", "/local") == "/local/x.cbz"
        # /downloads2 is not under /downloads: returned untouched.
        assert translate_remote_path("/downloads2/x.cbz", "/downloads", "/local") == "/downloads2/x.cbz"
        assert translate_remote_path("F:\\dl2\\x.cbz", "F:\\dl", "/local") == "F:\\dl2\\x.cbz"
        assert translate_remote_path("F:\\dl\\x.cbz", "F:\\dl", "/local") == "/local/x.cbz"

    def test_metadata_scanner_does_not_mangle_a_sibling_root(self):
        from core import metadata_scanner as ms

        seen = []
        with patch.object(ms, "get_data_dir", return_value="/srv/data"), \
                patch.object(ms, "get_file_index_entry_by_path",
                             side_effect=lambda p: seen.append(p)):
            ms.queue_file_for_scan("/srv/data2/x.cbz")
            ms.queue_file_for_scan("/srv/data/Batman/x.cbz")
        assert seen == ["/srv/data2/x.cbz", "/data/Batman/x.cbz"]
