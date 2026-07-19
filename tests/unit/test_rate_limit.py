"""Unit tests for core/rate_limit.py -- the shared retry/throttle primitives."""
import time

import pytest

from core.rate_limit import SlidingWindowRateLimiter, call_with_retry


class _RateLimit(Exception):
    """Marker exception treated as a rate-limit rejection in these tests."""


def _is_rl(exc):
    return isinstance(exc, _RateLimit)


class TestCallWithRetry:

    def test_returns_immediately_on_success(self):
        sleeps = []
        result = call_with_retry(
            lambda: "ok",
            max_attempts=3,
            is_rate_limit=_is_rl,
            next_wait=lambda e, a: 1.0,
            context="ctx",
            sleep=sleeps.append,
        )
        assert result == "ok"
        assert sleeps == []  # no retry, no sleep

    def test_retries_then_succeeds(self):
        calls = {"n": 0}
        sleeps = []

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise _RateLimit("slow down")
            return "ok"

        result = call_with_retry(
            flaky,
            max_attempts=5,
            is_rate_limit=_is_rl,
            next_wait=lambda e, a: (a + 1) * 2.0,
            context="ctx",
            sleep=sleeps.append,
        )
        assert result == "ok"
        assert calls["n"] == 3
        assert sleeps == [2.0, 4.0]  # backed off before each retry, next_wait uses attempt

    def test_gives_up_and_reraises_last_exc(self):
        calls = {"n": 0}
        sleeps = []

        def always():
            calls["n"] += 1
            raise _RateLimit(f"attempt {calls['n']}")

        with pytest.raises(_RateLimit, match="attempt 3"):
            call_with_retry(
                always,
                max_attempts=3,
                is_rate_limit=_is_rl,
                next_wait=lambda e, a: 1.0,
                context="ctx",
                sleep=sleeps.append,
            )
        assert calls["n"] == 3  # first attempt + 2 retries
        assert sleeps == [1.0, 1.0]  # slept before the 2 retries, not after the last

    def test_non_rate_limit_error_propagates_immediately(self):
        calls = {"n": 0}
        sleeps = []

        def boom():
            calls["n"] += 1
            raise ValueError("something else")

        with pytest.raises(ValueError):
            call_with_retry(
                boom,
                max_attempts=3,
                is_rate_limit=_is_rl,
                next_wait=lambda e, a: 1.0,
                context="ctx",
                sleep=sleeps.append,
            )
        assert calls["n"] == 1  # raised immediately, no retry
        assert sleeps == []

    def test_next_wait_none_gives_up_early(self):
        calls = {"n": 0}
        sleeps = []

        def always():
            calls["n"] += 1
            raise _RateLimit("daily limit")

        with pytest.raises(_RateLimit):
            call_with_retry(
                always,
                max_attempts=5,
                is_rate_limit=_is_rl,
                next_wait=lambda e, a: None,  # e.g. daily limit exceeded
                context="ctx",
                sleep=sleeps.append,
            )
        assert calls["n"] == 1  # gave up after first rate-limit, no retry
        assert sleeps == []

    def test_pre_call_invoked_once_per_attempt(self):
        calls = {"n": 0}
        pre = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 2:
                raise _RateLimit("slow down")
            return "ok"

        def pre_call():
            pre["n"] += 1

        result = call_with_retry(
            flaky,
            max_attempts=3,
            is_rate_limit=_is_rl,
            next_wait=lambda e, a: 0.0,
            context="ctx",
            pre_call=pre_call,
            sleep=lambda s: None,
        )
        assert result == "ok"
        assert calls["n"] == 2
        assert pre["n"] == 2  # throttle hook ran before both attempts


class TestSlidingWindowRateLimiter:

    def test_allows_up_to_limit_without_blocking(self):
        limiter = SlidingWindowRateLimiter(max_requests=3, window_seconds=60.0)
        start = time.monotonic()
        for _ in range(3):
            limiter.acquire()
        assert time.monotonic() - start < 0.5

    def test_blocks_until_window_frees_a_slot(self):
        limiter = SlidingWindowRateLimiter(max_requests=2, window_seconds=0.2)
        limiter.acquire()
        limiter.acquire()
        start = time.monotonic()
        limiter.acquire()
        assert time.monotonic() - start >= 0.15

    def test_reset_clears_recorded_requests(self):
        limiter = SlidingWindowRateLimiter(max_requests=1, window_seconds=60.0)
        limiter.acquire()
        limiter.reset()
        start = time.monotonic()
        limiter.acquire()
        assert time.monotonic() - start < 0.5
