"""
Metron API integration for comic metadata retrieval using Mokkari library.
"""

from core.app_logging import app_logger
from typing import Optional, Dict, Any, List
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from core.version import CLU_USER_AGENT

import requests as requests_lib
import requests.exceptions as requests_exceptions
from core.provider_client import (
    AuthLockout, GuardedClient, LibraryClient, QuotaLatch, Verdict,
)
from mokkari.rate_limit import HeaderPacedRateLimiter, RateLimitStatus, RateLimitWindow
from mokkari.session import Session as MokkariSession
from mokkari.sqlite_cache import SqliteCache
from mokkari.exceptions import ApiError, AuthenticationError, RateLimitError
from mokkari.schemas.collection import ScrobbleRequest

_RATE_LIMIT_MAX_RETRIES = 3
_RATE_LIMIT_DEFAULT_WAIT = 60  # seconds, used when retry_after is 0 or unset
_DAILY_RATE_LIMIT_THRESHOLD = (
    60  # seconds; retry_after above this implies daily limit exceeded
)

# Metron's burst limit is 20 requests/minute. CLU runs multiple background
# threads (bulk metadata jobs, wanted-cache refresh, reading-list sync) plus
# live web requests that can all reach Metron concurrently. Pacing happens
# inside mokkari, on every HTTP send -- each page of a paginated list
# included -- through the ``rate_limiter`` each Session is built with (#703).
# One Session per API token, so one limiter per Metron account sees every
# request this process makes. A slot taken once per call instead let one
# call fetch dozens of pages unpaced. The cache holds the ``MetronClient``
# wrapping each Session.
_SESSION_CACHE: Dict[str, "MetronClient"] = {}
_SESSION_CACHE_LOCK = threading.Lock()

# mokkari 5.0 removed username/password auth: a token is the only credential.
# Set once the "username is no longer supported" warning has been logged, so a
# basic-auth-only install says so once rather than on every call.
_BASIC_AUTH_WARNED = False


def _metron_cache_expire_days() -> int:
    """Days a cached Metron response stays valid (``METRON_CACHE_EXPIRE_DAYS``).

    Deliberately short. ``scheduled_series_sync`` decides whether to do the
    expensive issue fetch by comparing the API's ``issue_count`` against the
    local count, so a cached series response means that decision runs on stale
    data and a newly published issue can be missed for one cycle. One day keeps
    that bounded while still collapsing a whole library sweep -- which re-reads
    the same series and issue endpoints from ``_sync_and_match``, the scan tail,
    the re-match job and the wanted-cache refresh -- down to one request each.

    Never ``None``: mokkari treats a ``None`` TTL as "never expire".
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

    ``get()`` honours each row's expiry, so ``default_ttl`` is a real TTL.
    It covers every resource CLU reads; mokkari's own ``DEFAULT_TTLS`` only
    disable caching for the per-user collection, pull list and wish list.
    A database written by mokkari 4.x (``responses`` table) is discarded on
    open, so the same file path is reused across the upgrade.
    """
    try:
        from helpers.provider_cache import provider_cache_dir

        return SqliteCache(
            db_name=os.path.join(provider_cache_dir("mokkari"), "mokkari_cache.db"),
            default_ttl=timedelta(days=_metron_cache_expire_days()),
        )
    except Exception as e:
        # A read-only volume must degrade to "uncached", never to "no Metron".
        app_logger.warning(f"Metron response cache unavailable, continuing uncached: {e}")
        return None


# Metron's documented minimum burst limit, and what Settings shows
# (``MetronProvider.rate_limit``; a test keeps the two equal). Used only until
# the first response reports the real one.
_DEFAULT_BURST_LIMIT = 20


