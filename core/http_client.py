"""The call policy for providers CLU talks to with plain ``requests`` (#704).

GCD API, MangaDex, MangaUpdates, Bedetheque and AniList have no client library
to pace them, so each used to keep its own gap in an unlocked class attribute
(or none at all: GCD API sent five pages back-to-back from as many threads as
were asking). ``HttpClient`` is the one place their requests leave from.

* **Paced per send.** ``_send`` takes one slot per HTTP request, each page of a
  paginated fetch included, from a limiter shared by every client of that
  provider in the process. The limiter is a one-slot window of
  ``60 / per_minute`` seconds, i.e. requests are *evenly spaced*: a 180/min
  sliding window would allow 180 at once and trip MangaDex's 5/s per-IP limit.
* **The limit is the one Settings shows.** Callers pass the provider class's
  ``rate_limit``; there is no second number.
* **A 429/503 holds every thread.** ``Retry-After`` (seconds or an HTTP date)
  or MangaDex's ``X-RateLimit-Retry-After`` (an epoch) sets one hold for the
  provider, which every ``_send`` waits out before taking a slot. The retry is
  therefore ``RETRY(0)``: the pacer has already backed off, so ``call()`` must
  not sleep on top of it (the stacked-retry mistake from #703). A wait longer
  than ``quota_threshold`` is a spent quota, latched instead of waited out.
* **A live web request waits a bounded time.** Inside a Flask request the hold
  and the slot are capped at ``interactive_wait``; past it the call returns its
  default rather than parking a request thread behind a long back-off.
* **A Cloudflare 403 is not a credential rejection.** Several providers sit
  behind Cloudflare, whose bot block is an HTML 403. Latching the lockout on it
  would pause the provider for something no credential change can fix, so a
  403 counts as an auth failure only when the body is not HTML.

Imports nothing from ``models``.
"""

import threading
import time
from email.utils import parsedate_to_datetime
from typing import Dict, Optional

import requests

from core.app_logging import app_logger
from core.provider_client import AuthLockout, GuardedClient, QuotaLatch, Verdict
from helpers.rate_limit import get_limiter

# A 429 that names no wait still means "not so fast".
DEFAULT_HOLD_SECONDS = 5.0


class ProviderBusy(Exception):
    """The pacer would have kept a live web request waiting too long."""


class _Pacer:
    """One provider's send schedule: the even spacing and the back-off hold."""

    def __init__(self, name: str, per_minute: int):
        self.limiter = get_limiter(f"http:{name}", 1, 60.0 / max(1, int(per_minute)))
        self._lock = threading.Lock()
        self._hold_until = 0.0

    def hold_for(self, seconds: float) -> None:
        until = time.monotonic() + max(0.0, seconds)
        with self._lock:
            self._hold_until = max(self._hold_until, until)

    def hold_remaining(self) -> float:
        with self._lock:
            return max(0.0, self._hold_until - time.monotonic())

    def wait_turn(self, timeout: Optional[float]) -> None:
        """Block until this send may go, or raise ProviderBusy past ``timeout``."""
        deadline = None if timeout is None else time.monotonic() + timeout
        hold = self.hold_remaining()
        while hold > 0:
            if deadline is not None and time.monotonic() + hold > deadline:
                raise ProviderBusy(f"provider is backing off for {hold:.0f}s")
            time.sleep(hold)
            hold = self.hold_remaining()
        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if not self.limiter.acquire(timeout=remaining):
            raise ProviderBusy("too many requests are already queued")


_PACERS: Dict[str, _Pacer] = {}
_PACERS_LOCK = threading.Lock()


def _pacer_for(name: str, per_minute: int) -> _Pacer:
    with _PACERS_LOCK:
        pacer = _PACERS.get(name)
        if pacer is None:
            pacer = _Pacer(name, per_minute)
            _PACERS[name] = pacer
        return pacer


def reset_pacers() -> None:
    """Forget every provider's spacing, hold and shared client (used by tests)."""
    with _PACERS_LOCK:
        pacers = list(_PACERS.values())
        _PACERS.clear()
    for pacer in pacers:
        pacer.limiter.reset()
    with _SHARED_LOCK:
        _SHARED.clear()


_SHARED: Dict[str, "HttpClient"] = {}
_SHARED_LOCK = threading.Lock()


def shared_http_client(name: str, build) -> "HttpClient":
    """The process-wide client called ``name``, built by ``build()`` on first use.

    For providers with no per-user credentials: one session (one connection
    pool) for every thread, instead of a bare ``requests.request`` per call.
    """
    with _SHARED_LOCK:
        client = _SHARED.get(name)
        if client is None:
            client = build()
            _SHARED[name] = client
        return client


def json_or_none(resp, display_name: str, context: str):
    """``resp.json()``, or None when there is no response or no valid JSON."""
    if resp is None:
        return None
    try:
        return resp.json()
    except ValueError as e:
        app_logger.error(f"{display_name} returned invalid JSON {context}: {e}")
        return None


