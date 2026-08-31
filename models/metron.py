"""
Metron API integration for comic metadata retrieval using Mokkari library.
"""

from core.app_logging import app_logger
from typing import Optional, Dict, Any, List, Tuple
import inspect
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from core.version import __version__

import requests as requests_lib
import requests.exceptions as requests_exceptions
from helpers.rate_limit import SlidingWindowRateLimiter, get_limiter
from mokkari.session import Session as MokkariSession
from mokkari.sqlite_cache import SqliteCache
from mokkari.exceptions import ApiError, RateLimitError
from mokkari.schemas.collection import ScrobbleRequest

# User agent for Metron API requests
CLU_USER_AGENT = f"CLU/{__version__}"

_RATE_LIMIT_MAX_RETRIES = 3
_RATE_LIMIT_DEFAULT_WAIT = 60  # seconds, used when retry_after is 0 or unset
_DAILY_RATE_LIMIT_THRESHOLD = (
    60  # seconds; retry_after above this implies daily limit exceeded
)

# Metron's burst limit is 20 requests/minute. CLU runs multiple background
# threads (bulk metadata jobs, wanted-cache refresh, reading-list sync) plus
# live web requests that can all reach Metron concurrently. mokkari.Session
# tracks rate-limit state from response headers and pre-emptively raises
# RateLimitError, that check is advisory only -- it isn't
# synchronized with request dispatch, so concurrent callers can race past it.
# We therefore (1) share one Session per credential pair so mokkari's header
# tracking actually accumulates real state across all callers in this
# process, and (2) throttle at the process level to a sliding-window request
# rate safely under the burst limit.
_SESSION_CACHE: Dict[Tuple[str, str, str], MokkariSession] = {}
_SESSION_CACHE_LOCK = threading.Lock()

# Whether the installed mokkari accepts ``api_token`` (added in 4.4.0).
# Resolved once, lazily, by ``supports_api_token()``.
_SUPPORTS_API_TOKEN: Optional[bool] = None


def _metron_cache_expire_days() -> int:
    """Days a cached Metron response stays valid (``METRON_CACHE_EXPIRE_DAYS``).

    Deliberately short. ``scheduled_series_sync`` decides whether to do the
    expensive issue fetch by comparing the API's ``issue_count`` against the
    local count, so a cached series response means that decision runs on stale
    data and a newly published issue can be missed for one cycle. One day keeps
    that bounded while still collapsing a whole library sweep -- which re-reads
    the same series and issue endpoints from ``_sync_and_match``, the scan tail,
    the re-match job and the wanted-cache refresh -- down to one request each.

    Never ``None``: mokkari treats a falsy ``expire`` as "never expire".
    """
    try:
        days = int(os.environ.get("METRON_CACHE_EXPIRE_DAYS", "1"))
    except (ValueError, TypeError):
        return 1
    return max(1, days)


def _metron_cache() -> Optional[SqliteCache]:
    """Build mokkari's response cache, or ``None`` if it can't be created.

    ``Session._get`` consults the cache *before* dispatching, so a hit costs
    neither an HTTP request nor a rate-limit slot -- the cheapest reduction in
    Metron quota burn available to us.

    Caveat worth knowing when reading this code: mokkari's ``SqliteCache.get()``
    never checks the ``expire`` column. Expiry is only ever applied by
    ``cleanup()``, which mokkari itself runs just once, in ``__init__``. With a
    process-shared, long-lived Session the effective TTL is therefore
    ``expire + process uptime``, which is why ``invalidate_session_cache()``
    also calls ``cleanup()`` and why the default expiry is kept small.
    """
    try:
        from helpers.provider_cache import provider_cache_dir

        return SqliteCache(
            db_name=os.path.join(provider_cache_dir("mokkari"), "mokkari_cache.db"),
            expire=_metron_cache_expire_days(),
        )
    except Exception as e:
        # A read-only volume must degrade to "uncached", never to "no Metron".
        app_logger.warning(f"Metron response cache unavailable, continuing uncached: {e}")
        return None


_BURST_WINDOW_SECONDS = 60.0
_SAFE_BURST_LIMIT = 15  # stay under Metron's 20/min burst limit for headroom


# Re-exported so existing callers (and tests) keep importing it from here.
_SlidingWindowRateLimiter = SlidingWindowRateLimiter

_metron_rate_limiter = get_limiter(
    "metron", _SAFE_BURST_LIMIT, _BURST_WINDOW_SECONDS
)

# Longest we'll pause waiting for the per-minute burst window to roll over.
# The window is 60s, so this is a guard against a bad/skewed reset header
# parking a thread, not a normal-path value.
_MAX_BURST_WAIT_SECONDS = 90.0


def _as_aware(dt):
    """Coerce a datetime to UTC-aware, or None. mokkari returns tz-aware values;
    comparing one against a naive ``datetime.now()`` raises."""
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# Preference keys backing the auth lockout below. They live in
# ``user_preferences`` rather than in memory so that a container restarting with
# still-bad credentials does not resume hammering Metron -- which is exactly how
# one user's IP ended up blocked by Metron's fail2ban.
PREF_AUTH_BLOCKED = "metron_auth_blocked"
PREF_AUTH_ERROR = "metron_auth_error"
PREF_AUTH_BLOCKED_AT = "metron_auth_blocked_at"

# Statuses that mean "your credentials are wrong", not "try again later".
# Metron's own API guidance is explicit: only 429 and 5xx are worth retrying,
# because retrying any other 4xx just burns requests.
_AUTH_FAILURE_STATUSES = (401, 403)


def auth_failure_status(exc: Exception) -> Optional[int]:
    """Return the HTTP status when ``exc`` is a Metron auth rejection, else None.

    mokkari raises ``ApiError(...) from err`` where ``err`` is the underlying
    ``requests.HTTPError``, so the real status is on ``__cause__`` -- the same
    place ``is_connection_error`` looks. Reading it there beats matching on the
    message text, which is only assembled for humans.
    """
    if not isinstance(exc, ApiError) or requests_exceptions is None:
        return None
    cause = exc.__cause__
    if not isinstance(cause, requests_exceptions.HTTPError):
        return None
    status = getattr(getattr(cause, "response", None), "status_code", None)
    return status if status in _AUTH_FAILURE_STATUSES else None


def _auth_detail(exc: Exception) -> str:
    """The server's own explanation of a rejection, trimmed for a UI toast."""
    cause = exc.__cause__
    body = getattr(getattr(cause, "response", None), "text", "") or ""
    body = body.strip()
    if not body:
        return ""
    try:
        import json

        parsed = json.loads(body)
        if isinstance(parsed, dict) and parsed.get("detail"):
            body = str(parsed["detail"])
    except (ValueError, TypeError):
        pass
    return body[:200]


