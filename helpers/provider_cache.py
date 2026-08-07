"""Writable on-disk cache locations for the third-party provider clients.

Both Simyan (ComicVine) and Mokkari (Metron) keep SQLite files -- Simyan for its
HTTP cache and rate-limit buckets, Mokkari for its response cache. Both default
to ``$HOME/.cache``, which doesn't exist for the container's non-root user and
can't be created by it (issue #396), so CLU resolves the location itself.

Kept in one place so the two providers can't drift apart: a permissions fix for
one is a permissions fix for both, and their cache files sit side by side.
"""

import os
import tempfile

from core.app_logging import app_logger


def provider_cache_dir(name: str) -> str:
    """Return a writable directory for provider ``name``'s cache files.

    Mirrors Simyan's own resolution order (``$XDG_CACHE_HOME`` first, then a
    CLU-owned location under ``CONFIG_DIR``), falling back to a temp dir if the
    preferred location isn't writable.

    ``CONFIG_DIR`` is used rather than ``CACHE_DIR`` because the latter only
    exists as ``app.config["CACHE_DIR"]``; these clients are built from daemon
    threads with no Flask app context. ``CONFIG_DIR`` is a module constant and
    is already a mounted volume in Docker.

    Args:
        name: Leaf directory name, e.g. ``"simyan"`` or ``"mokkari"``.

    Returns:
        Path to an existing, writable directory.
    """
    from core.config import CONFIG_DIR

    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(CONFIG_DIR, ".cache")
    target = os.path.join(base, name)
    try:
        os.makedirs(target, exist_ok=True)
        return target
    except OSError:
        fallback = os.path.join(tempfile.gettempdir(), f"clu-{name}")
        os.makedirs(fallback, exist_ok=True)
        app_logger.warning(
            f"Provider cache dir {target} not writable; using {fallback}"
        )
        return fallback
