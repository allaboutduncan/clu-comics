"""The wanted-issue digest hook in app.process_incoming_wanted_issues.

A TARGET sweep can import dozens of issues in one run, so the notification is a
single digest per sweep rather than one push per issue. That is a property of
*where* the call sits — inside the ``if moved_count > 0`` branch and outside the
per-match loop — so it is asserted against the parsed AST. app.py cannot be
imported in tests (it starts the scheduler and spawns monitor.py at import).

``format_digest`` itself, including its truncation, is covered in
tests/unit/test_notifications.py.
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


def _notify_calls(node):
    return [
        child for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == "notify_async"
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


class TestWantedDigestHook:
    def test_exactly_one_notification_in_the_function(self, func_node):
        assert len(_notify_calls(func_node)) == 1

    def test_it_is_the_wanted_added_event(self, func_node):
        call = _notify_calls(func_node)[0]
        names = [n.id for n in ast.walk(call) if isinstance(n, ast.Name)]
        assert "EVENT_WANTED_ADDED" in names

    def test_it_is_inside_the_moved_count_guard(self, func_node):
        """No files moved means no push at all."""
        guard = _moved_count_guard(func_node)
        in_guard = sum(len(_notify_calls(stmt)) for stmt in guard.body)
        assert in_guard == 1
        # ...and not in the else branch, which is the "nothing matched" case.
        assert sum(len(_notify_calls(stmt)) for stmt in guard.orelse) == 0

    def test_it_is_not_inside_the_per_match_loop(self, func_node):
        """One digest per sweep, not one push per issue."""
        for stmt in ast.walk(func_node):
            if isinstance(stmt, (ast.For, ast.AsyncFor)):
                assert not _notify_calls(stmt), (
                    "notify_async is inside a loop — that sends one push per "
                    "issue instead of a single digest"
                )

    def test_it_uses_format_digest(self, func_node):
        call = _notify_calls(func_node)[0]
        called = [
            c.func.id for c in ast.walk(call)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        ]
        assert "format_digest" in called

    def test_moved_issues_is_collected_alongside_moved_count(self, func_node):
        """The digest body needs the issue names, not just the count."""
        appended = [
            c for c in ast.walk(func_node)
            if isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "append"
            and isinstance(c.func.value, ast.Name)
            and c.func.value.id == "moved_issues"
        ]
        assert appended, "moved_issues is never appended to"
