"""An in-memory task-status dict that forgets finished tasks.

``routes/reading_lists.py`` keeps the state of every background import, sync
and re-match in a module-level dict that the page polls. Nothing ever removed
an entry, so the dict grew for the life of the process. ``TaskRegistry`` is a
drop-in ``dict`` (workers keep writing ``tasks[task_id]['status'] = ...``) that
drops a finished task once it has gone ``retention_seconds`` without being
polled.

Only a task whose ``status`` is ``complete`` or ``error`` is ever dropped. A
running task is never touched, because its worker still writes into its entry
and would fail with a ``KeyError`` if the entry vanished. The retention clock
starts when a sweep first sees the task finished and restarts on every poll
(:meth:`touch`), so a client still polling a finished task keeps it.

Pruning runs whenever a task is added, so the dict is bounded by the tasks
started in one retention window plus the ones still running.
"""

import threading
import time

FINISHED_STATUSES = frozenset({"complete", "error"})
DEFAULT_RETENTION_SECONDS = 3600


class TaskRegistry(dict):
    def __init__(self, retention_seconds=DEFAULT_RETENTION_SECONDS, clock=time.monotonic):
        super().__init__()
        self._retention = retention_seconds
        self._clock = clock
        self._lock = threading.Lock()
        # task_id -> when a sweep first saw it finished, or when it was last polled.
        self._finished_at = {}

    def __setitem__(self, key, value):
        with self._lock:
            self._prune_locked()
            super().__setitem__(key, value)
            self._finished_at.pop(key, None)

    def __delitem__(self, key):
        with self._lock:
            super().__delitem__(key)
            self._finished_at.pop(key, None)

    def touch(self, key):
        """Record a poll of *key*, restarting its retention if it is finished."""
        with self._lock:
            if key in self._finished_at:
                self._finished_at[key] = self._clock()

    def prune(self):
        with self._lock:
            self._prune_locked()

    def _prune_locked(self):
        now = self._clock()
        for key, task in list(self.items()):
            status = task.get("status") if isinstance(task, dict) else None
            if status not in FINISHED_STATUSES:
                self._finished_at.pop(key, None)
                continue
            seen = self._finished_at.setdefault(key, now)
            if now - seen >= self._retention:
                super().__delitem__(key)
                del self._finished_at[key]
        for key in [k for k in self._finished_at if k not in self]:
            del self._finished_at[key]
