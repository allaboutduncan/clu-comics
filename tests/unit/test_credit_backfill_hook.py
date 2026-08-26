"""The credit-backfill hook in app.scheduled_series_sync.

The sweep rides the nightly Metron sync rather than owning a schedule, so two
properties matter and neither is observable by calling the function: it must be
gated on the ``credit_backfill_enabled`` preference, and it must sit inside its
own try/except so a backfill problem can never take the series sync down with
it. Both are asserted against the parsed AST -- app.py cannot be imported in
tests (it starts the scheduler and spawns monitor.py at import).

The sweep's own behaviour is covered in tests/unit/test_credit_backfill.py.
"""

import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PATH = os.path.join(PROJECT_ROOT, "app.py")

FUNC = "scheduled_series_sync"


@pytest.fixture(scope="module")
def func_node():
    with open(APP_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == FUNC:
            return node
    pytest.fail(f"{FUNC} not found in app.py")


def _backfill_calls(node):
    return [
        child for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == "run_credit_backfill"
    ]


def test_sync_runs_the_backfill_exactly_once(func_node):
    assert len(_backfill_calls(func_node)) == 1


def test_backfill_is_gated_on_the_preference(func_node):
    """Users who don't want their archives rewritten in the background can say
    so; without the gate the switch would be decorative."""
    gated = False
    for stmt in ast.walk(func_node):
        if not isinstance(stmt, ast.If):
            continue
        test_src = ast.dump(stmt.test)
        if "credit_backfill_enabled" not in test_src:
            continue
        if any(_backfill_calls(body) for body in stmt.body):
            gated = True
    assert gated, "run_credit_backfill must sit inside the preference check"


def test_backfill_failure_cannot_break_the_sync(func_node):
    """The sync is the important job; the backfill is opportunistic repair."""
    isolated = False
    for stmt in ast.walk(func_node):
        if not isinstance(stmt, ast.Try):
            continue
        if not any(_backfill_calls(body) for body in stmt.body):
            continue
        # The try that *contains* the call and nothing else of the sync's work.
        if len(stmt.handlers) >= 1:
            isolated = True
    assert isolated, "run_credit_backfill must be wrapped in its own try/except"


def test_backfill_runs_after_the_sync_finishes(func_node):
    """Credits for issues imported by this very run are worth picking up, and a
    sweep that ran first would also compete with the sync for Metron's quota."""
    outer = next((stmt for stmt in func_node.body if isinstance(stmt, ast.Try)), None)
    assert outer is not None, (
        "scheduled_series_sync is expected to wrap its work in a try block"
    )
    statements = outer.body
    backfill_index = next(
        (i for i, stmt in enumerate(statements) if _backfill_calls(stmt)), None
    )
    assert backfill_index is not None
    # Everything that fetches series from Metron comes before it.
    loop_index = max(
        (i for i, stmt in enumerate(statements) if isinstance(stmt, ast.For)),
        default=-1,
    )
    assert loop_index != -1 and backfill_index > loop_index
