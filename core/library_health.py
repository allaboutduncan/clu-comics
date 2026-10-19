"""The rolling library health scan: integrity, duplicates and strays.

Problem Files used to be report-only -- a file appeared because an operation
tried to read it and failed -- so an empty page meant "nothing has failed", not
"your library is clean". This module is the opt-in answer: it goes looking.

Things that look arbitrary and are not:

- **It is rolling and resumable, not a full pass.** Each run checks a bounded
  batch (``library_health_batch`` files, and a wall-clock budget), oldest-checked
  first, so a multi-TB library is covered over days with bounded I/O. There is no
  cursor to lose: "needs checking" is derived from ``file_health`` against
  ``file_index`` every time, so a restart, a crash or an index rebuild just
  continues. Files are re-verified after ``library_health_recheck_days`` too --
  without that, bit rot in a file nobody touches would never be found.
- **Results live in ``file_health``, not on ``file_index``.** A rebuild rewrites
  the index wholesale (``DELETE FROM file_index``), and a content hash is the one
  thing here that is expensive to recompute. A row is trusted only while its
  ``(size, mtime)`` still equal the index's, so a rewritten file is re-checked
  with no hook on the writer.
- **A file is stamped only after its outcome is recorded**, the same rule as
  ``core.reading_list_sync.apply``: stamping first would make the next run skip a
  file whose problem was never written down.
- **A mount that goes away mid-run must not flood the ledger.** An unreachable
  library root is excluded from the query up front, and ten consecutive OSErrors
  abort the batch *without* stamping or recording those ten (the streak is held
  back and dropped), because "every file failed at once" is a mount, not ten
  damaged comics. ``_is_definitely_gone`` is the same evidence rule: a missing
  parent directory proves nothing.
- **Heavy checks are paced separately from the cheap ones.** The series.json,
  staging-folder and empty-folder walks run at most once a day on a schedule
  (``CHEAP_CHECK_INTERVAL``), though the batch runs every time. A manual run does
  both.
- **Duplicates are derived, never stored.** "Exact" is two ``file_health`` rows
  with one hash whose files are still indexed; "likely" is the same tagged
  series+issue(+year+publisher), or a same-folder name that differs only by a
  copy marker. A likely group is never acted on automatically, and nothing here
  deletes: ``resolve_group`` is the only mover and it is user-driven.
- **A dismissed group is keyed on its member set.** A new copy of an issue the
  user already waved through re-opens the group.
- **The summary never recomputes duplicates.** The home-page badge polls it; the
  group counts are stored when a scan finishes and the review page computes the
  live set.
"""

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import zipfile

from core.app_logging import app_logger
from core.problem_files import (
    CLASS_CBR_BESIDE_CBZ,
    CLASS_EMPTY_FOLDER,
    CLASS_NO_PAGES,
    CLASS_SIDECAR_ONLY_FOLDER,
    CLASS_STRAY_STAGING,
    SOURCE_EMPTY_FOLDER,
    SOURCE_INTEGRITY,
    SOURCE_ORPHAN,
    SOURCE_SERIES_JSON,
    clear_problem,
    count_problems,
    get_problem,
    record_problem,
)

HASHED_EXTS = (".cbz", ".zip", ".cbr", ".rar", ".pdf")
ZIP_EXTS = (".cbz", ".zip")
COMIC_EXTS = (".cbz", ".cbr", ".pdf")
PAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

# A staged comic is "stray" only after this long; a download passing through
# TARGET is normal and the wanted sweep files it within minutes.
STRAY_MIN_AGE_SECONDS = 24 * 3600
# series.json and staging walks are not worth repeating on every batch.
CHEAP_CHECK_INTERVAL = 24 * 3600
# Ten files failing in a row is a mount, not ten damaged comics.
MAX_CONSECUTIVE_IO_ERRORS = 10
DEFAULT_TIME_BUDGET = 900
MAX_DUPLICATE_GROUPS = 500

STATE_KEY = "library_health_state"

# One scan at a time: they walk the same rows and the same disks.
_scan_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Claiming (shared by the schedule and the "Run scan now" button)
# ---------------------------------------------------------------------------

def claim_scan():
    """Take the single-flight claim. False means a scan is already running."""
    return _scan_lock.acquire(blocking=False)


def release_scan():
    try:
        _scan_lock.release()
    except RuntimeError:
        pass


def scan_running():
    return _scan_lock.locked()


# ---------------------------------------------------------------------------
# Single-file check
# ---------------------------------------------------------------------------

def hash_file(path):
    """MD5 of the file's bytes (a duplicate fingerprint, not for security)."""
    h = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect_archive(path):
    """``(status, error_class, error_message)`` for one archive.

    ``status`` is ``"ok"`` or ``"damaged"``. Only ZIP/CBZ is CRC-checked: CLU
    has no CRC check for RAR (the WATCH pipeline converts those anyway) and a
    PDF is not an archive, so those are hashed and passed. The messages are the
    ones ``core.problem_files.classify`` already knows how to explain.
    """
    lower = path.lower()
    if not lower.endswith(ZIP_EXTS):
        return "ok", None, None
    try:
        if os.path.getsize(path) == 0:
            return "damaged", "EmptyFile", "The file is empty (0 bytes)"
        if not zipfile.is_zipfile(path):
            return "damaged", "BadZipFile", "File is not a zip file"
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad is not None:
                return "damaged", "BadCRC", f"Bad CRC-32 for file '{bad}'"
            # Only a .cbz is *a comic*; a .zip in a library may be anything, and
            # "no pages" would be a false alarm on it. Its CRCs are still checked.
            if lower.endswith(".cbz"):
                pages = [
                    n for n in zf.namelist()
                    if n.lower().endswith(PAGE_EXTENSIONS)
                    and not os.path.basename(n).startswith(("._", "."))
                ]
                if not pages:
                    return "damaged", CLASS_NO_PAGES, "Archive contains no page images"
    except OSError:
        raise
    except Exception as e:
        from helpers import archive_error_detail

        return "damaged", type(e).__name__, archive_error_detail(e)
    return "ok", None, None


