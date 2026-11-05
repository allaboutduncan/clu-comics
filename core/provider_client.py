"""The call policy shared by CLU's third-party metadata providers.

Every provider CLU calls over HTTP needs the same three brakes, and each used
to grow its own copy (#704):

* **An auth lockout.** A rejected credential does not heal on its own, and
  retrying it is how a user's IP was banned by Metron's fail2ban. Latched on a
  401/403, persisted to ``user_preferences`` so a crash-looping container does
  not resume, cleared only by a human (credential save, connection test, the
  Re-enable button).
* **A quota latch.** A spent daily/hourly window is hours away; waiting it out
  inside a sweep of 800 series is 800 waits that cannot succeed.
* **429 classification.** A burst rejection is worth retrying, a quota
  rejection is not, and each provider reports which one it was differently.

``GuardedClient.call()`` owns those decisions. A subclass supplies
``_classify(exc)`` -- the only provider-specific part -- and optionally
``_gate()`` and ``_after_success()``.

> **Pace where each HTTP request is sent, not once per call.** ``call()``
> deliberately never paces. One call can be many requests: mokkari and simyan
> follow every ``next`` link inside a single list call, so a slot taken per
> call let a long series send dozens of unpaced pages (#703). A library that
> paginates internally is paced through its own per-send hook (mokkari's
> ``rate_limiter``); a plain-``requests`` provider paces in the method that
> sends each request.

This module imports nothing from ``models`` -- ``models/providers/__init__``
imports every provider, so a provider module importing back through it would
be a cycle -- and reaches ``core.database`` lazily.
"""

import json
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from core.app_logging import app_logger


# ---------------------------------------------------------------------------
# Auth lockout
# ---------------------------------------------------------------------------

_LOCKOUTS: Dict[str, "AuthLockout"] = {}
_LOCKOUTS_LOCK = threading.Lock()


def lockout_for(provider: str) -> Optional["AuthLockout"]:
    """The auth lockout registered for ``provider``, or None if it has none."""
    with _LOCKOUTS_LOCK:
        return _LOCKOUTS.get(provider)


class AuthLockout:
    """Latched "the provider rejected our credentials" state, persisted to the DB.

    Deliberately has no timer. A 401 does not heal on its own, so retrying it
    later only repeats the request that got the user banned; the block is
    cleared by saving credentials, by a successful connection test, or by the
    button on the settings page -- all of them acts of a human who has changed
    something.

    The database is the source of truth. The in-process copy is only a memo so
    the hot path is not a query per API call; ``forget()`` drops it so the next
    read comes from the database again.

    The preference keys are ``<provider>_auth_blocked`` / ``_auth_error`` /
    ``_auth_blocked_at``, which is exactly what Metron stored before this class
    was shared, so no install needs a migration.

    Constructing one registers it under ``provider`` for ``lockout_for``.
    """

    def __init__(self, provider: str, display_name: str):
        self.provider = provider
        self.display_name = display_name
        self.pref_blocked = f"{provider}_auth_blocked"
        self.pref_error = f"{provider}_auth_error"
        self.pref_blocked_at = f"{provider}_auth_blocked_at"
        self._lock = threading.Lock()
        self._loaded = False
        self._blocked = False
        self._error = None
        self._blocked_at = None
        with _LOCKOUTS_LOCK:
            _LOCKOUTS[provider] = self

    def _read_persisted(self) -> Optional[Dict[str, Any]]:
        """The stored state, or None when the store could not be read at all.

        Queried directly rather than through ``get_user_preference``, which
        swallows its own errors and returns the default: a read made before the
        database is ready would then be indistinguishable from "not blocked".
        Caching *that* would leave the lockout switched off for the life of the
        process -- the exact failure this feature exists to prevent.
        """
        from core.database import get_db_connection

        conn = get_db_connection()
        if conn is None:
            return None
        try:
            rows = conn.execute(
                "SELECT key, value FROM user_preferences WHERE key IN (?, ?, ?)",
                (self.pref_blocked, self.pref_error, self.pref_blocked_at),
            ).fetchall()
        finally:
            conn.close()

        values = {}
        for key, raw in rows:
            try:
                values[key] = json.loads(raw)
            except (TypeError, ValueError):
                values[key] = None
        return values

    def _load_locked(self) -> None:
        if self._loaded:
            return
        try:
            values = self._read_persisted()
        except Exception as e:
            app_logger.debug(f"{self.display_name} auth-block state unreadable: {e}")
            values = None

        if values is None:
            # Fail open, but do not remember it: leaving the memo unloaded means
            # the next call tries again, so a lockout is picked up as soon as
            # the database becomes readable.
            return

        self._blocked = bool(values.get(self.pref_blocked))
        self._error = values.get(self.pref_error)
        self._blocked_at = values.get(self.pref_blocked_at)
        self._loaded = True

    def _persist(self, blocked: bool, error, blocked_at) -> None:
        try:
            from core.database import set_user_preference

            set_user_preference(self.pref_blocked, bool(blocked), category="providers")
            set_user_preference(self.pref_error, error, category="providers")
            set_user_preference(self.pref_blocked_at, blocked_at, category="providers")
        except Exception as e:
            app_logger.warning(
                f"Could not persist {self.display_name} auth-block state: {e}"
            )

    def blocked(self) -> bool:
        with self._lock:
            self._load_locked()
            return self._blocked

    def state(self) -> Dict[str, Any]:
        with self._lock:
            self._load_locked()
            return {
                "blocked": self._blocked,
                "error": self._error,
                "blocked_at": self._blocked_at,
            }

    def latch(self, status: Optional[int], detail: str, context: str = "") -> None:
        """Stop all traffic to this provider until a human fixes the credentials."""
        name = self.display_name
        message = f"{name} rejected the configured credentials (HTTP {status})."
        if detail:
            message = f"{message} {detail}"
        blocked_at = datetime.now(timezone.utc).isoformat()

        with self._lock:
            self._load_locked()
            already = self._blocked
            self._blocked = True
            self._error = message
            self._blocked_at = blocked_at

        if already:
            return

        self._persist(True, message, blocked_at)
        suffix = f" {context}".rstrip()
        app_logger.error(
            f"{name} authentication failed{suffix}: {message} "
            f"Pausing all {name} requests until the credentials are updated."
        )
        try:
            from core.database import update_provider_validity

            update_provider_validity(self.provider, False)
        except Exception:
            pass

    def clear(self) -> None:
        with self._lock:
            self._load_locked()
            was_blocked = self._blocked
            self._blocked = False
            self._error = None
            self._blocked_at = None
        self._persist(False, None, None)
        if was_blocked:
            app_logger.info(
                f"{self.display_name} authentication block cleared; "
                f"resuming {self.display_name} requests"
            )

    def forget(self) -> None:
        """Drop the memo so the next read comes from the database."""
        with self._lock:
            self._loaded = False


