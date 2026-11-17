"""Content-hash version tokens for files under ``static/``.

Templates reference JS/CSS through the ``asset()`` template global
(``routes/auth.py``), which appends ``?v=<token>``. A URL whose token matches
the file's current bytes is served ``immutable`` for a year; anything else gets
werkzeug's default ``no-cache``.

The token is a hash of the content, not the mtime: a Docker rebuild or a fresh
checkout resets every mtime, which would re-download every asset on every
release. It is memoised per path on ``(mtime_ns, size)``, so a render costs one
``stat`` per asset and a file is hashed once per process until it changes --
an edited file gets a new URL without a restart.
"""
import hashlib
import os
import threading

from werkzeug.security import safe_join

STATIC_MAX_AGE = 31536000  # one year

_TOKEN_LENGTH = 10
_cache = {}  # absolute path -> (st_mtime_ns, st_size, token)
_lock = threading.Lock()


def asset_version(static_folder, filename):
    """Token for ``static_folder/filename``, or None when it cannot be resolved.

    None for a path that escapes the folder, a missing file or an unreadable
    one -- callers fall back to something harmless rather than raise.
    """
    if not static_folder or not filename:
        return None
    path = safe_join(static_folder, filename)
    if path is None:
        return None
    path = os.path.abspath(path)
    try:
        st = os.stat(path)
    except OSError:
        return None

    with _lock:
        cached = _cache.get(path)
    if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]

    try:
        with open(path, "rb") as fh:
            digest = hashlib.sha1(fh.read(), usedforsecurity=False).hexdigest()
    except OSError:
        return None
    token = digest[:_TOKEN_LENGTH]
    with _lock:
        _cache[path] = (st.st_mtime_ns, st.st_size, token)
    return token
