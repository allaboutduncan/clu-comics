"""models.comicvine.auto_move_file: a '/' in a series or publisher name must
not become a path separator; values go through the folder-name character map.

``auto_move_file`` builds its target with ``os.path.join('/data', structure)``,
which uses the *host* separator -- '\' on Windows, where this project is
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


def _run(series, publisher, cmap, pattern="{publisher}/{series_name}/v{start_year}"):
    from models.comicvine import auto_move_file
    src = "/downloads/processed/Armageddon X-Men CGD 2026 - 001.cbz"
    config = {"ENABLE_AUTO_MOVE": True, "CUSTOM_MOVE_PATTERN": pattern}
    volume = {"name": series, "start_year": 2026, "publisher_name": publisher}
    with patch("core.filename_chars.load_char_map", return_value=cmap), \
         patch("models.comicvine.os.makedirs") as makedirs, \
         patch("models.comicvine.os.path.exists", side_effect=lambda p: p == src), \
         patch("models.comicvine.shutil.move") as move:
        new_path = auto_move_file(src, volume, config)
    return new_path, makedirs.call_args[0][0], move


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


@pytest.mark.parametrize("series", [None, ""])
def test_missing_series_does_not_crash(series):
    _, target_dir, _ = _run(series, "DC", {})
    assert target_dir == _target("DC", "v2026")