# ---------------------------------------------------------------------------
# Quota latch
# ---------------------------------------------------------------------------


class QuotaLatch:
    """"This provider's long rate-limit window is spent until <time>".

    In memory only: a restart forgets it, and the first request after one
    re-learns it from the provider's own answer. Announced on the transition
    in each direction, never per call.
    """

    def __init__(self, display_name: str, window_label: str = "daily"):
        self.display_name = display_name
        self.window_label = window_label
        self._lock = threading.Lock()
        self._until: Optional[datetime] = None

    def latch_until(self, reset: datetime, context: str = "") -> None:
        if reset.tzinfo is None:
            reset = reset.replace(tzinfo=timezone.utc)
        if reset <= datetime.now(timezone.utc):
            return
        with self._lock:
            already = self._until is not None
            self._until = reset
        if not already:
            where = f" {context}".rstrip()
            app_logger.warning(
                f"{self.display_name} {self.window_label} rate limit exhausted{where}; "
                f"pausing {self.display_name} calls until {reset.isoformat()}"
            )

    def latch_for(self, seconds: float, context: str = "") -> None:
        self.latch_until(
            datetime.now(timezone.utc) + timedelta(seconds=max(0.0, seconds)),
            context,
        )

    def reached(self) -> bool:
        """True while the window is spent. Logs once when it rolls over."""
        with self._lock:
            until = self._until
            if until is None:
                return False
            if datetime.now(timezone.utc) < until:
                return True
            self._until = None
        app_logger.info(
            f"{self.display_name} {self.window_label} rate limit window has reset; resuming"
        )
        return False

    def remaining_seconds(self) -> float:
        """Seconds until the window resets; 0 when it is not latched."""
        with self._lock:
            until = self._until
        if until is None:
            return 0.0
        return max(0.0, (until - datetime.now(timezone.utc)).total_seconds())

    def reset(self) -> None:
        with self._lock:
            self._until = None


# ---------------------------------------------------------------------------
# Guarded calls
# ---------------------------------------------------------------------------

AUTH = "auth"
QUOTA = "quota"
RETRY = "retry"
CONNECTION = "connection"
ERROR = "error"


@dataclass(frozen=True)
class Verdict:
    """What ``GuardedClient.call`` should do about one exception.

    * ``AUTH``: latch the lockout (``status``/``detail`` describe it).
    * ``QUOTA``: give up without retrying. ``wait > 0`` latches the quota for
      that long; ``wait == 0`` means the classifier already latched it.
    * ``RETRY``: try again. ``wait > 0`` sleeps first; ``wait == 0`` means the
      per-send pacer has already backed off and will block the next send.
    * ``CONNECTION`` / ``ERROR``: log and return the default (or re-raise
      under ``raise_api_errors``).
    """

    kind: str
    wait: float = 0.0
    status: Optional[int] = None
    detail: str = ""

    @classmethod
    def auth(cls, status: Optional[int], detail: str = "") -> "Verdict":
        return cls(AUTH, status=status, detail=detail)

    @classmethod
    def quota(cls, wait: float = 0.0) -> "Verdict":
        return cls(QUOTA, wait=wait)

    @classmethod
    def retry(cls, wait: float = 0.0) -> "Verdict":
        return cls(RETRY, wait=wait)

    @classmethod
    def connection(cls) -> "Verdict":
        return cls(CONNECTION)

    @classmethod
    def error(cls) -> "Verdict":
        return cls(ERROR)


