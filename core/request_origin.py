"""Same-origin check for state-changing requests (cross-site request forgery).

CLU is usually reached over plain HTTP on a LAN, and in its default
single-user mode there is no login and no cookie at all: any web page the
user visits could otherwise POST to the LAN host and act as the owner. So
every POST, PUT, PATCH and DELETE must come from CLU's own pages, a
non-browser client, or a request carrying a credential a browser cannot
attach on its own. There are no per-form tokens; the browser already says
where a request came from.

The decision, in order (:func:`decide`):

1. Safe methods (GET, HEAD, OPTIONS...) are never checked.
2. Exempt: ``/download`` (the browser extension's auth-free endpoint: its
   ``Origin`` is ``chrome-extension://...`` by design, see ``core/cors.py``),
   and ``/api/v1/`` requests carrying ``Authorization: Bearer <token>``. A
   browser never attaches a bearer token by itself, and a cross-site page
   cannot add the header without a CORS preflight that CLU does not approve.
   A request to ``/api/v1/`` *without* one is checked like any other.
3. ``Sec-Fetch-Site`` (sent only to secure contexts: HTTPS or localhost):
   ``same-origin`` and ``none`` (user-initiated) pass, ``cross-site`` is
   refused, and ``same-site`` (a sibling subdomain is a different app) or any
   unknown value must pass the ``Origin`` comparison below.
4. Otherwise the ``Origin`` header, or when absent the ``Referer``'s origin,
   must name this server: the ``Host`` header, the host ProxyFix resolved, or
   any ``X-Forwarded-Host`` value, case-insensitively, with ports 80/443
   dropped. The scheme is not compared (a TLS-terminating proxy makes it
   unreliable). ``Origin: null`` is refused.
5. No ``Sec-Fetch-Site``, ``Origin`` or ``Referer`` at all: a non-browser
   client (curl, scripts, OPDS readers). CSRF needs a browser, so it passes.

``CLU_TRUSTED_ORIGINS`` (comma-separated origins or ``host[:port]``) adds
addresses accepted in step 4, for a reverse proxy that rewrites ``Host``
without sending ``X-Forwarded-Host``. It is an environment variable on
purpose: an owner wrongly refused could not save a setting.

The decision is a pure function of its arguments so the tests can cover
every branch; :func:`check_current_request` adapts it to Flask.
"""
import functools
import os
import threading
from collections import OrderedDict, namedtuple
from urllib.parse import urlsplit

TRUSTED_ORIGINS_ENV = "CLU_TRUSTED_ORIGINS"

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# Exact paths exempt from the check (see module docstring, step 2).
EXEMPT_PATHS = frozenset({"/download"})

# Path prefixes whose own before_request authenticates a bearer token.
BEARER_PATH_PREFIXES = ("/api/v1/",)

_DEFAULT_PORTS = (80, 443)

Decision = namedtuple("Decision", "allowed reason source origin")


def normalize_host(value):
    """``host[:port]`` from a Host-style value, an origin or a URL, or None.

    Lower-cased, trailing dot removed, IPv6 kept in brackets, and ports 80 and
    443 dropped (the scheme is not compared, so either default is "no port").
    """
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    try:
        parts = urlsplit(value if "//" in value else "//" + value)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if not host:
        return None
    host = host.rstrip(".")
    if not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    if port is None or port in _DEFAULT_PORTS:
        return host
    return f"{host}:{port}"


def _origin_host(value):
    """The ``host[:port]`` of an Origin or Referer value, or None.

    Both must be absolute URLs with a scheme; a bare host is not an origin.
    """
    if not value:
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    return normalize_host(f"{parts.scheme}://{parts.netloc}")


@functools.lru_cache(maxsize=8)
def parse_trusted_origins(raw):
    """The normalised hosts in a ``CLU_TRUSTED_ORIGINS`` value."""
    hosts = set()
    for entry in (raw or "").split(","):
        host = normalize_host(entry)
        if host:
            hosts.add(host)
    return frozenset(hosts)


def trusted_origins():
    """The hosts ``CLU_TRUSTED_ORIGINS`` adds, read per call (cheap, cached)."""
    return parse_trusted_origins(os.environ.get(TRUSTED_ORIGINS_ENV, ""))


def is_exempt(path, headers):
    """True for the requests step 2 exempts entirely."""
    if path in EXEMPT_PATHS:
        return True
    if path.startswith(BEARER_PATH_PREFIXES):
        auth = headers.get("Authorization") or ""
        if auth.startswith("Bearer ") and auth[len("Bearer "):].strip():
            return True
    return False


