"""Run FlareSolverr so that it, Chromium and Xvfb all die with CLU.

Run by ``core/flaresolverr_supervisor.py`` under **FlareSolverr's own
interpreter** (``/opt/flaresolverr-venv/bin/python``), never imported by CLU.
Standard library only, for that reason.

    python flaresolverr_launch.py <expected parent pid> <path to flaresolverr.py>

Things that look arbitrary and are not:

- **PR_SET_PDEATHSIG is set here, not with Popen(preexec_fn=...).** preexec_fn
  is not safe in a threaded parent, and the gunicorn worker has dozens of
  threads.
- **This process stays up as a reaper rather than exec'ing FlareSolverr.**
  The death signal reaches only the direct child. Exec'd, that was FlareSolverr
  itself, and a SIGKILLed worker (gunicorn's ``--timeout``) left its Xvfb
  running as an orphan -- verified in the image. Here the signal lands on this
  process, which then terminates its whole process group (the supervisor starts
  it in a new session, so the group is exactly FlareSolverr, Chromium and Xvfb).
- **The parent-pid check closes the window between fork and prctl.** If CLU
  died in that gap the signal would never come.
"""
import ctypes
import os
import signal
import subprocess
import sys
import time

PR_SET_PDEATHSIG = 1
GRACE_SECONDS = 5.0


def _kill_group(child):
    """SIGTERM the group (this process ignores it), then SIGKILL stragglers."""
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        os.killpg(0, signal.SIGTERM)
    except Exception:
        child.terminate()
    deadline = time.monotonic() + GRACE_SECONDS
    while child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    try:
        os.killpg(0, signal.SIGKILL)  # takes this process with it, which is the point
    except Exception:
        child.kill()


def main(argv, popen=subprocess.Popen):
    if len(argv) != 3:
        sys.stderr.write("usage: flaresolverr_launch.py <parent pid> <flaresolverr.py>\n")
        return 2
    expected_parent = int(argv[1])
    script = argv[2]
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except Exception as e:  # not Linux: run unguarded rather than not at all
        sys.stderr.write(f"flaresolverr_launch: prctl unavailable ({e})\n")
    if os.getppid() != expected_parent:
        return 0
    child = popen([sys.executable, "-u", script],
                  cwd=os.path.dirname(os.path.abspath(script)))

    def on_term(_signum, _frame):
        _kill_group(child)
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    return child.wait()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
