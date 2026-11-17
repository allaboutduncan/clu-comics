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

Routes that WRITE (ComicInfo.xml, cvinfo/series.json, folder art, bulk
metadata jobs) use the same rules through :func:`write_paths_refusal` (always
``'full'``; one refusal refuses the request). Work that runs after the request
returns and expands a folder into files does so in the request, narrowed by
:func:`capture_write_scope` -- the worker thread has no request user.

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


def write_paths_refusal(paths, staging=()):
    """``(message, status)`` if any of ``paths`` may not be written, else None.

    The write-side twin of :func:`resolve_served_path` for routes that write
    ComicInfo.xml, sidecars, folder art or index rows about a path taken from
    the request: every path must pass containment and, inside a library, a
    ``'full'`` grant. One refusal refuses the whole request, as the File
    Manager's batch operations do -- nothing is written or queued.
    """
    for path in paths:
        _served, refusal = resolve_served_path(path, staging=staging, mode="full")
        if refusal:
            return refusal
    return None


def grant_refusal(*paths, staging=()):
    """:func:`write_paths_refusal` for non-owners only.

    For routes whose owner behaviour must stay exactly as it was:

    - a path read from the database rather than the request -- a review or
      audit row names a file another user's job may have written, and a stale
      row must still report "file no longer exists" to an owner;
    - routes that historically accepted any folder from an owner (mapping or
      subscribing a series to a folder that may not be in a library yet).

    Owners and single-user installs are not restricted at all; anyone else
    needs exactly what :func:`write_paths_refusal` requires.
    """
    if not is_login_required():
        return None
    user = current_user()
    if user and user.get("role") == "owner":
        return None
    return write_paths_refusal(paths, staging=staging)


def folder_art_write_refusal(folder_path):
    """``(message, status)`` if the user may not (re)generate ``folder_path``'s art.

    ``/api/generate-folder-thumbnail`` and the thumbnail sweeps write a
    ``folder_art`` row and a cache image for the folder. The folder must lie
    inside a library (or TARGET, which the collection grid also browses) and,
    inside a library, carry a ``'full'`` grant: generating art is a write, so
    the 'traverse' that lets the grid *show* an ancestor's art is not enough.
    """
    return write_paths_refusal([folder_path], staging=(STAGING_TARGET,))


def capture_write_scope():
    """The current user's write scope, captured now, as ``allowed(path)``.

    Background work (bulk metadata jobs, thumbnail sweeps) runs on a worker
    thread with no request user, and some of it expands a requested folder
    into files and subfolders. This snapshot -- taken in the request -- lets
    that expansion keep to the grants the request was authorised with.

    Returns None when unrestricted (owner, single-user install). Otherwise
    ``allowed(path)`` is True only for a path that has a ``'full'`` grant both
    as named and as it really resolves (re-expressed under its library root,
    so a symlink into an ungranted folder is judged by its target).
    """
    if not is_login_required():
        return None
    user = current_user()
    if user and user.get("role") == "owner":
        return None
    if not user:
        return lambda _p: False
    libraries, granted, grants = _load_scope(user)
    roots = _library_roots()

    def allowed(path):
        if not path or not isinstance(path, str):
            return False
        if _level_for(path, libraries, granted, grants) != "full":
            return False
        try:
            raw_real = os.path.realpath(path)
        except (OSError, ValueError):
            return False
        lib = _containing(os.path.normcase(raw_real), roots)
        if not lib:
            return False
        return _level_for(_reexpress(raw_real, lib[0]), libraries, granted, grants) == "full"

    return allowed


def keep_writable(folder, paths, allowed=None):
    """``paths`` found by listing ``folder`` (already authorised), minus any the
    user may not write -- in practice a symlinked entry pointing into an
    ungranted folder. Entries of a staging folder carry no grants and are kept
    unless they really resolve into a library. ``folder=None`` judges each
    entry on its own (a walk over several roots). ``allowed`` defaults to a
    fresh :func:`capture_write_scope`.
    """
    if allowed is None:
        allowed = capture_write_scope()
    if allowed is None:
        return list(paths)
    roots = _library_roots()
    folder_in_library = bool(folder) and bool(_containing(_real(folder), roots))
    kept = []
    for p in paths:
        in_library = folder_in_library or bool(_containing(_real(p), roots))
        if not in_library or allowed(p):
            kept.append(p)
    return kept


def filter_buckets(buckets, allowed):
    """``{folder: [files]}`` narrowed to what ``allowed`` permits.

    A folder is kept only if it is itself writable (bulk jobs write cvinfo and
    series.json into it), and then only its writable files. Returns
    ``(kept, dropped_file_count)``; ``allowed=None`` keeps everything.
    """
    if allowed is None:
        return buckets, 0
    kept = {}
    dropped = 0
    for folder, files in buckets.items():
        if not allowed(folder):
            dropped += len(files)
            continue
        ok = [f for f in files if allowed(f)]
        dropped += len(files) - len(ok)
        if ok:
            kept[folder] = ok
    return kept, dropped


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
