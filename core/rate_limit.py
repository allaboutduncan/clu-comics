"""Shared rate-limit / retry primitives for HTTP API wrappers.

Both ``models/metron.py`` and ``models/comicvine.py`` wrap third-party comic
metadata APIs that throttle burst traffic. They share the same needs -- a
process-wide throttle so concurrent background threads can't collectively
exceed the upstream burst limit, and a retry loop that backs off on rate-limit
rejections -- but differ in the mechanics (typed ``RateLimitError`` with a
``retry_after`` header vs. a message-string match with linear backoff). This
module holds the common skeleton; each caller injects the parts that differ.
"""

import threading
import time
from collections import deque
from typing import Callable, Optional

from core.app_logging import app_logger


class SlidingWindowRateLimiter:
    """Caps outgoing requests to ``max_requests`` per ``window_seconds``, process-wide.

    Blocking (not rejecting): ``acquire()`` sleeps the calling thread until a slot
    frees up rather than raising, so a caller's own retry/error handling doesn't
    need to change.
    """

    def __init__(self, max_requests: int, window_seconds: float):
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._timestamps: deque = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= self._window_seconds:
                    self._timestamps.popleft()
                if len(self._timestamps) < self._max_requests:
                    self._timestamps.append(now)
                    return
                wait = self._window_seconds - (now - self._timestamps[0])
            if wait > 0:
                time.sleep(wait)

    def reset(self) -> None:
        with self._lock:
            self._timestamps.clear()


def call_with_retry(
    fn: Callable,
    *,
    max_attempts: int,
    is_rate_limit: Callable[[BaseException], bool],
    next_wait: Callable[[BaseException, int], Optional[float]],
    context: str,
    pre_call: Optional[Callable[[], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    logger=app_logger,
):
    """Call ``fn()`` with rate-limit-aware retry.

    Args:
        fn: Zero-arg callable performing the API request.
        max_attempts: Total number of attempts (including the first).
        is_rate_limit: Predicate deciding whether an exception is a rate-limit
            rejection. Non-rate-limit exceptions propagate immediately.
        next_wait: ``(exc, attempt) -> seconds`` to wait before the next attempt,
            or ``None`` to give up early (e.g. a daily limit was hit).
        context: Human-readable description used in log messages.
        pre_call: Optional throttle hook run before every attempt (e.g. a shared
            limiter's ``acquire``).
        sleep: Sleep function (injectable for tests).
        logger: Logger for retry messages.

    Returns:
        The return value of ``fn()``.

    Raises:
        The last rate-limit exception once attempts are exhausted, or any
        non-rate-limit exception immediately.
    """
    last_exc: Optional[BaseException] = None
    for attempt in range(max_attempts):
        if pre_call is not None:
            pre_call()
        try:
            return fn()
        except Exception as exc:
            if not is_rate_limit(exc):
                raise
            last_exc = exc
            if attempt >= max_attempts - 1:
                break
            wait = next_wait(exc, attempt)
            if wait is None:
                break
            logger.info(
                f"Rate limit hit {context}: waiting {wait:.0f}s before retry "
                f"(attempt {attempt + 1}/{max_attempts})"
            )
            # Flush handlers so the warning is visible before the sleep.
            for handler in getattr(logger, "handlers", []):
                handler.flush()
            sleep(wait)
    raise last_exc
