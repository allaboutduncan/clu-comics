"""The problem-files worklist: which comics a subsystem could not process, why,
and what the user can do about it.

Before this module, a damaged comic produced an ERROR log line and nothing else.
``thumbnail_jobs`` recorded a bare ``status='error'`` with no message, so the
reason existed only in a log that scrolls away, and the only UI surface was a
generic ``error.svg`` tile in the grid. This table is where a per-file failure
goes so it can be listed, explained and acted on.

Several things here look arbitrary and are not:

- **This is a ledger, not a history.** A fixed file's row is *deleted*, not
  flagged resolved. The page is a worklist; nothing reads a resolved row, and
  keeping them would cost a ``WHERE resolved_at IS NULL`` on every read while
  the table grew without bound behind a UI that never shows them. ``dcpp_jobs``
  states the same rule for the same reason. There is deliberately no
  ``forget_problem``: "the user fixed it elsewhere" and "the operation
  succeeded" are the same DELETE, and two names for one body is exactly the
  duplication this codebase keeps getting bitten by.
- **The key is ``(path, source)``, not ``path``.** One file can be broken in
  two ways at once — the thumbnailer cannot read page 1 *and* a metadata
  rewrite hit bad CRCs elsewhere. With ``path`` alone the last writer wins,
  ``occurrences`` counts unrelated event streams, and — the real breakage — a
  successful thumbnail would clear a row describing a *metadata* failure that
  nobody fixed.
- **Dismissal is keyed on the file's mtime, not on the error text.** A damaged
  archive raises ``zlib.error`` or ``BadZipFile`` depending on which page is
  read first, so matching on the message would un-dismiss a row at random.
  Dismiss means "I know about this file"; it lifts only when the file is
  actually rewritten and still fails, which is the only moment the user's
  knowledge is genuinely stale.
- **Pruning requires positive evidence.** A missing file is only proof of a
  deletion when its *parent directory* is still there. An unmounted library
  makes every path look deleted, and pruning on that would empty this table the
  first time a NAS went to sleep. "I cannot tell" means "keep the row" — the
  same reading :func:`core.thumbnail_cache.is_thumbnail_stale` takes.
- **Recording never raises.** Every caller is inside an ``except`` block whose
  job is to recover, several are background threads, and one is a subprocess.
  A diagnostic must never break the thing it is diagnosing. Same contract, and
  same reasoning, as :func:`core.thumbnail_cache.set_job_status`.
"""

import os

from core.app_logging import app_logger

SOURCE_THUMBNAIL = "thumbnail"
SOURCE_REBUILD = "rebuild"
SOURCE_METADATA_WRITE = "metadata-write"

KNOWN_SOURCES = (SOURCE_THUMBNAIL, SOURCE_REBUILD, SOURCE_METADATA_WRITE)

SOURCE_LABELS = {
    SOURCE_THUMBNAIL: "Thumbnail",
    SOURCE_REBUILD: "Rebuild",
    SOURCE_METADATA_WRITE: "Metadata write",
}

# Our own classes, for failures that are not exceptions. They must stay
# distinguishable from archive damage — see classify().
CLASS_NO_PAGES = "NoPagesInArchive"
CLASS_CACHE_WRITE = "ThumbnailCacheUnwritable"
CLASS_CORRUPT_ENTRIES = "PartialCRCSalvage"
CLASS_RAR_FAILED = "RarConversionFailed"

# A systemic failure — a mount whose permissions get revoked mid-scan — turns
# every file in the library into an OSError. A 40,000-row table behind an
# owner-facing page is useless and slow, so new inserts stop here while updates
# to rows that already exist carry on. One COUNT(*), only on the failure path.
MAX_OPEN_PROBLEMS = 2000

_cap_warned = False


