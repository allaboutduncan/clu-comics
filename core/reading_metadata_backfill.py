"""Re-read ComicInfo.xml for every comic in the reading history.

The Insights page's *Backfill metadata* button fills ``issues_read.writer``,
``penciller``, ``characters`` and ``publisher`` from each file's ComicInfo.

Things that look arbitrary and are not:

- **No transaction is ever open across file I/O.** This used to run inside the
  request on one connection: the first ``UPDATE`` opened the implicit write
  transaction and every later archive read happened while holding the WAL
  write lock, until one ``commit`` at the very end. On a large history or a
  slow NAS that outlasted gunicorn's 120s timeout, and meanwhile every other
  writer burned its 30s ``busy_timeout`` and failed with "database is locked".
  Now: one short read of the paths, the archive reads with no connection open,
  and the results written in short batches of ``BATCH_SIZE``.
- **Off the request thread, through the operations registry.** The page
  follows ``/api/operation/<op_id>`` and then reads the report from
  ``/api/backfill-reading-metadata/result``.
- **One run at a time** (``_run_lock``, non-blocking). The route answers 409
  rather than starting a second walk over the same rows.
- **One archive read per path, not per row.** Several users can have read the
  same comic; the metadata is a fact about the file, so every row for the path
  gets the same values.
- **Per-item lines are DEBUG; the run logs one INFO summary.**
"""

import os
import threading
import time

from core.app_logging import app_logger

BATCH_SIZE = 50
MAX_REPORTED_SKIPS = 500

_run_lock = threading.Lock()
_last_result = None
_last_result_lock = threading.Lock()


def is_running():
    return _run_lock.locked()


def last_result(user_id=None):
    """The last run's report (a copy) if ``user_id`` started it, else None."""
    with _last_result_lock:
        if not _last_result:
            return None
        if user_id is not None and _last_result.get("user_id") != user_id:
            return None
        return dict(_last_result)


def _read_history_paths():
    """Distinct ``issues_read`` paths and user ids; ``(None, None)`` on failure."""
    from core.database import db_conn

    try:
        with db_conn() as conn:
            if not conn:
                return None, None
            paths = [r[0] for r in conn.execute(
                "SELECT DISTINCT issue_path FROM issues_read ORDER BY issue_path"
            ).fetchall()]
            user_ids = [r[0] for r in conn.execute(
                "SELECT DISTINCT user_id FROM issues_read WHERE user_id IS NOT NULL"
            ).fetchall()]
            return paths, user_ids
    except Exception as e:
        app_logger.error(f"Reading metadata backfill: could not read history: {e}")
        return None, None


