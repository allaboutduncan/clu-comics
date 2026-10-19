"""app.py runs monitor.py through core.monitor_supervisor, not the MONITOR env var.

app.py cannot be imported in tests, so the wiring is asserted over its AST:

- the env var is read in exactly one place, the one-time seed
  (``core.config.seed_monitor_from_env``), and never by app.py itself;
- ``start_background_services`` starts the supervisor;
- ``shutdown_server`` stops it before the WAL checkpoint and ``os._exit``;
- ``restart_app`` stops it before ``os.execv`` -- execv keeps the pid, so the
  old monitor would never notice its parent went away and would hold the lock
  against the new one;
- the seed runs at import, after ``init_db()``.
"""
import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
APP_PATH = os.path.join(PROJECT_ROOT, "app.py")


@pytest.fixture(scope="module")
def source():
    with open(APP_PATH, "r", encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def tree(source):
    return ast.parse(source)


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in app.py")


def _attr_calls(node):
    """(lineno, 'obj.attr') for every attribute call, in source order."""
    out = []
    for sub in ast.walk(node):
        if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                and isinstance(sub.func.value, ast.Name)):
            out.append((sub.lineno, f"{sub.func.value.id}.{sub.func.attr}"))
    return [name for _, name in sorted(out)]


def test_app_never_reads_the_monitor_env_var(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "MONITOR":
            raise AssertionError(
                f"app.py line {node.lineno} reads MONITOR; the setting replaced it "
                "(core.config.seed_monitor_from_env imports it once)"
            )


def test_old_monitor_process_helpers_are_gone(tree):
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert "run_monitor" not in names
    assert "cleanup" not in names


def test_background_services_start_the_supervisor(tree):
    assert "monitor_supervisor.start" in _attr_calls(
        _function(tree, "start_background_services"))


def test_shutdown_stops_the_supervisor_first(tree):
    calls = _attr_calls(_function(tree, "shutdown_server"))
    assert "monitor_supervisor.stop" in calls
    assert calls.index("monitor_supervisor.stop") < calls.index("os._exit")


def test_restart_stops_the_supervisor_before_execv(tree):
    calls = _attr_calls(_function(tree, "restart_app"))
    assert "monitor_supervisor.stop" in calls
    assert calls.index("monitor_supervisor.stop") < calls.index("os.execv")


def test_seed_runs_after_init_db(source):
    init_at = source.index("\ninit_db()")
    seed_at = source.index("\nseed_monitor_from_env()")
    assert init_at < seed_at


def test_settings_save_pokes_the_supervisor(tree):
    calls = _attr_calls(_function(tree, "save_file_processing_config"))
    assert "monitor_supervisor.poke" in calls
