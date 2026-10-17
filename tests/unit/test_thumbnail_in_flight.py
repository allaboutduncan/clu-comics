"""core.thumbnail_cache's in-flight registry.

A thumbnail_jobs row saying 'processing' outlives the in-memory executor queue
that was meant to settle it, so the registry is the only thing that can say a
job really exists. These pin its contract: dedupe while queued, release however
the task ends.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core import thumbnail_cache as tc


@pytest.fixture(autouse=True)
def _clean_registry():
    tc._in_flight.clear()
    yield
    tc._in_flight.clear()


class _HeldExecutor:
    """Records submissions without running them, like a queue a restart loses."""

    def __init__(self):
        self.jobs = []

    def submit(self, fn):
        self.jobs.append(fn)


def test_unknown_path_is_not_in_flight():
    assert not tc.is_in_flight("/data/a.cbz")


def test_a_queued_job_is_in_flight_and_not_queued_twice():
    ex = _HeldExecutor()
    calls = []
    assert tc.submit_thumbnail_job(ex, lambda p, c: calls.append(p), "/data/a.cbz", "c")
    assert tc.is_in_flight("/data/a.cbz")
    assert not tc.submit_thumbnail_job(ex, lambda p, c: None, "/data/a.cbz", "c")
    assert len(ex.jobs) == 1

    ex.jobs[0]()
    assert calls == ["/data/a.cbz"]
    assert not tc.is_in_flight("/data/a.cbz")


def test_a_failing_task_still_releases_the_path():
    ex = _HeldExecutor()

    def boom(p, c):
        raise RuntimeError("bad archive")

    tc.submit_thumbnail_job(ex, boom, "/data/b.cbz", "c")
    with pytest.raises(RuntimeError):
        ex.jobs[0]()
    assert not tc.is_in_flight("/data/b.cbz")


def test_a_refused_submit_releases_the_path():
    class Shutdown:
        def submit(self, fn):
            raise RuntimeError("cannot schedule new futures after shutdown")

    with pytest.raises(RuntimeError):
        tc.submit_thumbnail_job(Shutdown(), lambda p, c: None, "/data/c.cbz", "c")
    assert not tc.is_in_flight("/data/c.cbz")


def test_with_a_real_executor():
    started, release = threading.Event(), threading.Event()

    def task(p, c):
        started.set()
        release.wait(5)

    with ThreadPoolExecutor(max_workers=1) as ex:
        assert tc.submit_thumbnail_job(ex, task, "/data/d.cbz", "c")
        assert started.wait(5)
        assert tc.is_in_flight("/data/d.cbz")
        assert not tc.submit_thumbnail_job(ex, task, "/data/d.cbz", "c")
        release.set()
    assert not tc.is_in_flight("/data/d.cbz")
