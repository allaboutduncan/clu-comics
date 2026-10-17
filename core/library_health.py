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
- **Heavy checks are paced separately from the cheap ones.** The series.json walk
  and the staging-folder walk run at most once a day on a schedule
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
    CLASS_NO_PAGES,
    CLASS_STRAY_STAGING,
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


def _staging_dirs():
    """WATCH/TARGET folders worth walking: configured, present, outside a library.

    A staging folder inside a library is indistinguishable from library
    structure (see ``helpers.collection.collect_target_candidates``), so it is
    left alone rather than guessed at.
    """
    from core.config import get_target_dir, get_watch_dir
    from helpers.library import library_root_for

    out = []
    for kind, getter in (("target", get_target_dir), ("watch", get_watch_dir)):
        try:
            folder = getter()
        except Exception:
            continue
        if not folder or not os.path.isdir(folder):
            continue
        if library_root_for(folder) is not None:
            continue
        out.append((kind, folder))
    return out


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

        # 2. Comics left in TARGET/WATCH for more than a day.
        stray_seen = set()
        walked = []
        cutoff = time.time() - STRAY_MIN_AGE_SECONDS
        for kind, folder in _staging_dirs():
            walked.append(os.path.join(folder, ""))
            if kind == "target":
                from helpers.collection import collect_target_candidates

                files, _ = collect_target_candidates(folder, skip_converted_siblings=False)
                candidates = [p for _, p in files]
            else:
                candidates = []
                for root, dirs, names in os.walk(folder):
                    dirs[:] = [d for d in dirs if not is_hidden(os.path.join(root, d))]
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
        for path in _existing_orphan_rows(CLASS_STRAY_STAGING) - stray_seen:
            if walked and path.startswith(tuple(walked)):
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
    from core.database import db_conn

    try:
        with db_conn() as conn:
            if not conn:
                return {}
            return dict(
                conn.execute("SELECT group_key, members_sig FROM duplicate_dismissals").fetchall()
            )
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


def find_duplicates(include_dismissed=False):
    """``{exact: [...], likely: [...], total_groups}`` -- derived on every call.

    Never raises; a read failure yields empty lists.
    """
    from core.database import db_conn

    out = {"exact": [], "likely": []}
    try:
        with db_conn() as conn:
            if not conn:
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
            is_dismissed = dismissals.get(group_key) == sig
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
        return {"exact": [], "likely": []}
    return out


def count_duplicates():
    found = find_duplicates()
    return len(found["exact"]), len(found["likely"])


def _find_group(group_id):
    found = find_duplicates(include_dismissed=True)
    for kind in ("exact", "likely"):
        for g in found[kind]:
            if g["id"] == group_id:
                return g
    return None


def dismiss_group(group_id, dismissed=True):
    """Hide (or re-show) a group until its member set changes. ``(ok, error)``."""
    from core.database import db_conn

    group = _find_group(group_id)
    if group is None:
        return False, "That group no longer exists"
    try:
        with db_conn() as conn:
            if not conn:
                return False, "Database unavailable"
            if dismissed:
                conn.execute(
                    "INSERT OR REPLACE INTO duplicate_dismissals (group_key, members_sig) "
                    "VALUES (?, ?)",
                    (group["key"], _members_sig([m["path"] for m in group["members"]])),
                )
            else:
                conn.execute(
                    "DELETE FROM duplicate_dismissals WHERE group_key = ?", (group["key"],)
                )
            conn.commit()
        return True, None
    except Exception as e:
        app_logger.error(f"Library health: could not update dismissal: {e}")
        return False, str(e)


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
            state["comicinfo"] = comicinfo_counts()
            state["cheap_checks_at"] = now

        if op_id:
            from core.app_state import update_operation

            update_operation(op_id, detail="Looking for duplicates")
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
