"""
Request-supplied redirect targets (the login ``next`` parameter).

``safe_redirect_target`` is the one check: a target is followed only when it is
a same-site path, otherwise the caller falls back to its default page.
``login_next_target`` builds the value the login gate puts in ``next``.

CLU can run behind ``ProxyFix(x_prefix=1)`` on a subpath. ``request.path``
excludes that prefix, so a ``next`` built from it alone would send the browser
to the host root. The target therefore carries ``request.script_root``, and a
target outside the script root is refused.
"""
from urllib.parse import quote, urlsplit

from flask import request

# Generous for a path + query string, and stops a megabyte Location header.
_MAX_TARGET_LENGTH = 2048


def safe_redirect_target(target, script_root=""):
    """Return ``target`` if it is a same-site relative path, else ``None``.

    Accepted: an absolute path (``/collection?x=1``) under ``script_root``.
    Refused: anything that is not a string, empty, too long, or contains a
    control character, whitespace or a backslash (browsers read ``/\\host``
    and ``\\\\host`` as ``//host``); a scheme-relative ``//host``; and
    anything ``urlsplit`` finds a scheme or netloc in.
    """
    if not isinstance(target, str) or not target:
        return None
    if len(target) > _MAX_TARGET_LENGTH:
        return None
    # Control characters (C0, DEL, C1) and any whitespace: browsers strip tabs
    # and newlines from URLs, so "/\t/evil.example" becomes "//evil.example".
    for ch in target:
        code = ord(ch)
        if code < 0x20 or 0x7F <= code <= 0x9F or ch.isspace():
            return None
    if "\\" in target:
        return None
    if not target.startswith("/") or target.startswith("//"):
        return None

    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return None

    script_root = (script_root or "").rstrip("/")
    if script_root:
        path = parts.path
        if path != script_root and not path.startswith(script_root + "/"):
            return None

    return target


def login_next_target():
    """The ``next`` value for the current request: script root + path + query.

    The path is percent-encoded so a legitimate target never contains the
    whitespace or backslashes that ``safe_redirect_target`` refuses.
    """
    target = quote(request.script_root + request.path, safe="/")
    query = request.query_string.decode("latin-1")
    if query:
        target += "?" + query
    return target