class _AuthBlock:
    """Latched "Metron rejected our credentials" state, persisted to the DB.

    Deliberately has no timer. A 401 does not heal on its own, so retrying it
    later only repeats the request that got the user banned; the block is
    cleared by saving credentials, by a successful connection test, or by the
    button on the settings page -- all of them acts of a human who has changed
    something.

    The database is the source of truth. The in-process copy is only a memo so
    the hot path is not a query per API call; ``forget()`` drops it so the next
    read comes from the database again.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._loaded = False
        self._blocked = False
        self._error = None
        self._blocked_at = None

    @staticmethod
    def _read_persisted() -> Optional[Dict[str, Any]]:
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
                (PREF_AUTH_BLOCKED, PREF_AUTH_ERROR, PREF_AUTH_BLOCKED_AT),
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
            app_logger.debug(f"Metron auth-block state unreadable: {e}")
            values = None

        if values is None:
            # Fail open, but do not remember it: leaving the memo unloaded means
            # the next call tries again, so a lockout is picked up as soon as
            # the database becomes readable.
            return

        self._blocked = bool(values.get(PREF_AUTH_BLOCKED))
        self._error = values.get(PREF_AUTH_ERROR)
        self._blocked_at = values.get(PREF_AUTH_BLOCKED_AT)
        self._loaded = True

    @staticmethod
    def _persist(blocked: bool, error, blocked_at) -> None:
        try:
            from core.database import set_user_preference

            set_user_preference(PREF_AUTH_BLOCKED, bool(blocked), category="providers")
            set_user_preference(PREF_AUTH_ERROR, error, category="providers")
            set_user_preference(PREF_AUTH_BLOCKED_AT, blocked_at, category="providers")
        except Exception as e:
            app_logger.warning(f"Could not persist Metron auth-block state: {e}")

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
        """Stop all Metron traffic until a human fixes the credentials."""
        message = f"Metron rejected the configured credentials (HTTP {status})."
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
            f"Metron authentication failed{suffix}: {message} "
            f"Pausing all Metron requests until the credentials are updated."
        )
        try:
            from core.database import update_provider_validity

            update_provider_validity("metron", False)
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
            app_logger.info("Metron authentication block cleared; resuming Metron requests")

    def forget(self) -> None:
        """Drop the memo so the next read comes from the database."""
        with self._lock:
            self._loaded = False


_auth_block = _AuthBlock()


def auth_blocked() -> bool:
    """True while Metron has rejected our credentials and nothing has changed."""
    return _auth_block.blocked()


def auth_block_state() -> Dict[str, Any]:
    """The lockout state, for the settings page."""
    return _auth_block.state()


def clear_auth_block() -> None:
    """Re-enable Metron after a credential change or a successful test."""
    _auth_block.clear()


def latch_auth_failure(status: Optional[int], detail: str = "", context: str = "") -> None:
    """Record a Metron auth rejection and stop calling until it is cleared."""
    _auth_block.latch(status, detail, context)


class _MetronPacer:
    """Paces Metron requests from the limits Metron itself reports.

    mokkari parses ``X-RateLimit-*`` into ``session.rate_limit_status`` but its
    own pre-emptive check is advisory -- its docstring says callers must cap
    their own concurrency. So we read the same headers and do the capping here.

    Two windows matter, and they need opposite responses:

    * **burst** (per-minute, same for everyone). Short. Worth waiting out.
    * **sustained** (daily, varies by donor tier). Hours away. Waiting is
      pointless -- and worse, mokkari reports ``retry_after`` as
      ``max(burst_wait, sustained_wait)``, so an exhausted daily quota surfaced
      as a 59.5s wait that sat just under the "this must be the daily cap"
      threshold and got retried 3x *per series*, for every series in a sweep.
      That is the log in the bug report. Once the daily window is known to be
      empty we stop calling until it resets.
    """

    def __init__(self, limiter):
        self._limiter = limiter
        self._lock = threading.Lock()
        self._sustained_until = None
        self._burst_until = None

    def before(self, ignore_auth_lock: bool = False) -> bool:
        """Take a slot. False when the daily quota is spent or auth is blocked."""
        if not ignore_auth_lock and _auth_block.blocked():
            return False
        now = datetime.now(timezone.utc)
        with self._lock:
            sustained_until = self._sustained_until
            burst_until = self._burst_until
            if burst_until is not None and now >= burst_until:
                self._burst_until = None
                burst_until = None

        if sustained_until is not None:
            if now < sustained_until:
                return False
            with self._lock:
                self._sustained_until = None
            app_logger.info("Metron daily rate limit window has reset; resuming")

        if burst_until is not None:
            wait = min((burst_until - now).total_seconds(), _MAX_BURST_WAIT_SECONDS)
            if wait > 0:
                app_logger.info(
                    f"Metron burst window exhausted; waiting {wait:.1f}s for reset"
                )
                time.sleep(wait)
            with self._lock:
                self._burst_until = None

        self._limiter.acquire()
        return True

    def observe(self, session) -> None:
        """Update pacing from the rate-limit headers on the last response.

        Every field is ``None`` until the first response completes, and Metron
        may not report a given window at all, so each branch has to fall back to
        the configured defaults rather than assume a number is present.
        """
        status = getattr(session, "rate_limit_status", None)
        if status is None:
            return
        try:
            self._observe_burst(getattr(status, "burst", None))
            self._observe_sustained(getattr(status, "sustained", None))
        except Exception as e:  # never let telemetry break a worker thread
            app_logger.debug(f"Metron rate-limit header parse skipped: {e}")

    def _observe_burst(self, burst) -> None:
        if burst is None:
            return
        limit = getattr(burst, "limit", None)
        if isinstance(limit, int) and limit > 0:
            # Stop guessing at 15/min once Metron tells us the real number.
            self._limiter.retune(max(1, int(limit * 0.8)), _BURST_WINDOW_SECONDS)

        remaining = getattr(burst, "remaining", None)
        reset = _as_aware(getattr(burst, "reset", None))
        if (
            isinstance(remaining, int)
            and remaining <= 1
            and reset is not None
            and reset > datetime.now(timezone.utc)
        ):
            with self._lock:
                self._burst_until = reset

    def _observe_sustained(self, sustained) -> None:
        if sustained is None:
            return
        remaining = getattr(sustained, "remaining", None)
        reset = _as_aware(getattr(sustained, "reset", None))
        if not (isinstance(remaining, int) and remaining <= 0):
            return
        if reset is None or reset <= datetime.now(timezone.utc):
            return
        with self._lock:
            already_latched = self._sustained_until is not None
            self._sustained_until = reset
        if not already_latched:
            app_logger.warning(
                f"Metron daily rate limit exhausted; pausing Metron calls until "
                f"{reset.isoformat()}"
            )

    def daily_limit_reached(self) -> bool:
        """True when the last response said the daily quota is spent."""
        with self._lock:
            until = self._sustained_until
        return until is not None and datetime.now(timezone.utc) < until

    def latch_daily_limit(self, seconds: float, context: str) -> None:
        """Record a daily-cap rejection reported via ``retry_after``."""
        reset = datetime.now(timezone.utc) + timedelta(seconds=max(0.0, seconds))
        with self._lock:
            already_latched = self._sustained_until is not None
            self._sustained_until = reset
        if not already_latched:
            app_logger.warning(
                f"Metron daily rate limit exceeded {context}; pausing Metron "
                f"calls until {reset.isoformat()}"
            )

    def reset(self) -> None:
        with self._lock:
            self._sustained_until = None
            self._burst_until = None
        self._limiter.retune(_SAFE_BURST_LIMIT, _BURST_WINDOW_SECONDS)
        self._limiter.reset()


_metron_pacer = _MetronPacer(_metron_rate_limiter)


def _cached_sessions() -> List[MokkariSession]:
    with _SESSION_CACHE_LOCK:
        return list(_SESSION_CACHE.values())


def invalidate_session_cache() -> None:
    """Clear the cached Metron sessions and rate-limiter window (used by tests).

    Also runs each session's cache ``cleanup()``. mokkari only expires rows in
    ``SqliteCache.__init__``, so on a long-lived shared session this is the only
    thing that ever drops stale responses before the process restarts.
    """
    with _SESSION_CACHE_LOCK:
        sessions = list(_SESSION_CACHE.values())
        _SESSION_CACHE.clear()
    for session in sessions:
        cache = getattr(session, "cache", None)
        if cache is None:
            continue
        try:
            cache.cleanup()
        except Exception:
            pass
    _metron_pacer.reset()
    # Credentials may have changed under us; re-read the lockout from the
    # database rather than trusting a memo taken with the old ones.
    _auth_block.forget()


def _purge_cached_keys(like_patterns, matches, context: str, api=None) -> int:
    """Delete cached mokkari responses whose key satisfies ``matches``.

    ``like_patterns`` narrows the scan in SQL; ``matches(key)`` decides, because
    LIKE can't tell 8859 from 88591. Every duplicate row for a key goes:
    ``store()`` is a plain INSERT and ``get()`` returns the first row, so one
    leftover row keeps serving the stale body forever.

    Args:
        like_patterns: SQL LIKE patterns used to narrow the candidate rows.
        matches: Predicate called with each candidate key; truthy means delete.
        context: Human-readable subject, used only in log messages.
        api: Optional Session to purge; defaults to every cached session.

    Returns:
        Number of cached rows removed.
    """
    if not like_patterns:
        return 0

    removed = 0
    seen: List[Any] = []
    where = " OR ".join(["key LIKE ?"] * len(like_patterns))
    for session in [api] if api is not None else _cached_sessions():
        cache = getattr(session, "cache", None)
        con = getattr(cache, "con", None)
        if con is None or any(cache is c for c in seen):
            continue
        seen.append(cache)
        lock = getattr(cache, "_lock", None) or threading.Lock()
        try:
            with lock:
                rows = con.execute(
                    f"SELECT key FROM responses WHERE {where}",
                    tuple(like_patterns),
                ).fetchall()
                keys = [row[0] for row in rows if matches(row[0])]
                if keys:
                    con.executemany(
                        "DELETE FROM responses WHERE key = ?", [(k,) for k in keys]
                    )
                    con.commit()
            removed += len(keys)
        except Exception as e:
            app_logger.warning(
                f"Could not purge Metron response cache for {context}: {e}"
            )
    return removed


def purge_issue_cache(issue_id, api=None) -> int:
    """Drop mokkari's cached ``/issue/<id>/`` body so the next call re-fetches.

    That response is the one carrying ``credits`` and ``characters``, and Metron
    records are routinely completed *after* release day -- an issue tagged on
    release morning can be indexed hours later. Because the cache has no working
    expiry (see ``_metron_cache``), the incomplete body would otherwise be
    replayed for the life of the process, so a re-tag would rewrite exactly the
    metadata the user is trying to repair.

    Args:
        issue_id: Metron issue id whose cached detail response should be dropped.
        api: Optional Session to purge; defaults to every cached session.

    Returns:
        Number of cached rows removed.
    """
    try:
        issue_id = int(issue_id)
    except (TypeError, ValueError):
        return 0

    # Anchored so 172615 can't take out 1726150 or ``?issue_id=172615``.
    detail_re = re.compile(rf"/issue/{issue_id}/?(?:\?|$)")
    removed = _purge_cached_keys(
        [f"%/issue/{issue_id}%"], detail_re.search, f"issue {issue_id}", api=api
    )
    if removed:
        app_logger.debug(
            f"Purged {removed} cached Metron response(s) for issue {issue_id}"
        )
    return removed


def _known_issue_ids(series_id) -> List[int]:
    """Metron issue ids CLU has cached for a series; empty if the DB can't say."""
    try:
        from core.database import get_issues_for_series

        rows = get_issues_for_series(series_id) or []
    except Exception as e:
        app_logger.debug(f"Could not list cached issues for series {series_id}: {e}")
        return []

    ids = []
    for row in rows:
        try:
            ids.append(int(row["id"]))
        except (TypeError, ValueError, KeyError, IndexError):
            continue
    return ids


