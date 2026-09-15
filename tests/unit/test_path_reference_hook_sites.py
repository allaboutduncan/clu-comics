"""Which call sites follow a path, which clear it, and which must do neither.

A comic's path is stored as a bare string in six places, so a rename or a delete
has to be followed by hand. The rule is not uniform, and the exceptions are the
whole point:

* a rename follows the path -- ``move_path_references``;
* a deliberate deletion drops the reading-list mapping -- ``forget_deleted_path``;
* three callers of ``delete_file_index_entry`` are NOT deletions (a CBR->CBZ
  conversion, a post-download rename tidy-up, and a folder re-scan) and must
  keep calling the plain index delete, or they would throw away mappings they
  are in the middle of moving.

app.py cannot be imported in tests (importing it starts the scheduler and spawns
monitor.py), so these are asserted against the parsed AST.
"""

import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _tree(*parts):
    with open(os.path.join(PROJECT_ROOT, *parts), encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    pytest.fail(f"{name} not found")


def _called_names(node):
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


@pytest.fixture(scope="module")
def app_tree():
    return _tree("app.py")


class TestAppWrappers:
    """Both stay thin wrappers over bodies in core/, because that is the only
    place tests can reach them."""

    def test_delete_clears_the_reading_list_mapping(self, app_tree):
        called = _called_names(_func(app_tree, "update_index_on_delete"))
        assert "forget_deleted_path" in called
        assert "delete_file_index_entry" not in called

    def test_directory_rename_follows_every_path_reference(self, app_tree):
        called = _called_names(_func(app_tree, "update_index_on_move"))
        assert "move_path_references" in called, (
            "the directory branch rewrites file_index in raw SQL, so everything "
            "else keyed on the path has to be followed explicitly"
        )

    def test_the_post_download_tidy_up_is_a_rename_not_a_deletion(self, app_tree):
        """app.py drops a stale row after a ComicVine rename. Clearing the
        mapping there would undo the rename it is bookkeeping for."""
        called = _called_names(_func(app_tree, "process_incoming_wanted_issues"))
        assert "delete_file_index_entry" in called
        assert "forget_deleted_path" not in called


class TestRenameModuleStaysMonitorSafe:
    """monitor.py imports cbz_ops.rename at module top, in a process where
    app.py is not loaded. Even a try/except-guarded `from app import ...` there
    would SUCCEED and execute all of app.py -- starting a second APScheduler and
    spawning another monitor. So the module returns pairs and the route does the
    database work.
    """

    def test_no_import_from_app(self):
        tree = _tree("cbz_ops", "rename.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "app":
                pytest.fail(
                    "cbz_ops/rename.py must never import from app -- monitor.py "
                    "imports this module in a process with no Flask app"
                )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] != "app"

    def test_rename_files_returns_the_pairs(self):
        node = _func(_tree("cbz_ops", "rename.py"), "rename_files")
        returns = [
            sub.value.id
            for sub in ast.walk(node)
            if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Name)
        ]
        assert "renamed_pairs" in returns


class TestRescanIsNotADeletion:
    def test_collection_rescan_keeps_the_mappings(self):
        """The recursive scan deletes the subtree and immediately re-adds it.
        Clearing here would wipe every match under the folder on every rescan."""
        tree = _tree("routes", "collection.py")
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called.add(node.func.id)
        assert "delete_file_index_entry" in called
        assert "forget_deleted_path" not in called


class TestDeliberateDeletionSites:
    def test_multi_select_delete_clears_the_mapping(self):
        """This route bypasses update_index_on_delete entirely.

        Names, not calls: the batch runs on a background thread, so it is
        referenced as ``target=`` rather than invoked.
        """
        node = _func(_tree("routes", "files.py"), "delete_multiple")
        names = {
            sub.id for sub in ast.walk(node) if isinstance(sub, ast.Name)
        } | {
            alias.name
            for sub in ast.walk(node)
            if isinstance(sub, ast.ImportFrom)
            for alias in sub.names
        }
        assert "forget_deleted_paths" in names
        assert "delete_file_index_entries" not in names

    def test_file_watcher_clears_the_mapping(self):
        tree = _tree("core", "file_watcher.py")
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                called.add(node.func.id)
        assert "forget_deleted_path" in called
        assert "delete_file_index_entry" not in called


class TestRenameDirectoryFollowsItsRenames:
    def test_the_route_does_the_database_work(self):
        node = _func(_tree("routes", "files.py"), "rename_directory")
        called = _called_names(node)
        assert "rename_files" in called
        assert "update_index_on_move" in called, (
            "without this, Rename Directory orphans the index, every user's "
            "bookmarks and read history, the tags and every reading-list mapping"
        )
        assert "reconcile_wanted_for_series" in called, (
            "reconcile is deferred per file and coalesced per series -- a folder "
            "of 300 issues must not fire 300 whole-series recomputes"
        )
