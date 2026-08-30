"""Invariants of the series-scoped GetComics run.

``app.scheduled_getcomics_download(only_series_id=...)`` backs the "Check for
Missing Issues" button on a series page. Three properties make the scoped run
different from the unattended sweep, and all three are easy to undo by editing
the shared body:

* it narrows ``mapped_series`` to the requested id;
* it ignores the series' Monitor toggle — that toggle exists to keep the
  *unattended* sweep off a series, and a click is an explicit request;
* it leaves the schedule's last-run stamp alone, so a one-series check can't
  make the nightly sweep look as though it has already run.

app.py cannot be imported in tests (importing it starts the scheduler and
spawns monitor.py), so these are asserted against the parsed AST.
"""

import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PATH = os.path.join(PROJECT_ROOT, "app.py")

FUNC = "scheduled_getcomics_download"


@pytest.fixture(scope="module")
def func_node():
    with open(APP_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == FUNC:
            return node
    pytest.fail(f"{FUNC} not found in app.py")


def _guards_on(func_node, name):
    """Every ``if`` whose test mentions ``name``."""
    out = []
    for stmt in ast.walk(func_node):
        if not isinstance(stmt, ast.If):
            continue
        if any(
            isinstance(n, ast.Name) and n.id == name
            for n in ast.walk(stmt.test)
        ):
            out.append(stmt)
    return out


def _calls_named(node, name):
    return [
        child for child in ast.walk(node)
        if isinstance(child, ast.Call)
        and isinstance(child.func, ast.Name)
        and child.func.id == name
    ]


class TestScopedRunSignature:
    def test_accepts_only_series_id_and_op_id(self, func_node):
        args = [a.arg for a in func_node.args.args]
        assert "only_series_id" in args
        assert "op_id" in args

    def test_scope_parameter_is_not_named_series_id(self, func_node):
        # The per-series loop rebinds ``series_id`` to the series it is
        # processing; a parameter of that name would be shadowed and the
        # scoping silently lost.
        assert "series_id" not in [a.arg for a in func_node.args.args]

    def test_both_new_parameters_default_to_none(self, func_node):
        args = func_node.args.args
        defaults = dict(
            zip([a.arg for a in args[-len(func_node.args.defaults):]],
                func_node.args.defaults)
        )
        for name in ("only_series_id", "op_id"):
            node = defaults[name]
            assert isinstance(node, ast.Constant) and node.value is None


class TestScopedRunBehaviour:
    def test_mapped_series_is_narrowed_to_the_requested_id(self, func_node):
        """A scoped run filters the mapped-series list before the loop."""
        narrowing = [
            stmt for stmt in _guards_on(func_node, "only_series_id")
            if any(
                isinstance(n, ast.Name) and n.id == "mapped_series"
                for sub in stmt.body for n in ast.walk(sub)
            )
        ]
        assert narrowing, "no `if only_series_id is not None:` narrowing mapped_series"

    def test_monitor_toggle_is_bypassed_when_scoped(self, func_node):
        """The `monitored == 0` skip must also require an unscoped run."""
        monitored_guards = [
            stmt for stmt in ast.walk(func_node)
            if isinstance(stmt, ast.If)
            and any(
                isinstance(n, ast.Constant) and n.value == "monitored"
                for n in ast.walk(stmt.test)
            )
        ]
        assert monitored_guards, "the monitored skip disappeared from the sweep"
        for stmt in monitored_guards:
            names = {n.id for n in ast.walk(stmt.test) if isinstance(n, ast.Name)}
            assert "only_series_id" in names, (
                "the Monitor toggle would skip an explicitly requested series"
            )

    def test_last_run_stamp_is_only_written_by_an_unscoped_run(self, func_node):
        calls = _calls_named(func_node, "update_last_getcomics_run")
        assert len(calls) == 1, "expected exactly one last-run write"
        guarded = [
            stmt for stmt in _guards_on(func_node, "only_series_id")
            if _calls_named(stmt, "update_last_getcomics_run")
        ]
        assert guarded, (
            "update_last_getcomics_run() is not guarded by `only_series_id is None` — "
            "a one-series check would move the schedule's last-run stamp"
        )

    def test_progress_is_reported_through_the_operations_registry(self, func_node):
        """The op the route hands back must be updated and completed."""
        attr_calls = {
            child.func.attr for child in ast.walk(func_node)
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
        }
        assert "update_operation" in attr_calls
        assert "complete_operation" in attr_calls

    def test_the_operation_is_completed_on_the_failure_path_too(self, func_node):
        """An abandoned op would sit in the header indicator until it goes stale."""
        handlers = [
            h for h in ast.walk(func_node) if isinstance(h, ast.ExceptHandler)
        ]
        completing = [
            h for h in handlers
            if any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Attribute)
                and c.func.attr == "complete_operation"
                for c in ast.walk(h)
            )
        ]
        assert completing, "no except handler completes the operation"
