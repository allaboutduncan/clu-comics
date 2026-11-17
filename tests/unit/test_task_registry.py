"""core.task_registry.TaskRegistry: the reading-list import task store.

routes/reading_lists.import_tasks was a plain dict that only ever grew: every
import, sync and re-match left its entry behind for the life of the process.
"""
import threading

from core.task_registry import TaskRegistry


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _registry(retention=100):
    clock = Clock()
    return TaskRegistry(retention_seconds=retention, clock=clock), clock


def test_is_a_dict_workers_can_write_into():
    tasks, _ = _registry()
    tasks["a"] = {"status": "pending"}
    tasks["a"]["status"] = "processing"
    assert isinstance(tasks, dict)
    assert tasks.get("a") == {"status": "processing"}


def test_finished_task_is_dropped_after_retention():
    tasks, clock = _registry(retention=100)
    tasks["old"] = {"status": "pending"}
    tasks["old"]["status"] = "complete"
    tasks["x"] = {"status": "pending"}       # first sweep sees "old" finished
    clock.now = 99
    tasks.prune()
    assert "old" in tasks
    clock.now = 100
    tasks["y"] = {"status": "pending"}       # adding a task prunes
    assert "old" not in tasks
    assert "x" in tasks and "y" in tasks


def test_errored_task_is_dropped_too():
    tasks, clock = _registry(retention=10)
    tasks["e"] = {"status": "error", "message": "boom"}
    tasks.prune()
    clock.now = 10
    tasks.prune()
    assert "e" not in tasks


def test_running_task_is_never_dropped():
    tasks, clock = _registry(retention=10)
    tasks["run"] = {"status": "processing"}
    tasks.prune()
    clock.now = 10_000
    tasks.prune()
    assert tasks["run"]["status"] == "processing"


def test_polling_keeps_a_finished_task():
    tasks, clock = _registry(retention=100)
    tasks["t"] = {"status": "complete"}
    tasks.prune()                             # retention starts at 0
    clock.now = 90
    tasks.touch("t")                          # client still polling
    clock.now = 150
    tasks.prune()
    assert "t" in tasks
    clock.now = 190
    tasks.prune()
    assert "t" not in tasks


def test_retention_restarts_if_a_task_is_reused():
    tasks, clock = _registry(retention=10)
    tasks["t"] = {"status": "complete"}
    tasks.prune()
    clock.now = 9
    tasks["t"] = {"status": "pending"}        # re-registered under the same id
    tasks["t"]["status"] = "complete"
    clock.now = 15
    tasks.prune()                             # first seen finished at 15
    assert "t" in tasks


def test_del_still_works():
    tasks, _ = _registry()
    tasks["t"] = {"status": "complete"}
    tasks.prune()
    del tasks["t"]
    assert "t" not in tasks
    tasks.prune()


def test_concurrent_adds_do_not_raise():
    tasks, clock = _registry(retention=0)
    errors = []

    def worker(n):
        try:
            for i in range(300):
                tasks[f"{n}-{i}"] = {"status": "complete"}
        except Exception as e:  # pragma: no cover - the failure being tested
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    tasks.prune()
    tasks.prune()
    assert len(tasks) == 0
