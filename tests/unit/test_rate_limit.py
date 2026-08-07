"""Tests for helpers.rate_limit -- shared process-wide provider pacing.

CLU reaches Metron and ComicVine from background sweeps, bulk jobs, the
wanted-cache refresh and live web requests at once, so the budget has to be
shared across threads or the aggregate trips the provider's burst limit even
when no single caller does.
"""
import threading
import time

import pytest

from helpers.rate_limit import (
    SlidingWindowRateLimiter,
    get_limiter,
    reset_all_limiters,
)


class TestSlidingWindowAcquire:

    def test_allows_up_to_the_budget_without_waiting(self):
        limiter = SlidingWindowRateLimiter(3, 60.0)

        started = time.monotonic()
        assert all(limiter.acquire() for _ in range(3))

        assert time.monotonic() - started < 0.5

    def test_blocks_once_the_window_is_full(self):
        limiter = SlidingWindowRateLimiter(2, 0.2)
        limiter.acquire()
        limiter.acquire()

        started = time.monotonic()
        assert limiter.acquire() is True

        assert time.monotonic() - started >= 0.1

    def test_reset_clears_the_window(self):
        limiter = SlidingWindowRateLimiter(1, 60.0)
        limiter.acquire()

        limiter.reset()

        started = time.monotonic()
        assert limiter.acquire() is True
        assert time.monotonic() - started < 0.5


class TestAcquireTimeout:
    """A limiter with an hour-long window would park a Flask worker for most of
    an hour, turning a visible rate-limit error into a hung page. Interactive
    callers must be able to give up."""

    def test_returns_false_immediately_when_it_cannot_be_served(self):
        limiter = SlidingWindowRateLimiter(1, 3600.0)
        limiter.acquire()

        started = time.monotonic()
        assert limiter.acquire(timeout=0) is False

        assert time.monotonic() - started < 0.5

    def test_a_refused_acquire_does_not_consume_a_slot(self):
        limiter = SlidingWindowRateLimiter(1, 0.2)
        limiter.acquire()

        assert limiter.acquire(timeout=0) is False
        time.sleep(0.25)
        # The window has rolled over; exactly one slot should be free.
        assert limiter.acquire(timeout=0) is True
        assert limiter.acquire(timeout=0) is False

    def test_waits_up_to_the_timeout_then_succeeds(self):
        limiter = SlidingWindowRateLimiter(1, 0.2)
        limiter.acquire()

        assert limiter.acquire(timeout=5.0) is True

    def test_timeout_none_waits_indefinitely(self):
        limiter = SlidingWindowRateLimiter(1, 0.2)
        limiter.acquire()

        assert limiter.acquire(timeout=None) is True


class TestRetune:
    """Providers report their real limit in response headers; we should stop
    guessing without discarding what has already been sent."""

    def test_changes_the_budget_in_place(self):
        limiter = SlidingWindowRateLimiter(1, 3600.0)
        limiter.acquire()
        assert limiter.acquire(timeout=0) is False

        limiter.retune(5)

        assert limiter.max_requests == 5
        assert limiter.acquire(timeout=0) is True

    def test_never_drops_below_one(self):
        limiter = SlidingWindowRateLimiter(10, 60.0)
        limiter.retune(0)
        assert limiter.max_requests == 1

    def test_window_optional(self):
        limiter = SlidingWindowRateLimiter(10, 60.0)
        limiter.retune(20)
        assert limiter.window_seconds == 60.0
        limiter.retune(20, 30.0)
        assert limiter.window_seconds == 30.0


class TestGetLimiter:

    def test_same_name_returns_the_same_instance(self):
        a = get_limiter("test-provider", 5, 60.0)
        b = get_limiter("test-provider", 999, 1.0)

        assert a is b
        # Later callers must not silently widen an existing budget.
        assert a.max_requests == 5

    def test_different_names_are_independent(self):
        a = get_limiter("test-provider-a", 5, 60.0)
        b = get_limiter("test-provider-b", 5, 60.0)

        assert a is not b

    def test_reset_all_clears_every_window(self):
        limiter = get_limiter("test-reset-all", 1, 3600.0)
        limiter.acquire()
        assert limiter.acquire(timeout=0) is False

        reset_all_limiters()

        assert limiter.acquire(timeout=0) is True


class TestConcurrency:

    def test_never_admits_more_than_the_budget(self):
        limiter = SlidingWindowRateLimiter(5, 3600.0)
        granted = []
        granted_lock = threading.Lock()
        start = threading.Barrier(20)

        def worker():
            start.wait()
            if limiter.acquire(timeout=0):
                with granted_lock:
                    granted.append(1)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(granted) == 5
