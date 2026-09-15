"""Salvage a malformed database from the Database tab, with a visible diff.

``tools/repair_db.py`` has done the hard part for a while -- it tries the
sqlite3 CLI's ``.recover`` and falls back to a pure-Python per-table salvage
that cursors past corrupt pages by rowid -- but it was reachable only by
``docker cp`` + ``docker exec``. Its own docstring says it is for "when the app
logs *database disk image is malformed* and the Database tab shows a red
*Corrupted* badge": it was written for this UI and never connected to it.

This module is the orchestration around it, and nothing more. **The dependency
points this way on purpose**: ``tools/repair_db.py`` stays standard-library
only so it still runs when the app cannot start, which is the situation it is
for. Importing it here costs nothing; importing ``core`` from there would cost
everything.

The flow is deliberately two-phase -- produce a candidate, then apply it:

    start_salvage(op_id)  ->  get_candidate()  ->  apply_candidate(token)

Salvage always loses rows. The user is entitled to see how many, per table,
*before* their library database is replaced. A one-click "repair" that silently
discards 23 rows of ``file_index`` and an entire ``reading_positions`` table is
not a feature.
"""
import hashlib
import os
import shutil
import threading
import time

from core.app_logging import app_logger
from tools import repair_db

# Salvage needs room for a full second copy.
SALVAGE_FREE_SPACE_FACTOR = 1.3

# One candidate at a time, in memory. Deliberately NOT a database table: the
# database is the thing that is broken, and a ledger written into it is a
# ledger that disappears exactly when it is needed. gunicorn runs -w 1, so a
# module global is genuinely process-wide for every HTTP caller.
_candidate = None
_candidate_lock = threading.Lock()
_running = False


def candidate_path():
    from core.database import get_db_path

    return get_db_path() + ".salvaged"


def _file_md5(path):
    h = hashlib.md5(usedforsecurity=False)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def is_running():
    with _candidate_lock:
        return _running


def get_candidate():
    """The current salvage candidate, or None.

    Kept out of the operations registry on purpose: ``get_active_operations``
    prunes completed operations after a TTL, and the user would lose the
    row-count diff while they were still reading it.
    """
    with _candidate_lock:
        if _candidate is None:
            return None
        if not os.path.exists(_candidate.get("path", "")):
            return None
        return dict(_candidate)


def discard_candidate():
    """Delete the candidate file and forget it."""
    global _candidate
    with _candidate_lock:
        current = _candidate
        _candidate = None
    if current and os.path.exists(current.get("path", "")):
        try:
            os.remove(current["path"])
        except OSError as e:
            app_logger.warning(f"Could not remove salvage candidate: {e}")
    return True


def _diff_counts(before, after):
    """Per-table row counts before and after, and an honest loss figure.

    A count of ``None`` means the table could not be counted. On the *after*
    side that means the table was lost entirely. On the *before* side it is the
    normal case for the damaged table -- and it is the subtlety that makes a
    naive total misleading.

    Comparing raw totals is wrong precisely where it matters most. In a real
    salvage the corrupt table is exactly the one whose ``SELECT COUNT(*)``
    fails, so it contributes nothing to ``total_before`` while contributing
    thousands to ``total_after``. That makes ``total_before - total_after``
    negative and the loss read as **zero** on the one table that actually lost
    rows.

    So ``rows_lost`` is summed only over tables where *both* counts are known,
    and any table we could not count beforehand is named in ``unknown_before``
    for the UI to report as unknown rather than as no loss. Overstating
    confidence here would defeat the entire purpose of showing a diff.
    """
    rows = []
    total_before = 0
    total_after = 0
    tables_lost = []
    unknown_before = []
    rows_lost = 0
    for name in sorted(set(before) | set(after)):
        b = before.get(name)
        a = after.get(name)
        b_int = b if isinstance(b, int) else None
        a_int = a if isinstance(a, int) else None
        if b_int is not None:
            total_before += b_int
        if a_int is not None:
            total_after += a_int

        missing = name not in after or a_int is None
        if missing:
            tables_lost.append(name)
        if b_int is None and name in before:
            unknown_before.append(name)

        delta = None
        if b_int is not None and a_int is not None:
            delta = a_int - b_int
            if delta < 0:
                rows_lost += -delta
        rows.append({
            "name": name,
            "before": b_int,
            "before_error": b_int is None and b is not None,
            "after": a_int,
            "missing": missing,
            "delta": delta,
        })
    return {
        "tables": rows,
        "total_before": total_before,
        "total_after": total_after,
        "rows_lost": rows_lost,
        "tables_lost": tables_lost,
        "unknown_before": unknown_before,
    }


