"""What counts as "inside WATCH or TARGET" -- one helper, used everywhere.

``app.py`` used to carry two nested ``is_in_target_dir`` copies (the index
walk in ``scan_filesystem_for_sync`` and one inside ``update_index_on_move``).
They knew about TARGET only, read it from ``app.config``, and were the *only*
exclusion anywhere: every other writer put WATCH/TARGET files in
``file_index``, where the library health scan hashed them and the duplicate
search paired them with the library copies they were about to be filed as.

``helpers.library.staging_matcher`` replaces both. This file keeps the
boundary cases the old test pinned (a sibling such as ``processed2`` is not
inside ``processed``) and adds the nested layouts, and asserts structurally
that app.py -- which cannot be imported -- uses the helper in every walk that
feeds the index.
"""
import ast
import os
import pathlib
from unittest.mock import patch

import pytest

from helpers.library import is_staging_path, staging_kind, staging_matcher

ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture
def layout(tmp_path):
    """A library, and WATCH/TARGET that do not exist on disk (so nothing here
    depends on the filesystem agreeing)."""
    library = tmp_path / "library"
    library.mkdir()
    state = {
        "watch": str(tmp_path / "downloads" / "temp"),
        "target": str(tmp_path / "downloads" / "processed"),
        "libraries": [str(library)],
    }
    with patch("core.config.get_watch_dir", side_effect=lambda: state["watch"]), \
            patch("core.config.get_target_dir", side_effect=lambda: state["target"]), \
            patch("helpers.library.get_library_roots", side_effect=lambda: state["libraries"]):
        yield state


class TestBoundaries:
    def test_the_folders_themselves_are_staging(self, layout):
        assert staging_kind(layout["target"], is_dir=True) == "target"
        assert staging_kind(layout["watch"], is_dir=True) == "watch"

    def test_a_file_under_either_is_staging(self, layout):
        assert staging_kind(os.path.join(layout["target"], "Batman", "Batman 001.cbz")) == "target"
        assert staging_kind(os.path.join(layout["watch"], "Batman 001.cbz")) == "watch"

    def test_a_sibling_with_the_same_prefix_is_outside(self, layout):
        target = layout["target"]
        assert not is_staging_path(target + "2", is_dir=True)
        assert not is_staging_path(os.path.join(target + "2", "Batman 001.cbz"))
        assert not is_staging_path(target + " old" + os.sep + "x.cbz")

    def test_a_library_file_is_not_staging(self, layout):
        assert not is_staging_path(os.path.join(layout["libraries"][0], "DC", "x.cbz"))

    def test_a_trailing_slash_in_the_setting_changes_nothing(self, layout):
        layout["target"] = layout["target"] + os.sep
        assert is_staging_path(os.path.join(layout["target"], "x.cbz"))

    def test_unset_folders_match_nothing(self, layout):
        layout["watch"] = layout["target"] = ""
        assert not is_staging_path(os.path.join(layout["libraries"][0], "x.cbz"))
        assert not is_staging_path("")
        assert not is_staging_path(None)

    def test_a_matcher_reads_the_settings_once(self, layout):
        match = staging_matcher()
        inside = os.path.join(layout["target"], "x.cbz")
        layout["target"] = ""
        # Built for a whole walk: no setting or filesystem read per path.
        with patch("os.path.realpath", side_effect=AssertionError("resolved a path")):
            assert match(inside) == "target"