def purge_series_cache(series_id, api=None) -> int:
    """Drop mokkari's cached responses for one series so the next call re-fetches.

    ``Session._get`` consults ``SqliteCache`` *before* dispatching, and
    ``SqliteCache.get()`` never checks the ``expire`` column (see
    ``_metron_cache``), so a user-initiated "refresh from Metron" that only
    bypasses CLU's own database still gets whatever Metron returned the first
    time this process asked. A Summary edited on Metron would then never appear.
    Worse, ``store()`` is a plain INSERT and ``get()`` returns the first row, so
    a later fetch cannot overwrite the stale one -- it has to be deleted.

    Args:
        series_id: Metron series id whose cached responses should be dropped.
        api: Optional Session to purge; defaults to every cached session.

    Returns:
        Number of cached rows removed.
    """
    try:
        series_id = int(series_id)
    except (TypeError, ValueError):
        return 0

    # The series detail response, plus the paged issue lists filtered by it.
    detail_re = re.compile(rf"/series/{series_id}/?(?:\?|$)")
    issues_re = re.compile(rf"[?&]series_id={series_id}(?:&|$)")
    removed = _purge_cached_keys(
        [f"%/series/{series_id}%", f"%series_id={series_id}%"],
        lambda key: detail_re.search(key) or issues_re.search(key),
        f"series {series_id}",
        api=api,
    )

    # Those two only cover the series body and the issue *lists*. The per-issue
    # detail bodies hold the credits, and their keys say nothing about the
    # series -- ask CLU's own issue cache which ids belong to it. Guarded, so an
    # uncached session never pays for the database round trip.
    if any(getattr(s, "cache", None) is not None
           for s in ([api] if api is not None else _cached_sessions())):
        for issue_id in _known_issue_ids(series_id):
            removed += purge_issue_cache(issue_id, api=api)

    if removed:
        app_logger.info(
            f"Purged {removed} cached Metron responses for series {series_id}"
        )
    return removed


def _handle_rate_limit(e: "RateLimitError", attempt: int, context: str) -> bool:
    """Sleep and signal whether to retry after a RateLimitError.

    Returns True if the caller should retry, False if retries are exhausted
    or the daily API rate limit has been exceeded.
    """
    wait = e.retry_after if e.retry_after else _RATE_LIMIT_DEFAULT_WAIT
    if e.retry_after and e.retry_after > _DAILY_RATE_LIMIT_THRESHOLD:
        _metron_pacer.latch_daily_limit(e.retry_after, context)
        return False

    # mokkari reports retry_after as max(burst_wait, sustained_wait), so a spent
    # daily quota can arrive as a sub-threshold wait (observed: 59.5s) and get
    # retried three times per series for a whole sweep. The headers say which
    # window it really is -- trust them over the magnitude.
    for session in _cached_sessions():
        _metron_pacer.observe(session)
    if _metron_pacer.daily_limit_reached():
        app_logger.info(
            f"Metron daily rate limit reached {context}: giving up without retrying"
        )
        return False

    if attempt < _RATE_LIMIT_MAX_RETRIES - 1:
        app_logger.info(
            f"Metron rate limit hit {context}: waiting {wait}s before retry "
            f"(attempt {attempt + 1}/{_RATE_LIMIT_MAX_RETRIES})"
        )
        # Flush log handlers so the warning is visible before the sleep
        for handler in app_logger.handlers:
            handler.flush()
        time.sleep(wait)
        app_logger.info(f"Metron rate limit wait complete, retrying {context}")
        return True
    app_logger.info(
        f"Metron rate limit exceeded {context}: giving up after {_RATE_LIMIT_MAX_RETRIES} attempts"
    )
    return False


def _api_call(
    fn,
    context: str,
    default=None,
    ignore_auth_lock: bool = False,
    raise_auth_errors: bool = False,
):
    """Call fn() paced by the shared limiter, with rate-limit retry.

    Returns ``default`` immediately while Metron's daily quota is spent or its
    credentials have been rejected, rather than sleeping: a sweep of 800 series
    must not sit through 800 x 3 x 60s of waits that can't succeed. Callers
    already tolerate ``default`` -- it's the same contract as an ApiError.

    ``ignore_auth_lock`` and ``raise_auth_errors`` exist for the connection
    test, which must be able to run while the lockout is latched and needs the
    rejection itself in order to report it back to the user.
    """
    for attempt in range(_RATE_LIMIT_MAX_RETRIES):
        if not _metron_pacer.before(ignore_auth_lock=ignore_auth_lock):
            app_logger.debug(
                f"Metron calls are paused (rate limit or auth); skipping {context}"
            )
            return default
        try:
            result = fn()
        except RateLimitError as e:
            if not _handle_rate_limit(e, attempt, context):
                return default
        except ApiError as e:
            status = auth_failure_status(e)
            if status is not None:
                if raise_auth_errors:
                    # The connection test wants the reason, not a silent default.
                    raise
                latch_auth_failure(status, _auth_detail(e), context)
                return default
            app_logger.error(f"Metron API error {context}: {e}")
            return default
        else:
            for session in _cached_sessions():
                _metron_pacer.observe(session)
            return result
    return default


def is_connection_error(exc: Exception) -> bool:
    """Check if an exception is a Metron connectivity/timeout error."""
    if (
        isinstance(exc, ApiError)
        and exc.__cause__ is not None
        and requests_exceptions is not None
    ):
        return isinstance(
            exc.__cause__,
            (
                requests_exceptions.ConnectionError,
                requests_exceptions.ReadTimeout,
            ),
        )
    return False


def supports_api_token() -> bool:
    """True when the installed mokkari accepts ``api_token`` (>= 4.4.0)."""
    global _SUPPORTS_API_TOKEN
    if _SUPPORTS_API_TOKEN is not None:
        return _SUPPORTS_API_TOKEN
    try:
        params = inspect.signature(MokkariSession.__init__).parameters
    except (TypeError, ValueError):
        return True
    if "passwd" not in params:
        # Not the real Session -- a mock or a stand-in. Answer without caching,
        # so a patched class in one test cannot decide this for the process.
        return True
    _SUPPORTS_API_TOKEN = "api_token" in params
    return _SUPPORTS_API_TOKEN


