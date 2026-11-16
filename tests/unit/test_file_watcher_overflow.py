"""An inotify queue overflow is reported once and followed by an index sync (#732).

watchdog logs two WARNING lines per overflow on its own loggers and has no
public hook, so ``OverflowGate`` is a logging filter: it swallows those lines,
says it once per episode in app.log, and runs one recovery callback.
"""
import ast
import logging
import os
import threading
from unittest.mock import patch

import pytest

from core import file_watcher
from core.file_watcher import OVERFLOW_LOGGERS, OverflowGate

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LOST = "Inotify queue overflowed; filesystem events were lost"
RECONCILING = "Inotify queue overflowed; reconciling recursive watches below %r"


def _record(msg, *args):
    return logging.LogRecord("watchdog.observers.inotify_c", logging.WARNING,
                             __file__, 1, msg, args, None)


class _ManualTimer:
    """threading.Timer stand-in: records the delay, fires only when told to."""
    created = []

    def __init__(self, delay, fn):
        self.delay, self.fn, self.daemon = delay, fn, False
        _ManualTimer.created.append(self)

    def start(self):
        pass

    def fire(self):
        self.fn()


@pytest.fixture
def timers():
    _ManualTimer.created = []
    with patch.object(file_watcher.threading, "Timer", _ManualTimer):
        yield _ManualTimer.created


@pytest.fixture
def log():
    with patch.object(file_watcher, "app_logger") as mock_log:
        yield mock_log


def test_overflow_records_are_swallowed(timers, log):
    gate = OverflowGate()
    assert gate.filter(_record(LOST)) is False
    assert gate.filter(_record(RECONCILING, b"/data")) is False


def test_other_watchdog_records_pass_through(timers, log):
    gate = OverflowGate()
    assert gate.filter(_record("Unable to rebuild recursive inotify watches below %r", b"/data")) is True
    assert timers == []
    log.warning.assert_not_called()


def test_a_burst_is_one_warning_and_one_timer(timers, log):
    gate = OverflowGate()
    for _ in range(300):
        gate.filter(_record(LOST))
        gate.filter(_record(RECONCILING, b"/data"))
    assert len(timers) == 1
    assert log.warning.call_count == 1
    assert "max_queued_events" in log.warning.call_args.args[0]


def test_recovery_runs_once_with_the_overflow_count(timers, log):
    calls = []
    gate = OverflowGate()
    gate.set_recovery(lambda: calls.append(1))
    for _ in range(7):
        gate.filter(_record(LOST))
        gate.filter(_record(RECONCILING, b"/data"))  # not an overflow of its own
    timers[0].fire()
    assert calls == [1]
    assert "7 inotify queue overflow(s)" in log.warning.call_args.args[0]


def test_a_new_episode_after_recovery_starts_a_new_timer(timers, log):
    gate = OverflowGate(settle_seconds=60, min_interval=900)
    gate.set_recovery(lambda: None)
    gate.filter(_record(LOST))
    timers[0].fire()
    gate.filter(_record(LOST))
    assert len(timers) == 2
    # A sync walks every library root: the second one waits out the interval.
    assert 890 < timers[1].delay <= 900
    assert timers[0].delay == 60


def test_a_failing_recovery_is_logged_not_raised(timers, log):
    gate = OverflowGate()

    def boom():
        raise RuntimeError("scheduler is gone")

    gate.set_recovery(boom)
    gate.filter(_record(LOST))
    timers[0].fire()
    log.error.assert_called_once()
    # ...and the gate is usable again afterwards.
    gate.filter(_record(LOST))
    assert len(timers) == 2


def test_no_recovery_registered_still_reports(timers, log):
    gate = OverflowGate()
    gate.filter(_record(LOST))
    timers[0].fire()
    assert "no index sync is registered" in log.warning.call_args.args[0]


def test_the_timer_is_a_daemon(log):
    """A pending recovery must not keep the process alive at shutdown."""
    made = []
    real_timer = threading.Timer

    def spy(delay, fn):
        t = real_timer(delay, fn)
        made.append(t)
        return t

    with patch.object(file_watcher.threading, "Timer", spy):
        OverflowGate(settle_seconds=3600).filter(_record(LOST))
    try:
        assert made[0].daemon is True
    finally:
        made[0].cancel()


def test_install_is_idempotent_and_covers_both_loggers():
    gate = file_watcher._overflow_gate
    try:
        file_watcher.install_overflow_gate()
        file_watcher.install_overflow_gate()
        for name in OVERFLOW_LOGGERS:
            assert logging.getLogger(name).filters.count(gate) == 1
    finally:
        for name in OVERFLOW_LOGGERS:
            logging.getLogger(name).removeFilter(gate)


def test_the_gate_stops_a_real_watchdog_warning(timers, log):
    """End to end through the logging machinery, not just filter()."""
    seen = []

    class Capture(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    gate = file_watcher._overflow_gate
    handler = Capture()
    logger = logging.getLogger(OVERFLOW_LOGGERS[0])
    logger.addHandler(handler)
    try:
        file_watcher.install_overflow_gate()
        logger.warning(LOST)
        logger.warning("something else")
    finally:
        logger.removeHandler(handler)
        for name in OVERFLOW_LOGGERS:
            logging.getLogger(name).removeFilter(gate)
        with gate._lock:
            gate._count, gate._timer = 0, None
    assert seen == ["something else"]
    assert len(timers) == 1


def test_start_installs_the_gate(tmp_path):
    with patch.object(file_watcher, "Observer"), \
         patch.object(file_watcher, "install_overflow_gate") as install:
        assert file_watcher.FileWatcher(str(tmp_path)).start() is True
    install.assert_called_once()


def test_marker_matches_watchdogs_own_messages():
    """The gate keys on watchdog's log text. If an installed watchdog that has
    overflow handling words it differently, fail here rather than in the field."""
    import watchdog.observers
    src_dir = os.path.dirname(watchdog.observers.__file__)
    text = ""
    for name in ("inotify_c.py", "inotify.py"):
        with open(os.path.join(src_dir, name), encoding="utf-8") as fh:
            text += fh.read()
    if "on_queue_overflow" not in text:
        pytest.skip("this watchdog predates overflow reporting (< 7.1)")
    assert LOST in text
    assert file_watcher.OVERFLOW_MARKER in text


# --- app.py wiring (cannot be imported in tests, so read the AST) -----------

def _app_func(name):
    with open(os.path.join(PROJECT_ROOT, "app.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _calls(func):
    out = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            f = node.func
            out.append(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None))
    return out


def test_app_registers_the_recovery_before_starting_watchers():
    func = _app_func("start_file_watcher_background")
    first = func.body[0]
    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call)
    assert first.value.func.id == "set_overflow_recovery"
    assert first.value.args[0].id == "resync_index_after_watcher_overflow"


def test_app_recovery_hands_the_sync_to_the_scheduler():
    """The callback runs on the watcher's timer thread; the sync walks every
    library root and must not run there, nor replace the scheduled job."""
    func = _app_func("resync_index_after_watcher_overflow")
    assert _calls(func) == ["add_job"]
    call = next(n for n in ast.walk(func) if isinstance(n, ast.Call))
    assert call.args[0].id == "scheduled_file_index_rebuild"
    kwargs = {k.arg: k.value for k in call.keywords}
    assert kwargs["id"].value == "file_index_overflow_sync"
    assert kwargs["id"].value != "file_index_rebuild"
    assert kwargs["replace_existing"].value is True
