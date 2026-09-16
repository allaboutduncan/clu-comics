"""Invariants of the scheduled local-ComicVine-DB update.

Two things have to stay true and neither is visible from the function body
alone:

* ``app.scheduled_comicvine_db_update`` is a **wrapper**. The body lives in
  ``core.comicvine_db_update`` so it can be tested at all; a body left in
  app.py would be reachable only through the AST, which is how the reading-list
  sweep came to carry a private copy that covered one source out of four.

* The APScheduler trigger is a **heartbeat, not the cadence**. The 2-week
  window is enforced in the body against a persisted timestamp. An
  ``IntervalTrigger``'s clock restarts at process start and this scheduler has
  no jobstore, so a ``restart: always`` container restarted more often than
  every two weeks would never fire ``IntervalTrigger(weeks=2)`` at all -- the
  feature would look enabled and do nothing, forever.

app.py cannot be imported in tests (importing it starts the scheduler and
spawns monitor.py), so these are asserted against the parsed AST.
"""

import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP_PATH = os.path.join(PROJECT_ROOT, "app.py")

FUNC = "scheduled_comicvine_db_update"


@pytest.fixture(scope="module")
def app_src():
    with open(APP_PATH, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(scope="module")
def tree(app_src):
    return ast.parse(app_src)


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in app.py")


def _called_names(node):
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _imported_names(node):
    names = set()
    for child in ast.walk(node):
        if isinstance(child, ast.ImportFrom):
            for alias in child.names:
                names.add(f"{child.module}.{alias.name}")
    return names


class TestWrapper:
    def test_exists(self, tree):
        assert _function(tree, FUNC) is not None

    def test_delegates_to_the_core_body(self, tree):
        fn = _function(tree, FUNC)
        assert "core.comicvine_db_update.run_scheduled_update" in _imported_names(fn), (
            "The body belongs in core/ where tests can reach it."
        )
        assert "run_scheduled_update" in _called_names(fn)

    def test_is_a_wrapper_not_a_body(self, tree):
        fn = _function(tree, FUNC)
        assert len(fn.body) <= 4, f"{FUNC} looks like a body, not a wrapper"

    def test_does_not_reimplement_the_update(self, tree):
        """Every one of these is a sign the body has leaked back into app.py."""
        leaked = {
            "probe", "run_update", "_download_zip", "_extract_db",
            "verify_database", "_replace_with_retry",
        }
        assert not (leaked & _called_names(_function(tree, FUNC)))


class TestRegistration:
    def test_job_is_registered(self, tree):
        fn = _function(tree, "start_background_services")
        ids = {
            node.value.value
            for node in ast.walk(fn)
            if isinstance(node, ast.keyword)
            and node.arg == "id"
            and isinstance(node.value, ast.Constant)
        }
        assert "comicvine_db_update" in ids

    def test_trigger_is_a_heartbeat_not_a_two_week_interval(self, app_src):
        """The cadence lives in the body, gated on a persisted timestamp.

        ``IntervalTrigger(weeks=2)`` here would never fire on a container that
        restarts more often than that, which is most of them.
        """
        block = app_src.split("scheduled_comicvine_db_update,")[1].split(")")[0]
        assert "IntervalTrigger" in block
        assert "weeks" not in block, (
            "A multi-week interval trigger never fires on a restarting container"
        )

    def test_has_an_explicit_first_run(self, app_src):
        """Without next_run_time the first check is a full interval away."""
        block = app_src.split("scheduled_comicvine_db_update,")[1].split("replace_existing")[0]
        assert "next_run_time" in block


class TestBodyEnforcesTheCadence:
    def test_core_body_owns_the_interval(self):
        from core import comicvine_db_update as mod

        assert mod.UPDATE_INTERVAL_DAYS == 14

    def test_core_body_gates_on_the_stored_timestamp(self):
        import inspect

        from core import comicvine_db_update as mod

        src = inspect.getsource(mod.run_scheduled_update)
        assert "_interval_elapsed" in src, (
            "The 2-week window has to be checked in the body; the trigger "
            "cannot be trusted to carry it across restarts."
        )
