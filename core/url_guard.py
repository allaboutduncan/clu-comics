"""Destination checks for URLs the download queue fetches.

``POST /download`` is auth-free on purpose (the browser extension posts
``{"link": ...}`` with no session cookie), so the URL it carries is not one CLU
chose. Everything the download queue fetches goes through this module:

* :func:`check_url` refuses anything that is not ``http``/``https`` and any host
  that resolves to an address that is not on the public internet -- loopback,
  RFC1918, link-local (cloud metadata included), CGNAT, unspecified, multicast,
  reserved, and the IPv6 equivalents, including IPv4-mapped/6to4/Teredo/NAT64
  forms that embed one of those. The hostname is resolved and *every* address
  returned is checked, so ``2130706433``, ``0x7f.1`` and friends are judged by
  what they actually resolve to, not by how they are spelled.
* :func:`guard_session` puts that check in front of every request a
  ``requests`` session sends, which includes each redirect hop ``requests``
  follows on its own, and keeps the user's ``custom_headers`` off hosts they
  were not meant for.
* :func:`scope_headers` does the header half for code that sends one request
  at a time and follows redirects itself.

There is an unavoidable gap between the lookup here and the one the HTTP stack
does when it connects (a name that changes its answer in between). Closing it
needs the connection pinned to the checked address, which ``requests`` does
not offer without replacing its transport.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")

# The hosts the download resolver routes to a handler (api.process_download).
# The user's custom headers are only ever sent to these, and their subdomains.
KNOWN_DOWNLOAD_HOSTS = (
    "getcomics.org",
    "pixeldrain.com",
    "mega.nz",
    "mega.co.nz",
    "comicbookplus.com",
    "comicfiles.ru",
)

# Not covered by every Python's is_private/is_global, so listed explicitly.
_EXTRA_BLOCKED_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),   # CGNAT (RFC 6598)
    ipaddress.ip_network("0.0.0.0/8"),       # "this network"
    ipaddress.ip_network("192.0.0.0/24"),    # IETF protocol assignments
    ipaddress.ip_network("198.18.0.0/15"),   # benchmarking
    ipaddress.ip_network("240.0.0.0/4"),     # reserved (incl. broadcast)
    ipaddress.ip_network("fc00::/7"),        # unique local
    ipaddress.ip_network("fec0::/10"),       # site-local (deprecated)
)

_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class BlockedURLError(ValueError):
    """The URL's scheme or destination is not one CLU will fetch."""


