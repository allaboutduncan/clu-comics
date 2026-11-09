"""Pass a Cloudflare challenge with the bundled FlareSolverr.

cloudscraper cannot pass Cloudflare's managed "Just a moment..." challenge, so
a GetComics mirror that serves one used to end the download with a
"download manually" notice. FlareSolverr drives a real Chromium, which can.

**FlareSolverr never fetches the payload.** It returns a page's HTML plus the
clearance cookies and the User-Agent it earned them with. We copy those onto
the requests/cloudscraper session the caller already has, and the caller retries
its own request, with streaming, resume, progress and cancellation intact.

Things that look arbitrary and are not:

- **We solve the site's root, not the URL that was challenged.**
  ``cf_clearance`` is issued per site, and the challenged URL is often the
  archive itself. Pointing Chromium at it would download the whole file into
  the browser profile just to read a cookie.
- **The User-Agent is replaced, not merged.** Cloudflare binds the clearance to
  the UA that earned it. ``download_getcomics`` deliberately keeps
  cloudscraper's UA over caller headers; after a solve, the solver's UA has to
  win instead.
- **Clearance is cached per host**, under ``CLEARANCE_TTL_SECONDS`` (shorter
  than Cloudflare's own lifetime). Each solve is a browser launch: seconds of
  CPU and hundreds of MB. A per-host lock makes concurrent workers that hit the
  same mirror wait for one solve, rather than start three browsers.
- **A clearance that was already applied and still challenged is dropped.**
  Handing the same cookie back would loop. We tag the session with the
  clearance we gave it, so we can tell.
- **Nothing here raises.** Every failure returns None/False, and the caller
  carries on to the manual-download path it always had.

Import-light on purpose (no api, app or database imports): ``api.py`` and
``models/getcomics.py`` both call it.
"""
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import requests

from core.app_logging import app_logger

CLEARANCE_TTL_SECONDS = 25 * 60
DEFAULT_TIMEOUT_SECONDS = 60
_SESSION_TAG = "_clu_flaresolverr_clearance"


@dataclass
class Clearance:
    host: str
    cookies: list = field(default_factory=list)
    user_agent: str = ""
    expires_at: float = 0.0

    def is_fresh(self, now=None):
        return (now if now is not None else time.monotonic()) < self.expires_at

    def cookie_names(self):
        return [c.get("name") for c in self.cookies if c.get("name")]


_cache = {}                 # host -> Clearance
_host_locks = {}            # host -> Lock
_registry_lock = threading.Lock()

# How much the solver is used, for the debug log. In memory, per process, since
# start. Every use logs one DEBUG line carrying the running totals, so the most
# recent line answers "how often is this used, and does it help?" with no extra
# summary line at INFO. The keys are the outcomes below.
USAGE_OUTCOMES = (
    "solved",             # a browser solve that returned clearance
    "cache_hit",          # clearance reused from an earlier solve, no browser
    "resolved_again",     # cached clearance was still challenged, solved again
    "solve_failed",       # the solver ran (or tried to start) and gave nothing
    "skipped",            # a challenge was met but the solver is off or absent
    "still_challenged",   # the retry after clearance was challenged anyway
)
_usage = {k: 0 for k in USAGE_OUTCOMES}
_usage_lock = threading.Lock()


def usage_stats():
    """A copy of the usage counters since start."""
    with _usage_lock:
        return dict(_usage)


def reset_usage():
    with _usage_lock:
        for k in _usage:
            _usage[k] = 0


def _record(outcome, context, host, detail=""):
    with _usage_lock:
        _usage[outcome] += 1
        totals = ", ".join(f"{k}={v}" for k, v in _usage.items())
    app_logger.debug(
        f"FlareSolverr [{context or 'unknown'}] {host or '?'}: {outcome}"
        f"{' (' + detail + ')' if detail else ''} | totals: {totals}"
    )


def note_still_challenged(url, context=None):
    """The request retried with FlareSolverr clearance was challenged again.

    Callers report this so the debug log can show whether a solve helped, not
    just that one ran.
    """
    try:
        _record("still_challenged", context, _host(url))
    except Exception:
        pass


def is_enabled():
    """The setting. A read failure means "off" here: this is opt-out, never load-bearing."""
    try:
        from core.settings import get_bool
        return get_bool("FLARESOLVERR_ENABLED")
    except Exception:
        return False


def is_available():
    """Enabled, and the solver is installed in this image."""
    if not is_enabled():
        return False
    return _installed()


def _installed():
    from core import flaresolverr_supervisor
    return flaresolverr_supervisor.is_installed()


def _timeout_seconds():
    try:
        from core.settings import get_int
        return max(10, get_int("FLARESOLVERR_TIMEOUT"))
    except Exception:
        return DEFAULT_TIMEOUT_SECONDS


def _host(url):
    return (urlparse(url).hostname or "").lower()


def _origin(url):
    p = urlparse(url)
    return f"{p.scheme or 'https'}://{p.netloc}/"


def _host_lock(host):
    with _registry_lock:
        lock = _host_locks.get(host)
        if lock is None:
            lock = _host_locks[host] = threading.Lock()
        return lock


