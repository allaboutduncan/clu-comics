"""The monitor's periodic cleanup and reconciliation follow Settings live.

monitor.py used to read AUTO_CLEANUP_ORPHAN_FILES, CLEANUP_INTERVAL_HOURS and
RECONCILE_INTERVAL_MINUTES into module constants at import and build its main
loop's schedule from them once, so a change on the settings page did nothing
until the monitor restarted. A stored cleanup interval of 0 also made the
"interval elapsed" test true on every one-second tick, so the cleanup walked
WATCH once a second.

The decision is IntervalSchedule.due(interval, now) with an injected clock, so
none of this sleeps.
"""
import ast
import logging
import os

import pytest

import monitor
from monitor import (
    IntervalSchedule,
    _UNKNOWN,
    cleanup_interval_seconds,
    read_periodic_intervals,
    reconcile_interval_seconds,
)

HOUR = 3600
MINUTE = 60


def _ticks(schedule, interval_for, start, seconds):
    """Run one tick per second; return the times the job was due."""
    runs = []
    for t in range(start, start + seconds):
        if schedule.due(interval_for(t), t):
            runs.append(t)
    return runs


class TestIntervalValues:
    @pytest.mark.parametrize("hours", [0, -1, -24, "0", "", "soon", None, 1.0 - 1, True])
    def test_cleanup_zero_negative_or_garbage_is_off(self, hours):
        assert cleanup_interval_seconds(True, hours) is None

    def test_cleanup_is_off_when_unticked(self):
        assert cleanup_interval_seconds(False, 6) is None

    def test_cleanup_hours(self):
        assert cleanup_interval_seconds(True, 6) == 6 * HOUR
        assert cleanup_interval_seconds(True, "2") == 2 * HOUR

    @pytest.mark.parametrize("minutes", [0, -5, "0", "", "later", None, False])
    def test_reconcile_zero_negative_or_garbage_is_off(self, minutes):
        assert reconcile_interval_seconds(minutes) is None

    def test_reconcile_minutes(self):
        assert reconcile_interval_seconds(5) == 5 * MINUTE
        assert reconcile_interval_seconds(1) == MINUTE


class TestNeverBusyLoops:
    @pytest.mark.parametrize("hours", [0, -3, "garbage"])
    def test_cleanup_interval_zero_never_runs(self, hours):
        s = IntervalSchedule("cleanup", HOUR, "hour", now=0)
        interval = cleanup_interval_seconds(True, hours)
        assert _ticks(s, lambda t: interval, 1, 600) == []

    @pytest.mark.parametrize("minutes", [0, -3, "garbage"])
    def test_reconcile_interval_zero_never_runs(self, minutes):
        s = IntervalSchedule("reconcile", MINUTE, "minute", now=0)
        interval = reconcile_interval_seconds(minutes)
        assert _ticks(s, lambda t: interval, 1, 600) == []

    def test_smallest_real_interval_runs_once_a_minute(self):
        s = IntervalSchedule("reconcile", MINUTE, "minute", now=0)
        runs = _ticks(s, lambda t: reconcile_interval_seconds(1), 1, 300)
        assert runs == [60, 120, 180, 240, 300]


class TestLiveChanges:
    def test_shortened_interval_is_used_on_the_next_tick(self):
        s = IntervalSchedule("cleanup", HOUR, "hour", now=0)
        assert not s.due(24 * HOUR, 10)
        # Two hours in, the user shortens the interval to one hour: it is
        # overdue, so it runs once now...
        assert s.due(HOUR, 2 * HOUR)
        # ...and then waits a full new interval, not a run per tick.
        runs = _ticks(s, lambda t: HOUR, 2 * HOUR + 1, HOUR + 5)
        assert runs == [3 * HOUR]

    def test_lengthened_interval_defers_the_next_run(self):
        s = IntervalSchedule("cleanup", HOUR, "hour", now=0)
        assert not s.due(HOUR, 30 * MINUTE)
        assert not s.due(6 * HOUR, HOUR)
        assert s.due(6 * HOUR, 6 * HOUR)

    def test_turning_cleanup_off_stops_it(self):
        s = IntervalSchedule("cleanup", HOUR, "hour", now=0)
        assert s.due(HOUR, HOUR)
        assert not s.due(None, 2 * HOUR)
        assert not s.due(None, 50 * HOUR)

    def test_turning_cleanup_on_waits_one_interval_not_a_burst(self):
        s = IntervalSchedule("cleanup", HOUR, "hour", now=0)
        s.due(None, 0)
        # Off for a day, then switched back on: the old last-run is a day
        # stale, but switching on starts the clock rather than firing at once.
        assert not s.due(HOUR, 24 * HOUR)
        assert not s.due(HOUR, 24 * HOUR + 1)
        assert s.due(HOUR, 25 * HOUR)

    def test_unreadable_settings_keep_the_interval_in_effect(self):
        s = IntervalSchedule("cleanup", HOUR, "hour", now=0)
        s.due(None, 0)
        # A "database is locked" must not read as the defaults (cleanup on).
        assert not s.due(_UNKNOWN, 10 * HOUR)
        s2 = IntervalSchedule("cleanup", HOUR, "hour", now=0)
        s2.due(HOUR, 0)
        assert s2.due(_UNKNOWN, HOUR)

    def test_unknown_before_anything_was_read_never_runs(self):
        s = IntervalSchedule("cleanup", HOUR, "hour", now=0)
        assert not s.due(_UNKNOWN, 100 * HOUR)

    def test_clock_going_backwards_does_not_stall(self):
        s = IntervalSchedule("reconcile", MINUTE, "minute", now=1000)
        assert not s.due(MINUTE, 10)
        assert s.due(MINUTE, 70)


