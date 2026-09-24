"""The TARGET -> library move in app.process_incoming_wanted_issues.

``shutil.move``'s cross-device fallback copies the contents and then insists on
copying the timestamps and mode too. A mount that refuses ``utime``/``chmod``
(CIFS/SMB without ``noperm``, a Windows-backed WSL2 bind mount) raises EPERM
*after* the library copy is complete, so the move looks like a failure that it
is not:

    12:13:40 ERROR Failed to move/rename Stuff of Nightmares 001 (2022).cbz:
             [Errno 1] Operation not permitted: '/data/Boom! Studios/...cbz'
    12:13:45 ✅ Indexed recent file from watcher: Stuff of Nightmares 001.cbz

``moved_count`` never incremented, the TARGET source was never unlinked, and the
same comic was re-copied into the library on every subsequent sweep -- 19 times
in ten hours in the report this came from. ``helpers.move_file`` demotes the
metadata failure and keeps raising for the two failures that matter.

Asserted against the parsed AST because app.py cannot be imported in tests (it
starts the scheduler and spawns monitor.py at import). ``move_file`` itself is
covered in tests/unit/test_move_file_metadata_tolerance.py.
"""

import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PATH = os.path.join(PROJECT_ROOT, "app.py")

FUNC = "process_incoming_wanted_issues"


@pytest.fixture(scope="module")
def func_node():
    with open(APP_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == FUNC:
            return node
    pytest.fail(f"{FUNC} not found in app.py")


def _calls(node, module, attr):
    return [
        child for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Attribute)
        and child.func.attr == attr
        and isinstance(child.func.value, ast.Name)
        and child.func.value.id == module
    ]


def _bare_calls(node, name):
    return [
        child for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == name
    ]


def test_uses_move_file(func_node):
    assert _bare_calls(func_node, "move_file"), (
        "the TARGET -> library move must go through helpers.move_file"
    )


def test_does_not_use_shutil_move(func_node):
    assert not _calls(func_node, "shutil", "move"), (
        "shutil.move here fails the move on a refused utime/chmod, leaving a "
        "complete copy in the library AND the source in TARGET to be re-copied "
        "on every sweep"
    )


def test_move_file_is_imported(func_node):
    """A local import: this function sits above app.py's module-level
    ``from helpers import ...`` block."""
    imported = [
        node for node in ast.walk(func_node)
        if isinstance(node, ast.ImportFrom)
        and node.module == "helpers"
        and any(alias.name == "move_file" for alias in node.names)
    ]
    assert imported, "move_file must be imported where it is used"


def test_helpers_exports_move_file():
    import helpers

    assert callable(helpers.move_file)


class TestConvertedSiblingsAreSkipped:
    """A .cbr next to its own .cbz must not also be filed into the library.

    A conversion that fails after writing the CBZ deliberately leaves the source
    archive in place. Both then match the same wanted issue, and the logs show
    both being moved -- so the CBR/CBZ pair that was stuck in TARGET gets
    reproduced inside /data:

        12:16:51 ✓ Match found: 'Stuff of Nightmares 004 (2022).cbr'
                                matches 'Stuff of Nightmares #4'

    This is the same rule as problem_replacements.is_acceptable_replacement:
    a .cbr never stands in for a .cbz.
    """

    def test_the_scan_still_asks_the_collector_for_its_candidates(self, func_node):
        """The filter moved out of app.py, so what is pinned here is the call.

        It used to be an inline ``os.walk`` and this test read the expression
        out of app.py's source. The walk now lives in
        ``helpers.collection.collect_target_candidates`` -- which is also where
        the sibling rule is exercised for real, in
        tests/unit/test_target_candidate_collection.py, instead of being
        restated. What app.py still owes is asking for candidates rather than
        enumerating TARGET itself.
        """
        src = ast.get_source_segment(
            open(APP_PATH, encoding="utf-8").read(), func_node
        )
        assert "collect_target_candidates" in src
        assert "os.walk" not in src

    def test_the_rule_as_executed(self, tmp_path, monkeypatch):
        """The filter itself, run through the helper on real files."""
        from helpers.collection import collect_target_candidates

        # Keep this off the real database: the collector asks whether TARGET is
        # inside a library, and a unit test must not depend on what happens to
        # be configured locally.
        monkeypatch.setattr("helpers.library.get_library_roots", lambda: [])

        for name in [
            "Batman 001.cbr", "Batman 001.cbz",   # a stuck pair
            "Batman 002.cbr",                      # never converted
            "Batman 003.cbz",                      # converted cleanly
            "notes.txt",
        ]:
            (tmp_path / name).write_bytes(b"stub")

        files, _ = collect_target_candidates(str(tmp_path), mapped_dirs=[])
        assert sorted(n for n, _ in files) == [
            "Batman 001.cbz", "Batman 002.cbr", "Batman 003.cbz"
        ]
