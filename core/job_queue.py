"""A FIFO job queue with one worker, reporting through the operations registry.

Heavy per-file work (fetching metadata, rewriting CBZs) used to start one
unbounded thread per file. A 70-file drop meant ~70 threads in the single
gunicorn worker, all contending for the GIL, the one SQLite write lock and the
shared provider rate limiters -- the rest of the app slowed to a crawl, and
threads parked on the Metron limiter went quiet long enough for the registry
to mark them "stalled" while they were still working.

One worker, strictly first-in-first-out:

- **One worker is not a throughput loss.** The work is paced by Metron's and
  ComicVine's rate limits, which are process-wide; a second worker would wait
  on the same limiter. It is also the only writer of a folder's ``cvinfo`` at a
  time, which the per-file threads raced on.
- **A job owns its op.** ``submit`` registers it ``queued``; the worker flips it
  to ``running`` and the job reports progress with ``app_state.update_operation``.
  A job should update its op at least once per item -- that is the heartbeat
  that keeps a long batch clear of ``STALE_TIMEOUT``.
- **A job that raises or forgets to finish is completed for it**, so a bug in
  one job can neither kill the worker nor leave a row spinning forever.
"""

import queue
import threading

from core import app_state
from core.app_logging import app_logger


class JobQueue:
    def __init__(self, name):
        self.name = name
        self._queue = queue.Queue()
        self._waiting = []          # op_ids in submission order, not yet started
        self._lock = threading.Lock()
        self._worker = None

    def submit(self, op_type, label, total, fn, user_id=None):
        """Queue ``fn(op_id)`` behind everything already submitted. Returns op_id."""
        op_id = app_state.register_operation(
            op_type, label, total=total, user_id=user_id, status="queued")
        with self._lock:
            self._waiting.append(op_id)
            self._queue.put((op_id, fn))
            self._ensure_worker()
        self._refresh_positions()
        return op_id

    def pending_count(self):
        with self._lock:
            return len(self._waiting)

    def _ensure_worker(self):
        # Caller holds self._lock.
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._run, name=f"jobqueue-{self.name}", daemon=True)
            self._worker.start()

    def _refresh_positions(self):
        with self._lock:
            waiting = list(self._waiting)
        for ahead, op_id in enumerate(waiting):
            detail = "Queued: next up" if ahead == 0 else f"Queued: {ahead} ahead"
            app_state.update_operation(op_id, detail=detail)

    def _run(self):
        while True:
            op_id, fn = self._queue.get()
            with self._lock:
                if op_id in self._waiting:
                    self._waiting.remove(op_id)
            self._refresh_positions()
            app_state.start_operation(op_id)
            try:
                fn(op_id)
                op = app_state.get_operation(op_id)
                if op and op["status"] in ("running", "queued"):
                    app_state.complete_operation(op_id)
            except Exception:
                app_logger.exception(f"Job {op_id} in queue '{self.name}' failed")
                app_state.complete_operation(op_id, error=True)
            finally:
                self._queue.task_done()


# Moves' follow-on metadata work. Bulk metadata, Remove XML and batch rename
# are candidates to move onto this same queue.
file_jobs = JobQueue("file-jobs")
