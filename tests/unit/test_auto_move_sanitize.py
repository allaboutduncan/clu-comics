"""models.comicvine.auto_move_file: a '/' in a series or publisher name must
not become a path separator; values go through the folder-name character map.

``auto_move_file`` builds its target with ``os.path.join(<library root>, structure)``
(the default library, stubbed here as ``/data``), which uses the *host* separator -- '\' on Windows, where this project is
developed. So the expectations are written as ``_target(...)`` rather than a
hardcoded '/'-joined literal: what is under test is the number of segments the
sanitized values produce, not which slash the host joins them with.
"""
import os
from unittest.mock import patch

import pytest


def _target(*segments):
    """The path auto_move_file builds for these folder segments, on this host."""
    return os.path.join("/data", "/".join(segments))


def _run(series, publisher, cmap, pattern="{publisher}/{series_name}/v{start_year}",
         src="/downloads/processed/Armageddon X-Men CGD 2026 - 001.cbz",
         library=None, default_root="/data", root_exists=True):
    from models.comicvine import auto_move_file
    config = {"ENABLE_AUTO_MOVE": True, "CUSTOM_MOVE_PATTERN": pattern}
    volume = {"name": series, "start_year": 2026, "publisher_name": publisher}
    with patch("core.filename_chars.load_char_map", return_value=cmap), \
         patch("helpers.library.get_library_for_path", return_value=library), \
         patch("helpers.library.get_default_library_root", return_value=default_root), \
         patch("models.comicvine.os.path.isdir", return_value=root_exists), \
         patch("models.comicvine.os.makedirs") as makedirs, \
         patch("models.comicvine.os.path.exists", side_effect=lambda p: p == src), \
         patch("models.comicvine.shutil.move") as move:
        new_path = auto_move_file(src, volume, config)
    target_dir = makedirs.call_args[0][0] if makedirs.called else None
    return new_path, target_dir, move


def test_slash_in_series_is_not_a_separator():
    new_path, target_dir, move = _run("Armageddon / X-Men CGD 2026", "Marvel", {})
    assert target_dir == _target("Marvel", "Armageddon X-Men CGD 2026", "v2026")
    assert new_path == os.path.join(target_dir, "Armageddon X-Men CGD 2026 - 001.cbz")
    move.assert_called_once()


def test_follows_char_map():
    _, target_dir, _ = _run("Armageddon / X-Men", "Marvel: Max", {"/": "-", ":": " -"})
    assert target_dir == _target("Marvel - Max", "Armageddon - X-Men", "v2026")


def test_empty_values_leave_no_empty_segments():
    _, target_dir, _ = _run("Batman", "", {})
    assert target_dir == _target("Batman", "v2026")


def test_repeated_token_is_replaced_everywhere():
    """#616: a pattern that names a token twice resolves both occurrences."""
    _, target_dir, _ = _run(
        "Green Lantern", "DC Comics", {},
        pattern="{publisher}/{series_name}/{series_name} {volume_number} ({start_year})",
    )
    assert "{" not in target_dir
    assert target_dir == _target("DC Comics", "Green Lantern", "Green Lantern (2026)")


def test_unknown_token_is_dropped_not_written():
    _, target_dir, _ = _run("Batman", "DC", {}, pattern="{publisher}/{foo}{series_name}")
    assert target_dir == _target("DC", "Batman")


@pytest.mark.parametrize("series", [None, ""])
def test_missing_series_does_not_crash(series):
    _, target_dir, _ = _run(series, "DC", {})
    assert target_dir == _target("DC", "v2026")


class TestLibraryRoot:
    """The base folder is a library root, never a hard-coded /data: a library
    mounted at /comics has no /data at all, and a folder created there lands in
    the container's own filesystem and is lost with it."""

    def test_moves_within_the_files_own_library(self):
        new_path, target_dir, _ = _run(
            "Batman", "DC Comics", {}, src="/comics/Unsorted/Batman 001.cbz",
            library={"id": 2, "path": "/comics"}, default_root="/data",
        )
        assert target_dir == os.path.join("/comics", "DC Comics/Batman/v2026")
        assert new_path == os.path.join(target_dir, "Batman 001.cbz")

    def test_uses_the_default_library_for_a_file_outside_any_library(self):
        _, target_dir, _ = _run("Batman", "DC Comics", {}, default_root="/comics")
        assert target_dir == os.path.join("/comics", "DC Comics/Batman/v2026")

    def test_refuses_when_the_library_root_is_missing(self):
        new_path, target_dir, move = _run(
            "Batman", "DC Comics", {}, default_root="/comics", root_exists=False,
        )
        assert new_path is None
        assert target_dir is None
        move.assert_not_called()
