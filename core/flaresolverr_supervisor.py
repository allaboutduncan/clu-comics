"""Runs the bundled FlareSolverr only while something needs it.

FlareSolverr (a real Chromium driven through undetected-chromedriver) ships in
the image so that a Cloudflare "Just a moment..." challenge on a GetComics
mirror can be passed without the user installing a second container. It costs
200-500 MB of RAM while a browser is up, and most installs hit a challenge
rarely, so it is **started on the first challenge and stopped after an idle
window** rather than at boot. Starting it at import would also put a browser
launch on gunicorn's import thread, inside its ``--timeout 120`` budget.

Things that look arbitrary and are not:

- **One long-lived thread owns the child.** ``PR_SET_PDEATHSIG`` (set by
  ``core/flaresolverr_launch.py``) fires when the *thread* that forked the child
  exits, not the process. Forking from whichever download worker happened to
  see the challenge would tie the solver's life to that thread. So callers ask
  this thread to start it, and wait.
- **PDEATHSIG is what stops an orphan.** A gunicorn worker killed on
  ``--timeout`` leaves no supervisor behind to ``terminate()`` anything -- the
  same hazard ``monitor.py``'s parent-pid check covers for the folder monitor.
  The child is also its own process group, so ``stop()`` takes Chromium and
  Xvfb down with it.
- **A server already answering on the port is reused.** That is the normal
  state while an orphan from a killed worker winds down, and it is also how a
  developer can point CLU at a FlareSolverr run by hand.
- **A failed start backs off.** Without it every challenged download would pay
  a full browser start that is going to fail again. Announced on the
  transition, per the logging budget in CLAUDE.md.
- **Not installed means "not available", never an error.** Local runs on
  Windows, and any image built without it, behave exactly as before.

Single-worker gunicorn is load-bearing here, as for the monitor supervisor.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request

from core.app_logging import app_logger

INSTALL_DIR = os.environ.get("FLARESOLVERR_HOME", "/opt/flaresolverr")
PYTHON = os.environ.get("FLARESOLVERR_PYTHON", "/opt/flaresolverr-venv/bin/python")
HOST = "127.0.0.1"
PORT = int(os.environ.get("FLARESOLVERR_PORT", "8191"))
BASE_URL = f"http://{HOST}:{PORT}"

READY_MESSAGE = "FlareSolverr is ready!"
START_TIMEOUT_SECONDS = 60.0
READY_POLL_SECONDS = 1.0
IDLE_CHECK_SECONDS = 30.0
STOP_GRACE_SECONDS = 5.0
BACKOFF_START_SECONDS = 60.0
BACKOFF_MAX_SECONDS = 900.0
DEFAULT_IDLE_MINUTES = 10

_LAUNCHER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "flaresolverr_launch.py")


def _script():
    return os.path.join(INSTALL_DIR, "src", "flaresolverr.py")


def is_installed():
    """True when the bundled solver is present (the Docker image, Linux only)."""
    return (sys.platform.startswith("linux")
            and os.path.isfile(_script())
            and os.path.isfile(PYTHON))


def probe_ready(base_url=BASE_URL, timeout=2.0):
    """True when a FlareSolverr on *base_url* answers its index as ready."""
    try:
        with urllib.request.urlopen(base_url + "/", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        return READY_MESSAGE.lower() in str(data.get("msg", "")).lower()
    except Exception:
        return False


def _idle_seconds():
    try:
        from core.settings import get_int
        minutes = get_int("FLARESOLVERR_IDLE_MINUTES")
    except Exception:
        minutes = DEFAULT_IDLE_MINUTES
    return max(1, minutes) * 60.0


def _child_env():
    env = dict(os.environ)
    # Chromium and undetected-chromedriver write a profile and a data dir under
    # HOME. As PUID there may be no writable home at all; /tmp always is.
    home = os.path.join("/tmp", "clu-flaresolverr")
    try:
        os.makedirs(home, exist_ok=True)
    except OSError:
        pass
    env.update({
        "HOME": home,
        "HOST": HOST,
        "PORT": str(PORT),
        "HEADLESS": "true",
        "LOG_LEVEL": env.get("FLARESOLVERR_LOG_LEVEL", "info"),
        "PYTHONUNBUFFERED": "1",
    })
    # CLU's venv must not leak into FlareSolverr's interpreter.
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONPATH", None)
    return env


def _spawn():
    """Start the solver through the PDEATHSIG launcher, in its own group."""
    return subprocess.Popen(
        [PYTHON, _LAUNCHER, str(os.getpid()), _script()],
        cwd=os.path.dirname(_script()),
        env=_child_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


class FlareSolverrSupervisor:
    """Start on demand, stop when idle. See the module docstring."""

    def __init__(self, spawn=_spawn, probe=probe_ready, installed=is_installed,
                 clock=time.monotonic, sleep=time.sleep, idle_seconds=_idle_seconds,
                 start_timeout=START_TIMEOUT_SECONDS, threaded=True):
        self._spawn = spawn
        self._probe = probe
        self._installed = installed
        self._clock = clock
        self._sleep = sleep
        self._idle_seconds = idle_seconds
        self._start_timeout = start_timeout
        self._threaded = threaded

        self._cond = threading.Condition()
        self._thread = None
        self._proc = None
        self._start_requested = False
        self._start_generation = 0   # bumped once per finished start attempt
        self._stopping = False
        self._last_used = 0.0
        self._retry_after = 0.0
        self._backoff = BACKOFF_START_SECONDS
        self._failing = False
        self._started_ok = False

    # -- public -----------------------------------------------------------
    def base_url(self):
        return BASE_URL

    def ensure_running(self, timeout=None):
        """Return the solver's base URL once it is ready, or None.

        None means unavailable right now: not installed, backing off after a
        failed start, or it did not become ready in time. Never raises.
        """
        try:
            if not self._installed():
                return None
            self.touch()
            if self._probe(BASE_URL):
                return BASE_URL
            with self._cond:
                if self._clock() < self._retry_after:
                    return None
                if not self._threaded:
                    self._start_locked()
                    return BASE_URL if self._started_ok else None
                self._ensure_thread_locked()
                generation = self._start_generation
                self._start_requested = True
                self._cond.notify_all()
                deadline = self._clock() + (timeout or self._start_timeout + 5.0)
                while self._start_generation == generation and not self._stopping:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        break
                    self._cond.wait(min(remaining, 1.0))
                ready = self._start_generation != generation and self._started_ok
            return BASE_URL if ready else None
        except Exception as e:
            app_logger.warning(f"FlareSolverr: could not start the solver: {e}")
            return None

    def touch(self):
        """Record use, so the idle reaper leaves a busy solver alone."""
        with self._cond:
            self._last_used = self._clock()

    def is_running(self):
        with self._cond:
            return self._proc is not None and self._proc.poll() is None

    def stop(self):
        """Stop the solver and the supervising thread (shutdown / restart)."""
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
            proc, self._proc = self._proc, None
        _terminate(proc)

    # -- internals --------------------------------------------------------
    def _ensure_thread_locked(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping = False
        self._thread = threading.Thread(target=self._run, name="flaresolverr-supervisor",
                                        daemon=True)
        self._thread.start()

    def _run(self):
        while True:
            with self._cond:
                if self._stopping:
                    return
                if not self._start_requested:
                    self._cond.wait(IDLE_CHECK_SECONDS)
                if self._stopping:
                    return
                if self._start_requested:
                    self._start_requested = False
                    self._start_locked()
                    self._start_generation += 1
                    self._cond.notify_all()
                    continue
                self._reap_locked()

    def _reap_locked(self):
        """Drop a dead child, stop an idle one. Called with the lock held."""
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is not None:
            app_logger.info(f"FlareSolverr exited (code {proc.returncode})")
            self._proc = None
            return
        idle = self._clock() - self._last_used
        if idle >= self._idle_seconds():
            app_logger.info(f"FlareSolverr idle for {int(idle // 60)} min, stopping it")
            self._proc = None
            _terminate(proc)

    def _start_locked(self):
        """Spawn the child and wait for it to answer. Called with the lock held.

        Holding the lock across the wait is deliberate: every other caller is
        waiting on this very start, and a second spawn would only race it for
        the port. ``_started_ok`` carries the verdict to the waiting callers.
        """
        self._started_ok = False
        if self._proc is not None and self._proc.poll() is None:
            if self._probe(BASE_URL):
                self._started_ok = True
                return
            # Alive but not answering: replace it.
            _terminate(self._proc)
            self._proc = None
        if self._probe(BASE_URL):
            # Someone else's (an orphan winding down, or one run by hand).
            self._started_ok = True
            return
        started = self._clock()
        try:
            proc = self._spawn()
        except Exception as e:
            self._note_failure(f"could not launch: {e}")
            return
        while self._clock() - started < self._start_timeout:
            if proc.poll() is not None:
                self._note_failure(f"exited during startup (code {proc.returncode})")
                return
            if self._probe(BASE_URL):
                self._proc = proc
                self._last_used = self._clock()
                self._started_ok = True
                self._note_success(self._clock() - started)
                return
            self._sleep(READY_POLL_SECONDS)
        _terminate(proc)
        self._note_failure(f"not ready after {int(self._start_timeout)}s")

    def _note_failure(self, reason):
        self._retry_after = self._clock() + self._backoff
        if not self._failing:
            app_logger.warning(
                f"FlareSolverr {reason}; Cloudflare-protected downloads fall back to "
                f"manual. Retrying in {int(self._backoff)}s"
            )
        else:
            app_logger.debug(f"FlareSolverr still failing: {reason}")
        self._failing = True
        self._backoff = min(self._backoff * 2, BACKOFF_MAX_SECONDS)

    def _note_success(self, elapsed):
        app_logger.info(f"FlareSolverr started in {elapsed:.1f}s")
        self._failing = False
        self._retry_after = 0.0
        self._backoff = BACKOFF_START_SECONDS


def _terminate(proc):
    """SIGTERM the child's whole group (Chromium, Xvfb), then SIGKILL."""
    if proc is None or proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = None
    try:
        if pgid is not None and hasattr(os, "killpg"):
            os.killpg(pgid, signal.SIGTERM)
        else:
            proc.terminate()
        proc.wait(timeout=STOP_GRACE_SECONDS)
    except Exception:
        try:
            if pgid is not None and hasattr(os, "killpg"):
                os.killpg(pgid, signal.SIGKILL)
            else:
                proc.kill()
        except Exception:
            pass


_supervisor = None
_supervisor_lock = threading.Lock()


def get_supervisor():
    global _supervisor
    with _supervisor_lock:
        if _supervisor is None:
            _supervisor = FlareSolverrSupervisor()
        return _supervisor


def ensure_running(timeout=None):
    return get_supervisor().ensure_running(timeout)


def touch():
    if _supervisor is not None:
        _supervisor.touch()


def stop():
    """Stop the solver if one was ever started. Safe to call at any time."""
    if _supervisor is not None:
        _supervisor.stop()
