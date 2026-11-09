"""core/flaresolverr_supervisor.py: start on demand, stop when idle.

The child is a fake process and the readiness probe a flag, so these tests
never launch a browser. ``threaded=False`` runs the start inline. One test
drives the real thread to check that concurrent callers share one spawn.
"""
import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from core import flaresolverr_supervisor as sup


class FakeProc:
    def __init__(self, exit_code=None):
        self.returncode = exit_code
        self.pid = 4242
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.terminated = True
        self.returncode = -9


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class Harness:
    """A supervisor whose solver becomes ready ``ready_after`` probes after spawn."""

    def __init__(self, ready_after=1, exit_code=None, installed=True, threaded=False,
                 idle_seconds=600.0, start_timeout=30.0, spawn_error=None):
        self.clock = Clock()
        self.spawned = []
        self.ready = False
        self.ready_after = ready_after
        self.exit_code = exit_code
        self.spawn_error = spawn_error
        self._probes_since_spawn = 0
        self.sup = sup.FlareSolverrSupervisor(
            spawn=self._spawn, probe=self._probe, installed=lambda: installed,
            clock=self.clock, sleep=self.clock.sleep,
            idle_seconds=lambda: idle_seconds, start_timeout=start_timeout,
            threaded=threaded,
        )

    def _spawn(self):
        if self.spawn_error:
            raise self.spawn_error
        proc = FakeProc(self.exit_code)
        self.spawned.append(proc)
        self._probes_since_spawn = 0
        return proc

    def _probe(self, _url):
        if self.spawned and self.spawned[-1].poll() is None:
            self._probes_since_spawn += 1
            if self._probes_since_spawn >= self.ready_after:
                self.ready = True
        return self.ready


@pytest.fixture(autouse=True)
def _no_real_signals():
    # _terminate uses killpg on the fake pid; route it to the fake instead.
    def fake_terminate(proc):
        if proc is not None and proc.poll() is None:
            proc.terminate()
    with patch.object(sup, "_terminate", side_effect=fake_terminate):
        yield


class TestEnsureRunning:
    def test_not_installed_is_none_and_spawns_nothing(self):
        h = Harness(installed=False)
        assert h.sup.ensure_running() is None
        assert h.spawned == []

    def test_starts_on_first_call_and_returns_the_local_url(self):
        h = Harness(ready_after=3)
        assert h.sup.ensure_running() == "http://127.0.0.1:8191"
        assert len(h.spawned) == 1

    def test_reuses_a_running_solver(self):
        h = Harness()
        h.sup.ensure_running()
        h.sup.ensure_running()
        assert len(h.spawned) == 1

    def test_reuses_a_solver_already_on_the_port(self):
        # An orphan from a killed worker, or one run by hand.
        h = Harness()
        h.ready = True
        assert h.sup.ensure_running() == "http://127.0.0.1:8191"
        assert h.spawned == []

    def test_child_that_exits_during_startup_fails(self):
        h = Harness(exit_code=1)
        assert h.sup.ensure_running() is None

    def test_child_that_never_answers_is_terminated(self):
        h = Harness(ready_after=10**9, start_timeout=5.0)
        assert h.sup.ensure_running() is None
        assert h.spawned[0].terminated

    def test_launch_error_is_swallowed(self):
        h = Harness(spawn_error=OSError("no such file"))
        assert h.sup.ensure_running() is None


class TestBackoff:
    def test_a_failed_start_is_not_retried_immediately(self):
        h = Harness(exit_code=1)
        h.sup.ensure_running()
        h.sup.ensure_running()
        assert len(h.spawned) == 1

    def test_retried_after_the_backoff(self):
        h = Harness(exit_code=1)
        h.sup.ensure_running()
        h.clock.now += sup.BACKOFF_START_SECONDS + 1
        h.sup.ensure_running()
        assert len(h.spawned) == 2

    def test_backoff_doubles_and_is_capped(self):
        h = Harness(exit_code=1)
        for _ in range(12):
            h.sup.ensure_running()
            h.clock.now += sup.BACKOFF_MAX_SECONDS + 1
        assert h.sup._backoff == sup.BACKOFF_MAX_SECONDS

    def test_failure_is_announced_once(self):
        h = Harness(exit_code=1)
        with patch.object(sup.app_logger, "warning") as warn:
            for _ in range(3):
                h.sup.ensure_running()
                h.clock.now += sup.BACKOFF_MAX_SECONDS + 1
        assert warn.call_count == 1

    def test_success_resets_the_backoff(self):
        h = Harness(exit_code=1)
        h.sup.ensure_running()
        h.exit_code = None
        h.clock.now += sup.BACKOFF_START_SECONDS + 1
        assert h.sup.ensure_running() is not None
        assert h.sup._backoff == sup.BACKOFF_START_SECONDS
        assert h.sup._failing is False


class TestIdleAndStop:
    def test_idle_solver_is_stopped(self):
        h = Harness(idle_seconds=600.0)
        h.sup.ensure_running()
        proc = h.spawned[0]
        h.clock.now += 601
        with h.sup._cond:
            h.sup._reap_locked()
        assert proc.terminated
        assert h.sup.is_running() is False

    def test_recent_use_keeps_it_running(self):
        h = Harness(idle_seconds=600.0)
        h.sup.ensure_running()
        h.clock.now += 500
        h.sup.touch()
        h.clock.now += 500
        with h.sup._cond:
            h.sup._reap_locked()
        assert not h.spawned[0].terminated

    def test_dead_child_is_forgotten_and_restarted_on_demand(self):
        h = Harness()
        h.sup.ensure_running()
        h.spawned[0].returncode = 1
        h.ready = False
        with h.sup._cond:
            h.sup._reap_locked()
        assert h.sup.ensure_running() is not None
        assert len(h.spawned) == 2

    def test_stop_terminates_the_child(self):
        h = Harness()
        h.sup.ensure_running()
        h.sup.stop()
        assert h.spawned[0].terminated

    def test_module_stop_is_safe_before_any_start(self):
        with patch.object(sup, "_supervisor", None):
            sup.stop()  # must not raise or create a supervisor
            assert sup._supervisor is None


