"""The Kavita sync's wiring in app.py.

app.py cannot be imported in tests (importing it starts the scheduler and
spawns monitor.py), so these are asserted against its source. The sync body
lives in ``core/kavita_sync.py``, where tests can reach it; the scheduled job
must stay a wrapper over it.
"""
import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(scope="module")
def source():
    with open(os.path.join(PROJECT_ROOT, "app.py"), encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def tree(source):
    return ast.parse(source)


def _function(tree, name):
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"{name} not found in app.py")


def _called_names(node):
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            names.add(func.id if isinstance(func, ast.Name) else getattr(func, "attr", None))
    return names


def test_the_scheduled_job_is_a_wrapper_over_the_core_sync(tree):
    node = _function(tree, "scheduled_kavita_sync")
    imports = {
        f"{sub.module}.{alias.name}"
        for sub in ast.walk(node) if isinstance(sub, ast.ImportFrom)
        for alias in sub.names
    }
    assert "core.kavita_sync.sync_all" in imports
    assert "sync_all" in _called_names(node)
    # No client, no matching, no writes of its own.
    assert not any(module.startswith("models.") for module in imports)
    assert len(node.body) <= 2  # docstring + one try


def test_the_scheduled_job_hands_over_the_app_and_stamps_last_run(tree):
    node = _function(tree, "scheduled_kavita_sync")
    assert "update_schedule_last_run" in _called_names(node)
    sync_calls = [
        sub for sub in ast.walk(node)
        if isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == "sync_all"
    ]
    assert [kw.arg for kw in sync_calls[0].keywords] == ["app"]


def test_the_schedule_is_registered_and_configured_at_startup(source, tree):
    assert '"kavita": {' in source
    assert '"job_id": "kavita_sync"' in source
    assert "configure_schedule" in _called_names(_function(tree, "configure_kavita_sync_schedule"))
    # Once in its definition, once at startup.
    assert source.count("configure_kavita_sync_schedule()") >= 2


def test_the_blueprint_is_registered(source):
    assert "from routes.kavita import kavita_bp" in source
    assert "app.register_blueprint(kavita_bp)" in source


def test_the_job_id_matches_what_the_status_route_asks_for():
    with open(os.path.join(PROJECT_ROOT, "routes", "kavita.py"), encoding="utf-8") as fh:
        assert 'get_next_run_for_job("kavita_sync")' in fh.read()