class _CluRateLimiter(HeaderPacedRateLimiter):
    """mokkari's header-paced limiter, paced from the first request.

    ``HeaderPacedRateLimiter`` learns the burst limit from response headers,
    so until the first response arrives it has no limit and spaces nothing:
    every thread that starts at once -- a scheduled sweep, a bulk job and a
    page load after a restart -- goes out together. Until Metron says
    otherwise, assume its documented minimum.
    """

    @staticmethod
    def _seeded(status):
        if status is None or status.burst.limit is not None:
            return status
        burst = status.burst
        return RateLimitStatus(
            burst=RateLimitWindow(
                limit=_DEFAULT_BURST_LIMIT, remaining=burst.remaining, reset=burst.reset
            ),
            sustained=status.sustained,
        )

    def acquire(self, status):
        super().acquire(self._seeded(status))

    def release(self, status):
        super().release(self._seeded(status))


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


# The lockout is shared with every provider (``core.provider_client``); the
# preference keys it derives from "metron" are the PREF_AUTH_* names above.
_auth_block = AuthLockout("metron", "Metron")


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


# Metron reports two windows, and they need opposite responses:
#
# * **burst** (per-minute, same for everyone). Short, worth waiting out -- and
#   the session's limiter does exactly that, per send. Nothing here.
# * **sustained** (daily, varies by donor tier). Hours away. Waiting is
#   pointless, so once it is known to be empty we stop calling until it resets.
_daily = QuotaLatch("Metron", "daily")


def daily_limit_reached() -> bool:
    """True while Metron's daily quota is known to be spent."""
    return _daily.reached()


def _observe_sustained(session) -> None:
    """Latch the daily window when the last response says it is empty.

    Every field is ``None`` until the first response completes, and Metron may
    not report the window at all, so each step has to tolerate a gap.
    """
    status = getattr(session, "rate_limit_status", None)
    sustained = getattr(status, "sustained", None)
    if sustained is None:
        return
    try:
        remaining = getattr(sustained, "remaining", None)
        reset = _as_aware(getattr(sustained, "reset", None))
        if isinstance(remaining, int) and remaining <= 0 and reset is not None:
            _daily.latch_until(reset)
    except Exception as e:  # never let telemetry break a worker thread
        app_logger.debug(f"Metron rate-limit header parse skipped: {e}")


def _observe_all_sessions() -> None:
    for session in _cached_sessions():
        _observe_sustained(session)


class _MetronPolicy(GuardedClient):
    """Metron's half of the shared call policy: how to read its errors."""

    def _classify(self, exc):
        if isinstance(exc, RateLimitError):
            return self._classify_rate_limit(exc)
        if isinstance(exc, ApiError):
            status = auth_failure_status(exc)
            if status is not None:
                return Verdict.auth(status, _auth_detail(exc))
            if is_connection_error(exc):
                return Verdict.connection()
            return Verdict.error()
        return None

    @staticmethod
    def _classify_rate_limit(exc):
        retry_after = getattr(exc, "retry_after", 0) or 0
        if retry_after > _DAILY_RATE_LIMIT_THRESHOLD:
            return Verdict.quota(retry_after)
        # mokkari reports retry_after as max(burst_wait, sustained_wait), so a
        # spent daily quota can arrive as a sub-threshold wait (observed: 59.5s)
        # and get retried three times per series for a whole sweep. The headers
        # say which window it really is -- trust them over the magnitude.
        _observe_all_sessions()
        if _daily.reached():
            return Verdict.quota()
        if type(exc) is RateLimitError:
            # mokkari raises the base class only for a request it never sent:
            # the session's limiter refusing because the daily window is spent.
            # A 429 that Metron returned is a subclass.
            if retry_after > 0:
                return Verdict.quota(retry_after)
            return Verdict.retry(_RATE_LIMIT_DEFAULT_WAIT)
        # A 429 that Metron returned. The session's limiter has already blocked
        # every thread for its Retry-After, so the next send waits by itself;
        # sleeping here too is how the two retry layers used to stack.
        return Verdict.retry(0)

    def _after_success(self):
        _observe_all_sessions()