def check_file(path):
    """Hash and verify one file. Raises OSError when it cannot be read.

    Returns ``(digest, status, error_class, error_message)``. The archive check
    runs straight after the hash so the file is still in the page cache.
    """
    digest = hash_file(path)
    status, cls, msg = inspect_archive(path)
    return digest, status, cls, msg


def _index_signature(conn, path):
    """``(size, modified_at)`` as the index holds them, or None if unindexed."""
    row = conn.execute(
        "SELECT size, modified_at FROM file_index WHERE path = ?", (path,)
    ).fetchone()
    return (row[0], row[1]) if row else None


def _store_health(conn, path, digest, size, mtime, status):
    conn.execute(
        """
        INSERT OR REPLACE INTO file_health
            (path, content_hash, size, mtime, checked_at, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (path, digest, size, mtime, time.time(), status),
    )


def _apply_outcome(path, status, cls, msg):
    """Write the ledger row for one finished check (before stamping it)."""
    if status == "ok":
        clear_problem(path, SOURCE_INTEGRITY)
        return True
    return record_problem(path, SOURCE_INTEGRITY, error_class=cls, error_message=msg)


def recheck_file(path):
    """Re-verify one file now. ``(ok, message)``; records or clears its own row."""
    from core.database import db_conn

    try:
        digest, status, cls, msg = check_file(path)
    except OSError as e:
        record_problem(path, SOURCE_INTEGRITY, exc=e)
        return False, "Could not read the file"
    _apply_outcome(path, status, cls, msg)
    try:
        with db_conn() as conn:
            if conn:
                sig = _index_signature(conn, path)
                if sig is None:
                    st = os.stat(path)
                    sig = (st.st_size, st.st_mtime)
                _store_health(conn, path, digest, sig[0], sig[1], status)
                conn.commit()
    except Exception as e:
        app_logger.error(f"Could not store health for {path}: {e}")
    if status == "ok":
        return True, "Archive verified"
    return False, "Still damaged"


# ---------------------------------------------------------------------------
# The rolling batch
# ---------------------------------------------------------------------------

def _ext_clause(column, exts):
    return "(" + " OR ".join(f"LOWER({column}) LIKE '%{e}'" for e in exts) + ")"


def _unreachable_roots():
    """Library roots that are not mounted right now."""
    from helpers.library import get_library_roots

    out = []
    try:
        for root in get_library_roots():
            if not os.path.isdir(root):
                out.append(root)
    except Exception as e:
        app_logger.debug(f"Library health: could not list roots: {e}")
    return out


def files_needing_check(limit, recheck_days, exclude_roots=()):
    """Index rows with no trustworthy ``file_health`` row, stalest first."""
    from core.database import db_conn

    cutoff = time.time() - max(1, recheck_days) * 86400
    sql = (
        "SELECT f.path, f.size, f.modified_at FROM file_index f "
        "LEFT JOIN file_health h ON h.path = f.path "
        "WHERE f.type = 'file' AND " + _ext_clause("f.path", HASHED_EXTS) + " "
        "AND (h.path IS NULL OR h.mtime IS NOT f.modified_at "
        "     OR h.size IS NOT f.size OR h.checked_at < ?) "
    )
    params = [cutoff]
    for root in exclude_roots:
        sql += "AND f.path NOT LIKE ? "
        params.append(os.path.join(root, "") + "%")
    sql += "ORDER BY COALESCE(h.checked_at, 0) ASC LIMIT ?"
    params.append(limit)
    try:
        with db_conn() as conn:
            if not conn:
                return []
            return [
                {"path": r[0], "size": r[1], "mtime": r[2]}
                for r in conn.execute(sql, params).fetchall()
            ]
    except Exception as e:
        app_logger.error(f"Library health: could not list files to check: {e}")
        return []


def run_batch(limit, recheck_days, time_budget=DEFAULT_TIME_BUDGET, op_id=None):
    """Check up to ``limit`` files. Never raises. Returns a stats dict."""
    from core.app_state import update_operation
    from core.database import db_conn

    stats = {
        "checked": 0, "damaged": 0, "unreadable": 0, "missing": 0,
        "aborted": False, "cleared": 0,
    }
    started = time.monotonic()
    try:
        todo = files_needing_check(limit, recheck_days, _unreachable_roots())
        pending = []  # outcomes held back until the streak is known to be clean
        streak = 0

        def flush(conn):
            for item in pending:
                path, size, mtime, digest, status, cls, msg, exc = item
                if exc is not None:
                    record_problem(path, SOURCE_INTEGRITY, exc=exc)
                    stats["unreadable"] += 1
                    _store_health(conn, path, None, size, mtime, "unreadable")
                else:
                    _apply_outcome(path, status, cls, msg)
                    if status != "ok":
                        stats["damaged"] += 1
                    _store_health(conn, path, digest, size, mtime, status)
                conn.commit()
                stats["checked"] += 1
            pending.clear()

        with db_conn() as conn:
            if not conn:
                return stats
            for i, item in enumerate(todo):
                if time.monotonic() - started > time_budget:
                    break
                path = item["path"]
                if op_id and i % 10 == 0:
                    update_operation(
                        op_id, detail=f"Checked {stats['checked']} of {len(todo)} files"
                    )
                try:
                    digest, status, cls, msg = check_file(path)
                except FileNotFoundError:
                    parent = os.path.dirname(path)
                    if parent and os.path.isdir(parent):
                        # Gone from a folder that is there: the index is behind.
                        # Stamp it so it stops heading the queue; no ledger row
                        # (listing would prune it at once -- _is_definitely_gone).
                        _store_health(conn, path, None, item["size"], item["mtime"], "missing")
                        conn.commit()
                        stats["missing"] += 1
                        streak = 0
                        continue
                    streak += 1
                    pending.append((path, item["size"], item["mtime"], None, None, None, None,
                                    FileNotFoundError(path)))
                except OSError as e:
                    streak += 1
                    pending.append((path, item["size"], item["mtime"], None, None, None, None, e))
                else:
                    streak = 0
                    pending.append((path, item["size"], item["mtime"], digest, status, cls, msg, None))
                    flush(conn)
                    continue
                if streak >= MAX_CONSECUTIVE_IO_ERRORS:
                    app_logger.warning(
                        f"Library health scan stopped: {streak} files in a row could "
                        "not be read. This looks like a mount problem, not damaged "
                        "comics; nothing was recorded for them."
                    )
                    pending.clear()
                    stats["aborted"] = True
                    break
            if pending:
                flush(conn)
    except Exception as e:
        app_logger.error(f"Library health batch failed: {e}")
        stats["aborted"] = True
    return stats


def prune_health_rows():
    """Drop ``file_health`` rows for paths that left the index.

    Only when the index is non-empty: a rebuild in progress would otherwise look
    like "everything was deleted" and throw every hash away.
    """
    from core.database import db_conn

    try:
        with db_conn() as conn:
            if not conn:
                return 0
            if not conn.execute("SELECT 1 FROM file_index LIMIT 1").fetchone():
                return 0
            cur = conn.execute(
                "DELETE FROM file_health WHERE path NOT IN (SELECT path FROM file_index)"
            )
            conn.commit()
            return cur.rowcount
    except Exception as e:
        app_logger.error(f"Library health: could not prune file_health: {e}")
        return 0


# ---------------------------------------------------------------------------
# Cheap checks
# ---------------------------------------------------------------------------

def comicinfo_counts():
    """``{total, missing}`` over indexed CBZ/ZIP. Counts only, no ledger rows:
    a library with 40,000 untagged files must not become a 40,000-row page."""
    from core.database import db_conn

    out = {"total": 0, "missing": 0}
    try:
        with db_conn() as conn:
            if not conn:
                return out
            row = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN has_comicinfo = 0 THEN 1 ELSE 0 END) "
                "FROM file_index WHERE type = 'file' AND " + _ext_clause("path", ZIP_EXTS)
            ).fetchone()
            out["total"] = row[0] or 0
            out["missing"] = row[1] or 0
    except Exception as e:
        app_logger.error(f"Library health: could not count ComicInfo: {e}")
    return out


def _existing_orphan_rows(klass):
    from core.database import db_conn

    try:
        with db_conn() as conn:
            if not conn:
                return set()
            return {
                r[0]
                for r in conn.execute(
                    "SELECT path FROM problem_files WHERE source = ? AND error_class = ?",
                    (SOURCE_ORPHAN, klass),
                ).fetchall()
            }
    except Exception as e:
        app_logger.error(f"Library health: could not read orphan rows: {e}")
        return set()


def _configured_dir(getter):
    try:
        return getter() or ""
    except Exception:
        return ""


def _staging_dirs():
    """Staging folders worth walking for strays: configured, present, outside a library.

    Only WATCH. TARGET is excluded on purpose: files are expected to sit there
    until the wanted sweep (or the user) files them, so an old file there is not
    a problem worth listing.

    A staging folder inside a library is indistinguishable from library
    structure (see ``helpers.collection.collect_target_candidates``), so it is
    left alone rather than guessed at.
    """
    from core.config import get_watch_dir
    from helpers.library import library_root_for

    folder = _configured_dir(get_watch_dir)
    if not folder or not os.path.isdir(folder):
        return []
    if library_root_for(folder) is not None:
        return []
    return [("watch", folder)]


def _under(path, folder):
    return bool(folder) and path.startswith(os.path.join(folder, ""))


def scan_orphans():
    """Record CBR/CBZ twins and stale staged comics. Never raises.

    Returns ``{pairs, strays, cleared}``. Rows are only cleared under places
    that were actually reachable this run -- an unmounted library looks exactly
    like "every stray was fixed".
    """
    from core.database import db_conn
    from core.download_utils import is_temporary_download_file
    from helpers import is_hidden
    from helpers.library import get_library_roots

    stats = {"pairs": 0, "strays": 0, "cleared": 0}
    try:
        # 1. A .cbr beside its own .cbz (from the index; no library walk).
        reachable = tuple(
            os.path.join(r, "") for r in get_library_roots() if os.path.isdir(r)
        )
        seen = set()
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT path FROM file_index WHERE type = 'file' AND LOWER(path) LIKE '%.cbr'"
            ).fetchall() if conn else []
        for (path,) in rows:
            parent = os.path.dirname(path)
            if not parent or not os.path.isdir(parent):
                continue
            base = os.path.splitext(path)[0]
            if any(os.path.isfile(base + e) for e in (".cbz", ".CBZ")):
                seen.add(path)
                stats["pairs"] += 1
                record_problem(
                    path, SOURCE_ORPHAN, error_class=CLASS_CBR_BESIDE_CBZ,
                    error_message="A .cbz with the same name is in the same folder",
                )
        for path in _existing_orphan_rows(CLASS_CBR_BESIDE_CBZ) - seen:
            if reachable and path.startswith(reachable):
                if clear_problem(path, SOURCE_ORPHAN):
                    stats["cleared"] += 1

        # 2. Comics left in WATCH for more than a day. Never TARGET: files are
        # expected to wait there (see _staging_dirs), including a TARGET nested
        # inside WATCH.
        from core.config import get_target_dir, get_watch_dir

        target = _configured_dir(get_target_dir)
        target = os.path.normpath(target) if target else ""
        stray_seen = set()
        walked = []
        cutoff = time.time() - STRAY_MIN_AGE_SECONDS
        for kind, folder in _staging_dirs():
            walked.append(os.path.join(folder, ""))
            candidates = []
            for root, dirs, names in os.walk(folder):
                dirs[:] = [
                    d for d in dirs
                    if not is_hidden(os.path.join(root, d))
                    and os.path.normpath(os.path.join(root, d)) != target
                ]
                candidates.extend(os.path.join(root, n) for n in names)
            for path in candidates:
                name = os.path.basename(path)
                if (not name.lower().endswith(COMIC_EXTS)
                        or name.startswith((".", "_"))
                        or is_temporary_download_file(name)):
                    continue
                try:
                    if os.path.getmtime(path) > cutoff:
                        continue
                except OSError:
                    continue
                stray_seen.add(path)
                stats["strays"] += 1
                record_problem(
                    path, SOURCE_ORPHAN, error_class=CLASS_STRAY_STAGING,
                    error_message=f"Unfiled in {kind.upper()} for over a day",
                )
        # Rows under TARGET predate its exclusion; they go whether or not TARGET
        # is reachable, since nothing there is reported any more. WATCH nested
        # inside TARGET keeps its rows (the walk above owns those).
        watch = _configured_dir(get_watch_dir)
        for path in _existing_orphan_rows(CLASS_STRAY_STAGING) - stray_seen:
            in_target = _under(path, target) and not _under(path, watch)
            if in_target or (walked and path.startswith(tuple(walked))):
                if clear_problem(path, SOURCE_ORPHAN):
                    stats["cleared"] += 1
    except Exception as e:
        app_logger.error(f"Library health: orphan scan failed: {e}")
    return stats


def recheck_orphan(path):
    """Re-evaluate one orphan row. ``(ok, message)``; clears it when it no longer holds."""
    row = get_problem(path, SOURCE_ORPHAN)
    if row is None:
        return True, "No longer listed"
    klass = row.get("error_class")
    if not os.path.exists(path):
        clear_problem(path, SOURCE_ORPHAN)
        return True, "The file is gone"
    if klass == CLASS_CBR_BESIDE_CBZ:
        base = os.path.splitext(path)[0]
        if not any(os.path.isfile(base + e) for e in (".cbz", ".CBZ")):
            clear_problem(path, SOURCE_ORPHAN)
            return True, "No .cbz beside it any more"
        return False, "A .cbz is still beside it"
    if klass == CLASS_STRAY_STAGING:
        from core.config import get_target_dir, get_watch_dir

        target = _configured_dir(get_target_dir)
        if _under(path, target) and not _under(path, _configured_dir(get_watch_dir)):
            clear_problem(path, SOURCE_ORPHAN)
            return True, "TARGET is not checked for stray files"
        try:
            young = os.path.getmtime(path) > time.time() - STRAY_MIN_AGE_SECONDS
        except OSError:
            return False, "Could not read the file"
        if young:
            clear_problem(path, SOURCE_ORPHAN)
            return True, "Modified recently"
        return False, "Still unfiled"
    return False, "Unknown entry"


# ---------------------------------------------------------------------------
# Empty folders
# ---------------------------------------------------------------------------
#
# A library folder with no comics in it: nothing at all, or only the metadata a
# move or a delete elsewhere left behind. Things that look arbitrary and are not:
#
# - **Only the topmost empty folder is listed.** "Publisher/Series/" with an
#   empty "Series" and nothing else in "Publisher" is one row (Publisher), not
#   two: deleting it removes the lot, and two rows would offer to delete a folder
#   that the first Delete already took away.
# - **A library root and every protected root are never listed, and the walk
#   never descends into a protected one** (WATCH/TARGET inside a library). They
#   count as content, so the folder above them is not empty either.
# - **A hidden subfolder counts as content.** The walk does not look inside it,
#   and "I did not look" must not become "there is nothing there".
# - **Only names CLU itself writes count as leftovers** -- cvinfo, series.json
#   and folder art. Anything else (a cover.jpg, a .nfo, a text file) is the
#   user's and keeps the folder off the list. OS litter (dotfiles, Thumbs.db,
#   desktop.ini) is ignored altogether.
# - **Delete re-checks.** A folder flagged yesterday may hold a comic today, so
#   ``/api/problem-files/delete`` asks ``inspect_folder`` again before trashing.

SIDECAR_NAMES = frozenset({"cvinfo", "series.json"})
# The stems routes.collection.FOLDER_ART_STEMS serves as folder art (core must
# not import routes), with helpers.FOLDER_THUMBNAIL_EXTENSIONS.
FOLDER_ART_STEMS = frozenset({"folder", "header", "overlay"})
OS_LITTER_NAMES = frozenset({"thumbs.db", "desktop.ini"})
# New empty-folder rows per scan. The ledger caps *all* open rows at
# MAX_OPEN_PROBLEMS, and a reorganised library can leave thousands of empty
# folders behind; cosmetic rows must not crowd out damaged comics.
MAX_EMPTY_FOLDER_ROWS = 500

_empty_scan_lock = threading.Lock()


def _leftover_kind(name):
    """``None`` for content, ``"litter"`` to ignore, ``"sidecar"`` for leftovers."""
    from helpers import FOLDER_THUMBNAIL_EXTENSIONS

    lower = name.lower()
    if lower.startswith(".") or lower in OS_LITTER_NAMES:
        return "litter"
    if lower in SIDECAR_NAMES:
        return "sidecar"
    stem, ext = os.path.splitext(lower)
    if stem in FOLDER_ART_STEMS and ext in FOLDER_THUMBNAIL_EXTENSIONS:
        return "sidecar"
    return None


def _empty_tree(top, protected):
    """Every folder under ``top`` (itself included) that holds no content.

    Returns ``{dirpath: leftovers}``, where ``leftovers`` is the sorted list of
    sidecar names anywhere beneath it (empty for a truly empty folder). One
    top-down walk so hidden and protected folders can be pruned, then a pass in
    reverse, which visits every child before its parent.
    """
    from helpers import is_hidden

    order = []
    info = {}
    for dirpath, dirnames, filenames in os.walk(top):
        keep, blocked = [], False
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if is_hidden(full) or os.path.realpath(full) in protected:
                blocked = True
            else:
                keep.append(d)
        dirnames[:] = keep
        leftovers, content = [], blocked
        for name in filenames:
            kind = _leftover_kind(name)
            if kind is None:
                content = True
                break
            if kind == "sidecar":
                leftovers.append(name)
        order.append(dirpath)
        info[dirpath] = (content, leftovers, [os.path.join(dirpath, d) for d in keep])

    empty = {}
    for dirpath in reversed(order):
        content, leftovers, children = info[dirpath]
        if content or any(c not in empty for c in children):
            continue
        names = set(leftovers)
        for c in children:
            names.update(empty[c])
        empty[dirpath] = sorted(names)
    return empty


def _empty_outcome(leftovers):
    """``(error_class, message)`` for an empty folder's ledger row."""
    if not leftovers:
        return CLASS_EMPTY_FOLDER, "No files"
    shown = ", ".join(leftovers[:6])
    more = f" and {len(leftovers) - 6} more" if len(leftovers) > 6 else ""
    return CLASS_SIDECAR_ONLY_FOLDER, f"Only {shown}{more}"


def _protected_realpaths():
    from helpers.library import get_protected_roots

    return {os.path.realpath(p) for p in get_protected_roots()}


def inspect_folder(path):
    """``(error_class, message)`` if ``path`` is an empty library folder, else None.

    None also for a library root, a protected root, or anything that is not a
    reachable directory -- the answer Delete needs before it trashes a folder.
    """
    try:
        if not os.path.isdir(path):
            return None
        protected = _protected_realpaths()
        if os.path.realpath(path) in protected:
            return None
        found = _empty_tree(path, protected)
    except OSError:
        return None
    if path not in found:
        return None
    return _empty_outcome(found[path])


def _existing_empty_folder_rows():
    from core.database import db_conn

    try:
        with db_conn() as conn:
            if not conn:
                return None
            return {
                r[0]
                for r in conn.execute(
                    "SELECT path FROM problem_files WHERE source = ?",
                    (SOURCE_EMPTY_FOLDER,),
                ).fetchall()
            }
    except Exception as e:
        app_logger.error(f"Library health: could not read empty-folder rows: {e}")
        return None


def empty_folder_scan_running():
    return _empty_scan_lock.locked()


def scan_empty_folders(op_id=None):
    """Record the library's empty folders and clear rows that no longer hold.

    Never raises. Returns ``{folders, flagged, cleared, capped}`` (``folders``
    being the empty folders found, ``flagged`` the rows written), or None when
    another empty-folder scan is already running. Rows are cleared only under
    roots that were actually walked: an unmounted library would otherwise look
    like "every empty folder was filled".
    """
    from core.app_state import update_operation
    from helpers.library import get_library_roots

    if not _empty_scan_lock.acquire(blocking=False):
        return None
    stats = {"folders": 0, "flagged": 0, "cleared": 0, "capped": 0}
    try:
        protected = _protected_realpaths()
        existing = _existing_empty_folder_rows() or set()
        walked, seen = [], set()
        for root in get_library_roots():
            if not os.path.isdir(root):
                continue
            walked.append(os.path.join(root, ""))
            if op_id:
                update_operation(op_id, detail=f"Looking for empty folders in {root}")
            found = _empty_tree(root, protected)
            for path, leftovers in sorted(found.items()):
                # The root itself is never listed, and a folder inside an empty
                # one is covered by its parent's row.
                parent = os.path.dirname(path)
                if path == root or (parent in found and parent != root):
                    continue
                stats["folders"] += 1
                seen.add(path)
                if path not in existing and stats["flagged"] >= MAX_EMPTY_FOLDER_ROWS:
                    stats["capped"] += 1
                    continue
                cls, msg = _empty_outcome(leftovers)
                if record_problem(path, SOURCE_EMPTY_FOLDER, error_class=cls, error_message=msg):
                    stats["flagged"] += 1
        for path in existing - seen:
            if walked and path.startswith(tuple(walked)):
                if clear_problem(path, SOURCE_EMPTY_FOLDER):
                    stats["cleared"] += 1
    except Exception as e:
        app_logger.error(f"Library health: empty-folder scan failed: {e}")
    finally:
        _empty_scan_lock.release()
    app_logger.info(
        f"Empty-folder scan: {stats['flagged']} listed, {stats['cleared']} cleared"
        + (f", {stats['capped']} more not listed (cap {MAX_EMPTY_FOLDER_ROWS})"
           if stats["capped"] else "")
    )
    return stats


def recheck_empty_folder(path):
    """Re-evaluate one empty-folder row. ``(ok, message)``, as ``recheck_orphan``."""
    if get_problem(path, SOURCE_EMPTY_FOLDER) is None:
        return True, "No longer listed"
    if not os.path.isdir(path):
        clear_problem(path, SOURCE_EMPTY_FOLDER)
        return True, "The folder is gone"
    outcome = inspect_folder(path)
    if outcome is None:
        clear_problem(path, SOURCE_EMPTY_FOLDER)
        return True, "The folder has content now"
    record_problem(path, SOURCE_EMPTY_FOLDER, error_class=outcome[0], error_message=outcome[1])
    return False, "Still empty"


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------

_COPY_MARKER = re.compile(r"\s*\(\d{1,3}\)\s*$")
_CLAIM_SUFFIX = re.compile(r"_\d{1,3}$")


def _norm_text(value):
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _norm_number(value):
    """``"001"`` and ``"1"`` are one issue; ``"-1"`` keeps its sign (see the
    negative-issue-number handling in the matcher and renamer)."""
    v = (value or "").strip().lower()
    if not v:
        return ""
    m = re.fullmatch(r"(-?)0*(\d+)(.*)", v)
    if not m:
        return v
    return f"{m.group(1)}{m.group(2)}{m.group(3)}"


def name_key(filename):
    """A filename with only copy markers removed.

    Deliberately *not* "strip every parenthetical": that would fold
    ``Batman 001 (Variant A)`` into ``Batman 001 (Variant B)``. Only the two
    markers CLU and file managers actually append are dropped -- a trailing
    ``(1)`` and the ``_4`` a download name-claim adds.
    """
    base = os.path.splitext(filename)[0]
    base = _COPY_MARKER.sub("", base)
    base = _CLAIM_SUFFIX.sub("", base)
    # "-1" is a real issue number, distinct from "1" (see the negative-issue
    # handling in the matcher); _norm_text would otherwise drop the sign.
    base = re.sub(r"(?<![A-Za-z0-9])-(?=\d)", " neg", base)
    return _norm_text(base)


_BRACKETED = re.compile(r"[\(\[][^\)\]]*[\)\]]")


def issue_stem(filename, number):
    """The series part of a comic's filename: everything before its issue number.

    ``Future Quest 001 (2016).cbz`` -> ``future quest``;
    ``Aquaman - Jabberjaw Special 001 (2018).cbz`` -> ``aquaman jabberjaw special``.
    Brackets and copy markers go first, so ``Batman 001 (1).cbz`` and
    ``Batman 001 (digital).cbz`` share a stem. Plain regexes, not
    ``parse_comic_filename``: that logs at INFO on every match, and this runs
    once per file in the library.
    """
    base = os.path.splitext(os.path.basename(filename))[0]
    base = _BRACKETED.sub(" ", base)
    base = _CLAIM_SUFFIX.sub("", base)
    tokens = _norm_text(base).split()
    target = _norm_number(number)
    for i, tok in enumerate(tokens):
        if i > 0 and _norm_number(tok) == target:
            return " ".join(tokens[:i])
    return " ".join(tokens)


def _sha(text):
    return hashlib.sha1(text.encode("utf-8", "surrogatepass"), usedforsecurity=False).hexdigest()


def _members_sig(paths):
    return _sha("\n".join(sorted(paths)))


def _suggest_keeper(members):
    """The copy to keep: undamaged, tagged, a .cbz, bigger, newer -- in that order."""
    def score(m):
        return (
            m.get("status") != "damaged",
            bool(m.get("has_comicinfo")),
            m["path"].lower().endswith(".cbz"),
            m.get("size") or 0,
            m.get("modified_at") or 0,
        )
    return max(members, key=score)["path"]


def _load_dismissals():
    """``{group_key: (members_sig, until_scan)}``."""
    from core.database import db_conn

    try:
        with db_conn() as conn:
            if not conn:
                return {}
            return {
                key: (sig, bool(until_scan))
                for key, sig, until_scan in conn.execute(
                    "SELECT group_key, members_sig, until_scan FROM duplicate_dismissals"
                ).fetchall()
            }
    except Exception as e:
        app_logger.error(f"Library health: could not read dismissals: {e}")
        return {}


def _member(row, health):
    path = row["path"]
    h = health.get(path) or {}
    return {
        "path": path,
        "name": os.path.basename(path),
        "folder": os.path.dirname(path),
        "size": row["size"],
        "modified_at": row["modified_at"],
        "series": row["ci_series"],
        "number": row["ci_number"],
        "year": row["ci_year"],
        "has_comicinfo": bool(row["has_comicinfo"]),
        "provider": row["ci_provider"],
        "hash": h.get("hash"),
        "status": h.get("status"),
    }


def find_duplicates(include_dismissed=False, strict=False):
    """``{exact: [...], likely: [...], total_groups}`` -- derived on every call.

    Never raises; a read failure yields empty lists -- unless ``strict``, for a
    caller that must tell "this group is gone" from "could not look".
    """
    from core.database import db_conn

    out = {"exact": [], "likely": []}
    try:
        with db_conn() as conn:
            if not conn:
                if strict:
                    raise RuntimeError("Database unavailable")
                return out
            conn.row_factory = sqlite3.Row
            comics = conn.execute(
                "SELECT path, size, modified_at, ci_series, ci_number, ci_year, "
                "ci_publisher, has_comicinfo, ci_provider FROM file_index "
                "WHERE type = 'file' AND " + _ext_clause("path", COMIC_EXTS)
            ).fetchall()
            health = {
                r["path"]: {"hash": r["content_hash"], "status": r["status"]}
                for r in conn.execute(
                    "SELECT h.path, h.content_hash, h.status FROM file_health h "
                    "JOIN file_index f ON f.path = h.path "
                    "WHERE h.content_hash IS NOT NULL AND h.status != 'missing' "
                    "AND h.mtime IS f.modified_at AND h.size IS f.size"
                ).fetchall()
            }
        by_path = {r["path"]: r for r in comics}
        dismissals = _load_dismissals()
        claimed_sets = set()

        def emit(kind, reason, group_key, paths):
            paths = [p for p in paths if p in by_path]
            if len(paths) < 2:
                return
            sig = _members_sig(paths)
            stored_sig, until_scan = dismissals.get(group_key, (None, False))
            is_dismissed = stored_sig == sig
            if is_dismissed and not include_dismissed:
                return
            members = [_member(by_path[p], health) for p in paths]
            members.sort(key=lambda m: m["path"])
            keeper = _suggest_keeper(members)
            sizes = [m["size"] or 0 for m in members]
            keep_size = next((m["size"] or 0 for m in members if m["path"] == keeper), 0)
            out[kind].append({
                "id": _sha(group_key)[:16],
                "key": group_key,
                "kind": kind,
                "dismissed": is_dismissed,
                # "scan": hidden until the next scan; "not_duplicate": for good.
                "dismiss_mode": (
                    ("scan" if until_scan else "not_duplicate") if is_dismissed else None
                ),
                "reason": reason,
                "members": members,
                "keep_suggestion": keeper,
                "wasted_bytes": max(0, sum(sizes) - keep_size),
            })

        # Exact: identical bytes.
        by_hash = {}
        for path, h in health.items():
            if path in by_path and h["hash"]:
                by_hash.setdefault(h["hash"], []).append(path)
        exact_sets = set()
        for digest, paths in by_hash.items():
            if len(paths) > 1:
                exact_sets.add(frozenset(paths))
                emit("exact", "Identical files (same content hash)", f"exact:{digest}", paths)
        claimed_sets |= exact_sets

        # Likely 1: the same tagged issue. Tags alone are not enough -- an event
        # whose issues were all matched to the parent series (Future Quest and
        # its "Aquaman / Jabberjaw Special", "Green Lantern / Space Ghost
        # Special"...) carries one series, number and year across different
        # comics. So the year must be known, and the filenames must agree on the
        # series (issue_stem); a tag that contradicts the filename is a tagging
        # problem, not a duplicate.
        tagged = {}
        for r in comics:
            series, number = _norm_text(r["ci_series"]), _norm_number(r["ci_number"])
            year = str(r["ci_year"] or "").strip()
            if not series or not number or not year:
                continue
            stem = issue_stem(r["path"], r["ci_number"])
            key = (series, number, year, _norm_text(r["ci_publisher"]), stem)
            tagged.setdefault(key, []).append(r["path"])
        for key, paths in tagged.items():
            if len(paths) < 2 or frozenset(paths) in claimed_sets:
                continue
            claimed_sets.add(frozenset(paths))
            emit("likely", "Tagged as the same series, issue and year, with matching file names",
                 "tag:" + _sha("|".join(key)), paths)

        # Likely 2: one folder, names that differ only by a copy marker.
        named = {}
        for r in comics:
            key = (os.path.dirname(r["path"]), name_key(os.path.basename(r["path"])))
            if key[1]:
                named.setdefault(key, []).append(r["path"])
        for key, paths in named.items():
            if len(paths) < 2 or frozenset(paths) in claimed_sets:
                continue
            claimed_sets.add(frozenset(paths))
            emit("likely", "Same name in one folder, apart from a copy marker",
                 "name:" + _sha("|".join(key)), paths)

        for kind in ("exact", "likely"):
            out[kind].sort(key=lambda g: g["wasted_bytes"], reverse=True)
    except Exception as e:
        app_logger.error(f"Library health: duplicate search failed: {e}")
        if strict:
            raise
        return {"exact": [], "likely": []}
    return out


def count_duplicates():
    found = find_duplicates()
    return len(found["exact"]), len(found["likely"])


def _find_group(group_id, strict=False):
    found = find_duplicates(include_dismissed=True, strict=strict)
    for kind in ("exact", "likely"):
        for g in found[kind]:
            if g["id"] == group_id:
                return g
    return None


def dismiss_group(group_id, dismissed=True, until_scan=False, seen_paths=None):
    """Hide (or re-show) a group until its member set changes.

    Returns ``{success, gone, changed, error}`` for every expected outcome, and
    raises on a database failure: the caller logs it and answers with a fixed
    message (``helpers.responses.error_response``), so no exception text ever
    reaches the result.

    ``until_scan`` is for a group the user resolved somewhere else (the File
    Manager, another machine): it is hidden only until the next scan finishes
    (``expire_scan_dismissals``), so if the copies are in fact still there the
    group comes back instead of being forgotten for good.

    A group that no longer exists is a success (``gone``), not an error: the
    commonest reason is exactly that the user fixed it elsewhere before
    clicking, and there is nothing left to hide. Telling that apart from a
    failed read is why the lookup is ``strict``.

    ``seen_paths`` is the member list the page showed. A copy the user never
    saw must not be hidden along with the ones they did, so if one has turned
    up the dismissal is refused (``changed``) and the page reloads. Copies that
    have since gone are fine: the dismissal is signed on what is left.
    """
    from core.database import db_conn

    group = _find_group(group_id, strict=True)
    if group is None:
        return {"success": True, "gone": True}
    paths = [m["path"] for m in group["members"]]
    if dismissed and seen_paths is not None and not set(paths) <= set(seen_paths):
        return {"success": False, "changed": True,
                "error": "Another copy has turned up since the page loaded"}
    with db_conn() as conn:
        if not conn:
            return {"success": False, "error": "Database unavailable"}
        if dismissed:
            conn.execute(
                "INSERT OR REPLACE INTO duplicate_dismissals "
                "(group_key, members_sig, until_scan) VALUES (?, ?, ?)",
                (group["key"], _members_sig(paths), 1 if until_scan else 0),
            )
        else:
            conn.execute(
                "DELETE FROM duplicate_dismissals WHERE group_key = ?", (group["key"],)
            )
        conn.commit()
    return {"success": True, "gone": False}


def expire_scan_dismissals(started_at):
    """Drop the until-next-scan dismissals made before a scan began. Returns the count.

    Bounded by the scan's *start*, not its end, so a group dismissed while a
    scan is running waits for the next one rather than reappearing at once.
    """
    from core.database import db_conn

    try:
        with db_conn() as conn:
            if not conn:
                return 0
            cur = conn.execute(
                "DELETE FROM duplicate_dismissals "
                "WHERE until_scan = 1 AND dismissed_at < datetime(?, 'unixepoch')",
                (int(started_at),),
            )
            conn.commit()
            return cur.rowcount
    except Exception as e:
        app_logger.error(f"Library health: could not expire dismissals: {e}")
        return 0


def resolve_group(group_id, keep_path, trash_paths):
    """Keep one copy and move the others to the trash. Needs an app context.

    Everything is re-derived and validated server-side: the paths must be
    members of the group *as it is now*, the keeper cannot be among those
    trashed, and the keeper must still exist -- so a stale page, or a crafted
    request, cannot trash a file that is not a duplicate of something safe.
    Returns ``{success, results, error}``.
    """
    from core.database import db_conn, forget_deleted_paths
    from helpers.library import is_critical_path
    from helpers.trash import move_to_trash

    group = _find_group(group_id)
    if group is None:
        return {"success": False, "error": "That group no longer exists"}
    members = {m["path"] for m in group["members"]}
    trash_paths = list(dict.fromkeys(trash_paths or []))
    if keep_path not in members:
        return {"success": False, "error": "The file to keep is not in this group"}
    if not trash_paths or any(p not in members for p in trash_paths):
        return {"success": False, "error": "Some files are not in this group"}
    if keep_path in trash_paths:
        return {"success": False, "error": "Cannot trash the file you are keeping"}
    if not os.path.isfile(keep_path):
        return {"success": False, "error": "The file to keep is no longer on disk"}

    results, trashed = [], []
    for path in trash_paths:
        try:
            if is_critical_path(path):
                results.append({"path": path, "success": False, "error": "Protected path"})
                continue
            res = move_to_trash(path)
            trashed.append(path)
            results.append({"path": path, "success": True, "trashed": res.get("trashed")})
        except Exception as e:
            results.append({"path": path, "success": False, "error": str(e)})

    if trashed:
        try:
            forget_deleted_paths(trashed)
            for path in trashed:
                clear_problem(path)
            with db_conn() as conn:
                if conn:
                    conn.executemany(
                        "DELETE FROM file_health WHERE path = ?", [(p,) for p in trashed]
                    )
                    conn.commit()
        except Exception as e:
            app_logger.error(f"Library health: bookkeeping after duplicate trash failed: {e}")
    return {"success": all(r["success"] for r in results), "results": results}


# ---------------------------------------------------------------------------
# State, summary and score
# ---------------------------------------------------------------------------

def load_state():
    from core.database import get_user_preference

    try:
        state = get_user_preference(STATE_KEY, None)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def save_state(state):
    from core.database import set_user_preference

    try:
        return bool(set_user_preference(STATE_KEY, state, category="system"))
    except Exception as e:
        app_logger.error(f"Library health: could not save scan state: {e}")
        return False


def coverage():
    """``{total, checked, pct}`` -- how much of the library has a current result."""
    from core.database import db_conn

    out = {"total": 0, "checked": 0, "pct": 0}
    try:
        with db_conn() as conn:
            if not conn:
                return out
            clause = _ext_clause("f.path", HASHED_EXTS)
            out["total"] = conn.execute(
                f"SELECT COUNT(*) FROM file_index f WHERE f.type = 'file' AND {clause}"
            ).fetchone()[0]
            out["checked"] = conn.execute(
                "SELECT COUNT(*) FROM file_index f JOIN file_health h ON h.path = f.path "
                f"WHERE f.type = 'file' AND {clause} "
                "AND h.mtime IS f.modified_at AND h.size IS f.size"
            ).fetchone()[0]
    except Exception as e:
        app_logger.error(f"Library health: could not compute coverage: {e}")
    if out["total"]:
        out["pct"] = int(100 * out["checked"] / out["total"])
    return out


def compute_score(integrity_open, exact_groups, likely_groups, orphans_open, other_open):
    """0-100, 100 being nothing to report. Each term is capped so one noisy
    category cannot zero the score on its own."""
    penalty = (
        min(60, 10 * integrity_open)
        + min(15, exact_groups)
        + min(10, likely_groups)
        + min(10, orphans_open)
        + min(5, other_open)
    )
    return max(0, 100 - penalty)


def get_summary():
    """Everything the card and the badge show. Cheap: no scanning, no duplicates."""
    from core.settings import get_bool, get_int

    state = load_state()
    probs = count_problems()
    by = probs.get("by_source", {})
    integrity_open = by.get(SOURCE_INTEGRITY, 0)
    orphans_open = by.get(SOURCE_ORPHAN, 0)
    other_open = sum(
        n for src, n in by.items() if src not in (SOURCE_INTEGRITY, SOURCE_ORPHAN)
    )
    exact = state.get("dup_exact_groups", 0)
    likely = state.get("dup_likely_groups", 0)
    score = compute_score(integrity_open, exact, likely, orphans_open, other_open)
    if integrity_open:
        level = "red"
    elif score < 100:
        level = "amber"
    else:
        level = "green"
    cov = coverage()
    return {
        "enabled": get_bool("LIBRARY_HEALTH_SCAN_ENABLED"),
        "running": scan_running(),
        "interval_hours": get_int("LIBRARY_HEALTH_INTERVAL_HOURS"),
        "last_run_at": state.get("last_run_at"),
        "cheap_checks_at": state.get("cheap_checks_at"),
        "last_batch": state.get("last_batch"),
        "coverage": cov,
        "integrity_open": integrity_open,
        "orphans_open": orphans_open,
        "other_open": other_open,
        "series_json_open": by.get(SOURCE_SERIES_JSON, 0),
        "empty_folders_open": by.get(SOURCE_EMPTY_FOLDER, 0),
        "duplicates": {"exact": exact, "likely": likely},
        "comicinfo": state.get("comicinfo"),
        "score": score,
        "level": level,
    }


# ---------------------------------------------------------------------------
# Running a scan
# ---------------------------------------------------------------------------

def run_scan(op_id=None, run_cheap=True, batch=None, time_budget=DEFAULT_TIME_BUDGET):
    """One scan. The caller holds the claim (``claim_scan``). Never raises."""
    from core.settings import get_int

    state = load_state()
    now = time.time()
    result = {}
    try:
        limit = batch if batch is not None else max(1, get_int("LIBRARY_HEALTH_BATCH"))
        recheck_days = max(1, get_int("LIBRARY_HEALTH_RECHECK_DAYS"))

        stats = run_batch(limit, recheck_days, time_budget=time_budget, op_id=op_id)
        stats["pruned"] = prune_health_rows()
        state["last_batch"] = stats
        result["batch"] = stats

        if run_cheap:
            from core.app_state import update_operation

            if op_id:
                update_operation(op_id, detail="Checking series.json files")
            from core.series_json_check import scan_library

            result["series_json"] = scan_library()
            if op_id:
                update_operation(op_id, detail="Looking for stray files")
            result["orphans"] = scan_orphans()
            if op_id:
                update_operation(op_id, detail="Looking for empty folders")
            result["empty_folders"] = scan_empty_folders()
            state["comicinfo"] = comicinfo_counts()
            state["cheap_checks_at"] = now

        if op_id:
            from core.app_state import update_operation

            update_operation(op_id, detail="Looking for duplicates")
        # "Dismiss" promised to hide a group only until this scan: any that
        # still exist are counted and listed again.
        expire_scan_dismissals(now)
        exact, likely = count_duplicates()
        state["dup_exact_groups"] = exact
        state["dup_likely_groups"] = likely
        state["last_run_at"] = now
        save_state(state)
        app_logger.info(
            f"Library health scan: {stats['checked']} checked, {stats['damaged']} damaged, "
            f"{stats['unreadable']} unreadable, {exact} exact / {likely} likely duplicate "
            f"groups{' (aborted)' if stats['aborted'] else ''}"
        )
    except Exception as e:
        app_logger.error(f"Library health scan failed: {e}")
        result["error"] = str(e)
    return result


def run_scheduled_scan():
    """Heartbeat body. Gated on the opt-in and on the configured interval.

    Registered as a short ``IntervalTrigger`` and gated here on the persisted
    ``last_run_at`` -- the same reasoning as ``core.comicvine_db_update``: an
    interval trigger's clock restarts at process start and the scheduler has no
    jobstore, so a long trigger on a container restarted often would never fire.
    """
    from core.app_state import complete_operation, register_operation
    from core.settings import get_bool, get_int

    if not get_bool("LIBRARY_HEALTH_SCAN_ENABLED"):
        return None
    state = load_state()
    interval = max(1, get_int("LIBRARY_HEALTH_INTERVAL_HOURS")) * 3600
    last = state.get("last_run_at") or 0
    if time.time() - last < interval - 60:
        return None
    if not claim_scan():
        return None
    op_id = None
    failed = False
    try:
        try:
            op_id = register_operation("library_health", "Library health scan")
        except Exception:
            op_id = None
        cheap_due = time.time() - (state.get("cheap_checks_at") or 0) >= CHEAP_CHECK_INTERVAL
        result = run_scan(op_id=op_id, run_cheap=cheap_due)
        failed = "error" in result
        return result
    finally:
        if op_id:
            complete_operation(op_id, error=failed)
        release_scan()