def get_api(
    username: str = "",
    password: str = "",
    api_token: str = "",
    ignore_auth_lock: bool = False,
):
    """
    Return a shared Metron API client (Mokkari Session) for these credentials.

    Sessions are cached per (username, password, api_token) and reused across
    calls and threads, rather than created fresh each time, so mokkari's own
    rate-limit header tracking accumulates real state across the whole process
    instead of being discarded after every call.

    Returns None while Metron has rejected our credentials. This gate sits
    *before* the session cache on purpose: several callers fetch a client once
    and then drive mokkari directly in a loop, so refusing to hand out the
    client is the only thing that reliably stops them.

    Args:
        username: Metron username (basic auth)
        password: Metron password (basic auth)
        api_token: Metron API token; takes precedence over username/password
        ignore_auth_lock: Build a client even while the lockout is latched.
            Only the connection test may pass this -- without it a user could
            never verify the credentials that would clear the lockout.

    Returns:
        Mokkari Session client or None if unavailable
    """
    if not api_token and not (username and password):
        app_logger.warning("Metron credentials not configured")
        return None

    if not ignore_auth_lock and _auth_block.blocked():
        app_logger.debug("Metron authentication is blocked; not issuing a client")
        return None

    if api_token and not supports_api_token():
        app_logger.error(
            "Metron API token authentication needs mokkari >= 4.4.0; "
            "please upgrade mokkari or use a username and password."
        )
        return None

    cache_key = (username, password, api_token)
    with _SESSION_CACHE_LOCK:
        session = _SESSION_CACHE.get(cache_key)
        if session is not None:
            return session

    kwargs = {
        "username": username or None,
        "passwd": password or None,
        "user_agent": CLU_USER_AGENT,
        "cache": _metron_cache(),
    }
    if api_token:
        kwargs["api_token"] = api_token

    try:
        session = MokkariSession(**kwargs)
    except ApiError as e:
        app_logger.error(f"Metron API error initializing session: {e}")
        return None
    except Exception as e:
        app_logger.error(f"Failed to initialize Metron API: {e}")
        return None

    with _SESSION_CACHE_LOCK:
        # Another thread may have raced us to create a session for the same
        # credentials; keep whichever one won so only one is ever cached.
        session = _SESSION_CACHE.setdefault(cache_key, session)
    return session


def get_flask_api(app=None):
    """Get Metron API client using Flask app config credentials.

    Args:
        app: Flask app instance. If None, uses current_app (requires app context).

    Returns:
        Mokkari Session client or None if credentials missing/invalid.
    """
    if app is None:
        from flask import current_app
        config = current_app.config
    else:
        config = app.config
    username = (config.get("METRON_USERNAME") or "").strip()
    password = (config.get("METRON_PASSWORD") or "").strip()
    api_token = (config.get("METRON_API_TOKEN") or "").strip()
    if not api_token and not (username and password):
        return None
    return get_api(username, password, api_token)


def is_metron_configured(app=None):
    """Check if Metron credentials are present in Flask app config.

    Args:
        app: Flask app instance. If None, uses current_app (requires app context).

    Returns:
        True if an API token, or both a username and password, are configured
        and Metron has not rejected them.
    """
    if app is None:
        from flask import current_app
        config = current_app.config
    else:
        config = app.config
    if _auth_block.blocked():
        # Presence is not enough once Metron has told us the credentials are
        # wrong: every ``metron_available`` gate in the app reads this, and the
        # cheapest request is the one nobody attempts.
        return False
    username = (config.get("METRON_USERNAME") or "").strip()
    password = (config.get("METRON_PASSWORD") or "").strip()
    api_token = (config.get("METRON_API_TOKEN") or "").strip()
    return bool(api_token or (username and password))


