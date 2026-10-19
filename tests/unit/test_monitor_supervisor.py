"""core.monitor_supervisor: monitor.py runs exactly while the setting is on.

Popen and the clock are faked; nothing here starts a real process.
"""
import pytest

from core import monitor_supervisor as ms


class FakeProc:
    _next_pid = 1000

    def __init__(self):
        FakeProc._next_pid += 1
        self.pid = FakeProc._next_pid
        self.returncode = None
        self.stderr = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class Harness:
    def __init__(self):
        self.enabled = True
        self.now = 1000.0
        self.spawned = []
        self.sup = ms.MonitorSupervisor(
            popen=self._popen, enabled=lambda: self.enabled,
            clock=lambda: self.now, script="monitor.py",
        )

    def _popen(self, argv, **kwargs):
        assert argv[-1] == "monitor.py"
        proc = FakeProc()
        self.spawned.append(proc)
        return proc


@pytest.fixture
def h():
    return Harness()


def test_starts_when_enabled(h):
    proc = h.sup.tick()
    assert proc is h.spawned[0]
    assert len(h.spawned) == 1


def test_does_not_start_when_disabled(h):
    h.enabled = False
    assert h.sup.tick() is None
    assert h.spawned == []


def test_never_double_spawns(h):
    h.sup.tick()
    h.sup.tick()
    h.sup.tick()
    assert len(h.spawned) == 1


def test_stops_when_switched_off(h):
    proc = h.sup.tick()
    h.enabled = False
    h.sup.tick()
    assert proc.terminated
    assert h.sup.status()["running"] is False


def test_unreadable_setting_leaves_the_child_alone(h):
    """A transient read failure must not stop (or start) the monitor."""
    proc = h.sup.tick()
    h.enabled = None
    assert h.sup.tick() is proc
    assert not proc.terminated


def test_restarts_after_being_switched_back_on(h):
    h.sup.tick()
    h.enabled = False
    h.sup.tick()
    h.enabled = True
    h.sup.tick()
    assert len(h.spawned) == 2


def test_clean_exit_restarts_at_once_when_still_enabled(h):
    """Exit 0 is the monitor seeing the setting off; a cached read here may
    lag, so it is not treated as a crash."""
    h.sup.tick().returncode = 0
    h.sup.tick()
    assert len(h.spawned) == 2


def test_crash_backs_off_and_doubles(h):
    h.sup.tick().returncode = 1
    h.sup.tick()                       # reaps, schedules retry in 10s
    assert len(h.spawned) == 1
    h.now += ms.BACKOFF_START_SECONDS
    h.sup.tick().returncode = 1        # respawned, crashes at once
    h.sup.tick()
    h.now += ms.BACKOFF_START_SECONDS  # not enough: backoff doubled
    h.sup.tick()
    assert len(h.spawned) == 2
    h.now += ms.BACKOFF_START_SECONDS
    h.sup.tick()
    assert len(h.spawned) == 3


def test_backoff_is_capped(h):
    for _ in range(20):
        h.sup._schedule_retry(crashed=False)
    assert h.sup._backoff == ms.BACKOFF_MAX_SECONDS


def test_long_healthy_run_resets_backoff(h):
    h.sup._backoff = ms.BACKOFF_MAX_SECONDS
    proc = h.sup.tick()
    h.now += ms.HEALTHY_RUN_SECONDS + 1
    proc.returncode = 1
    h.sup.tick()
    assert h.sup._next_start_at == h.now + ms.BACKOFF_START_SECONDS


def test_already_running_is_a_short_retry_not_a_crash(h):
    h.sup._backoff = 40.0
    h.sup.tick().returncode = ms.EXIT_ALREADY_RUNNING
    h.sup.tick()
    assert h.sup._next_start_at == h.now + ms.ALREADY_RUNNING_RETRY_SECONDS
    assert h.sup._backoff == 40.0      # untouched


def test_stop_terminates_and_prevents_respawn(h):
    proc = h.sup.tick()
    h.sup.stop()
    assert proc.terminated
    h.sup.tick()
    assert len(h.spawned) == 1


def test_spawn_failure_is_retried_later(h):
    def boom(*a, **k):
        raise OSError("no python")
    h.sup._popen = boom
    assert h.sup.tick() is None
    assert h.sup._next_start_at > h.now


def test_status_reports_pid(h, monkeypatch):
    monkeypatch.setattr(ms, "_setting_on", lambda: True)
    monkeypatch.setattr("core.config.get_watch_dir", lambda: "/w")
    proc = h.sup.tick()
    s = h.sup.status()
    assert s == {"enabled": True, "running": True, "pid": proc.pid,
                 "watch": "/w", "last_exit_code": None}


def test_poke_wakes_the_loop(h):
    h.sup.poke()
    assert h.sup._wake.is_set()
