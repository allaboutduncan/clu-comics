"""Containment and folder grants for routes that take a path from the request.

Routes such as ``/list-new-files``, ``/count-files``, ``/list-downloads``,
``/cbz-metadata`` and ``/api/read-text-file`` read a raw absolute path from the
query string and then list, count or read what is there. Each one must first
decide two things, in this order and before anything is opened:

1. **Containment.** The path's *realpath* must lie inside one of the roots the
   route serves -- the enabled library roots, plus WATCH and/or TARGET for the
   routes the File Manager uses -- compared on a separator boundary
   (``/data/X`` is not inside ``/data/X-Men``). The path is then re-expressed
   under the configured root that really contains it, so a symlink is judged
   by where it points, not by its apparent name (the same shape as
   ``cbz_ops/edit_paths.py``).
2. **Grants.** Inside a library, a non-owner in multi-user mode needs a folder
   grant: ``'full'`` for a route that reads a file, or ``'browse'`` (a
   'traverse'-only ancestor is enough) for a listing whose results are then
   filtered with :func:`grant_level_filter`. WATCH and TARGET carry no grants;
   they are served only to Clerks and above, the role the File Manager
   requires. A path that is inside a library is always judged as a library
   path, even when TARGET is configured inside that library.

Owners and single-user (implicit-owner) installs skip the grant step, exactly
as ``core.auth.folder_access_level`` treats them; containment still applies.

Kept free of any ``app`` import so it is tested directly (app.py cannot be
imported in tests).
"""

import os
from collections import namedtuple

from core.auth import (
    _level_for,
    _load_scope,
    current_user,
    folder_access_level,
    is_login_required,
    role_at_least,
)

KIND_LIBRARY = "library"
KIND_STAGING = "staging"

STAGING_WATCH = "watch"
STAGING_TARGET = "target"

# /api/read-text-file serves the collection grid's .txt viewer and nothing else.
TEXT_FILE_EXTENSIONS = (".txt",)
TEXT_FILE_MAX_BYTES = 2 * 1024 * 1024

ServedPath = namedtuple("ServedPath", "path kind root")


def _real(path):
    return os.path.normcase(os.path.realpath(path))


def _inside_or_equal(real_path, real_root):
    """Separator-bounded containment on realpaths."""
    root = real_root.rstrip(os.sep) or real_root
    return real_path == root or real_path.startswith(root + os.sep)


def _library_roots():
    from helpers.library import get_library_roots

    try:
        return [r for r in get_library_roots() if r]
    except Exception:
        return []


def _staging_roots(which):
    """The configured WATCH/TARGET folders named in ``which``.

    Both the stored preference and the Flask config are consulted: the app
    falls back to a default TARGET in ``app.config`` when none is stored, and
    the File Manager's Downloads button starts there.
    """
    if not which:
        return []
    from core.config import get_target_dir, get_watch_dir

    roots = []
    if STAGING_TARGET in which:
        roots.append(get_target_dir())
    if STAGING_WATCH in which:
        roots.append(get_watch_dir())
    try:
        from flask import current_app

        if STAGING_TARGET in which:
            roots.append(current_app.config.get("TARGET"))
        if STAGING_WATCH in which:
            roots.append(current_app.config.get("WATCH"))
    except RuntimeError:
        pass
    return [r for r in roots if r]


def _may_use_staging():
    if not is_login_required():
        return True
    return role_at_least(current_user(), "clerk")


def _containing(real, roots):
    """``(configured, real_root)`` of the longest root containing ``real``."""
    best = None
    for configured in roots:
        try:
            real_root = _real(configured)
        except Exception:
            continue
        if _inside_or_equal(real, real_root):
            if best is None or len(real_root) > len(best[1]):
                best = (os.path.normpath(configured), real_root)
    return best


def _reexpress(raw_real, configured):
    """``raw_real`` rebuilt under the configured spelling of its root."""
    rel = os.path.relpath(raw_real, os.path.realpath(configured))
    if rel == os.curdir:
        return configured
    return os.path.normpath(os.path.join(configured, rel))


