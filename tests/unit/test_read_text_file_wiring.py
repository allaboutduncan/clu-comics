"""``/api/read-text-file`` decides what it may read through core.path_access.

app.py cannot be imported in tests, so the wiring is asserted over its AST:

- the route calls ``resolve_text_file`` (containment, the per-user folder
  grant, the ``.txt``-only rule and the size cap, all tested in
  ``tests/routes/test_path_grants_reader_routes.py``);
- it returns the refusal before anything is opened;
- it opens the *resolved* path (``served.path``), not the one from the query
  string, so a symlink is read where it was judged.
"""
import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
APP_PATH = os.path.join(PROJECT_ROOT, "app.py")


@pytest.fixture(scope="module")
def route():
    with open(APP_PATH, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "read_text_file":
            return node
    raise AssertionError("read_text_file() not found in app.py")


def _calls(node, name):
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            f = sub.func
            called = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if called == name:
                out.append(sub)
    return out


def test_route_is_registered_on_its_path(route):
    paths = [
        d.args[0].value for d in route.decorator_list
        if isinstance(d, ast.Call) and d.args and isinstance(d.args[0], ast.Constant)
    ]
    assert paths == ["/api/read-text-file"]


def test_calls_resolve_text_file(route):
    assert len(_calls(route, "resolve_text_file")) == 1


def test_refusal_returned_before_any_open(route):
    resolve_line = _calls(route, "resolve_text_file")[0].lineno
    opens = _calls(route, "open")
    assert opens, "the route no longer opens the file; update this test"
    assert all(o.lineno > resolve_line for o in opens)

    # An `if refusal: return ...` sits between the resolve and the first open.
    guarded = False
    for sub in ast.walk(route):
        if (isinstance(sub, ast.If) and isinstance(sub.test, ast.Name)
                and sub.test.id == "refusal"
                and any(isinstance(s, ast.Return) for s in sub.body)):
            guarded = resolve_line < sub.lineno < min(o.lineno for o in opens)
    assert guarded


def test_opens_the_resolved_path(route):
    # file_path is rebound to served.path after the check, and every open()
    # reads file_path.
    rebinds = [
        sub for sub in ast.walk(route)
        if isinstance(sub, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "file_path" for t in sub.targets)
        and isinstance(sub.value, ast.Attribute) and sub.value.attr == "path"
        and isinstance(sub.value.value, ast.Name) and sub.value.value.id == "served"
    ]
    assert len(rebinds) == 1
    for o in _calls(route, "open"):
        assert isinstance(o.args[0], ast.Name) and o.args[0].id == "file_path"
        assert o.lineno > rebinds[0].lineno


def test_no_hand_rolled_containment_left(route):
    # The old startswith() prefix test is what this replaced.
    assert not _calls(route, "startswith")
