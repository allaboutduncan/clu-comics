"""The replacement pass's hook inside app.process_incoming_wanted_issues.

A downloaded replacement is NOT a wanted issue: the damaged file still exists,
so the issue is not missing and the wanted pass below will never claim the
download. The replacement pass is what files it, and it has to run even when
the user has closed the Problem Files page — which is why it hangs off the
sweep the download pipeline already calls (api.py, once WATCH drains).

Asserted against the parsed AST because app.py cannot be imported in tests: it
starts the scheduler and spawns monitor.py at import.
"""

import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PATH = os.path.join(PROJECT_ROOT, "app.py")

FUNC = "process_incoming_wanted_issues"
PASS_NAME = "apply_pending_for_app"


@pytest.fixture(scope="module")
def func_node():
    with open(APP_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == FUNC:
            return node
    pytest.fail(f"{FUNC} not found in app.py")


def _calls(node, name):
    return [
        child for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == name
    ]


def test_the_sweep_runs_the_replacement_pass(func_node):
    assert _calls(func_node, PASS_NAME), (
        f"{FUNC} must call {PASS_NAME}; without it a downloaded replacement "
        "sits in TARGET forever, because a corrupt file is not a missing issue"
    )


def test_it_runs_before_the_wanted_matching(func_node):
    """Both passes look at the same TARGET files.

    The replacement pass claims its file by exact (series, issue) intent; the
    wanted pass matches far more loosely across every mapped series. Running
    the specific one first means a replacement can never be carried off to
    some other series folder.
    """
    replacement_line = min(c.lineno for c in _calls(func_node, PASS_NAME))
    matcher = _calls(func_node, "match_wanted_issues_to_files")
    assert matcher, "expected the wanted matcher in this function"
    assert replacement_line < min(c.lineno for c in matcher)


def test_the_pass_is_guarded(func_node):
    """It runs inside the download pipeline; a raise here would break the
    wanted sweep that follows it."""
    for call in _calls(func_node, PASS_NAME):
        enclosing = [
            node for node in ast.walk(func_node)
            if isinstance(node, ast.Try)
            and any(call is c for c in ast.walk(node))
        ]
        assert enclosing, f"{PASS_NAME} call at line {call.lineno} is not inside a try"


def test_the_logic_lives_in_core_not_app(func_node):
    """app.py is untestable, so the hook must stay a one-line delegation.

    Anything that grows here is only ever reachable through assertions like
    this one.
    """
    import core.problem_replacements as mod

    assert hasattr(mod, PASS_NAME)
    assert hasattr(mod, "apply_pending")
    assert hasattr(mod, "verify_replacement")


def test_core_module_has_no_flask_import():
    """It runs from an APScheduler job and from a subprocess-driven sweep, so
    it must not need an application context."""
    path = os.path.join(PROJECT_ROOT, "core", "problem_replacements.py")
    with open(path, encoding="utf-8") as fh:
        source = fh.read()
    assert "current_app" not in source
    assert "from flask import" not in source


def test_the_pass_runs_inside_an_application_context(func_node):
    """api.py calls this function from a bare daemon thread.

    Every function in helpers.trash reads `current_app`, so without a pushed
    context move_to_trash raises "Working outside of application context" and
    every automatic replacement fails. The rest of this function reads `app`
    directly, which is why nothing here needed a context before.
    """
    contexts = [
        node for node in ast.walk(func_node)
        if isinstance(node, ast.With)
        and any(
            isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Attribute)
            and item.context_expr.func.attr == "app_context"
            for item in node.items
        )
    ]
    assert contexts, "expected a `with app.app_context():` around the pass"

    guarded = [
        c for c in contexts
        if any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
            and n.func.id == PASS_NAME
            for n in ast.walk(c)
        )
    ]
    assert guarded, (
        f"{PASS_NAME} must be called inside `with app.app_context():` — "
        "helpers.trash reads current_app throughout"
    )


def test_the_core_module_holds_a_pass_lock():
    """The sweep thread and the page's 10s poll both drive the pass; without a
    lock they race to move the same file out of TARGET."""
    import core.problem_replacements as mod

    assert hasattr(mod, "_pass_lock")
    assert hasattr(mod, "is_acceptable_replacement")