_metron_policy = _MetronPolicy("Metron", _auth_block, _daily)


def paused_reason() -> Optional[str]:
    """Why Metron calls are being skipped right now, or None."""
    return _metron_policy.paused_reason()


def _cached_sessions() -> List[MokkariSession]:
    with _SESSION_CACHE_LOCK:
        return [client._library for client in _SESSION_CACHE.values()]


def invalidate_session_cache() -> None:
    """Clear the cached Metron sessions and rate-limiter window (used by tests).

    Also runs each session's cache ``cleanup()``. mokkari never returns an
    expired row, so this only reclaims the space they take on disk.
    """
    with _SESSION_CACHE_LOCK:
        sessions = [client._library for client in _SESSION_CACHE.values()]
        _SESSION_CACHE.clear()
    for session in sessions:
        cache = getattr(session, "cache", None)
        if cache is None:
            continue
        try:
            cache.cleanup()
        except Exception:
            pass
    _daily.reset()
    # Credentials may have changed under us; re-read the lockout from the
    # database rather than trusting a memo taken with the old ones.
    _auth_block.forget()


# mokkari's default; a Session built by a test stand-in may not carry one.
_METRON_API_URL = "https://metron.cloud/api/{}/"


def _sessions_to_purge(api) -> List[MokkariSession]:
    return [api._library] if api is not None else _cached_sessions()


def _purge_cached(api, context: str, keys_for, resources=()) -> int:
    """Drop cached mokkari responses through the cache's public API.

    mokkari keys a response by its full URL plus sorted query string, and
    ``SqliteCache.get()`` already ignores expired rows, so the only reason to
    purge is to make the *next* call reach Metron now rather than when the TTL
    runs out.

    Args:
        api: Optional ``MetronClient`` whose session to purge; defaults to
            every cached session.
        context: Human-readable subject, used only in log messages.
        keys_for: Called with a session's ``api_url`` template; returns the
            exact keys to delete.
        resources: ``(resource, kind)`` pairs to invalidate wholesale -- for
            lists, whose keys carry paging and filter params that can't be
            rebuilt. A list is served from the cache only when every page is
            cached, so dropping its pages is enough to force a refetch.

    Returns:
        Number of cached rows removed.
    """
    removed = 0
    seen: List[Any] = []
    for session in _sessions_to_purge(api):
        cache = getattr(session, "cache", None)
        if cache is None or any(cache is c for c in seen):
            continue
        seen.append(cache)
        api_url = getattr(session, "api_url", None)
        if not isinstance(api_url, str):
            api_url = _METRON_API_URL
        try:
            for key in keys_for(api_url):
                if cache.delete(key):
                    removed += 1
            for resource, kind in resources:
                removed += int(cache.invalidate(resource, kind) or 0)
        except Exception as e:
            app_logger.warning(
                f"Could not purge Metron response cache for {context}: {e}"
            )
    return removed


