"""Keeps ``monitor.py`` running exactly while Folder Monitoring is switched on.

Folder Monitoring used to be decided once, at import, by ``MONITOR=yes`` in the
environment: ``app.py`` Popen'd ``monitor.py`` and blocked a thread in
``communicate()`` until it died. Turning it on or off meant editing the compose
file and recreating the container -- a non-starter for an onboarding flow that
sets WATCH and enables monitoring from the UI.

Now the ``monitor_enabled`` setting is the switch, and this module applies it
live. One daemon thread in the gunicorn worker polls the setting (it is cached
for ``core.settings.CACHE_TTL`` anyway, so polling faster buys nothing) and
starts or stops the child. ``poke()`` wakes it early after a save.

Responsibility is split with ``monitor.py`` on purpose:

- **The monitor stops itself** when the setting goes off, and exits when its
  parent changes (the gunicorn worker it belonged to was killed and respawned).
  The ``terminate()`` here is the fallback, not the mechanism -- an orphan has
  no supervisor left to terminate it.
- **The monitor enforces single-instance** with a lock in CONFIG_DIR, and exits
  with ``EXIT_ALREADY_RUNNING`` when the lock is held. That is not a crash: it
  is the expected answer while an orphan from a killed worker winds down, so it
  is retried on a short fixed delay and never escalates the crash backoff.
- **WATCH changes are the monitor's business** -- it reschedules its observer
  in place, so nothing here restarts it for a settings change.

Single-worker gunicorn is load-bearing here, as for the thumbnail in-flight
guard (see CLAUDE.md).
"""
import os
import subprocess
import sys
import threading
import time

from core.app_logging import app_logger

# monitor.py's exit code when another monitor already holds the lock.
EXIT_ALREADY_RUNNING = 75

POLL_SECONDS = 5.0
ALREADY_RUNNING_RETRY_SECONDS = 10.0
BACKOFF_START_SECONDS = 10.0
BACKOFF_MAX_SECONDS = 300.0
# A child that ran this long before dying was not crash-looping; start the
# backoff over.
HEALTHY_RUN_SECONDS = 60.0
STOP_GRACE_SECONDS = 5.0

_MONITOR_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "monitor.py"
)


def _enabled():
    """The setting, or None when it cannot be read.

    ``monitor_enabled()`` reads a failed lookup as the default (off), which is
    right for api.py's "who finishes this download" but wrong here: acting on
    it would stop the monitor on every transient "database is locked".
    """
    from core.settings import peek_setting
    value = peek_setting("MONITOR_ENABLED")
    return None if value is None else bool(value)


def _setting_on():
    from core.download_utils import monitor_enabled
    return monitor_enabled()


