"""The reading-list gap hook in app.process_incoming_wanted_issues.

A reading-list entry has no ``mapped_path``, so nothing in that function files
anything against it -- and the nightly sweep never even considers an entry whose
year is still in the future, which is precisely the entry a freshly-shipped
issue closes. The arrival is therefore the only event that can close that gap,
and this pins the call that acts on it.

Like the digest hook next door, this is a property of *where* the call sits --
inside the ``if moved_count > 0`` branch and outside the per-match loop -- so it
is asserted against the parsed AST. app.py cannot be imported in tests (it
starts the scheduler and spawns monitor.py at import).

``fill_gaps_for_series`` itself is covered in
tests/integration/test_reading_list_gap_match.py.
"""

import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PATH = os.path.join(PROJECT_ROOT, "app.py")

FUNC = "process_incoming_wanted_issues"
HOOK = "fill_gaps_for_series"


@pytest.fixture(scope="module")
def func_node():
    with open(APP_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == FUNC:
            return node
    pytest.fail(f"{FUNC} not found in app.py")


def _hook_calls(node):
    return [
        child for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == HOOK
    ]


def _moved_count_guard(func_node):
    """The ``if moved_count > 0:`` statement."""
    for stmt in ast.walk(func_node):
        if not isinstance(stmt, ast.If):
            continue
        test = stmt.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "moved_count"
            and isinstance(test.ops[0], ast.Gt)
        ):
            return stmt
    pytest.fail("`if moved_count > 0:` not found")


class TestGapHook:

    def test_it_is_called_exactly_once(self, func_node):
        assert len(_hook_calls(func_node)) == 1

    def test_it_is_inside_the_moved_count_guard(self, func_node):
        """Nothing moved means there is no arrival to match against."""
        guard = _moved_count_guard(func_node)
        assert sum(len(_hook_calls(stmt)) for stmt in guard.body) == 1
        assert sum(len(_hook_calls(stmt)) for stmt in guard.orelse) == 0

    def test_it_is_not_inside_the_per_match_loop(self, func_node):
        """One pass per sweep. Per file it would re-walk the same gaps once
        for every issue imported, on the request thread."""
        for stmt in ast.walk(func_node):
            if isinstance(stmt, (ast.For, ast.AsyncFor)):
                assert not _hook_calls(stmt), (
                    f"{HOOK} is inside a loop -- that repeats the whole gap "
                    f"pass once per moved file"
                )

    def test_the_moved_series_names_are_collected_in_the_loop(self, func_node):
        """The pass is scoped to what actually arrived; without the set it
        would fall back to every gap in every tracked list."""
        adds = [
            c for c in ast.walk(func_node)
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "add"
            and isinstance(c.func.value, ast.Name)
            and c.func.value.id == "moved_series_names"
        ]
        assert adds, "moved_series_names is never added to"

    def test_it_is_passed_that_set(self, func_node):
        call = _hook_calls(func_node)[0]
        first = call.args[0]
        assert isinstance(first, ast.Name) and first.id == "moved_series_names"

    def test_the_rename_pattern_comes_from_app_config(self, func_node):
        """Reading it through current_app would raise: api.py calls this from a
        bare daemon thread. Defaulting it here instead would silently match
        against a pattern the user does not use."""
        call = _hook_calls(func_node)[0]
        config_reads = [
            c for c in ast.walk(call)
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "get"
            and isinstance(c.func.value, ast.Attribute)
            and c.func.value.attr == "config"
        ]
        assert config_reads, "the rename pattern is not read from app.config"
        assert any(
            isinstance(a, ast.Constant) and a.value == "CUSTOM_RENAME_PATTERN"
            for c in config_reads for a in c.args
        )