def _in_request() -> bool:
    try:
        from flask import has_request_context

        return has_request_context()
    except Exception:
        return False


def retry_after_seconds(resp) -> Optional[float]:
    """The wait a 429/503 asked for, or None if it named none."""
    headers = getattr(resp, "headers", None) or {}
    value = headers.get("Retry-After")
    if value:
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            pass
        try:
            when = parsedate_to_datetime(value)
            return max(0.0, when.timestamp() - time.time())
        except (TypeError, ValueError, IndexError, OverflowError):
            pass
    value = headers.get("X-RateLimit-Retry-After")  # MangaDex: an epoch
    if value:
        try:
            return max(0.0, float(value) - time.time())
        except (TypeError, ValueError):
            pass
    return None


def _status_of(exc) -> Optional[int]:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None)


def _is_html(resp) -> bool:
    content_type = (getattr(resp, "headers", None) or {}).get("Content-Type", "")
    return "html" in content_type.lower()


def _auth_detail(resp) -> str:
    try:
        body = resp.json()
    except Exception:
        return ""
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("error") or body.get("message")
        if isinstance(detail, str):
            return detail.strip()
    return ""


def auth_rejection(exc) -> Optional[tuple]:
    """``(status, detail)`` when ``exc`` is the provider rejecting our
    credentials, else None. An HTML 403 is a bot block (Cloudflare), not a
    rejection: no credential change can fix it, so it must not latch."""
    if not isinstance(exc, requests.HTTPError):
        return None
    resp = exc.response
    status = _status_of(exc)
    if status == 401 or (status == 403 and resp is not None and not _is_html(resp)):
        return status, (_auth_detail(resp) if resp is not None else "")
    return None


class HttpClient(GuardedClient):
    """One provider's ``requests.Session`` behind the shared call policy.

    ``get``/``post`` run one request under ``GuardedClient.call`` and return
    the ``Response``, or ``default`` when the call failed or is paused. Pass
    ``raise_api_errors=True`` to get connection and HTTP errors raised instead
    (a rejected credential still latches and returns ``default``).
    """

    def __init__(
        self,
        name: str,
        display_name: str,
        base_url: str,
        per_minute: int,
        *,
        headers: Optional[Dict[str, str]] = None,
        auth=None,
        lockout: Optional[AuthLockout] = None,
        quota: Optional[QuotaLatch] = None,
        retry_statuses=(429, 503),
        quota_threshold: float = 300.0,
        interactive_wait: float = 10.0,
        timeout: float = 30.0,
    ):
        super().__init__(display_name, lockout, quota or QuotaLatch(display_name, "hourly"))
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.retry_statuses = tuple(retry_statuses)
        self.quota_threshold = quota_threshold
        self.interactive_wait = interactive_wait
        self.timeout = timeout
        self.pacer = _pacer_for(name, per_minute)
        self.session = requests.Session()
        if headers:
            self.session.headers.update(headers)
        if auth is not None:
            self.session.auth = auth

    # -- sending -------------------------------------------------------------

    def _url(self, path: str) -> str:
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}{path}"

    def _send(self, method: str, path: str, **kwargs):
        """The only place a request to this provider leaves. One slot per send."""
        self.pacer.wait_turn(self.interactive_wait if _in_request() else None)
        kwargs.setdefault("timeout", self.timeout)
        resp = self.session.request(method, self._url(path), **kwargs)
        if resp.status_code in self.retry_statuses:
            wait = retry_after_seconds(resp)
            if wait is None:
                wait = DEFAULT_HOLD_SECONDS
            if wait <= self.quota_threshold:
                self.pacer.hold_for(wait)
        resp.raise_for_status()
        return resp

    def request(self, method: str, path: str, *, context: str, default=None,
                ignore_auth_lock: bool = False, raise_auth_errors: bool = False,
                raise_api_errors: bool = False, **kwargs):
        return self.call(
            lambda: self._send(method, path, **kwargs),
            context,
            default,
            ignore_auth_lock=ignore_auth_lock,
            raise_auth_errors=raise_auth_errors,
            raise_api_errors=raise_api_errors,
        )

    def get(self, path: str, params=None, **opts):
        return self.request("GET", path, params=params, **opts)

    def post(self, path: str, json=None, **opts):
        return self.request("POST", path, json=json, **opts)

    # -- the policy ----------------------------------------------------------

    def _classify(self, exc):
        if isinstance(exc, ProviderBusy):
            return Verdict.error()
        if isinstance(exc, requests.HTTPError):
            rejection = auth_rejection(exc)
            if rejection is not None:
                return Verdict.auth(*rejection)
            status = _status_of(exc)
            resp = exc.response
            if status in self.retry_statuses:
                wait = retry_after_seconds(resp)
                if wait is not None and wait > self.quota_threshold:
                    return Verdict.quota(wait)
                # _send has already set the hold every thread waits out.
                return Verdict.retry(0)
            return Verdict.error()
        if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
            return Verdict.connection()
        if isinstance(exc, requests.RequestException):
            return Verdict.error()
        return None