def purge_issue_cache(issue_id, api=None) -> int:
    """Drop mokkari's cached ``/issue/<id>/`` body so the next call re-fetches.

    That response is the one carrying ``credits`` and ``characters``, and Metron
    records are routinely completed *after* release day -- an issue tagged on
    release morning can be indexed hours later. Within the cache TTL the
    incomplete body would otherwise be replayed, so a re-tag would rewrite
    exactly the metadata the user is trying to repair.

    Args:
        issue_id: Metron issue id whose cached detail response should be dropped.
        api: Optional ``MetronClient`` to purge; defaults to every cached session.

    Returns:
        Number of cached rows removed.
    """
    try:
        issue_id = int(issue_id)
    except (TypeError, ValueError):
        return 0

    removed = _purge_cached(
        api, f"issue {issue_id}", lambda url: [url.format(f"issue/{issue_id}")]
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

    ``Session._get`` consults ``SqliteCache`` *before* dispatching, so a
    user-initiated "refresh from Metron" that only bypasses CLU's own database
    would still get whatever Metron returned within the cache TTL. A Summary
    edited on Metron would then not appear.

    Args:
        series_id: Metron series id whose cached responses should be dropped.
        api: Optional ``MetronClient`` to purge; defaults to every cached session.

    Returns:
        Number of cached rows removed.
    """
    try:
        series_id = int(series_id)
    except (TypeError, ValueError):
        return 0

    # The series detail body, plus every cached issue *list*: those are keyed
    # by filter and page (``series_id=``, ``number=``, ``page=``...), so they
    # can't be singled out. Issue lists are cheap to refetch.
    removed = _purge_cached(
        api,
        f"series {series_id}",
        lambda url: [url.format(f"series/{series_id}")],
        resources=[("issue", "list")],
    )

    # The per-issue detail bodies hold the credits, and their keys say nothing
    # about the series -- ask CLU's own issue cache which ids belong to it.
    # Guarded, so an uncached session never pays for the database round trip.
    if any(getattr(s, "cache", None) is not None for s in _sessions_to_purge(api)):
        for issue_id in _known_issue_ids(series_id):
            removed += purge_issue_cache(issue_id, api=api)

    if removed:
        app_logger.info(
            f"Purged {removed} cached Metron responses for series {series_id}"
        )
    return removed


# mokkari has no per-call cache bypass, so a call that asks "what changed?"
# has to drop its own cached answer first -- otherwise a second sweep within
# the TTL replays the first one's and misses everything edited in between. A
# paged list is only served from the cache when every page is cached, so
# dropping the first page is enough to make the whole list re-fetch.


def purge_modified_since_cache(resource: str, since_date: str, api=None) -> int:
    """Drop the cached ``/<resource>/?modified_gt=<since_date>`` listing.

    The key is the one ``Session._fetch`` builds: the URL plus the sorted,
    urlencoded params. Only valid for resources that are not per-user.
    """
    query = urlencode(sorted({"modified_gt": since_date}.items()))
    return _purge_cached(
        api,
        f"{resource} modified since {since_date}",
        lambda url: [f"{url.format(resource)}?{query}"],
    )


def purge_reading_list_cache(api=None) -> int:
    """Drop every cached Metron reading-list response.

    ``reading_list`` is a per-user resource: mokkari suffixes its keys with a
    hash of the token, so an exact ``delete(url)`` never matches. Invalidating
    the resource wholesale is cheap -- reading lists are only read by import
    and sync -- and it takes the detail *and* its items together, so a sync
    can never pair a fresh ``modified`` with stale items and stamp the token.
    """
    return _purge_cached(
        api, "reading lists", lambda url: [], resources=[("reading_list", None)]
    )


def purge_arc_issues_cache(arc_id, api=None) -> int:
    """Drop the cached ``/arc/<id>/issue_list/`` (cached as an ``issue`` list)."""
    try:
        arc_id = int(arc_id)
    except (TypeError, ValueError):
        return 0
    return _purge_cached(
        api, f"arc {arc_id}", lambda url: [url.format(f"arc/{arc_id}/issue_list")]
    )


# mokkari 5 Session's read methods, plus ``collection_scrobble`` -- the one
# write CLU sends. Every other write (``*_post``, ``*_patch``,
# ``collection_add``...) is deliberately absent, so it cannot be called.
_MOKKARI_READS = frozenset({
    "arc", "arc_issues_list", "arcs_list", "character", "character_issues_list",
    "characters_list", "collection", "collection_missing_issues",
    "collection_missing_series", "collection_scrobble", "collection_stats",
    "collections_list", "creator", "creators_list", "imprint", "imprints_list",
    "issue", "issues_list", "publisher", "publishers_list", "pull_list",
    "pull_list_issues", "pull_list_series", "reading_list", "reading_list_items",
    "reading_lists_list", "role_list", "series", "series_issues_list",
    "series_list", "series_type_list", "team", "team_issues_list", "teams_list",
    "universe", "universes_list", "wish_list", "wish_list_items",
})


class MetronClient(LibraryClient):
    """A mokkari ``Session`` whose methods run behind Metron's call policy.

    ``get_api()`` / ``get_flask_api()`` hand these out. Every method has
    mokkari's name (``api.series(5)``, ``api.issues_list(params)``) and keeps
    the contract Metron callers have always relied on: on failure it returns
    the default (``None``, or whatever ``using(default=...)`` set) instead of
    raising.

    * While Metron's daily quota is spent or its credentials have been
      rejected, a call returns the default immediately rather than sleeping:
      a sweep of 800 series must not sit through 800 x 3 x 60s of waits that
      can't succeed.
    * Pacing is not done here: the session's limiter paces every HTTP send,
      each page of a list included.
    * ``using(ignore_auth_lock=True, raise_auth_errors=True)`` is for the
      connection test, which must run while the lockout is latched and needs
      the rejection itself to report it. ``using(raise_api_errors=True)``
      re-raises any other ``ApiError``, for a route that answers "Metron is
      unavailable" from ``is_connection_error``.
    """

    READ_METHODS = _MOKKARI_READS

    def __init__(self, library, options=None):
        super().__init__(library, _metron_policy, options)

    def arcs_page(self, params=None, page=1):
        """Fetch one page of story arcs, without mokkari's auto-pagination.

        The one Metron request mokkari does not send, so it carries the rules
        by hand: it takes the session's limiter around its own send, reports
        a 429 to it, and latches the lockout and the daily quota itself.

        Returns:
            dict with keys: results (list of dicts), has_next (bool),
            count (int), page (int)
        """
        session = self._library
        url = "https://metron.cloud/api/arc/"
        query_params = {"page": page}
        if params:
            query_params.update(params)

        # mokkari 5 keeps the Bearer token out of ``session.header`` (so it
        # isn't logged with the request headers), so this hand-built request
        # has to add it. Without it every arc page is a 401 -- which latches
        # the lockout below and stops *all* Metron traffic.
        headers = dict(getattr(session, "header", None) or {})
        api_token = getattr(session, "api_token", None)
        if isinstance(api_token, str) and api_token:
            headers["Authorization"] = f"Bearer {api_token}"

        # A hand-built request never passes through mokkari's send, so it
        # takes the session's limiter itself -- the same one mokkari takes for
        # every request it sends -- or it is the one Metron request nothing
        # paces. ``release`` gets the session's last status: this response's
        # headers cannot be fed back through mokkari's public API, and the
        # next mokkari send refreshes it.
        limiter = getattr(session, "rate_limiter", None)
        context = f"fetching arcs page {page}"

        for attempt in range(_RATE_LIMIT_MAX_RETRIES):
            if _auth_block.blocked() or _daily.reached():
                break
            if limiter is not None:
                try:
                    limiter.acquire(session.rate_limit_status)
                except RateLimitError as e:
                    # The limiter refuses only when the daily window is spent.
                    _daily.latch_for(getattr(e, "retry_after", 0) or 0, context)
                    break
            try:
                resp = requests_lib.get(
                    url, params=query_params, headers=headers, timeout=30
                )
            except requests_lib.exceptions.RequestException as e:
                app_logger.error(f"Error fetching arcs page {page}: {e}")
                break
            finally:
                if limiter is not None:
                    try:
                        limiter.release(session.rate_limit_status)
                    except Exception as e:
                        app_logger.debug(f"Metron rate limiter release failed: {e}")

            if resp.status_code in _AUTH_FAILURE_STATUSES:
                # This request is built by hand, so it never passes through
                # the policy -- it has to latch the lockout itself.
                latch_auth_failure(
                    resp.status_code, (resp.text or "").strip()[:200], context
                )
                break
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("Retry-After", 0) or 0)
                if retry_after > _DAILY_RATE_LIMIT_THRESHOLD:
                    _daily.latch_for(retry_after, context)
                    break
                if limiter is not None:
                    # Blocks every Metron send, mokkari's included, for Retry-After.
                    limiter.on_rate_limited(retry_after)
                if attempt < _RATE_LIMIT_MAX_RETRIES - 1:
                    app_logger.info(
                        f"Metron rate limit hit {context}: retrying after "
                        f"{retry_after or _RATE_LIMIT_DEFAULT_WAIT}s "
                        f"(attempt {attempt + 1}/{_RATE_LIMIT_MAX_RETRIES})"
                    )
                    if limiter is None:
                        time.sleep(retry_after or _RATE_LIMIT_DEFAULT_WAIT)
                    continue
                break
            try:
                resp.raise_for_status()
            except requests_lib.exceptions.RequestException as e:
                app_logger.error(f"Error fetching arcs page {page}: {e}")
                break
            data = resp.json()
            return {
                "results": data.get("results", []),
                "has_next": data.get("next") is not None,
                "count": data.get("count", 0),
                "page": page,
            }

        return {"results": [], "has_next": False, "count": 0, "page": page}


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


def _warn_basic_auth_unsupported() -> None:
    """Say once that a username/password without a token no longer works."""
    global _BASIC_AUTH_WARNED
    if _BASIC_AUTH_WARNED:
        return
    _BASIC_AUTH_WARNED = True
    app_logger.warning(
        "Metron username/password authentication is no longer supported "
        "(mokkari 5.0 removed it); add a Metron API token in Settings."
    )


def get_api(
    username: str = "",
    password: str = "",
    api_token: str = "",
    ignore_auth_lock: bool = False,
):
    """
    Return the shared ``MetronClient`` for this API token.

    Clients are cached per token and reused across calls and threads, rather
    than created fresh each time. Each wraps one mokkari Session. Each Session carries its own
    ``_CluRateLimiter``, so one cache entry per token is one limiter per
    Metron account, seeing every request this process sends -- as mokkari
    asks ("construct one instance per Session").

    Returns None while Metron has rejected our credentials. This gate sits
    *before* the session cache on purpose: a caller that fetches a client once
    and loops over it then never starts, rather than skipping each call.

    Args:
        username: Ignored. Kept so existing positional callers still work;
            mokkari 5.0 supports token authentication only.
        password: Ignored, as ``username``.
        api_token: Metron API token.
        ignore_auth_lock: Build a client even while the lockout is latched.
            Only the connection test may pass this -- without it a user could
            never verify the credentials that would clear the lockout.

    Returns:
        A ``MetronClient``, or None if unavailable
    """
    if not api_token:
        if username and password:
            _warn_basic_auth_unsupported()
        else:
            app_logger.warning("Metron credentials not configured")
        return None

    if not ignore_auth_lock and _auth_block.blocked():
        app_logger.debug("Metron authentication is blocked; not issuing a client")
        return None

    with _SESSION_CACHE_LOCK:
        client = _SESSION_CACHE.get(api_token)
        if client is not None:
            return client

    try:
        session = MokkariSession(
            api_token,
            user_agent=CLU_USER_AGENT,
            cache=_metron_cache(),
            rate_limiter=_CluRateLimiter(),
        )
    except (ApiError, AuthenticationError) as e:
        app_logger.error(f"Metron API error initializing session: {e}")
        return None
    except Exception as e:
        app_logger.error(f"Failed to initialize Metron API: {e}")
        return None

    with _SESSION_CACHE_LOCK:
        # Another thread may have raced us to create a session for the same
        # token; keep whichever one won so only one is ever cached.
        client = _SESSION_CACHE.setdefault(api_token, MetronClient(session))
    return client


def get_flask_api(app=None):
    """Get Metron API client using Flask app config credentials.

    Args:
        app: Flask app instance. If None, uses current_app (requires app context).

    Returns:
        A ``MetronClient``, or None if no token is configured or it is invalid.
    """
    if app is None:
        from flask import current_app
        config = current_app.config
    else:
        config = app.config
    api_token = (config.get("METRON_API_TOKEN") or "").strip()
    if not api_token:
        if (config.get("METRON_USERNAME") or "").strip():
            _warn_basic_auth_unsupported()
        return None
    return get_api(api_token=api_token)


def is_metron_configured(app=None):
    """Check if a Metron API token is present in Flask app config.

    Args:
        app: Flask app instance. If None, uses current_app (requires app context).

    Returns:
        True if an API token is configured and Metron has not rejected it.
        A username and password alone no longer count: mokkari 5.0 dropped
        basic authentication.
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
    return bool((config.get("METRON_API_TOKEN") or "").strip())


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
    results = api.using(context=f"looking up CV ID {cv_series_id}").series_list(
        {"cv_id": cv_series_id}
    )
    if results is None:
        # Failed or paused: that says nothing about Metron's catalogue.
        return None
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
    Metron records are routinely completed hours after release day, so a body
    fetched while the record was half-entered would be replayed until the cache
    TTL ran out -- and a re-tag meant to repair the file would rewrite the very
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

    label = f"fetching details for issue ID {issue_id}"
    return api.using(context=f"{label} {context}".strip()).issue(issue_id)


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

    issues = api.using(
        context=f"searching issue {issue_number} in series {series_id}"
    ).issues_list({"series_id": series_id, "number": issue_number})
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
    return api.using(default=[], context="getting releases").issues_list(params) or []


def get_all_issues_for_series(api, series_id):
    """
    Retrieves all issues associated with a specific series ID.
    """

    params = {"series_id": series_id}
    app_logger.info(f"Fetching issues for series_id: {series_id} with params: {params}")
    return (
        api.using(default=[], context=f"retrieving issues for series {series_id}")
        .issues_list(params)
        or []
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

    # A same-day re-run must re-list the day, not replay the cached answer.
    purge_modified_since_cache("issue", since_date, api=api)
    params = {"modified_gt": since_date}
    label = f"listing issues modified since {since_date}{(' ' + context) if context else ''}"
    issues = api.using(default=[], context=label).issues_list(params) or []

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

    app_logger.info(f"Searching Metron for series: '{series_name}' (year: {year})")
    results = api.using(context=f"searching for series '{series_name}'").series_list(
        {"name": series_name}
    )
    if results is None:
        return None
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


_SERIES_SEP_RE = re.compile(r"\s+[-–—]\s+|\s*:\s*")


def _norm_series_words(name: str) -> set:
    """Lower-cased word set of a series name, punctuation ignored."""
    return set(re.findall(r"[a-z0-9]+", (name or "").lower()))


def _series_search_terms(series_name: str) -> List[str]:
    """Search terms to try against Metron's substring ``name`` filter, in order.

    Metron titles a series "Nova: Return of the Rings" where a filename says
    "Nova - Return of the Rings"; the raw name is not a substring of the title,
    so it finds nothing. Try the raw name, the ":" spelling, then the longest
    segment on either side of the separator.
    """
    terms = [series_name.strip()]
    parts = [p.strip() for p in _SERIES_SEP_RE.split(series_name) if p.strip()]
    if len(parts) > 1:
        terms.append(": ".join(parts))
        terms.append(max(parts, key=len))
    out = []
    for t in terms:
        if t and t not in out:
            out.append(t)
    return out


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

    app_logger.info(f"Searching Metron (list) for series: '{series_name}' (year: {year})")
    series_list = []
    seen_ids = set()
    for term in _series_search_terms(series_name):
        # Each variant is its own guarded request: a failed one is skipped
        # and the next spelling is still tried.
        results = api.using(context=f"listing series '{term}'").series_list(
            {"name": term}
        )
        for s in (list(results) if results else []):
            if getattr(s, "id", None) in seen_ids:
                continue
            seen_ids.add(getattr(s, "id", None))
            series_list.append(s)
        if series_list:
            break
    if not series_list:
        return []

    # A shortened term (the tail after a "-"/":") is broader than the file's
    # series name; keep only candidates containing every word of it.
    want = _norm_series_words(series_name)
    narrowed = [
        s for s in series_list
        if want <= _norm_series_words(
            getattr(s, "name", "") or getattr(s, "display_name", ""))
    ]
    if narrowed:
        series_list = narrowed

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

    series = api.using(context=f"getting details for series {series_id}").series(series_id)
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


def get_series(api, series_id: int):
    """Fetch the full Metron series model, blocking and retrying on rate limits.

    Retries a 429 once the session's limiter allows, so callers that fetch
    many series in a row (sync, matching, name repair) get the real object
    instead of erroring out. Returns the Mokkari series model, or None.
    """
    if not api or not series_id:
        return None
    return api.using(context=f"getting series {series_id}").series(series_id)


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

    result = api.using(context=f"scrobbling issue {metron_issue_id}").collection_scrobble(
        scrobble_data
    )
    return result is not None


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

    results = api.using(default=[], context="fetching reading lists").reading_lists_list(
        params or {}
    )
    if not results:
        return []
    return [_to_dict(item) for item in results]


def list_reading_lists_modified_since(api, since_date: str) -> Optional[Dict[int, str]]:
    """Ask Metron which reading lists have changed since ``since_date``.

    One paged call answers for every imported list at once, against a detail
    request per list for anything that checks them individually. Reading list
    *items* belong to the list, so Metron bumps the list's ``modified`` when
    its contents change -- which is what makes this work here and not for arcs,
    whose membership lives on the issues.

    ``since_date`` is a plain ``YYYY-MM-DD`` string and Metron's filter is
    **exclusive on the date**, so a caller comparing against a stored timestamp
    must step back a day or lose a list that moved later on the day it was
    synced. ``core.reading_list_sync._metron_bulk_prefilter`` does.

    As with ``list_issues_modified_since``, mokkari follows every ``next`` link
    inside this one call and does so *below* the shared pacer, so the window
    has to stay small; see ``core.reading_list_sync.MAX_BULK_LOOKBACK_DAYS``.

    Returns:
        ``{list_id: modified_timestamp}``, or **None** if the call failed.
        Empty-dict and None are different answers and the caller acts on the
        difference: an empty dict is Metron saying nothing changed.
    """
    if not api or not since_date:
        return None

    # A cached answer still carries a list's old ``modified``, which matches
    # the stored token, so the sweep would skip a list that has moved.
    purge_reading_list_cache(api=api)
    params = {"modified_gt": since_date}
    results = api.using(
        context=f"listing reading lists modified since {since_date}"
    ).reading_lists_list(params)
    if results is None:
        return None

    changed = {}
    for item in results:
        item_id = _get_attr(item, "id", None)
        if item_id:
            changed[int(item_id)] = str(_get_attr(item, "modified", "") or "")

    app_logger.info(
        f"Metron reports {len(changed)} reading list(s) modified since {since_date}"
    )
    return changed


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

    result = api.using(context=f"fetching reading list {list_id}").reading_list(list_id)
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

    results = api.using(
        default=[], context=f"fetching items for reading list {list_id}"
    ).reading_list_items(list_id)
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

    results = api.using(default=[], context="fetching story arcs").arcs_list(params or {})
    if not results:
        return []
    return [_to_dict(item) for item in results]


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

    result = api.using(context=f"fetching story arc {arc_id}").arc(arc_id)
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

    results = api.using(
        default=[], context=f"fetching issues for story arc {arc_id}"
    ).arc_issues_list(arc_id)
    if not results:
        return []
    return [_to_dict(item) for item in results]