def resolve_served_path(path, staging=(), mode="full", apply_grants=True):
    """Decide whether the current user may be served ``path``.

    Returns ``(ServedPath, None)`` on success, where ``ServedPath.path`` is the
    path to act on (re-expressed under its configured root), or
    ``(None, (message, status))`` when the request must be refused. Nothing is
    opened or stat'ed beyond resolving the realpath, so a refused path is
    indistinguishable from a missing one.

    ``staging`` names the download folders this route also serves
    (``STAGING_WATCH``/``STAGING_TARGET``); they are offered only to Clerks
    and above. ``mode`` is the grant level a library path needs: ``'full'``
    to read a file, ``'browse'`` for a listing that filters its results.

    ``apply_grants=False`` keeps the containment and the staging role check
    but skips the folder grant. It exists for the File Manager's listings
    only, which are deliberately not folder-scoped (see ``/list-directories``
    and ``test_file_manager_not_folder_scoped``); anything that reads file
    content or metadata must leave it on.
    """
    if not path or not isinstance(path, str) or "\0" in path:
        return None, ("Invalid path", 400)
    if not os.path.isabs(path):
        return None, ("Path must be absolute", 400)

    raw_real = os.path.realpath(path)
    real = os.path.normcase(raw_real)

    lib = _containing(real, _library_roots())
    if lib:
        kind, root = KIND_LIBRARY, lib[0]
    else:
        stg = _containing(real, _staging_roots(staging)) if staging else None
        if not stg:
            return None, ("Access denied - path is outside the library", 403)
        if not _may_use_staging():
            return None, ("Access denied", 403)
        kind, root = KIND_STAGING, stg[0]

    canonical = _reexpress(raw_real, root)

    if kind == KIND_LIBRARY and apply_grants and is_login_required():
        user = current_user()
        if user is None:
            return None, ("Login required", 401)
        allowed = ("full", "traverse") if mode == "browse" else ("full",)
        for candidate in {os.path.normpath(path), canonical}:
            if folder_access_level(user, candidate) not in allowed:
                return None, ("Access denied - you do not have access to this folder", 403)

    return ServedPath(canonical, kind, root), None


def grant_level_filter(served):
    """A ``level(path)`` callable for filtering a walk under ``served``, or None.

    None means unrestricted: an owner, a single-user install, or a staging
    folder (which carries no grants). Otherwise ``level`` answers
    ``'full' | 'traverse' | 'none'`` for paths under the served library root,
    from one scope load, so a whole walk costs one set of queries.
    """
    if served.kind != KIND_LIBRARY or not is_login_required():
        return None
    user = current_user()
    if user and user.get("role") == "owner":
        return None
    if not user:
        return lambda _p: "none"
    libraries, granted, grants = _load_scope(user)
    return lambda p: _level_for(p, libraries, granted, grants)


def walk_visible(served, prune=None):
    """``os.walk`` over ``served.path`` limited to what the user may see.

    Yields ``(root, dirs, files)`` exactly like ``os.walk`` (top-down, links
    not followed), except that a directory with no access is pruned and the
    files of a 'traverse'-only directory are withheld -- the same rule the
    browse listing applies to a granted parent's children. ``prune``, if
    given, is called on the ``dirs`` list first (e.g. to drop hidden names).
    """
    level = grant_level_filter(served)
    for root, dirs, files in os.walk(served.path):
        if prune:
            prune(dirs)
        if level is None:
            yield root, dirs, files
            continue
        dirs[:] = [d for d in dirs if level(os.path.join(root, d)) != "none"]
        visible = files if level(root) == "full" else []
        yield root, dirs, visible


def resolve_text_file(path):
    """``(ServedPath, None)`` if ``/api/read-text-file`` may return ``path``.

    The one caller is the collection grid's text viewer, which opens ``.txt``
    files it lists from a library (or TARGET, which ``/list-directories`` also
    serves). So: library or TARGET containment and a ``'full'`` grant via
    :func:`resolve_served_path`; a ``.txt`` name both as requested and as
    resolved (a ``notes.txt`` symlink to something else is refused); a regular
    file; and at most :data:`TEXT_FILE_MAX_BYTES`.
    """
    served, refusal = resolve_served_path(path, staging=(STAGING_TARGET,), mode="full")
    if refusal:
        return None, refusal
    if not (path.lower().endswith(TEXT_FILE_EXTENSIONS)
            and served.path.lower().endswith(TEXT_FILE_EXTENSIONS)):
        return None, ("Only .txt files are supported", 400)
    if not os.path.isfile(served.path):
        return None, ("File not found", 404)
    try:
        size = os.path.getsize(served.path)
    except OSError:
        return None, ("File not found", 404)
    if size > TEXT_FILE_MAX_BYTES:
        return None, ("File is too large to display", 413)
    return served, None