def _file_mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def record_problem(path, source, exc=None, error_class=None, error_message=None):
    """Record — or re-record — that ``path`` failed under ``source``.

    Pass the exception as ``exc`` and the class and sanitised message are taken
    from it; pass ``error_class``/``error_message`` directly for failures that
    are not exceptions. Returns True if the row was written.

    Never raises.
    """
    global _cap_warned
    try:
        if exc is not None:
            from helpers import archive_error_detail

            error_class = error_class or type(exc).__name__
            error_message = error_message or archive_error_detail(exc)

        from core.database import get_db_connection

        conn = get_db_connection()
        if not conn:
            return False
        try:
            mtime = _file_mtime(path)
            existing = conn.execute(
                "SELECT occurrences FROM problem_files WHERE path = ? AND source = ?",
                (path, source),
            ).fetchone()

            if existing is None:
                open_count = conn.execute(
                    "SELECT COUNT(*) FROM problem_files WHERE dismissed_at IS NULL"
                ).fetchone()[0]
                if open_count >= MAX_OPEN_PROBLEMS:
                    if not _cap_warned:
                        _cap_warned = True
                        app_logger.warning(
                            f"Problem-files list is capped at {MAX_OPEN_PROBLEMS} "
                            "open entries; further new entries are not recorded. "
                            "This usually means a mount or permissions problem "
                            "rather than damaged comics."
                        )
                    return False
                conn.execute(
                    """
                    INSERT INTO problem_files
                        (path, source, error_class, error_message, file_mtime,
                         first_seen, last_seen, occurrences)
                    VALUES (?, ?, ?, ?, ?,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 1)
                    """,
                    (path, source, error_class, error_message, mtime),
                )
            else:
                # The newest failure is the accurate one, so class and message
                # are overwritten. Dismissal survives unless the file itself has
                # been rewritten since it was dismissed.
                conn.execute(
                    """
                    UPDATE problem_files
                       SET error_class   = ?,
                           error_message = ?,
                           last_seen     = CURRENT_TIMESTAMP,
                           occurrences   = occurrences + 1,
                           dismissed_at  = CASE
                               WHEN dismissed_at IS NULL THEN NULL
                               WHEN file_mtime IS NOT ? THEN NULL
                               ELSE dismissed_at
                           END,
                           file_mtime    = ?
                     WHERE path = ? AND source = ?
                    """,
                    (error_class, error_message, mtime, mtime, path, source),
                )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        app_logger.error(f"Could not record problem file {path} ({source}): {e}")
        return False


def clear_problem(path, source=None):
    """Drop the row(s) for ``path`` — the operation now succeeds.

    ``source=None`` clears every source for the path, which is what a rebuild
    that produced a genuinely different archive should do. Deletes outright,
    dismissal included: if the file breaks again it comes back as the new
    problem it is.

    This is also the "remove from the list" action — see the module docstring
    for why there is no second function for that.

    Never raises. Returns the number of rows removed.
    """
    try:
        from core.database import get_db_connection

        conn = get_db_connection()
        if not conn:
            return 0
        try:
            if source is None:
                cur = conn.execute(
                    "DELETE FROM problem_files WHERE path = ?", (path,)
                )
            else:
                cur = conn.execute(
                    "DELETE FROM problem_files WHERE path = ? AND source = ?",
                    (path, source),
                )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()
    except Exception as e:
        app_logger.error(f"Could not clear problem file {path} ({source}): {e}")
        return 0


