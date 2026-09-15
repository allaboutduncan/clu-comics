"""Structural checks on what app.py does at import time.

app.py cannot be imported in tests -- it starts the scheduler and spawns
monitor.py at import -- so these are asserted against the parsed AST, in the
style of tests/unit/test_thumbnail_route_staleness.py.

What is being protected:

Gunicorn binds the socket and then imports ``app:app`` in the worker. Anything
running at module level therefore holds up every request, inside the arbiter's
own ``--timeout 120`` budget -- a worker that exceeds it is killed and
respawned, which presents as a deploy that never comes up.

``backup_database`` was called straight from module level. It runs
``PRAGMA quick_check``, MD5s the whole database file and then ZIP-deflates it;
on a library-sized DB that is tens of seconds, and the skip-hash usually misses
because the migrations and ANALYZE that just ran dirtied the file. Nothing at
boot reads the backup, so it belongs on a thread -- and it should reuse the
integrity result app.py computed moments earlier rather than scanning twice.
"""
import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
APP_PATH = os.path.join(PROJECT_ROOT, "app.py")


@pytest.fixture(scope="module")
def tree():
    with open(APP_PATH, encoding="utf-8") as fh:
        return ast.parse(fh.read())


@pytest.fixture(scope="module")
def inside_a_function(tree):
    """Every AST node that sits inside some function body."""
    nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                nodes.add(id(child))
    return nodes


def _calls_named(tree, name):
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"{name} not found in app.py")


class TestTheStartupBackupDoesNotBlockTheWorker:

    def test_it_is_not_called_at_module_level(self, tree, inside_a_function):
        calls = _calls_named(tree, "backup_database")
        assert calls, "backup_database is no longer called from app.py at all"
        for call in calls:
            assert id(call) in inside_a_function, (
                f"app.py:{call.lineno} calls backup_database at import time; "
                "quick_check + MD5 + deflate there holds up the gunicorn worker"
            )

    def test_it_runs_on_a_daemon_thread(self, tree):
        starter = _func(tree, "_startup_backup")
        threads = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "Thread"
            and any(
                isinstance(kw.value, ast.Name) and kw.value.id == starter.name
                for kw in n.keywords
            )
        ]
        assert threads, "_startup_backup is defined but never started on a thread"
        assert any(
            kw.arg == "daemon" and getattr(kw.value, "value", None) is True
            for kw in threads[0].keywords
        ), "a non-daemon backup thread would hold the process open on shutdown"

    def test_it_reuses_the_integrity_result(self, tree):
        """app.py runs check_integrity a few lines above. Without handing the
        result over, the boot pays for two full scans of the database file."""
        starter = _func(tree, "_startup_backup")
        calls = _calls_named(starter, "backup_database")
        assert calls, "_startup_backup no longer takes the backup"
        assert any(
            kw.arg == "known_integrity" for kw in calls[0].keywords
        ), "the startup backup repeats the quick_check app.py already ran"

    def test_a_failed_backup_cannot_break_startup(self, tree):
        """It runs unattended on a thread now; an unwritable /config or a
        half-written DB must not take the log down with a bare traceback."""
        starter = _func(tree, "_startup_backup")
        assert any(
            isinstance(n, ast.ExceptHandler) for n in ast.walk(starter)
        ), "_startup_backup does not handle its own failure"
