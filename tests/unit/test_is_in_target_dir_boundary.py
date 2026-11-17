"""``is_in_target_dir`` treats a sibling of TARGET as outside it.

Both nested copies in app.py (the index walk in ``scan_filesystem_for_sync``
and the one inside ``update_index_on_move``) fall back to a string test when
``commonpath``/``samefile`` raise -- which is what happens whenever TARGET does
not exist. That fallback was a bare ``startswith(TARGET)``, so with TARGET
``/downloads/processed`` a file under ``/downloads/processed2`` was taken for a
TARGET file and kept out of the index (or a move into it was not indexed).

app.py cannot be imported, so each nested function is lifted out of its AST
and run with ``normalized_target_dir`` bound to a TARGET that does not exist,
which forces the fallback.
"""
import ast
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _copies():
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    found = {}
    for outer in ast.walk(tree):
        if not isinstance(outer, ast.FunctionDef):
            continue
        for inner in ast.walk(outer):
            if (inner is not outer and isinstance(inner, ast.FunctionDef)
                    and inner.name == "is_in_target_dir"):
                found[outer.name] = inner
    return found


def test_both_copies_are_found():
    assert set(_copies()) == {"scan_filesystem_for_sync", "update_index_on_move"}


@pytest.fixture(params=["scan_filesystem_for_sync", "update_index_on_move"])
def is_in_target_dir(request, tmp_path):
    target = os.path.normpath(str(tmp_path / "downloads" / "processed"))
    assert not os.path.exists(target)       # forces the fallback branch
    fn = _copies()[request.param]
    ns = {"os": os, "normalized_target_dir": target}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "app.py", "exec"), ns)
    return ns["is_in_target_dir"], target


def test_target_itself_is_inside(is_in_target_dir):
    check, target = is_in_target_dir
    assert check(target)


def test_a_file_under_target_is_inside(is_in_target_dir):
    check, target = is_in_target_dir
    assert check(os.path.join(target, "Batman", "Batman 001.cbz"))


def test_a_sibling_with_the_same_prefix_is_outside(is_in_target_dir):
    check, target = is_in_target_dir
    assert not check(target + "2")
    assert not check(os.path.join(target + "2", "Batman 001.cbz"))
    assert not check(target + " old" + os.sep + "x.cbz")


def test_an_unrelated_path_is_outside(is_in_target_dir):
    check, target = is_in_target_dir
    assert not check(os.path.join(os.path.dirname(target), "temp", "x.cbz"))