def decide(method, path, headers, own_hosts, trusted=frozenset()):
    """Whether a request may proceed. Returns a :class:`Decision`.

    ``headers`` is any mapping with ``.get``; ``own_hosts`` the raw host
    values that name this server (normalised here); ``trusted`` the already
    normalised ``CLU_TRUSTED_ORIGINS`` hosts.
    """
    if (method or "").upper() not in STATE_CHANGING_METHODS:
        return Decision(True, "safe-method", None, None)
    if is_exempt(path or "", headers):
        return Decision(True, "exempt", None, None)

    fetch_site = (headers.get("Sec-Fetch-Site") or "").strip().lower()
    if fetch_site in ("same-origin", "none"):
        return Decision(True, "fetch-metadata", "Sec-Fetch-Site", fetch_site)
    if fetch_site == "cross-site":
        return Decision(False, "cross-site", "Sec-Fetch-Site", fetch_site)

    origin = (headers.get("Origin") or "").strip()
    referer = (headers.get("Referer") or "").strip()
    if origin:
        source, value = "Origin", origin
    elif referer:
        source, value = "Referer", referer
    elif fetch_site:
        # same-site (or a value we do not know) with nothing to compare.
        return Decision(False, "no-origin", "Sec-Fetch-Site", fetch_site)
    else:
        return Decision(True, "no-browser-headers", None, None)

    if source == "Origin" and value.lower() == "null":
        return Decision(False, "null-origin", source, value)

    host = _origin_host(value)
    if host is None:
        return Decision(False, "unparseable", source, value)

    mine = {h for h in (normalize_host(v) for v in own_hosts) if h}
    if host in mine:
        return Decision(True, "same-host", source, host)
    if host in trusted:
        return Decision(True, "trusted", source, host)
    return Decision(False, "host-mismatch", source, host)


def request_hosts(request):
    """Every value that names this server for ``request``.

    The ``Host`` header (which ProxyFix may already have replaced), the host
    ProxyFix resolved, the original ``Host`` ProxyFix kept aside, and each
    ``X-Forwarded-Host`` value. A cross-site page can set none of these.
    """
    hosts = [request.headers.get("Host"), request.host]
    orig = request.environ.get("werkzeug.proxy_fix.orig") or {}
    hosts.append(orig.get("HTTP_HOST"))
    for value in (request.headers.get("X-Forwarded-Host") or "").split(","):
        hosts.append(value)
    return [h for h in hosts if h]


# ---------------------------------------------------------------------------
# Refusal: one warning per distinct cause, not one per request.
# ---------------------------------------------------------------------------

_LOGGED_MAX = 64
_logged = OrderedDict()
_logged_lock = threading.Lock()


def _first_refusal(key):
    """True the first time ``key`` is refused (bounded LRU of keys)."""
    with _logged_lock:
        if key in _logged:
            _logged.move_to_end(key)
            return False
        _logged[key] = True
        while len(_logged) > _LOGGED_MAX:
            _logged.popitem(last=False)
        return True


def _reset_logged():
    """Test hook: forget which refusals were announced."""
    with _logged_lock:
        _logged.clear()


def _printable(value, limit=120):
    """A header value made safe for a log line or a message."""
    text = "".join(ch if ch.isprintable() else "?" for ch in str(value or ""))
    return text[:limit]


def refusal_message(decision, server_host):
    """The user-facing explanation for a refused request."""
    remedy = (
        "If you reached CLU through a reverse proxy that changes the Host "
        f"header, add its public address to the {TRUSTED_ORIGINS_ENV} "
        "environment variable (comma-separated, for example "
        "https://comics.example.com) and restart the container."
    )
    origin = _printable(decision.origin)
    server = _printable(server_host)
    if decision.reason == "cross-site":
        cause = "the browser reported it as coming from another site"
    elif decision.reason == "no-origin":
        cause = "it came from another site and named no origin"
    elif decision.reason == "null-origin":
        cause = "it came from an opaque origin (Origin: null)"
    elif decision.reason == "unparseable":
        cause = f"its {decision.source} header ({origin}) could not be read"
    else:
        cause = (
            f"its {decision.source} ({origin}) does not match this "
            f"server's address ({server})"
        )
    return f"Cross-site request refused: {cause}. {remedy}"


def _wants_json(request):
    """JSON for API and script callers, plain text for a form submission."""
    if request.path.startswith(("/api/", "/opds")):
        return True
    if request.is_json:
        return True
    if request.headers.get("X-Requested-With", "").lower() == "xmlhttprequest":
        return True
    return request.accept_mimetypes.best == "application/json"


def check_current_request():
    """``before_app_request`` body: a 403 response, or None to continue."""
    from flask import jsonify, request

    decision = decide(
        request.method,
        request.path,
        request.headers,
        request_hosts(request),
        trusted_origins(),
    )
    if decision.allowed:
        return None

    message = refusal_message(decision, request.host)
    key = (decision.reason, decision.source, _printable(decision.origin))
    if _first_refusal(key):
        try:
            from core.app_logging import app_logger
            app_logger.warning(
                "Refused cross-site %s %s (%s %s; server %s). Further refusals "
                "for this origin are not logged. Set %s if this is your own "
                "reverse proxy.",
                request.method,
                _printable(request.path, 200),
                decision.source or "-",
                _printable(decision.origin) or "-",
                _printable(request.host),
                TRUSTED_ORIGINS_ENV,
            )
        except Exception:
            pass

    if _wants_json(request):
        return jsonify({"error": message}), 403
    return message, 403, {"Content-Type": "text/plain; charset=utf-8"}
