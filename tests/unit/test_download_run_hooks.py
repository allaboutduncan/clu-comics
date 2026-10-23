"""Where the download run reports (core/download_runs) are hooked in.

app.py and api.py cannot be imported in tests, so the placement of each hook is
asserted against the parsed source. Each one is a property of *where* a call
sits, and each has a way to go quietly wrong:

* a run that is not finished in the sweep's ``finally`` stays "Running" forever
  when the sweep raises;
* a dry run that opened a run would put simulations in the history;
* an item written *after* ``download_queue.put`` can lose the race with a
  worker that settles the download first, so its outcome is never recorded;
* a 'failed' outcome written before the cancel guard or the auto-retry gate
  would report cancels and transient failures as failed downloads.
"""

import ast
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _tree(rel):
    with open(os.path.join(PROJECT_ROOT, rel), encoding="utf-8") as f:
        return ast.parse(f.read())


def _func(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def _calls(node, dotted):
    """Every call in ``node`` whose callee unparses to ``dotted``."""
    return [
        n for n in ast.walk(node)
        if isinstance(n, ast.Call) and ast.unparse(n.func) == dotted
    ]


def _str_args(call):
    return [a.value for a in call.args if isinstance(a, ast.Constant)]


class TestGetComicsSweep:
    def setup_method(self):
        self.fn = _func(_tree("app.py"), "scheduled_getcomics_download")

    def _outer_try(self):
        return next(n for n in self.fn.body if isinstance(n, ast.Try))

    def test_run_is_finished_in_the_finally(self):
        final = self._outer_try().finalbody
        assert any(_calls(stmt, "run.finish") for stmt in final)

    def test_a_dry_run_opens_no_run(self):
        """start_run sits only under `if not dry_run:`."""
        starts = _calls(self.fn, "download_runs.start_run")
        assert len(starts) == 1
        guarded = [
            n for n in ast.walk(self.fn)
            if isinstance(n, ast.If) and ast.unparse(n.test) == "not dry_run"
            and any(starts[0] is c for s in n.body for c in ast.walk(s))
        ]
        assert guarded, "start_run must be guarded by `if not dry_run`"

    def test_default_recorder_is_the_null_one(self):
        assigns = [
            n for n in self.fn.body
            if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "run"
        ]
        assert assigns and ast.unparse(assigns[0].value) == "download_runs.NULL_RECORDER"

    def test_item_is_flushed_before_the_download_is_queued(self):
        puts = _calls(self.fn, "download_queue.put")
        assert len(puts) == 1
        put_line = puts[0].lineno
        flushes = [c.lineno for c in _calls(self.fn, "run.flush")]
        queued = [
            c.lineno for c in _calls(self.fn, "_record")
            if any(ast.unparse(a) == "download_runs.OUTCOME_QUEUED" for a in c.args)
        ]
        assert any(line < put_line for line in flushes)
        assert any(line < put_line for line in queued)

    def test_every_queue_site_records_a_queued_item(self):
        """Pre-source, GetComics and post-source fallback: the three sites
        that stamp _mark_queued are the three that queue something."""
        queued = [
            c for c in _calls(self.fn, "_record")
            if any(ast.unparse(a) == "download_runs.OUTCOME_QUEUED" for a in c.args)
        ]
        assert len(queued) == 3

    def test_a_fallback_hit_clears_the_getcomics_miss(self):
        """Otherwise an issue Usenet/DC++ found is also reported as not found."""
        clears = [
            n for n in ast.walk(self.fn)
            if isinstance(n, ast.Assign) and ast.unparse(n.targets[0]) == "gc_miss"
            and ast.unparse(n.value) == "None"
        ]
        # One reset per issue, one on a fallback hit.
        assert len(clears) == 2

    def test_per_issue_error_is_recorded(self):
        handlers = [
            h for n in ast.walk(self.fn) if isinstance(n, ast.Try)
            for h in n.handlers if h.name == "issue_err"
        ]
        assert handlers
        assert any(_calls(stmt, "_record") for stmt in handlers[0].body)


class TestWeeklyPacks:
    def test_item_is_flushed_before_the_pack_is_queued(self):
        fn = _func(_tree("app.py"), "_enqueue_weekly_pack")
        put_line = _calls(fn, "download_queue.put")[0].lineno
        assert any(c.lineno < put_line for c in _calls(fn, "run.item"))
        assert any(c.lineno < put_line for c in _calls(fn, "run.flush"))

    def test_run_is_finished_on_success_and_failure(self):
        fn = _func(_tree("app.py"), "scheduled_weekly_packs_download")
        outer = next(n for n in fn.body if isinstance(n, ast.Try))
        assert any(_calls(stmt, "run.finish") for stmt in outer.body)
        assert any(_calls(stmt, "run.finish") for h in outer.handlers for stmt in h.body)


class TestStartup:
    def test_interrupted_runs_are_reconciled_at_startup(self):
        tree = _tree("app.py")
        assert _calls(tree, "reconcile_interrupted_runs")


class TestApiSettlePoints:
    def setup_method(self):
        self.tree = _tree("api.py")
        self.fn = _func(self.tree, "process_download")

    def _outcome_lines(self, status):
        return [
            c.lineno for c in _calls(self.fn, "set_download_outcome")
            if status in _str_args(c)
        ]

    def test_failed_is_recorded_after_the_cancel_guard_and_retry_gate(self):
        (failed,) = self._outcome_lines("failed")
        retry_ifs = [
            n for n in self.fn.body
            if isinstance(n, ast.If) and "_schedule_auto_retry" in ast.unparse(n.test)
        ]
        cancel_ifs = [
            n for n in self.fn.body
            if isinstance(n, ast.If) and "is_cancel_requested" in ast.unparse(n.test)
        ]
        assert retry_ifs and cancel_ifs
        assert failed > retry_ifs[0].lineno
        assert failed > cancel_ifs[0].lineno

    def test_retry_is_recorded_inside_the_retry_gate(self):
        retry_if = next(
            n for n in self.fn.body
            if isinstance(n, ast.If) and "_schedule_auto_retry" in ast.unparse(n.test)
        )
        calls = [c for s in retry_if.body for c in _calls(s, "set_download_outcome")]
        assert calls and "retrying" in _str_args(calls[0])

    def test_complete_is_recorded(self):
        assert self._outcome_lines("complete")

    def test_cancels_are_recorded_where_every_cancel_goes(self):
        fn = _func(self.tree, "mark_cancelled")
        calls = _calls(fn, "set_download_outcome")
        assert calls and "cancelled" in _str_args(calls[0])


class TestManagedClientSettlePoints:
    """Both pollers record the outcome beside the notification, outside the lock."""

    def _check(self, rel):
        fn = _func(_tree(rel), "_set_status")
        calls = _calls(fn, "_record_run_outcome")
        assert calls, f"{rel} _set_status does not record the run outcome"
        locks = [n for n in ast.walk(fn) if isinstance(n, ast.With)]
        for lock in locks:
            assert not any(c is call for s in lock.body for c in ast.walk(s)
                           for call in calls)

    def test_usenet(self):
        self._check(os.path.join("models", "usenet.py"))

    def test_dcpp(self):
        self._check(os.path.join("models", "dcpp.py"))