class MonitorSupervisor:
    def __init__(self, popen=subprocess.Popen, enabled=_enabled, clock=time.monotonic,
                 script=_MONITOR_SCRIPT):
        self._popen = popen
        self._enabled = enabled
        self._clock = clock
        self._script = script
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread = None
        self._proc = None
        self._started_at = None
        self._next_start_at = 0.0
        self._backoff = BACKOFF_START_SECONDS
        self._last_exit_code = None
        self._last_state = None  # last announced "running"/"stopped" (logged on change)

    # -- lifecycle -------------------------------------------------------

    def start(self):
        """Start the supervising thread (idempotent)."""
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stopping.clear()
            self._thread = threading.Thread(
                target=self._run, name="monitor-supervisor", daemon=True)
            self._thread.start()

    def stop(self):
        """Stop supervising and terminate the child. Used at shutdown/restart."""
        self._stopping.set()
        self._wake.set()
        self._stop_child("shutting down")

    def poke(self):
        """Re-check the setting now rather than at the next poll."""
        self._wake.set()

    def _run(self):
        while not self._stopping.is_set():
            try:
                self.tick()
            except Exception as e:  # never let the supervisor thread die
                app_logger.error(f"Folder monitor supervisor error: {e}")
            self._wake.wait(POLL_SECONDS)
            self._wake.clear()

    # -- one pass --------------------------------------------------------

    def tick(self):
        """Reconcile the child with the setting once. Returns the child or None."""
        with self._lock:
            self._reap()
            if self._stopping.is_set():
                return None
            enabled = self._enabled()
            if enabled is None:
                return self._proc  # unreadable: leave things as they are
            if not enabled:
                if self._proc is not None:
                    self._stop_child("Folder Monitoring was switched off")
                self._announce("stopped", "Folder Monitoring is off")
                return None
            if self._proc is None and self._clock() >= self._next_start_at:
                self._spawn()
            return self._proc

    def _spawn(self):
        try:
            # UTF-8 stdio: a pipe otherwise takes the locale's codec (cp1252 on
            # Windows dev boxes), and the first emoji the logger writes to
            # stderr turns into a "--- Logging error ---" traceback.
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            proc = self._popen(
                [sys.executable, self._script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
            )
        except Exception as e:
            app_logger.error(f"Could not start monitor.py: {e}")
            self._schedule_retry(crashed=True)
            return
        self._proc = proc
        self._started_at = self._clock()
        self._announce("running", f"Folder Monitoring started (monitor.py pid {proc.pid})")
        if getattr(proc, "stderr", None) is not None:
            threading.Thread(target=self._drain_stderr, args=(proc,),
                             name="monitor-stderr", daemon=True).start()

    @staticmethod
    def _drain_stderr(proc):
        """Forward the child's stderr (tracebacks) to app.log as it arrives.

        Replaces the old ``communicate()``, which held a thread until the child
        died and so made it impossible to poll or reap.
        """
        try:
            for line in proc.stderr:
                line = line.rstrip()
                if line:
                    app_logger.error(f"monitor.py: {line}")
        except Exception:
            pass

    def _reap(self):
        """Notice a child that has exited and decide when to try again."""
        proc = self._proc
        if proc is None:
            return
        code = proc.poll()
        if code is None:
            return
        self._proc = None
        self._last_exit_code = code
        if code == EXIT_ALREADY_RUNNING:
            # Another monitor holds the lock -- usually an orphan of a killed
            # worker that will notice its parent is gone within a second.
            app_logger.debug("monitor.py: another monitor is still running; retrying shortly")
            self._next_start_at = self._clock() + ALREADY_RUNNING_RETRY_SECONDS
            return
        if code == 0:
            # A clean exit: the monitor saw the setting go off. Restart at once
            # if it has since been switched back on.
            self._next_start_at = 0.0
            self._backoff = BACKOFF_START_SECONDS
            self._announce("stopped", "Folder Monitoring stopped")
            return
        ran_for = self._clock() - (self._started_at or self._clock())
        self._schedule_retry(crashed=True, ran_for=ran_for, code=code)

    def _schedule_retry(self, crashed, ran_for=0.0, code=None):
        if ran_for >= HEALTHY_RUN_SECONDS:
            self._backoff = BACKOFF_START_SECONDS
        delay = self._backoff
        self._next_start_at = self._clock() + delay
        self._backoff = min(self._backoff * 2, BACKOFF_MAX_SECONDS)
        if crashed:
            self._announce(
                f"crashed:{delay:.0f}",
                f"monitor.py exited unexpectedly (code {code}); restarting in {delay:.0f}s",
                warn=True,
            )

    def _stop_child(self, reason):
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._proc = None
                return
            app_logger.info(f"Stopping monitor.py: {reason}")
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    app_logger.warning("monitor.py did not stop in time; killing it")
                    proc.kill()
            except Exception as e:
                app_logger.warning(f"Could not stop monitor.py: {e}")
            self._last_exit_code = proc.poll()
            self._proc = None
            self._next_start_at = 0.0
            self._last_state = "stopped"

    def _announce(self, state, message, warn=False):
        """Log a state change once, not on every poll (CLAUDE.md, INFO budget)."""
        if state == self._last_state:
            return
        self._last_state = state
        (app_logger.warning if warn else app_logger.info)(message)

    # -- reporting -------------------------------------------------------

    def status(self):
        from core.config import get_watch_dir
        with self._lock:
            proc = self._proc
            running = proc is not None and proc.poll() is None
            return {
                "enabled": _setting_on(),
                "running": running,
                "pid": proc.pid if running else None,
                "watch": get_watch_dir() or "/downloads/temp",
                "last_exit_code": self._last_exit_code,
            }


_supervisor = MonitorSupervisor()


def start():
    _supervisor.start()


def stop():
    _supervisor.stop()


def poke():
    _supervisor.poke()


def status():
    return _supervisor.status()
