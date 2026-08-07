"""Process-wide request pacing for the third-party metadata providers.

CLU reaches Metron and ComicVine from several places at once -- background
sweeps, bulk-metadata jobs, the wanted-cache refresh and live web requests --
so pacing has to be shared across threads or the providers' burst limits get
tripped by the aggregate rather than by any one caller.

The limiter here is the *outer* gate: it trips before the provider libraries'
own limits so the wait happens somewhere we control and log. It does not
replace Simyan's or Metron's own protection.
"""

import threading
import time
from collections import deque
from typing import Dict, Optional


class SlidingWindowRateLimiter:
    """Caps outgoing requests to `max_requests` per `window_seconds`, process-wide.

    Blocking by default: ``acquire()`` sleeps the calling thread until a slot
    frees up rather than raising, so retry/error-handling logic above it doesn't
    need to change.
    """

    def __init__(self, max_requests: int, window_seconds: float):
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._timestamps: deque = deque()
        self._lock = threading.Lock()

    def acquire(self, timeout: Optional[float] = None) -> bool:
        """Take a slot, waiting for one if necessary.

        Args:
            timeout: Longest to wait, in seconds. ``None`` waits indefinitely,
                which is right for background jobs. Interactive callers must
                pass a bound: a limiter with an hour-long window would
                otherwise park a Flask request thread for most of an hour,
                turning a visible rate-limit error into a hung spinner.

        Returns:
            True if a slot was taken, False if ``timeout`` elapsed first. On
            False the caller has *not* consumed a slot.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                while (
                    self._timestamps
                    and now - self._timestamps[0] >= self._window_seconds
                ):
                    self._timestamps.popleft()
                if len(self._timestamps) < self._max_requests:
                    self._timestamps.append(now)
                    return True
                wait = self._window_seconds - (now - self._timestamps[0])
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or wait > remaining:
                    return False
            if wait > 0:
                time.sleep(wait)

    def retune(self, max_requests: int, window_seconds: Optional[float] = None) -> None:
        """Adjust the budget in place, keeping the observed request history.

        Used when a provider reports its real limit in response headers, so we
        can stop guessing without discarding what has already been sent.
        """
        with self._lock:
            self._max_requests = max(1, int(max_requests))
            if window_seconds is not None:
                self._window_seconds = float(window_seconds)

    @property
    def max_requests(self) -> int:
        with self._lock:
            return self._max_requests

    @property
    def window_seconds(self) -> float:
        with self._lock:
            return self._window_seconds

    def reset(self) -> None:
        with self._lock:
            self._timestamps.clear()


_LIMITERS: Dict[str, SlidingWindowRateLimiter] = {}
_LIMITERS_LOCK = threading.Lock()


def get_limiter(
    name: str, max_requests: int, window_seconds: float
) -> SlidingWindowRateLimiter:
    """Return the shared limiter called ``name``, creating it on first use.

    ``max_requests``/``window_seconds`` only apply when the limiter is first
    created; later callers get the existing one so every caller in the process
    draws on the same budget. Use :meth:`SlidingWindowRateLimiter.retune` to
    change an existing limiter's budget.
    """
    with _LIMITERS_LOCK:
        limiter = _LIMITERS.get(name)
        if limiter is None:
            limiter = SlidingWindowRateLimiter(max_requests, window_seconds)
            _LIMITERS[name] = limiter
        return limiter


def reset_all_limiters() -> None:
    """Clear every shared limiter's window (used by tests)."""
    with _LIMITERS_LOCK:
        limiters = list(_LIMITERS.values())
    for limiter in limiters:
        limiter.reset()