class GuardedClient(ABC):
    """Runs provider calls behind the lockout, the quota latch and a retry loop.

    ``call()`` returns ``default`` rather than sleeping while the provider is
    paused, so a sweep over hundreds of items costs nothing once the quota is
    spent or the credentials are rejected. Callers already tolerate
    ``default`` -- it is the same contract as an API error.
    """

    max_retries = 3

    def __init__(
        self,
        display_name: str,
        lockout: Optional[AuthLockout],
        quota: QuotaLatch,
    ):
        """``lockout`` is None for a provider with no credentials to reject
        (MangaDex, Bedetheque...): it must not grow a Re-enable button, and an
        ``AUTH`` verdict from it is treated as an ordinary error."""
        self.display_name = display_name
        self.lockout = lockout
        self.quota = quota

    @abstractmethod
    def _classify(self, exc: BaseException) -> Optional[Verdict]:
        """Map an exception to a Verdict, or None to let it propagate."""

    def _gate(self) -> None:
        """Run before every attempt. A per-send pacer for a single-request
        provider belongs here; one whose library paginates must pace inside
        the library instead (see the module docstring)."""

    def _after_success(self) -> None:
        """Run after every successful attempt, e.g. to read quota headers."""

    def _retry_wait(self, wait: float, attempt: int) -> float:
        """Seconds to sleep before retry ``attempt + 1`` (0-based). The
        verdict's wait as-is by default; a provider whose pacer cannot learn
        from a rejection (simyan) scales it."""
        return wait

    def paused_reason(self, ignore_auth_lock: bool = False) -> Optional[str]:
        """Why calls are being skipped right now, for a user-facing message."""
        if (
            not ignore_auth_lock
            and self.lockout is not None
            and self.lockout.blocked()
        ):
            return f"{self.display_name} rejected the saved credentials"
        if self.quota.reached():
            return (
                f"{self.display_name}'s {self.quota.window_label} request limit "
                f"was reached"
            )
        return None

    def call(
        self,
        fn,
        context: str,
        default=None,
        *,
        ignore_auth_lock: bool = False,
        raise_auth_errors: bool = False,
        raise_api_errors: bool = False,
    ):
        """Call ``fn()`` under the shared policy.

        ``ignore_auth_lock`` and ``raise_auth_errors`` exist for the
        connection test, which must be able to run while the lockout is
        latched and needs the rejection itself to report it back.
        ``raise_api_errors`` lets a route keep its own "provider unavailable"
        response; a rejected credential still latches and returns ``default``.
        """
        name = self.display_name
        for attempt in range(self.max_retries):
            if self.paused_reason(ignore_auth_lock=ignore_auth_lock):
                app_logger.debug(
                    f"{name} calls are paused (rate limit or auth); skipping {context}"
                )
                return default
            try:
                # Inside the try: a gate that refuses (ComicVine's bounded
                # interactive wait) is classified like any other failure.
                self._gate()
                result = fn()
            except Exception as e:
                verdict = self._classify(e)
                if verdict is None:
                    raise
                if verdict.kind == AUTH and self.lockout is None:
                    verdict = Verdict.error()
                if verdict.kind == AUTH:
                    if raise_auth_errors:
                        raise
                    self.lockout.latch(verdict.status, verdict.detail, context)
                    return default
                if verdict.kind == QUOTA:
                    if verdict.wait > 0:
                        self.quota.latch_for(verdict.wait, context)
                    else:
                        app_logger.info(
                            f"{name} {self.quota.window_label} rate limit reached "
                            f"{context}: giving up without retrying"
                        )
                    return default
                if verdict.kind == RETRY:
                    if attempt < self.max_retries - 1:
                        self._log_retry(
                            self._retry_wait(verdict.wait, attempt), attempt, context
                        )
                        continue
                    app_logger.info(
                        f"{name} rate limit exceeded {context}: giving up after "
                        f"{self.max_retries} attempts"
                    )
                    return default
                app_logger.error(f"{name} API error {context}: {e}")
                if raise_api_errors:
                    raise
                return default
            else:
                self._after_success()
                return result
        return default

    def _log_retry(self, wait: float, attempt: int, context: str) -> None:
        name = self.display_name
        progress = f"(attempt {attempt + 1}/{self.max_retries})"
        if wait <= 0:
            app_logger.info(
                f"{name} rate limit hit {context}: retrying once the rate limiter "
                f"allows {progress}"
            )
            return
        app_logger.info(
            f"{name} rate limit hit {context}: waiting {wait}s before retry {progress}"
        )
        # Flush so the line is visible before the sleep.
        for handler in app_logger.handlers:
            handler.flush()
        time.sleep(wait)
        app_logger.info(f"{name} rate limit wait complete, retrying {context}")
