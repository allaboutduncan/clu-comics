"""Clearing finished downloads from api.py's progress dict.

Two defects lived in /clear_downloads and /clear_failed_downloads:

* **Clear Completed could undo a cancel.** /cancel_download relabels a running
  download 'cancelled' at once, but the worker only sees the flag at its next
  chunk. Clearing in that window deleted the row and the flag with it, so
  ``is_cancel_requested`` read False and the worker finished the download the
  user had cancelled into WATCH.
* **The routes iterated the live dict** while three workers and the retry
  timers write it, which can raise "dictionary changed size during iteration".

api.py cannot be imported here, so the decision lives in core.download_utils
and the api.py wiring is asserted structurally at the bottom.
"""
import ast
import os

import pytest

from core.download_utils import (
    CANCELLABLE_STATUSES,
    CLEAR_COMPLETED_STATUSES,
    CLEAR_FAILED_STATUSES,
    RETRY_PENDING,
    WORKER_SETTLED,
    begin_retry_wait,
    clear_settled_downloads,
    count_active_downloads,
    is_cancel_requested,
    mark_cancelled,
    mark_worker_settled,
    reset_for_retry,
    set_error_status,
    should_auto_retry,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
API_PATH = os.path.join(PROJECT_ROOT, "api.py")


def _cancel(progress, download_id):
    """What /cancel_download does to the row (api.cancel_download)."""
    details = progress[download_id]
    details["cancelled"] = True
    if details.get("status") in CANCELLABLE_STATUSES:
        details["status"] = "cancelled"


def _clear_completed(progress):
    return clear_settled_downloads(progress, CLEAR_COMPLETED_STATUSES)


def _clear_failed(progress):
    return clear_settled_downloads(progress, CLEAR_FAILED_STATUSES)


class TestClearDoesNotUndoACancel:
    def test_a_cancelled_download_whose_worker_runs_is_kept(self):
        progress = {"dl-1": {"status": "in_progress", "progress": 40}}
        _cancel(progress, "dl-1")
        assert progress["dl-1"]["status"] == "cancelled"

        assert _clear_completed(progress) == 0
        assert "dl-1" in progress
        # The worker still sees the request at its next chunk.
        assert is_cancel_requested(progress, "dl-1") is True

    def test_the_worker_stops_instead_of_finishing(self, tmp_path):
        """Drive a chunk loop the way api.py's downloaders run one, with the
        user cancelling and then clicking Clear Completed between chunks."""
        progress = {"dl-1": {"status": "in_progress", "progress": 0}}
        partial = tmp_path / "comic.cbz.0.crdownload"
        written = []

        def user_cancels_then_clears():
            _cancel(progress, "dl-1")
            _clear_completed(progress)

        with open(partial, "wb") as fh:
            for i in range(5):
                if is_cancel_requested(progress, "dl-1"):
                    break
                fh.write(b"x")
                written.append(i)
                if i == 1:
                    user_cancels_then_clears()
        if is_cancel_requested(progress, "dl-1"):
            mark_cancelled(progress, "dl-1", (str(partial),))
            outcome = "cancelled"
        else:
            outcome = "complete"
        mark_worker_settled(progress, "dl-1")  # api.worker's finally

        assert outcome == "cancelled"
        assert written == [0, 1]
        assert not partial.exists()
        # Settled now, so the next Clear Completed removes it as before.
        assert _clear_completed(progress) == 1
        assert progress == {}

    def test_a_cancelled_download_still_on_the_queue_is_kept(self):
        """Cancelled before a worker claimed it: the worker must still find
        the flag when it gets there, or it starts the download."""
        progress = {"dl-1": {"status": "queued"}}
        _cancel(progress, "dl-1")
        assert _clear_completed(progress) == 0
        assert is_cancel_requested(progress, "dl-1") is True

    def test_a_cancel_during_the_retry_backoff_clears_at_once(self):
        """No worker holds a download waiting out its backoff, and the timer
        drops a row that has gone -- so there is nothing to wait for."""
        progress = {"dl-1": {"status": "in_progress"}}
        set_error_status(progress, "dl-1", "reset")
        begin_retry_wait(progress, "dl-1", 0, 60, "reset")
        mark_worker_settled(progress, "dl-1")
        assert progress["dl-1"]["status"] == RETRY_PENDING

        _cancel(progress, "dl-1")
        assert _clear_completed(progress) == 1
        # What _fire_retry and _schedule_auto_retry check: nothing to resurrect.
        assert progress.get("dl-1") is None
        assert should_auto_retry(progress, "dl-1", 1) is False

    def test_a_failed_download_mid_failover_is_kept(self):
        """A provider sets 'error' before re-raising, and process_download
        then tries the next mirror; Clear Failed must not pull the row away."""
        progress = {"dl-1": {"status": "in_progress"}}
        set_error_status(progress, "dl-1", "mirror 1 died")
        assert _clear_failed(progress) == 0
        mark_worker_settled(progress, "dl-1")
        assert _clear_failed(progress) == 1


class TestFinishedDownloadsStillClear:
    def test_completed_and_cancelled_rows_clear(self):
        progress = {
            "done": {"status": "complete", WORKER_SETTLED: True},
            "stopped": {"status": "cancelled", "cancelled": True, WORKER_SETTLED: True},
            "failed": {"status": "error", WORKER_SETTLED: True},
            "running": {"status": "in_progress"},
            "waiting": {"status": "queued"},
            "retrying": {"status": RETRY_PENDING, WORKER_SETTLED: True},
        }
        assert _clear_completed(progress) == 2
        assert set(progress) == {"failed", "running", "waiting", "retrying"}

    def test_failed_rows_clear(self):
        progress = {
            "done": {"status": "complete", WORKER_SETTLED: True},
            "failed": {"status": "error", WORKER_SETTLED: True},
            "retrying": {"status": RETRY_PENDING, WORKER_SETTLED: True},
        }
        assert _clear_failed(progress) == 1
        assert set(progress) == {"done", "retrying"}

    def test_a_cancel_on_a_finished_row_does_not_pin_it(self):
        """Cancel clicked after the download completed: the label stays
        'complete' and the row clears normally."""
        progress = {"dl-1": {"status": "complete", WORKER_SETTLED: True}}
        _cancel(progress, "dl-1")
        assert _clear_completed(progress) == 1

    def test_junk_is_tolerated(self):
        assert clear_settled_downloads(None, CLEAR_COMPLETED_STATUSES) == 0
        progress = {"junk": "not a dict", "n": None}
        assert _clear_completed(progress) == 0
        assert set(progress) == {"junk", "n"}


class _MutatingEntry(dict):
    """A row whose status read makes a 'worker' add a download to the dict.

    Reading a row is the moment a live iteration is exposed to a concurrent
    writer, so this reproduces the race deterministically.
    """

    def __init__(self, progress, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._progress = progress
        self._n = 0

    def get(self, key, default=None):
        if key == "status":
            self._n += 1
            self._progress[f"new-{id(self)}-{self._n}"] = {"status": "queued"}
        return super().get(key, default)


class TestConcurrentWritesNeverRaise:
    def _progress(self):
        progress = {}
        progress["a"] = _MutatingEntry(progress, status="complete", **{WORKER_SETTLED: True})
        progress["b"] = _MutatingEntry(progress, status="error", **{WORKER_SETTLED: True})
        progress["c"] = {"status": "cancelled", WORKER_SETTLED: True}
        return progress

    def test_a_live_iteration_would_raise(self):
        """Guard on the test itself: the old route shape fails here."""
        progress = self._progress()
        with pytest.raises(RuntimeError, match="changed size"):
            [k for k, d in progress.items() if d.get("status") == "complete"]

    def test_clear_completed_takes_a_snapshot(self):
        progress = self._progress()
        assert _clear_completed(progress) == 2
        assert "a" not in progress and "c" not in progress
        assert "b" in progress

    def test_clear_failed_takes_a_snapshot(self):
        progress = self._progress()
        assert _clear_failed(progress) == 1
        assert "b" not in progress

    def test_a_row_replaced_mid_clear_is_left_alone(self):
        """Removal re-checks the row: if it changed since the snapshot (a
        retry put it back on the queue), it is not removed."""

        class Requeued(dict):
            def get(self, key, default=None):
                value = super().get(key, default)
                if key == "status":
                    progress["x"] = {"status": "queued"}
                return value

        progress = {}
        progress["x"] = Requeued(status="complete", **{WORKER_SETTLED: True})
        assert _clear_completed(progress) == 0
        assert progress["x"] == {"status": "queued"}

    def test_active_count_takes_a_snapshot(self):
        progress = {}
        progress["a"] = _MutatingEntry(progress, status="queued")
        progress["b"] = _MutatingEntry(progress, status="in_progress")
        assert count_active_downloads(progress) == 2


class TestMarkWorkerSettled:
    def test_marks_a_finished_row(self):
        progress = {"dl-1": {"status": "complete"}}
        mark_worker_settled(progress, "dl-1")
        assert progress["dl-1"][WORKER_SETTLED] is True

    def test_never_recreates_a_row(self):
        progress = {}
        mark_worker_settled(progress, "gone")
        mark_worker_settled(None, "gone")
        assert progress == {}

    @pytest.mark.parametrize("status", ["queued", "in_progress"])
    def test_leaves_a_row_another_worker_now_holds(self, status):
        """A hand-driven retry can re-queue the row as this worker returns."""
        progress = {"dl-1": {"status": status}}
        mark_worker_settled(progress, "dl-1")
        assert not progress["dl-1"].get(WORKER_SETTLED)

    def test_a_retry_unsettles_the_row(self):
        progress = {"dl-1": {"status": "error", WORKER_SETTLED: True}}
        reset_for_retry(progress, "dl-1")
        assert progress["dl-1"][WORKER_SETTLED] is False
        assert _clear_failed(progress) == 0


# ---------------------------------------------------------------------------
# api.py wiring (structural: api.py cannot be imported by the suite)
# ---------------------------------------------------------------------------

def _api_tree():
    with open(API_PATH, encoding="utf-8") as fh:
        return ast.parse(fh.read())


def _api_function(name):
    for node in ast.walk(_api_tree()):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"{name} not found in api.py")


def _calls(node):
    return {
        n.func.id for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }


class TestApiWiring:
    @pytest.mark.parametrize("route, statuses", [
        ("clear_downloads", "CLEAR_COMPLETED_STATUSES"),
        ("clear_failed_downloads", "CLEAR_FAILED_STATUSES"),
    ])
    def test_clear_routes_delegate(self, route, statuses):
        fn = _api_function(route)
        assert "clear_settled_downloads" in _calls(fn)
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        assert statuses in names
        # No hand-rolled iteration or deletion left in the route.
        assert not any(isinstance(n, (ast.Delete, ast.comprehension)) for n in ast.walk(fn))

    def test_worker_settles_in_its_finally(self):
        fn = _api_function("worker")
        tries = [n for n in ast.walk(fn) if isinstance(n, ast.Try)]
        assert tries, "worker() lost its try/finally"
        finally_calls = set()
        for t in tries:
            for stmt in t.finalbody:
                finally_calls |= _calls(stmt)
        assert "mark_worker_settled" in finally_calls

    def test_a_retry_that_could_not_be_requeued_is_settled(self):
        fn = _api_function("_fire_retry")
        handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers]
        assert any("mark_worker_settled" in _calls(h) for h in handlers)
