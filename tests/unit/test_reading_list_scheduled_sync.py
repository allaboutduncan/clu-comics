"""Invariants of the nightly reading-list sweep.

``app.scheduled_reading_list_sync`` used to carry a verbatim copy of the manual
GitHub sync -- hash compare, CBL parse, entry diff -- which is why it only ever
covered GitHub: a Metron or ComicVine list went in through a different door and
the copy did not know about it. The body now lives in
``core.reading_list_sync.sync_all``, where tests can reach it, and this job is a
wrapper. Two things must stay true of the wrapper:

* it delegates rather than re-implementing, so all four sources stay covered;
* it still stamps the schedule's last-run, which is what the Schedules page
  reads and what says the job actually ran.

app.py cannot be imported in tests (importing it starts the scheduler and
spawns monitor.py), so these are asserted against the parsed AST.
"""

import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PATH = os.path.join(PROJECT_ROOT, "app.py")

FUNC = "scheduled_reading_list_sync"


@pytest.fixture(scope="module")
def func_node():
    with open(APP_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == FUNC:
            return node
    pytest.fail(f"{FUNC} not found in app.py")


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


def _imported_names(node):
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.ImportFrom):
            for alias in sub.names:
                names.add(f"{sub.module}.{alias.name}")
    return names


def test_delegates_to_the_shared_sweep(func_node):
    assert "core.reading_list_sync.sync_all" in _imported_names(func_node)
    assert "sync_all" in _called_names(func_node)


def test_stamps_the_schedule_last_run(func_node):
    assert "update_schedule_last_run" in _called_names(func_node)


def test_does_not_reimplement_the_diff(func_node):
    """The copy that lived here is what made the job GitHub-only."""
    called = _called_names(func_node)
    for leaked in ("sync_reading_list_entries", "parse_entries", "match_file",
                   "update_reading_list_source_hash", "get_reading_lists_with_source",
                   "_is_github_url"):
        assert leaked not in called, (
            f"{FUNC} calls {leaked} directly; that logic belongs in "
            f"core.reading_list_sync so every source gets it"
        )


def test_is_registered_as_a_schedule_job():
    """The sweep reuses the existing reading_list_sync schedule and its UI."""
    with open(APP_PATH, encoding="utf-8") as fh:
        source = fh.read()
    assert '"reading_list_sync"' in source
    assert f'"callback": {FUNC}' in source
