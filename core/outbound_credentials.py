"""Reuse a stored secret only with the destination it was saved for.

A settings form never receives a saved password or API key back, so a "test
before save" request leaves a secret field blank to mean "keep the saved
one", and the route merges the request over the stored config. That merge is
safe only while the request still points at the stored destination: a request
that changes the host must bring its own secret.

``reused_secret_refusal`` is the one check. A route that merges request data
over a stored secret and then connects asks it first, and connects to nothing
when it returns a message.
"""
import re
from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}

# An authority made only of a hostname or IPv4 address and an optional port,
# or a bracketed IPv6 literal. Anything else (userinfo, backslashes, stray
# delimiters) can be read differently by different URL parsers, so it is
# never treated as matching anything -- not even an identical stored value.
_PLAIN_NETLOC = re.compile(
    r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)*\.?"
    r"|\[[0-9A-Fa-f:.]+\])"
    r"(?::\d{1,5})?$"
)

SECRET_LABELS = {
    "api_key": "API key",
    "password": "password",
}


def endpoint_identity(url):
    """Return ``(scheme, host, port)`` for ``url``, or None if it is not plain.

    Hosts compare case-insensitively and an omitted port is the scheme's
    default, so ``http://NAS`` and ``http://nas:80`` are the same destination.
    """
    try:
        parts = urlsplit((url or "").strip())
        if not _PLAIN_NETLOC.match(parts.netloc or ""):
            return None
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
    except (ValueError, TypeError):
        return None
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    if port is None:
        port = _DEFAULT_PORTS[scheme]
    return scheme, host, port


def _identity_of(destination, config):
    try:
        return endpoint_identity(destination(config))
    except Exception:
        # A config that cannot even be turned into a URL matches nothing.
        return None


def _norm(value):
    return "" if value is None else str(value).strip()


def reused_secret_refusal(stored, overrides, secret_fields, destination,
                          identity_fields=()):
    """Return why ``{**stored, **overrides}`` may not be used, or None if it may.

    ``destination(config_dict)`` returns the URL that config connects to. A
    secret counts as reused when it is stored and the request does not name
    the field at all (a field sent empty replaces it, so nothing is reused).
    Reuse is allowed only when the merged config connects to the same scheme,
    host and port as the stored one, and every ``identity_fields`` value (the
    username, for a password) is unchanged.
    """
    stored = stored or {}
    overrides = overrides or {}
    reused = [f for f in secret_fields if stored.get(f) and f not in overrides]
    if not reused:
        return None

    merged = {**stored, **overrides}
    before = _identity_of(destination, stored)
    after = _identity_of(destination, merged)
    same = (
        before is not None
        and before == after
        and all(_norm(stored.get(f)) == _norm(merged.get(f)) for f in identity_fields)
    )
    if same:
        return None

    labels = " and ".join(SECRET_LABELS.get(f, f.replace("_", " ")) for f in reused)
    return (
        f"Re-enter the {labels} to test a different host or user. "
        f"The saved {labels} is only used with the destination it was saved for."
    )
