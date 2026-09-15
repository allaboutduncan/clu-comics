"""Structural checks on app.py's database-lifecycle behaviour.

app.py cannot be imported in tests -- it starts the scheduler and spawns
monitor.py at import -- so these are asserted against the parsed AST, in the
style of tests/unit/test_startup_blocking_work.py.

What is being protected:

``shutdown_server`` is installed on SIGTERM and SIGINT *inside the gunicorn
worker*, overriding gunicorn's own graceful handler, and it called
``os._exit(0)`` directly. That kills the process with every SQLite connection
still open, mid-transaction, and with the write-ahead log never checkpointed --
on every ``docker stop``, every ``docker restart`` and every ``restart: always``
cycle. Nothing else in the app ever checkpointed either, so the WAL only ever
grew. That combination is the most likely cause of repeated "database disk
image is malformed" failures, and it is a one-line regression to reintroduce.

The scheduled jobs must stay thin wrappers over bodies in ``core/`` for the
reason ``scheduled_reading_list_sync`` does: a body left in app.py is only ever
reachable through assertions like these.
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
    with open(APP_PATH, "r", encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found in app.py")


def _calls(node):
    """Every called name/attribute inside a function, in source order."""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                out.append((func.lineno, func.id))
            elif isinstance(func, ast.Attribute):
                out.append((func.lineno, func.attr))
    return [name for _, name in sorted(out)]


class TestShutdownCheckpoints:
    def test_shutdown_server_checkpoints_before_exiting(self, tree):
        fn = _function(tree, "shutdown_server")
        calls = _calls(fn)
        assert "shutdown_checkpoint" in calls, (
            "shutdown_server must flush the WAL before os._exit -- see this "
            "module's docstring"
        )
        assert "_exit" in calls
        assert calls.index("shutdown_checkpoint") < calls.index("_exit"), (
            "the checkpoint has to happen before the process is killed"
        )

    def test_shutdown_still_exits(self, tree):
        """Bounded and unconditional: overrunning Docker's SIGTERM grace period
        earns a SIGKILL, which is the unclean shutdown we are avoiding."""
        fn = _function(tree, "shutdown_server")
        exits = [
            n for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "_exit"
        ]
        assert exits, "shutdown_server must still call os._exit"
        # os._exit must not sit inside a try/except that could skip it.
        for handler in ast.walk(fn):
            if isinstance(handler, ast.Try):
                for call in ast.walk(handler):
                    if (isinstance(call, ast.Call)
                            and isinstance(call.func, ast.Attribute)
                            and call.func.attr == "_exit"):
                        raise AssertionError(
                            "os._exit must not be inside a try block -- a "
                            "failure there would leave the container hanging"
                        )

    def test_restart_also_checkpoints(self, tree):
        """os.execv replaces the process image with the same open file
        descriptors and an un-checkpointed WAL."""
        fn = _function(tree, "restart_app")
        calls = _calls(fn)
        assert "shutdown_checkpoint" in calls
        assert "execv" in calls
        assert calls.index("shutdown_checkpoint") < calls.index("execv")


class TestScheduledJobsAreWrappers:
    @pytest.mark.parametrize("name,expected_call", [
        ("scheduled_db_health_check", "run_scheduled_health_check"),
        ("scheduled_db_backup", "backup_database"),
    ])
    def test_body_delegates_to_core(self, tree, name, expected_call):
        fn = _function(tree, name)
        assert expected_call in _calls(fn), (
            f"{name} must delegate to core/, not carry its own body"
        )
        # A wrapper is short. This is what stops the body creeping back in,
        # which is exactly how the reading-list sync ended up covering only
        # GitHub for so long.
        assert len(fn.body) <= 4, f"{name} looks like a body, not a wrapper"

    def test_jobs_are_registered(self, tree):
        fn = _function(tree, "start_background_services")
        source_ids = {
            n.value.value
            for n in ast.walk(fn)
            if isinstance(n, ast.keyword) and n.arg == "id"
            and isinstance(n.value, ast.Constant)
        }
        assert "db_health_check" in source_ids, (
            "the periodic integrity check is what closes the gap between a "
            "database breaking and anyone noticing"
        )
        assert "db_backup" in source_ids, (
            "startup-only backups mean a long-running container holds exactly "
            "one, taken before whatever went wrong"
        )


class TestIndexLogTellsTheTruth:
    def test_add_file_index_result_is_checked(self, tree):
        """add_file_index_entry swallows its own errors and returns False. The
        caller logged "Added file to index" regardless, so during the outage a
        comic was logged as indexed on the line straight after its insert
        failed with "database disk image is malformed"."""
        source = open(APP_PATH, encoding="utf-8").read()
        idx = source.index('app_logger.debug(f"Added file to index: {path}")')
        window = source[max(0, idx - 400):idx]
        assert "if add_file_index_entry(" in window, (
            "the success log must be conditional on the insert succeeding"
        )
