"""core/job_queue.py -- the one-worker FIFO behind File Manager metadata jobs."""
import threading
import time

import pytest

import core.app_state as app_state
from core.job_queue import JobQueue


@pytest.fixture(autouse=True)
def clean_registry():
    with app_state._operations_lock:
        app_state._operations.clear()
    yield
    with app_state._operations_lock:
        app_state._operations.clear()


def _wait_until(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def _status(op_id):
    op = app_state.get_operation(op_id)
    return op["status"] if op else None


class TestOrdering:

    def test_jobs_run_in_submission_order(self):
        q = JobQueue("t-order")
        ran = []
        ids = [q.submit("metadata", f"job {i}", 1, lambda op, i=i: ran.append(i))
               for i in range(5)]
        assert _wait_until(lambda: all(_status(i) == "completed" for i in ids))
        assert ran == [0, 1, 2, 3, 4]

    def test_only_one_job_runs_at_a_time(self):
        q = JobQueue("t-serial")
        active = []
        peak = []
        lock = threading.Lock()

        def job(op):
            with lock:
                active.append(op)
                peak.append(len(active))
            time.sleep(0.02)
            with lock:
                active.remove(op)

        ids = [q.submit("metadata", "j", 1, job) for _ in range(4)]
        assert _wait_until(lambda: all(_status(i) == "completed" for i in ids))
        assert max(peak) == 1


class TestQueuedState:

    def test_waiting_jobs_are_queued_with_their_position(self):
        q = JobQueue("t-pos")
        release = threading.Event()
        first = q.submit("metadata", "first", 1, lambda op: release.wait(5))
        assert _wait_until(lambda: _status(first) == "running")
        second = q.submit("metadata", "second", 1, lambda op: None)
        third = q.submit("metadata", "third", 1, lambda op: None)

        assert _status(second) == "queued"
        assert app_state.get_operation(second)["detail"] == "Queued: next up"
        assert app_state.get_operation(third)["detail"] == "Queued: 1 ahead"

        release.set()
        assert _wait_until(lambda: _status(third) == "completed")

    def test_a_queued_op_is_never_marked_stalled(self):
        op_id = app_state.register_operation("metadata", "waiting", status="queued")
        with app_state._operations_lock:
            app_state._operations[op_id]["updated_at"] -= app_state.STALE_TIMEOUT + 60
        app_state.get_active_operations()
        assert _status(op_id) == "queued"

    def test_start_operation_moves_queued_to_running(self):
        op_id = app_state.register_operation("metadata", "x", status="queued")
        app_state.start_operation(op_id)
        assert _status(op_id) == "running"


class TestFailures:

    def test_a_raising_job_errors_its_op_and_the_next_job_still_runs(self):
        q = JobQueue("t-raise")

        def boom(op):
            raise RuntimeError("provider down")

        bad = q.submit("metadata", "bad", 1, boom)
        good = q.submit("metadata", "good", 1, lambda op: None)
        assert _wait_until(lambda: _status(good) == "completed")
        assert _status(bad) == "error"

    def test_a_job_that_forgets_to_complete_is_completed_for_it(self):
        q = JobQueue("t-forget")
        op_id = q.submit("metadata", "x", 3,
                         lambda op: app_state.update_operation(op, current=1))
        assert _wait_until(lambda: _status(op_id) == "completed")

    def test_a_job_that_completes_itself_with_error_keeps_it(self):
        q = JobQueue("t-self")
        op_id = q.submit("metadata", "x", 1,
                         lambda op: app_state.complete_operation(op, error=True))
        assert _wait_until(lambda: _status(op_id) == "error")
        time.sleep(0.05)
        assert _status(op_id) == "error"