def set_dismissed(path, source, dismissed=True):
    """Hide (or un-hide) a row without touching the file.

    Dismissing leaves ``file_mtime`` at the value recorded by the last failure,
    which is what :func:`record_problem` compares against to decide whether a
    later failure is worth re-surfacing. Returns True if a row was updated.
    """
    try:
        from core.database import get_db_connection

        conn = get_db_connection()
        if not conn:
            return False
        try:
            cur = conn.execute(
                "UPDATE problem_files SET dismissed_at = "
                + ("CURRENT_TIMESTAMP" if dismissed else "NULL")
                + " WHERE path = ? AND source = ?",
                (path, source),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception as e:
        app_logger.error(f"Could not update dismissal for {path} ({source}): {e}")
        return False


def get_problem(path, source):
    """One row as a dict, or None."""
    try:
        from core.database import get_db_connection

        conn = get_db_connection()
        if not conn:
            return None
        try:
            row = conn.execute(
                "SELECT * FROM problem_files WHERE path = ? AND source = ?",
                (path, source),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except Exception as e:
        app_logger.error(f"Could not read problem file {path} ({source}): {e}")
        return None


def has_problem(path):
    """Is this exact path recorded as a problem file (any source)?

    The destructive routes gate on this. `is_critical_path` protects WATCH,
    TARGET and the trash root, but not /config or /cache -- where the database
    lives -- so "the user sent us a path" is not a good enough reason to trash
    it. The page only ever acts on rows it is displaying, so requiring a row
    costs nothing and removes arbitrary-path deletion from this blueprint
    entirely.
    """
    try:
        from core.database import get_db_connection

        conn = get_db_connection()
        if not conn:
            return False
        try:
            row = conn.execute(
                "SELECT 1 FROM problem_files WHERE path = ? LIMIT 1", (path,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception as e:
        app_logger.error(f"Could not check problem file {path}: {e}")
        return False


def _is_definitely_gone(path):
    """True only when the file's absence is *proven*.

    A missing parent directory is not evidence of a deleted comic — it is what
    an unmounted library looks like. Pruning on it would empty this table the
    first time a NAS went to sleep, so "I cannot tell" has to mean "keep it".
    """
    parent = os.path.dirname(path)
    return bool(parent) and os.path.isdir(parent) and not os.path.exists(path)


def _prune(conn, rows):
    """Delete rows whose file is provably gone; return the keys actually removed.

    The return value matters: a row is dropped from the listing only if it was
    really deleted. Hiding a row because we *intended* to delete it makes a read
    depend on a write succeeding — and when the write failed the page showed
    "Nothing has failed" while the summary still counted the rows.
    """
    if not rows:
        return set()
    try:
        conn.executemany(
            "DELETE FROM problem_files WHERE path = ? AND source = ?",
            [(r["path"], r["source"]) for r in rows],
        )
        conn.commit()
        return {(r["path"], r["source"]) for r in rows}
    except Exception as e:
        app_logger.error(f"Could not prune resolved problem files: {e}")
        return set()


def list_problems(
    source=None, include_dismissed=False, query=None, limit=500, offset=0,
    prune_missing=True,
):
    """Return problem rows, newest failure first, each with its classification.

    Rows whose file is provably gone are deleted and omitted. Rows CLU merely
    cannot reach — an unmounted library — are returned with ``reachable: False``
    so the page can grey them out instead of offering actions that must fail.

    **Returns ``None`` if the listing could not be read**, and a list (possibly
    empty) otherwise. The caller must act on the difference: an empty list means
    "nothing is wrong", ``None`` means "we do not know", and collapsing the two
    is what let the page assert "Nothing has failed" over a database it had just
    failed to query. Same contract, for the same reason, as
    ``models.metron.list_reading_lists_modified_since``.
    """
    try:
        from core.database import get_db_connection

        conn = get_db_connection()
        if not conn:
            return None
        try:
            sql = "SELECT * FROM problem_files WHERE 1=1"
            params = []
            if source:
                sql += " AND source = ?"
                params.append(source)
            if not include_dismissed:
                sql += " AND dismissed_at IS NULL"
            if query:
                sql += " AND path LIKE ?"
                params.append(f"%{query}%")
            sql += " ORDER BY last_seen DESC, id DESC LIMIT ? OFFSET ?"
            params.extend([int(limit), int(offset)])

            rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

            live, gone = [], []
            for row in rows:
                if prune_missing and _is_definitely_gone(row["path"]):
                    gone.append(row)
                else:
                    live.append(row)

            removed = _prune(conn, gone) if gone else set()
            # Anything we could not actually delete stays in the listing. It
            # renders as unreachable with a Remove button, which is honest and
            # actionable; silently dropping it is what made the page claim
            # "Nothing has failed" while the summary counted 56.
            stuck = [r for r in gone if (r["path"], r["source"]) not in removed]
            if stuck:
                app_logger.warning(
                    f"{len(stuck)} problem entries refer to missing files but "
                    "could not be removed; still listing them"
                )
                live.extend(stuck)

            for row in live:
                row["filename"] = os.path.basename(row["path"])
                row["folder"] = os.path.dirname(row["path"])
                row["source_label"] = SOURCE_LABELS.get(row["source"], row["source"])
                row["dismissed"] = bool(row.get("dismissed_at"))
                row["reachable"] = os.path.exists(row["path"])
                row["can_retry"] = row["source"] == SOURCE_THUMBNAIL
                row["classification"] = classify(
                    row.get("error_class"), row.get("error_message")
                )
            return live
        finally:
            conn.close()
    except Exception as e:
        app_logger.error(f"Could not list problem files: {e}")
        return None


def count_problems():
    """``{'open': n, 'dismissed': n, 'by_source': {...}}`` for the page header."""
    counts = {"open": 0, "dismissed": 0, "by_source": {}}
    try:
        from core.database import get_db_connection

        conn = get_db_connection()
        if not conn:
            return counts
        try:
            for src, n in conn.execute(
                "SELECT source, COUNT(*) FROM problem_files "
                "WHERE dismissed_at IS NULL GROUP BY source"
            ).fetchall():
                counts["by_source"][src] = n
                counts["open"] += n
            row = conn.execute(
                "SELECT COUNT(*) FROM problem_files WHERE dismissed_at IS NOT NULL"
            ).fetchone()
            counts["dismissed"] = row[0] if row else 0
        finally:
            conn.close()
    except Exception as e:
        app_logger.error(f"Could not count problem files: {e}")
    return counts


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
#
# What the page actually tells the user, and the reason it is a tested pure
# function rather than markup in the template.
#
# Honesty is the whole point. Rebuild extracts every entry inside one try, so a
# bad CRC on page 12 of 40 aborts the lot; it cannot recreate bytes that are
# gone. Its one real repair is the archive that is a RAR wearing a .cbz name.
# Offering it as a cure-all would walk the user through a destructive operation
# that has no chance of helping, and `healthy_file` exists so the page never
# offers to delete a comic whose only problem is a cache-permissions error.

ACTION_REBUILD = "rebuild"
ACTION_REPLACE = "replace"
ACTION_INSPECT = "inspect"
ACTION_CONFIG = "config"

# (message substrings, error classes, cause, advice, action, repairable, healthy)
_RULES = (
    (
        ("file is not a zip file", "bad magic number for file header"),
        (),
        "This is not a ZIP archive at all — usually a RAR file renamed .cbz, "
        "or an error page that was saved instead of the comic.",
        "Rebuild: it detects a misnamed RAR and converts it to a real CBZ.",
        ACTION_REBUILD, True, False,
    ),
    (
        ("bad crc-32",),
        (),
        "A page inside the archive is physically damaged. The file list is "
        "intact; the page data no longer matches its checksum.",
        "Replace the file. Rebuilding cannot recreate the damaged bytes — it "
        "will abort on the same page. The rest of the comic is still readable.",
        ACTION_REPLACE, False, False,
    ),
    (
        ("while decompressing data", "invalid stored block lengths",
         "invalid distance code", "invalid block type", "incorrect header check"),
        ("error", "zlib.error"),
        "The compressed data stream is damaged — typically an interrupted "
        "download, or bit rot on disk.",
        "Replace the file. The missing data cannot be reconstructed.",
        ACTION_REPLACE, False, False,
    ),
    (
        ("and header",),
        (),
        "The archive's index and its contents disagree, so entry names no "
        "longer line up. Usually a truncated or partially overwritten file.",
        "Replace the file — it is often noticeably smaller than its siblings.",
        ACTION_REPLACE, False, False,
    ),
    (
        ("broken data stream when reading image file", "cannot identify image file",
         "image file is truncated"),
        (),
        "The page image itself is truncated or unreadable.",
        "Replace the file. Rebuilding may salvage the other pages, but this one "
        "will stay broken.",
        ACTION_REPLACE, False, False,
    ),
    (
        ("no images found", "contains no page images"),
        (CLASS_NO_PAGES.lower(), "nopages"),
        "The archive opens correctly and contains no images. This is not "
        "corruption — it may be a PDF or a text file wearing a .cbz name.",
        "Open it to see what is inside, then replace or delete it.",
        ACTION_INSPECT, False, False,
    ),
    (
        (),
        (CLASS_CACHE_WRITE.lower(), "cachewritefailed"),
        "The comic is fine. CLU could not write its thumbnail to the cache "
        "directory.",
        "Check the /cache volume and your PUID/PGID settings, then retry. Do "
        "not delete the comic — there is nothing wrong with it.",
        ACTION_CONFIG, False, True,
    ),
    (
        (),
        (CLASS_CORRUPT_ENTRIES.lower(), "corruptentries"),
        "The file was re-tagged, but some entries failed their checksum and "
        "were copied without verification. Those pages may be blank.",
        "Check the affected pages and replace the file if they are damaged.",
        ACTION_REPLACE, False, False,
    ),
    (
        (),
        (CLASS_RAR_FAILED.lower(),),
        "This is a RAR archive and CLU could not convert it — the archive is "
        "damaged, or it needs a password.",
        "Replace the file.",
        ACTION_REPLACE, False, False,
    ),
    (
        ("permission denied",),
        ("permissionerror",),
        "CLU is not allowed to read this file.",
        "Check the file's ownership and your PUID/PGID settings, then retry.",
        ACTION_CONFIG, False, True,
    ),
    (
        ("no such file or directory",),
        ("filenotfounderror",),
        "The file could not be opened — it is gone, or its library is not "
        "mounted.",
        "Check that the library mount is online. The entry removes itself once "
        "CLU can see the folder again.",
        ACTION_CONFIG, False, True,
    ),
)


def classify(error_class, error_message=""):
    """Map a stored failure onto a plain-English cause and a recommended action.

    Returns ``{cause, advice, action, repairable, healthy_file}``. Always
    returns something: an unrecognised error falls back to neutral wording
    rather than inventing a diagnosis, and the fallback claims no
    repairability it cannot justify.
    """
    message = (error_message or "").lower()
    klass = (error_class or "").lower()

    for substrings, classes, cause, advice, action, repairable, healthy in _RULES:
        if any(s in message for s in substrings) or (klass and klass in classes):
            return {
                "cause": cause,
                "advice": advice,
                "action": action,
                "repairable": repairable,
                "healthy_file": healthy,
            }

    return {
        "cause": "CLU could not read this file. The exact error is in the details.",
        "advice": "Try rebuilding it; if that fails, replace the file.",
        "action": ACTION_INSPECT,
        "repairable": False,
        "healthy_file": False,
    }


def retry_problem(path, source):
    """Re-run the operation that recorded this problem.

    Returns ``(ok, message)``. Synchronous by design: this is one file and one
    fast operation — the thumbnail reads a single archive entry — so the
    operations registry would add a job row, a poller and a race for no benefit.
    A "retry everything" action would need the registry; this does not.

    The retried operation records or clears its own row, so there is no
    bookkeeping here.
    """
    if not os.path.exists(path):
        return False, "File not found"

    if source == SOURCE_THUMBNAIL:
        from core.thumbnail_cache import invalidate_thumbnail, regenerate_thumbnail

        # Clearing first is required, not belt-and-braces: /api/thumbnail
        # refuses to re-attempt an errored row whose mtime has not changed, and
        # a stale or truncated JPEG left on disk would go on being served.
        invalidate_thumbnail(path)
        ok = regenerate_thumbnail(path)
        return ok, "Thumbnail rebuilt" if ok else "Still failing"

    if source == SOURCE_REBUILD:
        # Retrying a rebuild *is* a rebuild. The page offers that action
        # instead; two ways to do one thing is how they drift apart.
        return False, "Use Rebuild for this entry"

    if source == SOURCE_METADATA_WRITE:
        # Re-tagging needs provider inputs this endpoint does not have, and
        # re-running it blindly would rewrite the archive again for no reason.
        return False, "Re-tag this file from the metadata page"

    return False, f"Unknown source: {source}"