def _embedded_ipv4(ip):
    """IPv4 addresses an IPv6 address carries inside it (mapped, 6to4, ...)."""
    found = []
    if ip.ipv4_mapped is not None:
        found.append(ip.ipv4_mapped)
    if ip.sixtofour is not None:
        found.append(ip.sixtofour)
    if ip.teredo is not None:
        found.extend(ip.teredo)
    if ip in _NAT64:
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    # IPv4-compatible (::a.b.c.d, deprecated) -- anything in ::/96 other than
    # :: and ::1, which are judged on their own below.
    if int(ip) >> 32 == 0 and int(ip) > 1:
        found.append(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
    return found


def blocked_reason(address):
    """Why *address* (str or ip_address) is not public, or None when it is."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "not an IP address"
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.scope_id:
            ip = ipaddress.IPv6Address(str(ip).split("%", 1)[0])
        for inner in _embedded_ipv4(ip):
            reason = blocked_reason(inner)
            if reason:
                return f"{reason} (embedded in {ip})"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_unspecified:
        return "unspecified address"
    if ip.is_private:
        return "private address"
    if ip.is_reserved:
        return "reserved address"
    if any(ip in net for net in _EXTRA_BLOCKED_NETWORKS if net.version == ip.version):
        return "non-public address"
    if not ip.is_global:
        return "non-public address"
    return None


def _resolve(host, port):
    """Every address *host* resolves to. Patched in tests."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


REMEDY = "The owner can allow private network addresses in Settings > Download and API."


def private_hosts_allowed():
    """The owner's opt-out (``DOWNLOAD_ALLOW_PRIVATE_HOSTS``), read per call.

    A failed read counts as off, so the check stays on.
    """
    try:
        from core.settings import get_bool
        return bool(get_bool("DOWNLOAD_ALLOW_PRIVATE_HOSTS"))
    except Exception:
        return False


def check_url(url):
    """Raise :class:`BlockedURLError` unless *url* is safe for CLU to fetch.

    Returns the URL's lower-cased hostname when it is. With the owner's
    ``DOWNLOAD_ALLOW_PRIVATE_HOSTS`` on, only the scheme and shape are checked:
    the address check (and the refusal of a host that will not resolve) is
    skipped. Header scoping does not depend on that setting.
    """
    if not isinstance(url, str) or not url.strip():
        raise BlockedURLError("empty URL")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as e:
        raise BlockedURLError(f"malformed URL: {e}") from None
    scheme = (parts.scheme or "").lower()
    if scheme not in ALLOWED_SCHEMES:
        raise BlockedURLError(f"unsupported URL scheme '{scheme or '(none)'}'")
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        raise BlockedURLError("URL has no host")
    if port is None:
        port = 443 if scheme == "https" else 80
    if private_hosts_allowed():
        return host

    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        addresses = [str(literal)]
    else:
        try:
            addresses = _resolve(host, port)
        except (OSError, UnicodeError) as e:
            raise BlockedURLError(
                f"could not resolve host '{host}': {e}. {REMEDY}") from None
        if not addresses:
            raise BlockedURLError(f"could not resolve host '{host}'. {REMEDY}")

    for address in addresses:
        reason = blocked_reason(address)
        if reason:
            raise BlockedURLError(
                f"refusing to fetch from '{host}': it resolves to a {reason}. {REMEDY}"
            )
    return host


def is_allowed_url(url):
    """True when :func:`check_url` accepts *url*."""
    try:
        check_url(url)
        return True
    except BlockedURLError:
        return False


# ---------------------------------------------------------------------------
# custom_headers scoping
# ---------------------------------------------------------------------------

def _host_of(url):
    try:
        return (urlsplit(url).hostname or "").rstrip(".").lower()
    except ValueError:
        return ""


def is_known_download_host(url):
    """True when *url*'s host is, or is under, one of KNOWN_DOWNLOAD_HOSTS."""
    host = _host_of(url)
    return bool(host) and any(
        host == d or host.endswith("." + d) for d in KNOWN_DOWNLOAD_HOSTS
    )


def custom_header_items(full_headers, basic_headers):
    """The entries *full_headers* adds to (or changes from) *basic_headers*.

    api.py builds its ``headers`` as the basic set plus the user's
    ``custom_headers``, so the difference is exactly the custom part.
    """
    basic_lower = {k.lower(): v for k, v in (basic_headers or {}).items()}
    return {
        k: v for k, v in (full_headers or {}).items()
        if basic_lower.get(k.lower()) != v
    }


def headers_allowed_for(url, origin=None):
    """May the user's custom headers go to *url*?

    Only to a known download host, and -- when *origin* (the URL a chain of
    redirects started from) is given -- only while the host has not changed.
    """
    if not is_known_download_host(url):
        return False
    if origin is not None and _host_of(origin) != _host_of(url):
        return False
    return True


def scope_headers(hdrs, url, basic_headers, origin=None):
    """*hdrs* for a request to *url*, minus the custom headers it may not carry.

    A custom header that replaced a basic one (a custom User-Agent, say) falls
    back to the basic value rather than disappearing.
    """
    hdrs = dict(hdrs or {})
    if headers_allowed_for(url, origin):
        return hdrs
    _strip_custom(hdrs, custom_header_items(hdrs, basic_headers), basic_headers)
    return hdrs


def _strip_custom(target, custom, basic_headers):
    """Remove each custom header from *target* (a dict or CaseInsensitiveDict)."""
    basic_lower = {k.lower(): (k, v) for k, v in (basic_headers or {}).items()}
    for name, value in custom.items():
        for key in [k for k in list(target.keys()) if k.lower() == name.lower()]:
            # Only a value that still *is* the custom one: a caller that set
            # its own value for the same name (cloudscraper's User-Agent) keeps it.
            if target[key] == value:
                del target[key]
                fallback = basic_lower.get(name.lower())
                if fallback is not None:
                    target[fallback[0]] = fallback[1]


def guard_session(session, full_headers=None, basic_headers=None):
    """Check every request *session* sends, redirect hops included.

    * Each request (the first and every hop ``requests`` follows) is refused
      with :class:`BlockedURLError` before it is sent unless :func:`check_url`
      accepts its URL.
    * The custom part of *full_headers* (see :func:`custom_header_items`) is
      removed from any request to a host outside KNOWN_DOWNLOAD_HOSTS, and from
      every redirect hop that changes host, the same way ``requests`` already
      drops ``Authorization``.

    Patches the instance, not the class: the sessions here are built by
    cloudscraper, whose class CLU does not own. Returns *session*.
    """
    custom = custom_header_items(full_headers, basic_headers) if full_headers else {}
    original_send = session.send
    original_rebuild_auth = session.rebuild_auth

    def send(request, **kwargs):
        check_url(request.url)
        if custom and not headers_allowed_for(request.url):
            _strip_custom(request.headers, custom, basic_headers)
        return original_send(request, **kwargs)

    def rebuild_auth(prepared_request, response):
        original_rebuild_auth(prepared_request, response)
        if custom:
            previous = getattr(getattr(response, "request", None), "url", None)
            if previous is None or _host_of(previous) != _host_of(prepared_request.url):
                _strip_custom(prepared_request.headers, custom, basic_headers)

    session.send = send
    session.rebuild_auth = rebuild_auth
    return session