class TestNestedLayouts:
    """The most specific root wins, counting library roots."""

    def test_target_inside_a_library_is_staging(self, layout):
        library = layout["libraries"][0]
        layout["target"] = os.path.join(library, "incoming")
        assert staging_kind(os.path.join(library, "incoming", "x.cbz")) == "target"
        assert staging_kind(os.path.join(library, "incoming"), is_dir=True) == "target"
        assert not is_staging_path(os.path.join(library, "DC", "x.cbz"))

    def test_watch_inside_a_library_is_staging(self, layout):
        library = layout["libraries"][0]
        layout["watch"] = os.path.join(library, "_watch")
        assert staging_kind(os.path.join(library, "_watch", "x.cbz")) == "watch"

    def test_watch_inside_target_is_watch(self, layout):
        layout["watch"] = os.path.join(layout["target"], "temp")
        assert staging_kind(os.path.join(layout["target"], "temp", "x.cbz")) == "watch"
        assert staging_kind(os.path.join(layout["target"], "x.cbz")) == "target"

    def test_target_inside_watch_is_target(self, layout):
        layout["target"] = os.path.join(layout["watch"], "processed")
        assert staging_kind(os.path.join(layout["watch"], "processed", "x.cbz")) == "target"
        assert staging_kind(os.path.join(layout["watch"], "x.cbz")) == "watch"

    def test_a_library_inside_target_is_still_a_library(self, layout):
        nested = os.path.join(layout["target"], "Comics")
        layout["libraries"] = [nested]
        assert not is_staging_path(os.path.join(nested, "DC", "x.cbz"))
        assert not is_staging_path(nested, is_dir=True)
        assert staging_kind(os.path.join(layout["target"], "x.cbz")) == "target"

    def test_target_that_is_a_library_root_owns_only_its_loose_files(self, layout):
        """Downloads land flat in the library root and are filed into series
        folders from there. The old walk pruned every sub-folder of such a
        root and indexed only the loose files -- exactly backwards."""
        library = layout["libraries"][0]
        layout["target"] = library
        assert staging_kind(os.path.join(library, "Loose 001.cbz")) == "target"
        assert not is_staging_path(os.path.join(library, "DC"), is_dir=True)
        assert not is_staging_path(os.path.join(library, "DC", "Batman 001.cbz"))
        assert not is_staging_path(library, is_dir=True)


class TestAppUsesTheHelper:
    @pytest.fixture(scope="class")
    def tree(self):
        return ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))

    def _function(self, tree, name):
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        raise AssertionError(f"{name} not in app.py")

    def test_the_private_copies_are_gone(self, tree):
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert "is_in_target_dir" not in names

    @pytest.mark.parametrize("fn", [
        "_build_file_index_locked",   # cold start
        "scan_filesystem_for_sync",   # scheduled and manual sync
        "update_index_on_create",     # a folder moved in or restored
        "update_index_on_move",
    ])
    def test_every_index_feeder_asks_the_staging_matcher(self, tree, fn):
        calls = [ast.unparse(n.func) for n in ast.walk(self._function(tree, fn))
                 if isinstance(n, ast.Call)]
        assert "staging_matcher" in calls, f"{fn} does not exclude WATCH/TARGET"

    @pytest.mark.parametrize("fn", ["_build_file_index_locked", "scan_filesystem_for_sync"])
    def test_the_walks_prune_directories_and_loose_files(self, tree, fn):
        src = ast.unparse(self._function(tree, fn))
        assert "_staging(os.path.join(root, d), is_dir=True)" in src
        assert "_staging(os.path.join(root, f))" in src

    def test_a_move_into_staging_is_a_move_out_of_the_index(self, tree):
        """It used to return early for a TARGET destination, leaving the old
        library row behind."""
        src = ast.unparse(self._function(tree, "update_index_on_move"))
        assert "new_in_target" not in src
        assert "is_valid_library_path(normalized_new) and (not _staging(" in src
        assert "is_valid_library_path(normalized_old) and (not _staging(" in src


class TestOtherWalks:
    def test_thumbnail_scan_skips_staging_inside_a_library(self, layout):
        from core.thumbnail_scan import iter_candidate_dirs

        library = layout["libraries"][0]
        layout["target"] = os.path.join(library, "incoming")
        os.makedirs(os.path.join(library, "incoming"))
        os.makedirs(os.path.join(library, "DC"))
        for rel in (("incoming", "Waiting 001.cbz"), ("DC", "Batman 001.cbz")):
            open(os.path.join(library, *rel), "wb").close()
        found = [p for _, paths in iter_candidate_dirs(library) for p in paths]
        assert found == [os.path.join(library, "DC", "Batman 001.cbz")]

    def test_series_json_scan_skips_staging_inside_a_library(self, layout, db_connection):
        from core.series_json_check import scan_library

        library = layout["libraries"][0]
        layout["target"] = os.path.join(library, "incoming")
        folder = os.path.join(library, "incoming", "Some Series")
        os.makedirs(folder)
        with open(os.path.join(folder, "series.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        stats = scan_library()
        assert stats["checked"] == 0 and stats["flagged"] == 0