class TestReadsSettingsEveryCall:
    """A change in the store is seen on the next read, with no restart."""

    def _store(self, monkeypatch, values):
        monkeypatch.setattr("core.settings.peek_setting", lambda key: values[key])

    def test_changes_are_seen_on_the_next_read(self, monkeypatch):
        values = {
            "AUTO_CLEANUP_ORPHAN_FILES": True,
            "CLEANUP_INTERVAL_HOURS": 1,
            "RECONCILE_INTERVAL_MINUTES": 5,
        }
        self._store(monkeypatch, values)
        assert read_periodic_intervals() == (HOUR, 5 * MINUTE)

        values["AUTO_CLEANUP_ORPHAN_FILES"] = False
        values["RECONCILE_INTERVAL_MINUTES"] = 0
        assert read_periodic_intervals() == (None, None)

        values["AUTO_CLEANUP_ORPHAN_FILES"] = True
        values["CLEANUP_INTERVAL_HOURS"] = 0
        values["RECONCILE_INTERVAL_MINUTES"] = 10
        assert read_periodic_intervals() == (None, 10 * MINUTE)

    def test_unreadable_store_is_unknown_not_default(self, monkeypatch):
        monkeypatch.setattr("core.settings.peek_setting", lambda key: None)
        assert read_periodic_intervals() == (_UNKNOWN, _UNKNOWN)

    def test_toggle_takes_effect_on_a_running_schedule(self, monkeypatch):
        values = {
            "AUTO_CLEANUP_ORPHAN_FILES": True,
            "CLEANUP_INTERVAL_HOURS": 1,
            "RECONCILE_INTERVAL_MINUTES": 5,
        }
        self._store(monkeypatch, values)
        s = IntervalSchedule("cleanup", HOUR, "hour", now=0)

        def tick(t):
            return s.due(read_periodic_intervals()[0], t)

        assert tick(HOUR)
        values["AUTO_CLEANUP_ORPHAN_FILES"] = False
        assert not tick(2 * HOUR)
        assert not tick(5 * HOUR)
        values["AUTO_CLEANUP_ORPHAN_FILES"] = True
        assert not tick(5 * HOUR + 1)
        assert tick(6 * HOUR + 1)


class TestLogsOnChangeOnly:
    def _lines(self, caplog):
        return [r.getMessage() for r in caplog.records if r.name == "monitor_logger"]

    def test_status_line_is_logged_once_per_change(self, caplog):
        caplog.set_level(logging.INFO, logger="monitor_logger")
        s = IntervalSchedule("Periodic orphan-file cleanup", HOUR, "hour", now=0)
        for t in range(0, 500):
            s.due(None, t)
        for t in range(500, 1000):
            s.due(2 * HOUR, t)
        for t in range(1000, 1500):
            s.due(_UNKNOWN, t)
        for t in range(1500, 2000):
            s.due(2 * HOUR, t)
        assert self._lines(caplog) == [
            "Periodic orphan-file cleanup: disabled",
            "Periodic orphan-file cleanup: every 2 hour(s)",
        ]


class TestNoSettingFrozenAtImport:
    """CLAUDE.md: never read a setting into a module constant."""

    READERS = {"get_setting", "get_bool", "get_int", "get_list", "peek_setting",
               "get_watch_dir", "get_target_dir"}

    def test_no_module_level_setting_reads(self):
        path = os.path.join(os.path.dirname(monitor.__file__), "monitor.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        offenders = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    fn = sub.func
                    name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
                    if name in self.READERS:
                        offenders.append((name, sub.lineno))
        assert offenders == []

    def test_main_loop_reads_intervals_per_tick(self):
        """The while-loop in main() must call read_periodic_intervals itself."""
        path = os.path.join(os.path.dirname(monitor.__file__), "monitor.py")
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
        loops = [n for n in ast.walk(main) if isinstance(n, ast.While)]
        assert loops
        called = {
            c.func.id for loop in loops for c in ast.walk(loop)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        }
        assert "read_periodic_intervals" in called
