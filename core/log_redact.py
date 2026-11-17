"""Mask credentials carried inside URLs before the text is logged or shown.

Newznab indexers and SABnzbd authenticate with an ``apikey`` query parameter,
and the NZB links an indexer returns carry the user's key as ``r=``/``apikey=``.
``requests`` puts the full URL, query string included, into its exception
messages (``... for url: http://sab:8080/api?mode=queue&apikey=...``), so a
plain ``f"... failed: {e}"`` writes the key to the log, to the status page and
to the debug package.

:func:`redact_url` is the one helper for that. It takes any text -- a bare
URL, an exception, a log message with URLs inside it -- and returns it with

* the value of every credential query parameter replaced by ``***``, in plain
  form (``?apikey=X``, ``&r=X``) and percent-encoded form (a URL passed as a
  parameter of another URL, as SABnzbd's ``addurl`` does), and
* the userinfo of every ``scheme://user:pass@host`` replaced by ``***@``.

It only changes what is *written down*. What is sent to a server is never
passed through it.
"""
from __future__ import annotations

import re

# Query parameters that carry a credential in the URLs CLU sends or receives:
# ``apikey`` (Newznab, SABnzbd), ``r`` (Newznab RSS/NZB links: the user's API
# key), ``api_key`` (ComicVine), and the generic spellings other services use.
CREDENTIAL_PARAMS = (
    "apikey", "api_key", "api-key", "key", "token", "access_token",
    "auth", "password", "pass", "passwd", "pwd", "passkey", "r",
)

_NAMES = "|".join(re.escape(p) for p in sorted(CREDENTIAL_PARAMS, key=len, reverse=True))

# ``?name=value`` / ``&name=value`` / ``;name=value``. The value stops at the
# next separator, whitespace, quote or bracket so the rest of a log line or an
# exception's repr survives.
_PLAIN_PARAM_RE = re.compile(
    r"(?i)([?&;](?:" + _NAMES + r")=)[^&#;\s'\"<>()\[\]]+"
)

# The same inside a percent-encoded URL: ``%3Fapikey%3D...`` / ``%26r%3D...``.
_ENCODED_PARAM_RE = re.compile(
    r"(?i)((?:%3F|%26)(?:" + _NAMES + r")%3D)(?:(?!%26|%23)[^&#\s'\"<>()\[\]])+"
)

# ``scheme://userinfo@`` -- userinfo has no ``/``, whitespace or ``@``.
_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s@'\"<>]+@")

MASK = "***"


def redact_url(value):
    """``value`` as text with URL credentials masked (see the module doc).

    Accepts anything (an exception, None, a URL, a whole message) and never
    raises. ``None`` comes back as ``None`` so optional fields stay optional.
    """
    if value is None:
        return None
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        return "<unprintable>"
    if not text:
        return text
    text = _USERINFO_RE.sub(r"\1" + MASK + "@", text)
    text = _PLAIN_PARAM_RE.sub(r"\1" + MASK, text)
    text = _ENCODED_PARAM_RE.sub(r"\1" + MASK, text)
    return text