class TestThreaded:
    def test_concurrent_callers_share_one_spawn(self):
        # A real thread owns the child (PDEATHSIG is per-thread). Three
        # download workers asking at once must get one solver, not three.
        spawned = []
        ready = threading.Event()

        def spawn():
            spawned.append(FakeProc())
            threading.Timer(0.2, ready.set).start()
            return spawned[-1]

        s = sup.FlareSolverrSupervisor(
            spawn=spawn, probe=lambda _u: ready.is_set(), installed=lambda: True,
            sleep=lambda _s: time.sleep(0.02), start_timeout=5.0,
        )
        results = []
        workers = [threading.Thread(target=lambda: results.append(s.ensure_running(timeout=5)))
                   for _ in range(3)]
        try:
            for w in workers:
                w.start()
            for w in workers:
                w.join(10)
            assert len(spawned) == 1
            assert results == ["http://127.0.0.1:8191"] * 3
            assert s._thread is not None and s._thread.name == "flaresolverr-supervisor"
        finally:
            s.stop()


class TestInstall:
    def test_not_installed_off_linux(self):
        with patch.object(sup.sys, "platform", "win32"):
            assert sup.is_installed() is False

    def test_child_env_binds_localhost_only(self):
        env = sup._child_env()
        assert env["HOST"] == "127.0.0.1"
        assert env["PORT"] == str(sup.PORT)
        assert env["HEADLESS"] == "true"
        assert "VIRTUAL_ENV" not in env

    def test_home_is_a_private_mkdtemp_dir_reused_per_process(self):
        # A fixed /tmp name could be pre-created or symlinked by another user
        # (Bandit B108). mkdtemp is unique and 0700.
        with patch.object(sup, "_home_dir", None):
            first = sup._solver_home()
            assert os.path.isdir(first)
            assert os.path.basename(first).startswith("clu-flaresolverr-")
            assert os.path.basename(first) != "clu-flaresolverr"
            assert sup._solver_home() == first
            if os.name == "posix":
                assert (os.stat(first).st_mode & 0o777) == 0o700
        os.rmdir(first)

    def test_probe_reads_the_ready_message(self):
        resp = MagicMock()
        resp.json.return_value = {"msg": "FlareSolverr is ready!", "version": "3.5.2"}
        with patch.object(sup.requests, "get", return_value=resp) as get:
            assert sup.probe_ready("http://127.0.0.1:8191") is True
        assert get.call_args.args[0] == "http://127.0.0.1:8191/"

    def test_probe_failure_is_false(self):
        with patch.object(sup.requests, "get", side_effect=OSError("refused")):
            assert sup.probe_ready() is False

    def test_spawn_uses_the_pdeathsig_launcher_in_its_own_session(self):
        with patch.object(sup.subprocess, "Popen") as popen:
            sup._spawn()
        argv = popen.call_args.args[0]
        assert argv[0] == sup.PYTHON
        assert argv[1].endswith("flaresolverr_launch.py")
        assert argv[2] == str(sup.os.getpid())
        assert popen.call_args.kwargs["start_new_session"] is True
        assert "preexec_fn" not in popen.call_args.kwargs


class TestLauncher:
    def test_exits_when_parent_already_gone(self):
        from core import flaresolverr_launch as launch
        popen = MagicMock()
        with patch.object(launch.os, "getppid", return_value=1):
            assert launch.main(["x", "999999", "/opt/flaresolverr/src/flaresolverr.py"],
                               popen=popen) == 0
        popen.assert_not_called()

    def test_runs_the_script_as_a_child_under_its_own_interpreter(self):
        # A child, not an exec: the death signal reaches only this process,
        # which must then take Chromium and Xvfb down with it.
        from core import flaresolverr_launch as launch
        popen = MagicMock()
        popen.return_value.wait.return_value = 0
        with patch.object(launch.os, "getppid", return_value=4321), \
                patch.object(launch.signal, "signal"):
            assert launch.main(["x", "4321", "/opt/flaresolverr/src/flaresolverr.py"],
                               popen=popen) == 0
        argv = popen.call_args.args[0]
        assert argv[0] == launch.sys.executable
        assert argv[-1] == "/opt/flaresolverr/src/flaresolverr.py"

    def test_death_signal_kills_the_whole_group(self):
        from core import flaresolverr_launch as launch
        child = MagicMock()
        child.poll.return_value = 0
        with patch.object(launch.os, "killpg", create=True) as killpg, \
                patch.object(launch.signal, "signal"):
            launch._kill_group(child)
        sent = [c.args for c in killpg.call_args_list]
        assert (0, launch.signal.SIGTERM) in sent
        assert sent[-1][0] == 0  # the final SIGKILL is for the group too

    def test_bad_usage(self):
        from core import flaresolverr_launch as launch
        assert launch.main(["x"]) == 2

    def test_launcher_is_standard_library_only(self):
        # It runs under FlareSolverr's interpreter, where CLU's packages are absent.
        import ast
        src = open(launch_path(), encoding="utf-8").read()
        mods = {a.name.split(".")[0] for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module.split(".")[0] for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.ImportFrom) and n.module}
        assert mods <= {"ctypes", "os", "signal", "subprocess", "sys", "time"}


def launch_path():
    import os
    return os.path.join(os.path.dirname(sup.__file__), "flaresolverr_launch.py")