def parse_cvinfo_for_metron_id(cvinfo_path: str) -> Optional[int]:
    """
    Parse a cvinfo file for series_id.

    cvinfo format:
        https://comicvine.gamespot.com/series-name/4050-123456/
        series_id: 10354

    Args:
        cvinfo_path: Path to the cvinfo file

    Returns:
        Metron series ID as integer, or None if not found
    """
    try:
        with open(cvinfo_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Look for series_id: <number>
        match = re.search(r"series_id:\s*(\d+)", content, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None
    except Exception as e:
        app_logger.error(f"Error parsing cvinfo for Metron ID: {e}")
        return None


def parse_cvinfo_for_comicvine_id(cvinfo_path: str) -> Optional[int]:
    """
    Parse a cvinfo file for ComicVine series ID.

    URL format: https://comicvine.gamespot.com/series-name/4050-123456/
    The CV series ID is 123456 (after 4050-)

    Args:
        cvinfo_path: Path to the cvinfo file

    Returns:
        ComicVine series ID as integer, or None if not found
    """
    try:
        with open(cvinfo_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Match pattern: 4050-{volume_id}
        match = re.search(r"/4050-(\d+)", content)
        if match:
            return int(match.group(1))
        return None
    except Exception as e:
        app_logger.error(f"Error parsing cvinfo for ComicVine ID: {e}")
        return None


def get_series_id_by_comicvine_id(api, cv_series_id: int) -> Optional[int]:
    """
    Look up Metron series ID using ComicVine series ID.

    Searches Metron for series with matching cv_id.

    Args:
        api: Mokkari API client
        cv_series_id: ComicVine series/volume ID

    Returns:
        Metron series ID, or None if not found
    """

    def _call():
        results = api.series_list({"cv_id": cv_series_id})
        if results:
            series_id = results[0].id
            app_logger.info(f"Found Metron series {series_id} for CV ID {cv_series_id}")
            return series_id
        # Informational, not a problem: Metron simply doesn't carry every
        # ComicVine volume, and callers (the library sweep in particular) fall
        # through to a ComicVine-sourced identity. Logging this at WARNING made
        # a routine cascade step look like a failure.
        app_logger.info(f"No Metron series found for ComicVine ID {cv_series_id}")
        return None

    return _api_call(_call, f"looking up CV ID {cv_series_id}")


def update_cvinfo_with_metron_id(cvinfo_path: str, series_id: int) -> bool:
    """
    Update cvinfo file to include series_id.

    Args:
        cvinfo_path: Path to the cvinfo file
        series_id: Metron series ID to add

    Returns:
        True if successful, False otherwise
    """
    from os.path import dirname
    from core.config import is_oneshot_folder
    if is_oneshot_folder(dirname(cvinfo_path)):
        app_logger.debug(f"Skipping cvinfo write in one-shot folder: {cvinfo_path}")
        return False
    try:
        with open(cvinfo_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Check if series_id already exists
        if re.search(r"series_id:", content, re.IGNORECASE):
            # Update existing
            content = re.sub(
                r"series_id:\s*\d+",
                f"series_id: {series_id}",
                content,
                flags=re.IGNORECASE,
            )
        else:
            # Append new line
            content = content.rstrip() + f"\nseries_id: {series_id}\n"

        with open(cvinfo_path, "w", encoding="utf-8") as f:
            f.write(content)

        app_logger.info(f"Updated cvinfo with series_id: {series_id}")
        return True
    except Exception as e:
        app_logger.error(f"Error updating cvinfo with Metron ID: {e}")
        return False


def read_cvinfo_fields(cvinfo_path: str) -> Dict[str, Any]:
    """
    Read publisher_name and start_year from cvinfo file if present.

    Args:
        cvinfo_path: Path to the cvinfo file

    Returns:
        Dict with 'publisher_name' and 'start_year' keys (values may be None)
    """
    result = {"publisher_name": None, "start_year": None}
    try:
        with open(cvinfo_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("publisher_name:"):
                    result["publisher_name"] = line.split(":", 1)[1].strip()
                elif line.startswith("start_year:"):
                    try:
                        result["start_year"] = int(line.split(":", 1)[1].strip())
                    except ValueError:
                        pass
    except Exception as e:
        app_logger.error(f"Error reading cvinfo fields from {cvinfo_path}: {e}")
    return result


def write_cvinfo_fields(
    cvinfo_path: str, publisher_name: Optional[str], start_year: Optional[int]
) -> bool:
    """
    Append publisher_name and start_year to cvinfo file if not already present.

    Args:
        cvinfo_path: Path to the cvinfo file
        publisher_name: Publisher name to save
        start_year: Series start year to save

    Returns:
        True if successful, False otherwise
    """
    from os.path import dirname
    from core.config import is_oneshot_folder
    if is_oneshot_folder(dirname(cvinfo_path)):
        app_logger.debug(f"Skipping cvinfo write in one-shot folder: {cvinfo_path}")
        return False
    try:
        existing = read_cvinfo_fields(cvinfo_path)
        lines_to_add = []

        if publisher_name and not existing["publisher_name"]:
            lines_to_add.append(f"publisher_name: {publisher_name}")
        if start_year and not existing["start_year"]:
            lines_to_add.append(f"start_year: {start_year}")

        if not lines_to_add:
            return True  # Nothing to add

        with open(cvinfo_path, "a", encoding="utf-8") as f:
            for line in lines_to_add:
                f.write(f"\n{line}")

        from helpers import match_parent_permissions
        match_parent_permissions(cvinfo_path)

        app_logger.debug(f"Added to cvinfo: {', '.join(lines_to_add)}")
        return True
    except Exception as e:
        app_logger.error(f"Error writing cvinfo fields to {cvinfo_path}: {e}")
        return False


def fetch_issue_detail(api, issue_id, context: str = ""):
    """Fetch ``/issue/<id>/`` live, with any cached body dropped first.

    The single entry point for fetching an issue *in order to write it into a
    file*. Metadata written into an archive must never come from a cached body:
    mokkari's cache has no working expiry (see ``_metron_cache``), and Metron
    records are routinely completed hours after release day, so a body fetched
    while the record was half-entered would be replayed for the life of the
    process -- and a re-tag meant to repair the file would rewrite the very
    metadata being repaired.

    Display-only reads deliberately keep using the cache; only writes pay for a
    live fetch, and within one job each issue is fetched once anyway.

    Args:
        api: Mokkari Session.
        issue_id: Metron issue id.
        context: Optional suffix for the rate-limit retry log messages.

    Returns:
        The mokkari ``Issue`` object, or None when the call failed.
    """
    try:
        issue_id = int(issue_id)
    except (TypeError, ValueError):
        return None

    purge_issue_cache(issue_id, api=api)

    def _fetch():
        return api.issue(issue_id)

    label = f"fetching details for issue ID {issue_id}"
    return _api_call(_fetch, f"{label} {context}".strip())


def get_issue_metadata(
    api, series_id: int, issue_number: str
) -> Optional[Dict[str, Any]]:
    """
    Fetch issue metadata from Metron.

    Uses the "double fetch" pattern: first search for issue, then get full details.
    Each API call has independent rate-limit retry handling to avoid re-executing
    successful calls when only the second call is rate-limited.

    Args:
        api: Mokkari API client
        series_id: Metron series ID
        issue_number: Issue number (string to handle "10.1", "Annual 1", etc.)

    Returns:
        Full issue data dict, or None if not found
    """
    # Step 1: Search for the issue (separate retry scope)
    app_logger.debug(f"Metron API: searching issue #{issue_number} in series {series_id}")

    def _search():
        return api.issues_list({"series_id": series_id, "number": issue_number})

    issues = _api_call(_search, f"searching issue {issue_number} in series {series_id}")
    if not issues:
        app_logger.warning(
            f"Issue {issue_number} not found in Metron series {series_id}"
        )
        return None

    metron_issue_id = issues[0].id
    app_logger.info(
        f"Found Metron issue ID {metron_issue_id}, fetching full details..."
    )

    # Step 2: Fetch full details (separate retry scope). Never from cache --
    # this body is about to be written into a file; see fetch_issue_detail.
    issue = fetch_issue_detail(api, metron_issue_id)
    result = _to_dict(issue) if issue is not None else None

    if result and isinstance(result, dict):
        app_logger.debug(f"Metron data keys: {list(result.keys())}")
        app_logger.debug(
            f"Series: {result.get('series')}, Number: {result.get('number')}"
        )
    return result


def _get_attr(obj, key, default=None):
    """Helper to get attribute from dict or object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _to_dict(obj):
    """Convert a Pydantic model (v1 or v2) or object to a dict."""
    if hasattr(obj, "model_dump"):
        app_logger.debug("Converting Metron response using model_dump()")
        return obj.model_dump()
    if hasattr(obj, "dict"):
        app_logger.debug("Converting Metron response using dict()")
        return obj.dict()
    if hasattr(obj, "json"):
        import json

        app_logger.debug("Converting Metron response using json()")
        return json.loads(obj.json())
    if hasattr(obj, "__dict__"):
        app_logger.debug("Converting Metron response using vars()")
        return vars(obj)
    app_logger.debug(f"Metron response type: {type(obj)}")
    return obj


def _extract_names(items) -> Optional[str]:
    """Extract 'name' from a list of dicts/objects and join as comma-separated string."""
    names = [n for item in items if (n := _get_attr(item, "name", ""))]
    return ", ".join(names) if names else None


def extract_credits_by_role(
    credits: List, role_names: List[str], substring: bool = False
) -> str:
    """
    Extract creator names for specific roles from credits list.

    Args:
        credits: List of credit dicts or objects with 'creator' and 'role' fields
        role_names: List of role names to match (e.g., ['Writer'])
        substring: Match when an entry in ``role_names`` appears *within* the
            role name (case-insensitively) instead of equalling it. Metron
            spells its editorial roles out in full -- "Editor In Chief",
            "Executive Editor", "Assistant Editor" -- so an exact match would
            collect almost none of them. This mirrors the ``editor`` needle in
            ``models.comicvine._CV_ROLE_MATCHERS`` so both providers fill
            <Editor> the same way.

    Returns:
        Comma-separated string of creator names
    """
    needles = [n.lower() for n in role_names] if substring else None
    creators = []
    for credit in credits:
        roles = _get_attr(credit, "role", [])
        if roles is None:
            roles = []
        for role in roles:
            role_name = _get_attr(role, "name", "")
            if role_name is None:
                role_name = str(role)
            if substring:
                matched = any(n in str(role_name).lower() for n in needles)
            else:
                matched = role_name in role_names
            if matched:
                creator_name = _get_attr(credit, "creator", "")
                if creator_name and creator_name not in creators:
                    creators.append(creator_name)
                break
    return ", ".join(creators)


def map_to_comicinfo(issue_data) -> Dict[str, Any]:
    """
    Map Metron issue data to ComicInfo.xml format.

    Args:
        issue_data: Issue data from Metron API (dict or object)

    Returns:
        Dictionary in ComicInfo.xml format
    """
    from datetime import datetime

    # Debug: log what we received
    app_logger.debug(f"map_to_comicinfo received type: {type(issue_data)}")
    if isinstance(issue_data, dict):
        app_logger.debug(f"map_to_comicinfo keys: {list(issue_data.keys())[:10]}...")

    # Parse cover_date for Year/Month/Day
    cover_date = _get_attr(issue_data, "cover_date", "")
    store_date = _get_attr(issue_data, "store_date", "")
    year = None
    month = None
    day = None
    if cover_date:
        try:
            dt = datetime.strptime(str(cover_date), "%Y-%m-%d")
            year = dt.year
            month = dt.month
            day = dt.day
        except ValueError:
            # Try parsing just year
            try:
                year = int(str(cover_date)[:4])
            except (ValueError, TypeError):
                pass

    # Extract series info
    series = _get_attr(issue_data, "series", {}) or {}
    series_name = _get_attr(series, "name", "") or ""
    # Use year_began for Volume field (series start year, not volume number)
    year_began = _get_attr(series, "year_began", None)

    # Extract genres from series
    genres = _get_attr(series, "genres", []) or []
    genre_str = _extract_names(genres)

    # Extract publisher
    publisher = _get_attr(issue_data, "publisher", {}) or {}
    publisher_name = _get_attr(publisher, "name", "") or ""

    # Extract credits
    credits = _get_attr(issue_data, "credits", []) or []
    writer = extract_credits_by_role(credits, ["Writer", "Script", "Story", "Plot"])
    penciller = extract_credits_by_role(credits, ["Penciller", "Artist", "Illustrator"])
    inker = extract_credits_by_role(credits, ["Inker"])
    colorist = extract_credits_by_role(credits, ["Colorist"])
    letterer = extract_credits_by_role(credits, ["Letterer"])
    cover_artist = extract_credits_by_role(credits, ["Cover"])
    # Substring, because Metron names its editorial roles in full ("Editor In
    # Chief", "Assistant Editor", "Group Editor"). Corporate titles that aren't
    # editorial -- President, Publisher, Chief Creative Officer, Designer --
    # don't contain the word and so stay out.
    editor = extract_credits_by_role(credits, ["Editor"], substring=True)

    # Extract characters
    characters = _get_attr(issue_data, "characters", []) or []
    characters_str = _extract_names(characters)

    # Extract teams
    teams = _get_attr(issue_data, "teams", []) or []
    teams_str = _extract_names(teams)

    # Get title from story_titles/name array (first element)
    # Mokkari model_dump() renames API "name" -> "story_titles" and "title" -> "collection_title"
    names = _get_attr(issue_data, "story_titles", None) or _get_attr(
        issue_data, "name", []
    )
    if isinstance(names, list) and names:
        title = names[0]
    elif isinstance(names, str):
        title = names
    else:
        title = None

    # Fall back to collection_title/title if story_titles is empty
    if not title:
        title = (
            _get_attr(issue_data, "collection_title", None)
            or _get_attr(issue_data, "title", None)
            or None
        )

    # Rating
    rating = _get_attr(issue_data, "rating", {})
    age_rating = _get_attr(rating, "name", None) if rating else None

    # Build notes
    resource_url = _get_attr(issue_data, "resource_url", "Unknown")
    modified = _get_attr(issue_data, "modified", "Unknown")
    notes = f"Metadata from Metron. Resource URL: {resource_url} — modified {modified}."

    comicinfo = {
        "Series": series_name,
        "Number": _get_attr(issue_data, "number", None),
        "Volume": year_began,
        "Title": title,
        "Summary": _get_attr(issue_data, "desc", None),
        "Publisher": publisher_name,
        "Year": year,
        "Month": month,
        "Day": day,
        # Raw provider dates surfaced for display/API. NOT written to
        # ComicInfo.xml (generate_comicinfo_xml uses an explicit tag allowlist)
        # and no longer used by rename templating — the cover year is read from
        # the ComicInfo Year tag via {issue_year}.
        "CoverDate": str(cover_date) if cover_date else None,
        "StoreDate": str(store_date) if store_date else None,
        "Writer": writer or None,
        "Penciller": penciller or None,
        "Inker": inker or None,
        "Colorist": colorist or None,
        "Letterer": letterer or None,
        "CoverArtist": cover_artist or None,
        "Editor": editor or None,
        "Characters": characters_str,
        "Teams": teams_str,
        "Genre": genre_str,
        "AgeRating": age_rating,
        "LanguageISO": "en",
        "Manga": "No",
        "Notes": notes,
        "PageCount": _get_attr(issue_data, "page_count", None)
        or _get_attr(issue_data, "page", None),
        "MetronId": _get_attr(issue_data, "id", None),
    }

    # Remove None values
    result = {k: v for k, v in comicinfo.items() if v is not None}
    # app_logger.info(f"map_to_comicinfo returning {len(result)} fields: {list(result.keys())}")
    return result


def get_series_id(cvinfo_path: str, api) -> Optional[int]:
    """
    Get Metron series ID from cvinfo, looking up by CV ID if needed.

    This is a convenience function that:
    1. Checks cvinfo for existing series_id
    2. If not found, extracts CV ID and looks up Metron series
    3. Updates cvinfo with the found Metron series ID

    Args:
        cvinfo_path: Path to cvinfo file
        api: Mokkari API client

    Returns:
        Metron series ID, or None if not found
    """
    # First, check if series_id already exists
    metron_id = parse_cvinfo_for_metron_id(cvinfo_path)
    if metron_id:
        app_logger.debug(f"Found existing series_id: {metron_id}")
        return metron_id

    # Not found, try to look up by ComicVine ID
    cv_id = parse_cvinfo_for_comicvine_id(cvinfo_path)
    if not cv_id:
        app_logger.warning("No ComicVine ID found in cvinfo")
        return None

    app_logger.info(f"Looking up Metron series by ComicVine ID: {cv_id}")
    metron_id = get_series_id_by_comicvine_id(api, cv_id)

    if metron_id:
        # Save to cvinfo for future use
        update_cvinfo_with_metron_id(cvinfo_path, metron_id)
        return metron_id

    return None


def fetch_and_map_issue(
    api, cvinfo_path: str, issue_number: str
) -> Optional[Dict[str, Any]]:
    """
    Convenience function to fetch issue metadata and map to ComicInfo format.

    This combines get_series_id, get_issue_metadata, and map_to_comicinfo.
    Also saves publisher_name and start_year to cvinfo for future use.

    Args:
        api: Mokkari API client
        cvinfo_path: Path to cvinfo file
        issue_number: Issue number to fetch

    Returns:
        ComicInfo-formatted dict, or None if not found
    """
    # Get the Metron series ID
    series_id = get_series_id(cvinfo_path, api)
    if not series_id:
        app_logger.warning("Could not determine Metron series ID")
        return None

    # Fetch issue metadata
    issue_data = get_issue_metadata(api, series_id, issue_number)
    if not issue_data:
        return None

    # Extract publisher_name and start_year for cvinfo
    publisher = _get_attr(issue_data, "publisher", {}) or {}
    publisher_name = _get_attr(publisher, "name", None)
    series = _get_attr(issue_data, "series", {}) or {}
    year_began = _get_attr(series, "year_began", None)

    # Save to cvinfo for future use
    if publisher_name or year_began:
        write_cvinfo_fields(cvinfo_path, publisher_name, year_began)

    # Map to ComicInfo format
    return map_to_comicinfo(issue_data)


def calculate_comic_week(date_obj=None):
    """
    Calculate the comic week (Sunday to Saturday) for a given date.

    Args:
        date_obj: datetime object (defaults to now)

    Returns:
        tuple of (start_date_obj, end_date_obj)
    """
    if date_obj is None:
        date_obj = datetime.now()

    # If date_obj is a string, parse it
    if isinstance(date_obj, str):
        try:
            date_obj = datetime.strptime(date_obj, "%Y-%m-%d")
        except ValueError:
            app_logger.error(f"Invalid date string format: {date_obj}")
            date_obj = datetime.now()

    # Calculate start of week (Sunday)
    # Weekday: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    # To get Sunday: (weekday + 1) % 7 gives days since Sunday
    days_since_sunday = (date_obj.weekday() + 1) % 7
    start_of_week = date_obj - timedelta(days=days_since_sunday)

    # End of week is Saturday (6 days later)
    end_of_week = start_of_week + timedelta(days=6)

    return start_of_week, end_of_week


def get_releases(
    api,
    date_after: str,
    date_before: Optional[str] = None,
    publisher_name: Optional[str] = None,
) -> List[Any]:
    """
    Fetch releases from Metron API within a date range.

    Args:
        api: Mokkari API client
        date_after: Start date (YYYY-MM-DD)
        date_before: End date (YYYY-MM-DD), optional. If None, fetches everything after start date.
        publisher_name: Optional publisher name to filter on server-side. The
            returned issues carry no publisher of their own, so this is also how
            the releases page learns which series belong to which publisher.

    Returns:
        List of issue objects
    """
    if not api:
        return []

    params = {"store_date_range_after": date_after}
    if date_before:
        params["store_date_range_before"] = date_before
    if publisher_name:
        params["publisher_name"] = publisher_name
    app_logger.info(f"Fetching releases with params: {params}")
    return (
        _api_call(lambda: api.issues_list(params), "getting releases", default=[]) or []
    )


def get_all_issues_for_series(api, series_id):
    """
    Retrieves all issues associated with a specific series ID.
    """

    def _call():
        params = {"series_id": series_id}
        app_logger.info(
            f"Fetching issues for series_id: {series_id} with params: {params}"
        )
        return api.issues_list(params)

    return (
        _api_call(_call, f"retrieving issues for series {series_id}", default=[]) or []
    )


def list_issues_modified_since(api, since_date: str, context: str = "") -> Dict[int, str]:
    """Ask Metron which issues its editors have touched since ``since_date``.

    This is the signal a tagger actually wants: Metron records are finished
    after a comic ships, and ``modified`` is the field that says so. Asking for
    the changed set is one paged list call, against one detail fetch per file
    for any approach that guesses from local timestamps instead.

    ``since_date`` is a plain ``YYYY-MM-DD`` string. Metron's filter is
    exclusive on the date, so a same-day sweep re-lists that day's changes --
    deliberate: the overlap costs a page and closes the gap a datetime-precise
    cursor would open around an interrupted run.

    Note that mokkari follows every ``next`` link inside one call
    (``Session._retrieve_all_results``) and does so *below* the shared pacer, so
    the window must stay small. Callers clamp it; see
    ``core.credit_backfill.MAX_LOOKBACK_DAYS``.

    Args:
        api: Mokkari API client
        since_date: Lower bound, ``YYYY-MM-DD``
        context: Extra text for the rate-limit log line

    Returns:
        ``{issue_id: modified_timestamp}``, empty on any failure.
    """
    if not api or not since_date:
        return {}

    params = {"modified_gt": since_date}
    label = f"listing issues modified since {since_date}{(' ' + context) if context else ''}"
    issues = _api_call(lambda: api.issues_list(params), label, default=[]) or []

    changed = {}
    for issue in issues:
        issue_id = _get_attr(issue, "id", None)
        if issue_id:
            changed[int(issue_id)] = str(_get_attr(issue, "modified", "") or "")

    app_logger.info(
        f"Metron reports {len(changed)} issue(s) modified since {since_date}"
    )
    return changed


def search_series_by_name(
    api, series_name: str, year: Optional[int] = None
) -> Optional[Dict[str, Any]]:
    """
    Search Metron for a series by name, optionally filtering by year.

    Args:
        api: Mokkari API client
        series_name: Series name to search for
        year: Optional year to filter/rank results by year_began

    Returns:
        Dict with id, name, cv_id, publisher_name, year_began, or None if not found
    """
    if not api or not series_name:
        return None

    def _call():
        app_logger.info(f"Searching Metron for series: '{series_name}' (year: {year})")
        results = api.series_list({"name": series_name})

        if not results:
            app_logger.info(f"No Metron series found for '{series_name}'")
            return None

        series_list = list(results)
        app_logger.info(f"Found {len(series_list)} Metron series matches")

        if year and len(series_list) > 1:

            def year_distance(s):
                s_year = getattr(s, "year_began", None)
                return abs(s_year - year) if s_year is not None else 9999

            series_list = sorted(series_list, key=year_distance)

        series = series_list[0]
        publisher = getattr(series, "publisher", None)
        publisher_name = getattr(publisher, "name", None) if publisher else None

        result = {
            "id": getattr(series, "id", None),
            "name": getattr(series, "name", "") or getattr(series, "display_name", ""),
            "cv_id": getattr(series, "cv_id", None),
            "publisher_name": publisher_name,
            "year_began": getattr(series, "year_began", None),
        }
        app_logger.info(
            f"Best Metron match: {result['name']} ({result['year_began']}) - cv_id: {result['cv_id']}"
        )
        return result

    return _api_call(_call, f"searching for series '{series_name}'")


def search_series_list(
    api, series_name: str, year: Optional[int] = None
) -> List[Dict[str, Any]]:
    """
    Search Metron for a series by name and return *all* candidate matches.

    Unlike search_series_by_name (which returns the single best match for the
    auto-tag path), this returns every result shaped for the selection modal so
    the user can pick when there are multiple ambiguous matches.

    Args:
        api: Mokkari API client
        series_name: Series name to search for
        year: Optional year to rank results by year_began proximity

    Returns:
        List of dicts with id, name, start_year, publisher_name, image_url,
        description, count_of_issues. Empty list if nothing found.
    """
    if not api or not series_name:
        return []

    def _call():
        app_logger.info(f"Searching Metron (list) for series: '{series_name}' (year: {year})")
        results = api.series_list({"name": series_name})
        if not results:
            return []

        series_list = list(results)

        if year and len(series_list) > 1:

            def year_distance(s):
                s_year = getattr(s, "year_began", None)
                return abs(s_year - year) if s_year is not None else 9999

            series_list = sorted(series_list, key=year_distance)

        matches = []
        for series in series_list:
            publisher = getattr(series, "publisher", None)
            publisher_name = getattr(publisher, "name", None) if publisher else None
            image = getattr(series, "image", None)
            image_url = str(image) if image else None
            matches.append({
                "id": getattr(series, "id", None),
                "name": getattr(series, "name", "") or getattr(series, "display_name", ""),
                "start_year": getattr(series, "year_began", None),
                "publisher_name": publisher_name or "",
                "image_url": image_url,
                "description": getattr(series, "desc", "") or "",
                "count_of_issues": getattr(series, "issue_count", None),
            })
        app_logger.info(f"Found {len(matches)} Metron series matches (list)")
        return matches

    return _api_call(_call, f"listing series '{series_name}'") or []


def get_series_details(api, series_id: int) -> Optional[Dict[str, Any]]:
    """
    Get full details for a Metron series including cv_id, publisher, year_began.

    Args:
        api: Mokkari API client
        series_id: Metron series ID

    Returns:
        Dict with id, cv_id, publisher_name, year_began, or None if not found
    """
    if not api or not series_id:
        return None

    def _call():
        series = api.series(series_id)
        if not series:
            return None
        publisher = getattr(series, "publisher", None)
        publisher_name = getattr(publisher, "name", None) if publisher else None
        result = {
            "id": series_id,
            "cv_id": getattr(series, "cv_id", None),
            "publisher_name": publisher_name,
            "year_began": getattr(series, "year_began", None),
        }
        app_logger.info(
            f"Metron series details: cv_id={result['cv_id']}, publisher={result['publisher_name']}, year={result['year_began']}"
        )
        return result

    return _api_call(_call, f"getting details for series {series_id}")


def get_series(api, series_id: int):
    """Fetch the full Metron series model, blocking and retrying on rate limits.

    Unlike a raw ``api.series(id)`` call (which raises ``RateLimitError`` under
    bulk load), this waits on the shared rate limiter and retries, so callers
    that fetch many series in a row (sync, matching, name repair) get the real
    object instead of erroring out. Returns the Mokkari series model, or None.
    """
    if not api or not series_id:
        return None
    return _api_call(lambda: api.series(series_id), f"getting series {series_id}")


def get_series_cv_id(api, series_id: int) -> Optional[int]:
    """
    Get the ComicVine ID for a Metron series.

    Args:
        api: Mokkari API client
        series_id: Metron series ID

    Returns:
        ComicVine volume ID, or None if not found
    """
    details = get_series_details(api, series_id)
    return details.get("cv_id") if details else None


def add_cvinfo_url(cvinfo_path: str, cv_id: int) -> bool:
    """
    Add or update the ComicVine URL as the first line of a cvinfo file.

    Args:
        cvinfo_path: Path to the cvinfo file
        cv_id: ComicVine volume ID

    Returns:
        True if successful, False otherwise
    """
    from os.path import dirname
    from core.config import is_oneshot_folder
    if is_oneshot_folder(dirname(cvinfo_path)):
        app_logger.debug(f"Skipping cvinfo write in one-shot folder: {cvinfo_path}")
        return False
    try:
        cv_url = f"https://comicvine.gamespot.com/volume/4050-{cv_id}/"

        # Read existing content
        with open(cvinfo_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Check if URL already exists
        if f"4050-{cv_id}" in content:
            app_logger.debug(f"CV URL already exists in {cvinfo_path}")
            return True

        # Check if any CV URL exists (different ID)
        if "comicvine.gamespot.com/volume/4050-" in content:
            app_logger.warning(
                f"Different CV URL exists in {cvinfo_path}, not overwriting"
            )
            return False

        # Prepend the URL to the content
        new_content = cv_url + "\n" + content

        with open(cvinfo_path, "w", encoding="utf-8") as f:
            f.write(new_content)

        app_logger.info(f"Added CV URL to cvinfo: {cv_url}")
        return True

    except Exception as e:
        app_logger.error(f"Error adding CV URL to {cvinfo_path}: {e}")
        return False


def create_cvinfo_file(
    cvinfo_path: str,
    cv_id: Optional[int],
    series_id: int,
    publisher_name: Optional[str] = None,
    start_year: Optional[int] = None,
) -> bool:
    """
    Create a cvinfo file with all available fields.

    Args:
        cvinfo_path: Path to create the cvinfo file
        cv_id: ComicVine volume ID (for URL)
        series_id: Metron series ID
        publisher_name: Publisher name
        start_year: Series start year (year_began)

    Returns:
        True if successful, False otherwise
    """
    from os.path import dirname
    from core.config import is_oneshot_folder
    if is_oneshot_folder(dirname(cvinfo_path)):
        app_logger.debug(f"Skipping cvinfo write in one-shot folder: {cvinfo_path}")
        return False
    try:
        lines = []

        # Add ComicVine URL if cv_id is available
        if cv_id:
            lines.append(f"https://comicvine.gamespot.com/volume/4050-{cv_id}/")

        # Add Metron series_id
        lines.append(f"series_id: {series_id}")

        # Add optional fields
        if publisher_name:
            lines.append(f"publisher_name: {publisher_name}")
        if start_year:
            lines.append(f"start_year: {start_year}")

        # Write to file
        with open(cvinfo_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        from helpers import match_parent_permissions
        match_parent_permissions(cvinfo_path)

        app_logger.info(f"Created cvinfo file: {cvinfo_path}")
        return True

    except Exception as e:
        app_logger.error(f"Error creating cvinfo file {cvinfo_path}: {e}")
        return False


def scrobble_issue(api, metron_issue_id: int, date_read: str = "") -> bool:
    """
    Scrobble (mark as read) an issue on Metron.

    Args:
        api: Mokkari API client
        metron_issue_id: Metron issue ID to mark as read
        date_read: Optional ISO timestamp for when the issue was read

    Returns:
        True if scrobble succeeded, False otherwise
    """
    # TODO: Fix date_read should be a datetime not a str, Metron's API is fixing this error
    scrobble_data = ScrobbleRequest(
        issue_id=metron_issue_id, date_read=date_read, rating=None
    )

    result = _api_call(
        lambda: api.collection_scrobble(scrobble_data) is not None,
        f"scrobbling issue {metron_issue_id}",
        default=False,
    )
    return bool(result)


def resolve_metron_issue_id(
    api, comic_path: str, issue_number: str = None
) -> Optional[int]:
    """
    Get Metron issue ID from ComicInfo.xml or by looking up via series.

    Strategy:
    1. Check ComicInfo.xml for <MetronId> tag
    2. Find cvinfo in parent folder to get series_id
    2.5. If no series_id, search Metron by series name from ComicInfo.xml
         and create/update cvinfo for future lookups
    3. Use get_all_issues_for_series() and match by issue number

    Args:
        api: Mokkari API client
        comic_path: Path to the comic file (CBZ)
        issue_number: Optional issue number (from ComicInfo.xml or filename)

    Returns:
        Metron issue ID, or None if not resolved
    """
    import os

    comic_info = None
    parent_folder = os.path.dirname(comic_path)

    # Step 1: Check ComicInfo.xml for MetronId
    try:
        from core.comicinfo import read_comicinfo_from_zip

        if os.path.exists(comic_path) and comic_path.lower().endswith((".cbz", ".zip")):
            comic_info = read_comicinfo_from_zip(comic_path)
            if comic_info:
                metron_id = comic_info.get("MetronId")
                if metron_id:
                    try:
                        return int(metron_id)
                    except (ValueError, TypeError):
                        pass
                # Also grab issue number from XML if not provided
                if not issue_number:
                    issue_number = comic_info.get("Number")
    except Exception as e:
        app_logger.warning(f"Could not read ComicInfo.xml for MetronId: {e}")

    if not issue_number:
        # Try extracting from filename as last resort
        from models.providers.base import extract_issue_number

        issue_number = extract_issue_number(os.path.basename(comic_path))

    if not issue_number:
        app_logger.debug(
            f"Cannot resolve Metron issue ID: no issue number for {comic_path}"
        )
        return None

    try:
        # Step 2: Find cvinfo in parent folder to get series_id
        from models.comicvine import find_cvinfo_in_folder

        cvinfo_path = find_cvinfo_in_folder(parent_folder)
        series_id = None

        if cvinfo_path:
            series_id = parse_cvinfo_for_metron_id(cvinfo_path)

        # Step 2.5: Search Metron by series name from ComicInfo.xml
        if not series_id and comic_info:
            series_name = comic_info.get("Series")
            volume_year = comic_info.get("Volume")
            if series_name:
                try:
                    year = int(volume_year) if volume_year else None
                except (ValueError, TypeError):
                    year = None
                search_result = search_series_by_name(api, series_name, year)
                if search_result:
                    series_id = search_result["id"]
                    # Persist to cvinfo for future lookups
                    if cvinfo_path:
                        update_cvinfo_with_metron_id(cvinfo_path, series_id)
                    else:
                        create_cvinfo_file(
                            os.path.join(parent_folder, "cvinfo"),
                            search_result.get("cv_id"),
                            series_id,
                            search_result.get("publisher_name"),
                            search_result.get("year_began"),
                        )
                    app_logger.info(
                        f"Found Metron series {series_id} via name search for '{series_name}'"
                    )

        if not series_id:
            app_logger.debug(f"Could not resolve series_id for {comic_path}")
            return None

        # Step 3: Fetch all issues for the series and match by number
        all_issues = get_all_issues_for_series(api, series_id)
        if not all_issues:
            return None

        # Normalize issue number for comparison (strip leading zeros)
        target = str(issue_number).strip().lstrip("0") or "0"

        for issue in all_issues:
            issue_num = getattr(issue, "number", None) or (
                issue.get("number") if isinstance(issue, dict) else None
            )
            if issue_num is not None:
                candidate = str(issue_num).strip().lstrip("0") or "0"
                if candidate == target:
                    issue_id = getattr(issue, "id", None) or (
                        issue.get("id") if isinstance(issue, dict) else None
                    )
                    if issue_id:
                        app_logger.info(
                            f"Resolved Metron issue ID {issue_id} for #{issue_number} in series {series_id}"
                        )
                        return int(issue_id)

        app_logger.debug(f"Could not match issue #{issue_number} in series {series_id}")
        return None

    except Exception as e:
        app_logger.warning(f"Error resolving Metron issue ID: {e}")
        return None


def fetch_reading_lists(api, params=None):
    """Fetch reading lists from Metron API.

    Args:
        api: Mokkari API client
        params: Optional dict of query params (e.g. {"name": "batman"})

    Returns:
        List of reading list dicts
    """
    if not api:
        return []

    results = _api_call(
        lambda: api.reading_lists_list(params or {}),
        "fetching reading lists",
        default=[],
    )
    if not results:
        return []
    return [_to_dict(item) for item in results]


def fetch_reading_list_detail(api, list_id):
    """Fetch full detail for a single Metron reading list.

    Args:
        api: Mokkari API client
        list_id: Metron reading list ID

    Returns:
        Dict with reading list detail, or None
    """
    if not api:
        return None

    result = _api_call(
        lambda: api.reading_list(list_id),
        f"fetching reading list {list_id}",
    )
    if not result:
        return None
    return _to_dict(result)


def fetch_reading_list_items(api, list_id):
    """Fetch items (issues) for a Metron reading list.

    Args:
        api: Mokkari API client
        list_id: Metron reading list ID

    Returns:
        List of reading list item dicts
    """
    if not api:
        return []

    results = _api_call(
        lambda: api.reading_list_items(list_id),
        f"fetching items for reading list {list_id}",
        default=[],
    )
    if not results:
        return []
    return [_to_dict(item) for item in results]


def fetch_arcs(api, params=None):
    """Fetch story arcs from Metron.

    Args:
        api: Mokkari API client
        params: Optional dict of query parameters (e.g. {"name": "..."})

    Returns:
        List of arc dicts
    """
    if not api:
        return []

    results = _api_call(
        lambda: api.arcs_list(params or {}),
        "fetching story arcs",
        default=[],
    )
    if not results:
        return []
    return [_to_dict(item) for item in results]


def fetch_arcs_page(api, params=None, page=1):
    """Fetch a single page of story arcs from Metron (no auto-pagination).

    Bypasses Mokkari's auto-pagination by making a direct HTTP request,
    returning only the requested page of results.

    Args:
        api: Mokkari API client (used for credentials and user-agent)
        params: Optional dict of query parameters (e.g. {"name": "..."})
        page: Page number to fetch (default 1)

    Returns:
        dict with keys: results (list of dicts), has_next (bool), count (int), page (int)
    """
    if not api:
        return {"results": [], "has_next": False, "count": 0, "page": page}

    url = "https://metron.cloud/api/arc/"
    query_params = {"page": page}
    if params:
        query_params.update(params)

    # ``api.header`` already carries the Bearer header on a token session, so
    # basic auth is only sent when there is a real pair to send. Passing
    # ``(None, None)`` here would strip the token session's only credential and
    # turn every arc page into a 401.
    headers = getattr(api, "header", {})
    username = getattr(api, "username", None)
    passwd = getattr(api, "passwd", None)
    auth = (username, passwd) if username and passwd else None

    for attempt in range(_RATE_LIMIT_MAX_RETRIES):
        if _auth_block.blocked():
            break
        _metron_rate_limiter.acquire()
        try:
            resp = requests_lib.get(
                url, params=query_params, auth=auth, headers=headers, timeout=30
            )
            if resp.status_code in _AUTH_FAILURE_STATUSES:
                # This request is built by hand, so it never passes through
                # ``_api_call`` -- it has to latch the lockout itself.
                latch_auth_failure(
                    resp.status_code,
                    (resp.text or "").strip()[:200],
                    f"fetching arcs page {page}",
                )
                break
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", _RATE_LIMIT_DEFAULT_WAIT))
                if retry_after > _DAILY_RATE_LIMIT_THRESHOLD:
                    app_logger.info(
                        f"Metron daily rate limit exceeded fetching arcs page {page}"
                    )
                    break
                if attempt < _RATE_LIMIT_MAX_RETRIES - 1:
                    app_logger.info(
                        f"Metron rate limit hit fetching arcs page {page}: "
                        f"waiting {retry_after}s (attempt {attempt + 1}/{_RATE_LIMIT_MAX_RETRIES})"
                    )
                    time.sleep(retry_after)
                    continue
                break
            resp.raise_for_status()
            data = resp.json()
            return {
                "results": data.get("results", []),
                "has_next": data.get("next") is not None,
                "count": data.get("count", 0),
                "page": page,
            }
        except requests_lib.exceptions.RequestException as e:
            app_logger.error(f"Error fetching arcs page {page}: {e}")
            break

    return {"results": [], "has_next": False, "count": 0, "page": page}


def fetch_arc_detail(api, arc_id):
    """Fetch full detail for a single Metron story arc.

    Args:
        api: Mokkari API client
        arc_id: Metron story arc ID

    Returns:
        Dict with arc detail, or None
    """
    if not api:
        return None

    result = _api_call(
        lambda: api.arc(arc_id),
        f"fetching story arc {arc_id}",
    )
    if not result:
        return None
    return _to_dict(result)


def fetch_arc_issues(api, arc_id):
    """Fetch issues for a Metron story arc.

    Args:
        api: Mokkari API client
        arc_id: Metron story arc ID

    Returns:
        List of issue dicts
    """
    if not api:
        return []

    results = _api_call(
        lambda: api.arc_issues_list(arc_id),
        f"fetching issues for story arc {arc_id}",
        default=[],
    )
    if not results:
        return []
    return [_to_dict(item) for item in results]