def _write_batch(batch):
    """Write one batch in its own short transaction. Returns True on success."""
    from core.database import db_conn

    if not batch:
        return True
    try:
        with db_conn() as conn:
            if not conn:
                return False
            try:
                conn.executemany(
                    "UPDATE issues_read "
                    "SET writer = ?, penciller = ?, characters = ?, publisher = ? "
                    "WHERE issue_path = ?",
                    batch,
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return True
    except Exception as e:
        app_logger.error(
            f"Reading metadata backfill: could not write {len(batch)} rows: {e}"
        )
        return False


def _metadata_for(path):
    """``(values, skip_reason)`` for one file. Opens no database connection."""
    from core.comicinfo import read_comicinfo_from_zip
    from core.metadata_normalize import normalize_credit_list, strip_provider_ids

    if not os.path.exists(path):
        return None, "File not found"
    if not path.lower().endswith((".cbz", ".zip")):
        return None, "Not a CBZ/ZIP file"
    try:
        info = read_comicinfo_from_zip(path)
    except Exception as e:
        app_logger.debug(f"Reading metadata backfill: cannot read {path}: {e}")
        return None, f"Error: {e}"
    if not info:
        return None, "No ComicInfo.xml"
    # Strip "[3258]"-style provider IDs, or this would re-dirty issues_read
    # after the migration cleaned it.
    return (
        normalize_credit_list(info.get("Writer", "")),
        normalize_credit_list(info.get("Penciller", "")),
        normalize_credit_list(info.get("Characters", "")),
        strip_provider_ids(info.get("Publisher", "")),
    ), None


def run_backfill(op_id=None, user_id=None):
    """Backfill every path in the reading history. Never raises.

    The caller must hold ``_run_lock`` (see :func:`start_backfill`). Returns
    ``{user_id, started_at, finished_at, total, updated, skipped,
    skipped_issues, skipped_issues_truncated, error}`` and keeps it for
    :func:`last_result`.
    """
    global _last_result
    from core.app_state import update_operation
    from core.database import clear_stats_cache_prefix

    result = {
        "user_id": user_id, "started_at": time.time(), "finished_at": None,
        "total": 0, "updated": 0, "skipped": 0, "skipped_issues": [],
        "skipped_issues_truncated": False, "error": None,
    }

    def _skip(path, reason):
        result["skipped"] += 1
        if len(result["skipped_issues"]) < MAX_REPORTED_SKIPS:
            result["skipped_issues"].append(
                {"file": os.path.basename(path), "reason": reason})
        else:
            result["skipped_issues_truncated"] = True

    paths, user_ids = _read_history_paths()
    if paths is None:
        result["error"] = "Could not read the reading history"
        paths, user_ids = [], []
    result["total"] = len(paths)
    if op_id:
        update_operation(op_id, current=0, total=len(paths), detail="Starting")

    batch = []

    def _flush():
        if _write_batch(batch):
            result["updated"] += len(batch)
        else:
            result["error"] = "Some results could not be saved to the database"
            for row in batch:
                _skip(row[-1], "Database write failed")
        batch.clear()

    for i, path in enumerate(paths, start=1):
        try:
            values, reason = _metadata_for(path)
        except Exception as e:  # belt and braces: one file never stops the run
            values, reason = None, f"Error: {e}"
        if values is not None:
            batch.append(values + (path,))
            app_logger.debug(f"Reading metadata backfill: read {path}")
        else:
            _skip(path, reason)
            app_logger.debug(f"Reading metadata backfill: skipped {path}: {reason}")
        if len(batch) >= BATCH_SIZE:
            _flush()
        if op_id:
            # The per-item update is the heartbeat that keeps the op clear of
            # the registry's STALE_TIMEOUT.
            update_operation(op_id, current=i, detail=os.path.basename(path))
    _flush()

    # Rows of every user who read these files changed, so every one of their
    # namespaced reading caches is stale -- not just the clicker's.
    for uid in set(user_ids or []) | ({user_id} if user_id is not None else set()):
        clear_stats_cache_prefix(f"u{uid}:")

    result["finished_at"] = time.time()
    app_logger.info(
        f"Reading metadata backfill complete: {result['updated']} updated, "
        f"{result['skipped']} skipped of {result['total']} files"
        + (f" ({result['error']})" if result["error"] else "")
    )
    with _last_result_lock:
        _last_result = result
    return dict(result)


def start_backfill(user_id=None):
    """Start a run on a daemon thread. Returns its op id, or None if one is running."""
    from core.app_state import complete_operation, register_operation

    if not _run_lock.acquire(blocking=False):
        return None
    try:
        op_id = register_operation(
            "reading_metadata_backfill", "Backfilling reading metadata",
            user_id=user_id,
        )
    except Exception:
        _run_lock.release()
        raise

    def _run():
        failed = False
        try:
            failed = run_backfill(op_id, user_id=user_id).get("error") is not None
        except Exception as e:  # run_backfill swallows; belt and braces
            failed = True
            app_logger.error(f"Reading metadata backfill crashed: {e}")
        finally:
            complete_operation(op_id, error=failed)
            _run_lock.release()

    try:
        threading.Thread(target=_run, daemon=True,
                         name="reading-metadata-backfill").start()
    except Exception:
        complete_operation(op_id, error=True)
        _run_lock.release()
        raise
    return op_id
