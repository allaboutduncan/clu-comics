"""monitor.py governs its own lifetime: one instance, stop when switched off,
stop when orphaned, and follow WATCH without a restart.

core.monitor_supervisor only starts the process and terminates it as a
fallback. An orphan from a killed gunicorn worker has no supervisor left, so the
monitor has to notice on its own -- two monitors on one WATCH race each other's
moves, which used to happen on every worker respawn.
"""
import pytest

import monitor
from core.db_lock import LOCK_BUSY, LOCK_HELD, try_hold_lock
from core.monitor_supervisor import EXIT_ALREADY_RUNNING


class TestSingleInstanceLock:
    def test_second_holder_is_refused(self, tmp_path):
        path = str(tmp_path / "monitor.lock")
        status, handle = try_hold_lock(path)
        assert status == LOCK_HELD
        try:
            assert try_hold_lock(path) == (LOCK_BUSY, None)
        finally:
            handle.close()

    def test_lock_is_free_again_once_released(self, tmp_path):
        path = str(tmp_path / "monitor.lock")
        _, handle = try_hold_lock(path)
        handle.close()
        status, again = try_hold_lock(path)
        assert status == LOCK_HELD
        again.close()

    def test_monitor_exits_with_already_running_code(self, tmp_path):
        path = str(tmp_path / "monitor.lock")
        _, held = try_hold_lock(path)
        try:
            with pytest.raises(SystemExit) as exc:
                monitor.acquire_single_instance_lock(path)
            assert exc.value.code == EXIT_ALREADY_RUNNING
        finally:
            held.close()

    def test_monitor_takes_a_free_lock(self, tmp_path):
        handle = monitor.acquire_single_instance_lock(str(tmp_path / "monitor.lock"))
        assert handle is not None
        handle.close()


class TestExitReason:
    def _peek(self, monkeypatch, value):
        monkeypatch.setattr("core.settings.peek_setting", lambda key: value)

    def test_keeps_running_while_enabled(self, monkeypatch):
        self._peek(monkeypatch, True)
        assert monitor.exit_reason(42, getppid=lambda: 42) is None

    def test_stops_when_switched_off(self, monkeypatch):
        self._peek(monkeypatch, False)
        assert "switched off" in monitor.exit_reason(42, getppid=lambda: 42)

    def test_unreadable_setting_is_not_a_reason_to_stop(self, monkeypatch):
        """A "database is locked" must not shut the monitor down."""
        self._peek(monkeypatch, None)
        assert monitor.exit_reason(42, getppid=lambda: 42) is None

    def test_stops_when_orphaned(self, monkeypatch):
        self._peek(monkeypatch, True)
        assert "exited" in monitor.exit_reason(42, getppid=lambda: 1)


class _Observer:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def unschedule_all(self):
        self.calls.append("unschedule_all")

    def schedule(self, handler, path, recursive):
        if self.fail:
            raise OSError("nope")
        self.calls.append(("schedule", path, recursive))


class _Handler:
    def __init__(self):
        self.reconciled = 0

    def reconcile_directory(self):
        self.reconciled += 1


class TestRescheduleOnWatchChange:
    def test_unchanged_does_nothing(self, monkeypatch, tmp_path):
        watch = str(tmp_path / "w")
        monkeypatch.setattr(monitor, "current_watch", lambda: (watch, True))
        obs, h = _Observer(), _Handler()
        assert monitor.reschedule_if_changed(obs, h, (watch, True)) == (watch, True)
        assert obs.calls == [] and h.reconciled == 0

    def test_new_watch_is_scheduled_and_drained(self, monkeypatch, tmp_path):
        new = str(tmp_path / "new-watch")
        monkeypatch.setattr(monitor, "current_watch", lambda: (new, False))
        obs, h = _Observer(), _Handler()
        result = monitor.reschedule_if_changed(obs, h, ("/old", False))
        assert result == (new, False)
        assert obs.calls == ["unschedule_all", ("schedule", new, False)]
        assert h.reconciled == 1
        assert (tmp_path / "new-watch").is_dir()

    def test_recursion_change_reschedules(self, monkeypatch, tmp_path):
        watch = str(tmp_path / "w")
        monkeypatch.setattr(monitor, "current_watch", lambda: (watch, True))
        obs, h = _Observer(), _Handler()
        assert monitor.reschedule_if_changed(obs, h, (watch, False)) == (watch, True)
        assert ("schedule", watch, True) in obs.calls

    def test_failure_keeps_the_old_schedule(self, monkeypatch, tmp_path):
        new = str(tmp_path / "new-watch")
        monkeypatch.setattr(monitor, "current_watch", lambda: (new, False))
        obs, h = _Observer(fail=True), _Handler()
        assert monitor.reschedule_if_changed(obs, h, ("/old", False)) == ("/old", False)
        assert h.reconciled == 0