def start_salvage(op_id=None, progress=None):
    """Build a salvaged copy beside the live database. Never swaps.

    Runs to completion on the calling thread; the route runs it in the
    background through the operations registry.
    """
    global _candidate, _running

    from core.database import check_integrity, get_db_path

    def say(msg):
        app_logger.info(f"Salvage: {msg}")
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    with _candidate_lock:
        if _running:
            return {"success": False, "error": "A salvage is already running."}
        _running = True

    db_path = get_db_path()
    target = candidate_path()
    try:
        if not os.path.exists(db_path):
            return {"success": False, "error": "Database does not exist."}

        size_before = os.path.getsize(db_path)
        try:
            free = shutil.disk_usage(os.path.dirname(db_path)).free
        except OSError:
            free = None
        if free is not None and free < size_before * SALVAGE_FREE_SPACE_FACTOR:
            return {
                "success": False,
                "error": (
                    "Not enough free space to salvage: this needs roughly "
                    f"{size_before * SALVAGE_FREE_SPACE_FACTOR / (1024 ** 2):.0f} MB "
                    f"and only {free / (1024 ** 2):.0f} MB is free."
                ),
            }

        say("reading what survives in the damaged database")
        counts_before = repair_db.table_counts(db_path)

        if os.path.exists(target):
            os.remove(target)

        use_cli = repair_db.cli_has_recover()
        method = "sqlite3 .recover" if use_cli else "python salvage"
        say(f"recovering using {method}")

        ok = False
        if use_cli:
            ok = repair_db.recover_cli(db_path, target, log=say)
            if not ok and os.path.exists(target):
                os.remove(target)
        if not ok:
            if os.path.exists(target):
                os.remove(target)
            if use_cli:
                say("falling back to the python salvage")
                method = "python salvage (fallback)"
            ok = repair_db.recover_python(db_path, target, log=say)

        if not ok or not os.path.exists(target):
            return {"success": False, "error": "Recovery produced no output."}

        # Full integrity_check, not quick_check. This file is being offered as
        # a replacement for the user's entire library database; it is a
        # brand-new one-shot artefact and the check runs exactly once. A
        # quick_check does not read every page, and this is the one result we
        # cannot afford to get wrong.
        say("verifying the recovered database")
        integrity_ok, integrity_message = check_integrity(target, quick=False)

        say("counting recovered rows")
        counts_after = repair_db.table_counts(target)
        diff = _diff_counts(counts_before, counts_after)

        candidate = {
            "path": target,
            "token": _file_md5(target),
            "created_at": time.time(),
            "method": method,
            "integrity_ok": integrity_ok,
            "integrity_message": integrity_message,
            "size_before": size_before,
            "size_after": os.path.getsize(target),
            "diff": diff,
        }
        with _candidate_lock:
            _candidate = candidate

        say("done")
        return {"success": True, "candidate": dict(candidate)}
    except Exception as e:
        app_logger.error(f"Salvage failed: {e}", exc_info=True)
        if os.path.exists(target):
            try:
                os.remove(target)
            except OSError:
                pass
        return {"success": False, "error": str(e)}
    finally:
        with _candidate_lock:
            _running = False


def apply_candidate(token):
    """Install the salvaged database. Returns the swap result.

    The token is the candidate's md5 *as read now*, not as recorded. It guards
    the window between the user reading the diff and pressing the button: if
    the candidate has been regenerated in between, the diff on their screen
    describes a different file and the apply is refused.
    """
    global _candidate

    from core.database import swap_in_database

    candidate = get_candidate()
    if candidate is None:
        raise FileNotFoundError("No salvage candidate is available.")
    if not candidate.get("integrity_ok"):
        raise RuntimeError(
            "The salvaged database did not pass its integrity check "
            f"({candidate.get('integrity_message')}); it will not be installed."
        )

    current_token = _file_md5(candidate["path"])
    if token != current_token:
        raise RuntimeError(
            "The salvaged database changed since it was inspected. Review the "
            "row counts again before applying."
        )

    result = swap_in_database(candidate["path"], reason="salvage")

    with _candidate_lock:
        _candidate = None

    result["diff"] = candidate.get("diff")
    result["requires_restart"] = True
    return result