def cached_clearance(host):
    with _registry_lock:
        c = _cache.get(host)
    if c is not None and c.is_fresh():
        return c
    return None


def invalidate(host=None):
    with _registry_lock:
        if host is None:
            _cache.clear()
        else:
            _cache.pop(host, None)


def solve(url, *, base_url=None, post=None):
    """Ask FlareSolverr to pass the challenge for *url*'s site. Clearance or None.

    *base_url* and *post* exist for tests. Normally the supervisor provides the
    URL, starting the solver if needed, and the POST goes through ``requests``.
    """
    host = _host(url)
    if not host:
        return None
    if base_url is None:
        from core import flaresolverr_supervisor
        base_url = flaresolverr_supervisor.ensure_running()
        if base_url is None:
            return None
    post = post or requests.post
    timeout = _timeout_seconds()
    target = _origin(url)
    started = time.monotonic()
    try:
        resp = post(
            f"{base_url}/v1",
            json={"cmd": "request.get", "url": target, "maxTimeout": timeout * 1000},
            timeout=timeout + 15,
        )
        data = resp.json()
    except Exception as e:
        app_logger.warning(f"FlareSolverr request for {host} failed: {e}")
        return None
    finally:
        try:
            from core import flaresolverr_supervisor
            flaresolverr_supervisor.touch()
        except Exception:
            pass
    elapsed = time.monotonic() - started
    solution = data.get("solution") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("status") != "ok" or not isinstance(solution, dict):
        message = data.get("message") if isinstance(data, dict) else data
        app_logger.warning(f"FlareSolverr could not pass the challenge on {host} "
                           f"({elapsed:.1f}s): {message}")
        return None
    cookies = [c for c in (solution.get("cookies") or []) if isinstance(c, dict) and c.get("name")]
    clearance = Clearance(
        host=host,
        cookies=cookies,
        user_agent=str(solution.get("userAgent") or ""),
        expires_at=time.monotonic() + CLEARANCE_TTL_SECONDS,
    )
    has_cf = "cf_clearance" in clearance.cookie_names()
    app_logger.info(
        f"FlareSolverr solved {host} in {elapsed:.1f}s "
        f"({len(cookies)} cookies{', cf_clearance' if has_cf else ', no cf_clearance'})"
    )
    return clearance


def apply_clearance(session, clearance):
    """Put *clearance*'s cookies and User-Agent on a requests session."""
    for c in clearance.cookies:
        try:
            session.cookies.set(
                c["name"], c.get("value", ""),
                domain=c.get("domain") or clearance.host,
                path=c.get("path") or "/",
            )
        except Exception:
            continue
    if clearance.user_agent:
        session.headers["User-Agent"] = clearance.user_agent
    try:
        setattr(session, _SESSION_TAG, clearance)
    except Exception:
        pass


def clear_challenge(url, session, context=None):
    """Get clearance for *url*'s site and apply it to *session*. True if applied.

    Uses a cached clearance when this session does not already carry it. If it
    does, that clearance just failed, so we solve again. Concurrent callers for
    the same host share one solve.

    *context* names the call site ("download", "resolve", "page", "search") in
    the usage debug line.
    """
    host = None
    try:
        host = _host(url)
        if not is_available():
            reason = ("not installed in this build" if is_enabled()
                      else "disabled in Settings")
            _record("skipped", context, host, reason)
            return False
        if not host:
            return False
        with _host_lock(host):
            c = cached_clearance(host)
            retried = False
            if c is not None and getattr(session, _SESSION_TAG, None) is c:
                invalidate(host)
                c = None
                retried = True
            if c is None:
                started = time.monotonic()
                c = solve(url)
                elapsed = time.monotonic() - started
                if c is None:
                    _record("solve_failed", context, host, f"{elapsed:.1f}s")
                    return False
                with _registry_lock:
                    _cache[host] = c
                _record("resolved_again" if retried else "solved", context, host,
                        f"{elapsed:.1f}s")
            else:
                left = max(0, int(c.expires_at - time.monotonic()))
                _record("cache_hit", context, host, f"clearance valid {left // 60}m more")
        apply_clearance(session, c)
        return True
    except Exception as e:
        app_logger.warning(f"FlareSolverr clearance failed for {url}: {e}")
        try:
            _record("solve_failed", context, host, f"error: {e}")
        except Exception:
            pass
        return False


def test_solver():
    """(ok, message) for the Settings "Test" button. Starts the solver if needed."""
    from core import flaresolverr_supervisor
    if not flaresolverr_supervisor.is_installed():
        return False, "FlareSolverr is not installed in this build (it ships in the Docker image)."
    base_url = flaresolverr_supervisor.ensure_running()
    if base_url is None:
        return False, "FlareSolverr did not start. Check the app log for details."
    try:
        data = requests.get(base_url + "/", timeout=5).json()
    except Exception as e:
        return False, f"FlareSolverr started but did not answer: {e}"
    version = data.get("version") if isinstance(data, dict) else None
    return True, f"FlareSolverr {version} is ready." if version else "FlareSolverr is ready."
