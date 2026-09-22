"""The memory monitor must not narrate a problem it cannot fix.

A reported log carried 215 identical "High memory usage: 3012.2MB" WARNINGs --
one a minute for the whole capture -- interleaved with "Memory cleanup: freed
0.0MB, collected 63817 objects". The process sat at 3GB the entire time, so
every one of those lines said exactly what the first had said, and the log
window shrank to eight minutes as a result.

gc.collect() only reclaims reference cycles. It cannot return freed heap to the
OS and it cannot touch memory something still references, so a run that frees
0.0MB while collecting tens of thousands of objects is evidence that this is
not the tool for whatever is holding the memory.
"""
import logging

import pytest

from core.memory_utils import MemoryMonitor


@pytest.fixture
def monitor(monkeypatch):
    """A monitor whose RSS reading we control, with no psutil involved."""
    m = MemoryMonitor.__new__(MemoryMonitor)
    m.threshold_mb = 1500
    m.cleanup_threshold_mb = 1000
    m.monitoring = False
    m.monitor_thread = None
    m.process = None
    m._last_cleanup_time = 0
    m._min_cleanup_interval = 300
    m._high_since = None
    m._last_high_warning = 0
    m._ineffective_cleanups = 0
    m._gave_up_warned = False
    return m


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING]


class TestHighMemoryWarning:

    def test_the_first_crossing_warns(self, monitor, caplog):
        with caplog.at_level(logging.DEBUG):
            monitor._report_high_memory(3012.2)
        assert any("High memory usage" in m for m in _warnings(caplog))

    def test_it_does_not_repeat_every_poll(self, monitor, caplog):
        with caplog.at_level(logging.DEBUG):
            for _ in range(60):          # an hour of 60s polls
                monitor._report_high_memory(3012.2)
        assert len(_warnings(caplog)) == 1, \
            "one crossing, one warning -- not one per poll"

    def test_it_repeats_eventually(self, monitor, caplog):
        """Silence forever would be its own failure: a 3GB process an hour in
        is still worth a line."""
        with caplog.at_level(logging.DEBUG):
            monitor._report_high_memory(3012.2)
            monitor._last_high_warning -= monitor.HIGH_MEMORY_REPEAT_SECONDS + 1
            monitor._report_high_memory(3012.2)

        warnings = _warnings(caplog)
        assert len(warnings) == 2
        assert "still high" in warnings[1]

    def test_dropping_below_the_threshold_is_reported_once(self, monitor, caplog):
        monitor._report_high_memory(3012.2)
        with caplog.at_level(logging.DEBUG):
            monitor._report_high_memory(400.0)
            monitor._report_high_memory(400.0)
        recovered = [r for r in caplog.records if "back below" in r.getMessage()]
        assert len(recovered) == 1

    def test_crossing_again_warns_again(self, monitor, caplog):
        """The throttle is per episode, not for the life of the process."""
        monitor._report_high_memory(3012.2)
        monitor._report_high_memory(400.0)
        with caplog.at_level(logging.DEBUG):
            monitor._report_high_memory(3012.2)
        assert any("High memory usage" in m for m in _warnings(caplog))

    def test_normal_usage_says_nothing_at_warning_level(self, monitor, caplog):
        with caplog.at_level(logging.DEBUG):
            monitor._report_high_memory(400.0)
        assert _warnings(caplog) == []


class TestIneffectiveCleanup:

    def _cleanup(self, monitor, monkeypatch, freed):
        """Run force_cleanup with a controlled before/after reading."""
        readings = iter([2000.0, 2000.0 - freed])
        monkeypatch.setattr(monitor, "get_memory_usage", lambda: next(readings))
        monkeypatch.setattr("core.memory_utils.gc.collect", lambda: 63817)
        return monitor.force_cleanup()

    def test_a_cleanup_that_frees_nothing_is_not_announced(
            self, monitor, monkeypatch, caplog):
        with caplog.at_level(logging.INFO):
            self._cleanup(monitor, monkeypatch, freed=0.0)
        assert not [r for r in caplog.records
                    if r.levelno == logging.INFO and "Memory cleanup" in r.getMessage()], \
            "collecting 63817 objects and freeing nothing is not news"

    def test_a_cleanup_that_frees_memory_is_announced(
            self, monitor, monkeypatch, caplog):
        with caplog.at_level(logging.INFO):
            self._cleanup(monitor, monkeypatch, freed=50.0)
        assert any("Memory cleanup" in r.getMessage() for r in caplog.records)

    def test_repeated_ineffective_cleanups_stop_being_attempted(
            self, monitor, monkeypatch, caplog):
        with caplog.at_level(logging.DEBUG):
            for _ in range(monitor.INEFFECTIVE_CLEANUP_LIMIT):
                self._cleanup(monitor, monkeypatch, freed=0.0)

        monkeypatch.setattr(monitor, "get_memory_usage", lambda: 3012.2)
        assert monitor.should_cleanup() is False, \
            "stop running a collection that has demonstrably nothing to collect"

    def test_giving_up_is_said_once(self, monitor, monkeypatch, caplog):
        with caplog.at_level(logging.DEBUG):
            for _ in range(monitor.INEFFECTIVE_CLEANUP_LIMIT + 3):
                self._cleanup(monitor, monkeypatch, freed=0.0)

        gave_up = [m for m in _warnings(caplog) if "freed nothing" in m]
        assert len(gave_up) == 1, \
            "say it once; repeating it is the noise this replaces"

    def test_a_successful_cleanup_resets_the_count(
            self, monitor, monkeypatch, caplog):
        for _ in range(monitor.INEFFECTIVE_CLEANUP_LIMIT - 1):
            self._cleanup(monitor, monkeypatch, freed=0.0)
        self._cleanup(monitor, monkeypatch, freed=50.0)

        assert monitor._ineffective_cleanups == 0
        monkeypatch.setattr(monitor, "get_memory_usage", lambda: 3012.2)
        assert monitor.should_cleanup() is True

    def test_dropping_below_the_threshold_re_arms_cleanup(self, monitor, monkeypatch):
        """Usage falling and rising again is a new episode, and deserves a new
        attempt -- the old garbage may genuinely have been collectable."""
        monitor._ineffective_cleanups = monitor.INEFFECTIVE_CLEANUP_LIMIT
        monitor._gave_up_warned = True

        monitor._report_high_memory(400.0)

        assert monitor._ineffective_cleanups == 0
        monkeypatch.setattr(monitor, "get_memory_usage", lambda: 3012.2)
        assert monitor.should_cleanup() is True
