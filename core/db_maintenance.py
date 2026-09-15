"""Database maintenance: checkpoint, compact, optimize, and page accounting.

None of this existed. There is no ``VACUUM``, no ``PRAGMA optimize`` and --
most consequentially -- **no ``wal_checkpoint`` anywhere in the repo**. The WAL
is left entirely to SQLite's auto-checkpoint, which cannot run while any
connection holds a read mark. This app leaks connections on ~290 error paths
(``conn.close()`` sits inside the ``try``), so during a fault every call leaks
one, auto-checkpoint stops, and the ``-wal`` grows without bound. Combined with
an unclean process exit that is a corruption engine.

Everything here runs on its own short-``busy_timeout`` connection and reports
what actually happened rather than hanging: with 8 gunicorn threads, ~10
APScheduler threads, the metadata scanner and a separate ``monitor.py``
process all writing, "could not get the lock" is a normal outcome and the UI
has to be able to say so.
"""
import os
import shutil
import sqlite3
import time

from core.app_logging import app_logger

# Long enough to win an ordinary race, short enough that a request thread is
# never parked for the 30s that get_db_connection() would allow.
MAINTENANCE_BUSY_TIMEOUT_MS = 5000

# VACUUM INTO writes a full second copy before anything is swapped. Refuse to
# start without headroom: /config on a named volume is often small, and filling
# it mid-vacuum is a second failure on top of the one being repaired.
COMPACT_FREE_SPACE_FACTOR = 1.3


def _connect(db_path=None, busy_timeout_ms=MAINTENANCE_BUSY_TIMEOUT_MS):
    """A dedicated autocommit connection for maintenance statements.

    ``isolation_level=None`` is required, not stylistic: Python's sqlite3 opens
    an implicit transaction around DML, and VACUUM cannot run inside one.
    Deliberately a raw connect rather than ``get_db_connection()`` for the same
    reason -- and because these statements are the ones that must not inherit
    the 30s busy timeout.
    """
    from core.database import get_db_path

    conn = sqlite3.connect(db_path or get_db_path(), timeout=busy_timeout_ms / 1000.0,
                           isolation_level=None)
    conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    return conn


def _sidecar_size(db_path, suffix):
    try:
        return os.path.getsize(db_path + suffix)
    except OSError:
        return 0


def get_page_stats(db_path=None):
    """Page accounting and the pragmas actually in force.

    ``reclaimable_bytes`` is what makes the Compact button honest -- without it
    the user is guessing whether compacting is worth doing at all.

    Never raises; returns ``{"error": ...}`` on failure.
    """
    from core.database import get_db_path

    db_path = db_path or get_db_path()
    stats = {
        "page_count": None,
        "page_size": None,
        "freelist_count": None,
        "reclaimable_bytes": None,
        "journal_mode": None,
        "synchronous": None,
        "auto_vacuum": None,
        "error": None,
    }
    if not os.path.exists(db_path):
        return stats
    conn = None
    try:
        conn = _connect(db_path)
        for pragma in ("page_count", "page_size", "freelist_count",
                       "journal_mode", "synchronous", "auto_vacuum"):
            row = conn.execute(f"PRAGMA {pragma}").fetchone()
            stats[pragma] = row[0] if row else None
        if isinstance(stats["freelist_count"], int) and isinstance(
            stats["page_size"], int
        ):
            stats["reclaimable_bytes"] = (
                stats["freelist_count"] * stats["page_size"]
            )
        return stats
    except sqlite3.Error as e:
        from core.db_health import note_db_error

        note_db_error(e, "get_page_stats")
        stats["error"] = str(e)
        return stats
    finally:
        if conn is not None:
            conn.close()


def checkpoint_wal(truncate=True, db_path=None, busy_timeout_ms=MAINTENANCE_BUSY_TIMEOUT_MS):
    """Checkpoint the write-ahead log, and report honestly if it could not.

    Tries PASSIVE first and only then the requested mode. PASSIVE always makes
    whatever progress it can without waiting; TRUNCATE needs *no other readers*
    and will simply return busy when one exists -- which, with this many
    threads plus the monitor process, is common and is not an error.

    ``PRAGMA wal_checkpoint`` returns ``(busy, log, checkpointed)``: ``busy`` is
    1 when it could not finish, ``log`` is the WAL size in pages and
    ``checkpointed`` how many were moved into the database. A WAL that will not
    truncate is itself the diagnostic -- it means something is holding a read
    open, which is the leak described in the module docstring.
    """
    from core.database import get_db_path

    db_path = db_path or get_db_path()
    result = {
        "busy": None, "log": None, "checkpointed": None,
        "wal_size_before": _sidecar_size(db_path, "-wal"),
        "wal_size_after": None,
        "mode": "TRUNCATE" if truncate else "PASSIVE",
        "error": None,
    }
    if not os.path.exists(db_path):
        result["error"] = "database does not exist"
        return result

    conn = None
    try:
        conn = _connect(db_path, busy_timeout_ms)
        conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        mode = "TRUNCATE" if truncate else "PASSIVE"
        row = conn.execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        if row:
            result["busy"], result["log"], result["checkpointed"] = (
                row[0], row[1], row[2]
            )
        result["wal_size_after"] = _sidecar_size(db_path, "-wal")
        if result["busy"]:
            app_logger.info(
                f"WAL checkpoint ({mode}) could not complete - a reader is "
                f"holding the log open ({result['log']} pages)."
            )
        return result
    except sqlite3.Error as e:
        from core.db_health import note_db_error

        note_db_error(e, "checkpoint_wal")
        result["error"] = str(e)
        result["wal_size_after"] = _sidecar_size(db_path, "-wal")
        return result
    finally:
        if conn is not None:
            conn.close()


def optimize_database(db_path=None):
    """Refresh query-planner statistics. Cheap and always safe."""
    result = {"analyzed": False, "optimized": False, "error": None}
    conn = None
    try:
        conn = _connect(db_path)
        conn.execute("ANALYZE")
        result["analyzed"] = True
        conn.execute("PRAGMA optimize")
        result["optimized"] = True
        return result
    except sqlite3.Error as e:
        from core.db_health import note_db_error

        note_db_error(e, "optimize_database")
        result["error"] = str(e)
        return result
    finally:
        if conn is not None:
            conn.close()


def compact_database(progress=None):
    """Rebuild the database compactly, reclaiming the freelist.

    Uses ``VACUUM INTO`` and a swap, **not** an in-place ``VACUUM``.

    An in-place VACUUM takes an exclusive lock on the whole database. With 8
    gunicorn request threads, ~10 APScheduler threads, 1-4 metadata scanner
    workers, a 2-worker thumbnail executor and a separate ``monitor.py``
    process, that lock is realistically never granted -- and while waiting for
    it, this connection makes every *other* connection burn its own 30s
    busy timeout. The button would stall the app and then fail.

    ``VACUUM INTO`` runs inside an ordinary read transaction, writes a
    compacted copy and never touches the original, so it is safe to run on a
    live database. The copy is then installed by ``swap_in_database``, the same
    quiesce -> verify -> snapshot -> replace path used by restore and by the
    salvage swap.

    Returns a dict; raises nothing. ``progress`` is an optional callable taking
    a status string, used to drive the operations registry.
    """
    from core.database import (
        check_integrity, get_db_path, swap_in_database,
    )

    def say(msg):
        app_logger.info(f"Compact: {msg}")
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    db_path = get_db_path()
    result = {"success": False, "error": None, "size_before": None,
              "size_after": None, "reclaimed": 0, "pre_swap_backup": None}

    if not os.path.exists(db_path):
        result["error"] = "database does not exist"
        return result

    size_before = os.path.getsize(db_path)
    result["size_before"] = size_before

    # Never compact a damaged file. VACUUM rewrites every page; doing that to a
    # database that is already showing corruption is the most destructive thing
    # on this page.
    say("checking integrity")
    ok, message = check_integrity(db_path)
    if not ok:
        result["error"] = (
            f"Database integrity check failed ({message}). Compacting a "
            "damaged database can spread the damage — salvage or restore "
            "it first."
        )
        return result

    try:
        free = shutil.disk_usage(os.path.dirname(db_path)).free
    except OSError:
        free = None
    if free is not None and free < size_before * COMPACT_FREE_SPACE_FACTOR:
        result["error"] = (
            f"Not enough free space: compacting needs about "
            f"{_human(size_before * COMPACT_FREE_SPACE_FACTOR)} and only "
            f"{_human(free)} is available."
        )
        return result

    target = db_path + ".compacting"
    if os.path.exists(target):
        try:
            os.remove(target)
        except OSError as e:
            result["error"] = f"Could not clear the previous compact file: {e}"
            return result

    conn = None
    try:
        say("building a compacted copy")
        conn = _connect(db_path)
        conn.execute("VACUUM INTO ?", (target,))
        conn.close()
        conn = None

        result["size_after"] = os.path.getsize(target)
        say("installing the compacted database")
        swap = swap_in_database(target, reason="compact")
        result["pre_swap_backup"] = swap.get("pre_swap_backup")
        result["success"] = True
        result["reclaimed"] = max(0, size_before - result["size_after"])
        say("done")
        return result
    except (sqlite3.Error, OSError, RuntimeError) as e:
        from core.db_health import note_db_error

        note_db_error(e, "compact_database")
        result["error"] = str(e)
        return result
    finally:
        if conn is not None:
            conn.close()
        if os.path.exists(target):
            try:
                os.remove(target)
            except OSError:
                pass


def _human(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def shutdown_checkpoint(timeout=5.0):
    """Best-effort WAL checkpoint on the way out. Never raises, always returns.

    The process is about to call ``os._exit(0)``, which does not close a single
    SQLite connection. Moving what we can out of the WAL first means the next
    start has less to recover. TRUNCATE will usually report busy here -- the
    monitor process and the scanner threads still hold connections -- and that
    is fine; PASSIVE still makes progress.

    Bounded so a stuck checkpoint can never hold the container past Docker's
    SIGTERM grace period, which would turn a clean stop into a SIGKILL: the
    exact unclean shutdown this is meant to avoid.
    """
    started = time.monotonic()
    try:
        result = checkpoint_wal(
            truncate=True, busy_timeout_ms=int(timeout * 1000)
        )
        elapsed = time.monotonic() - started
        if result.get("error"):
            app_logger.warning(
                f"Shutdown WAL checkpoint failed after {elapsed:.1f}s: "
                f"{result['error']}"
            )
        else:
            app_logger.info(
                f"Shutdown WAL checkpoint: busy={result.get('busy')} "
                f"log={result.get('log')} checkpointed={result.get('checkpointed')} "
                f"({elapsed:.1f}s)"
            )
        return result
    except Exception as e:  # pragma: no cover - shutdown must not raise
        app_logger.warning(f"Shutdown WAL checkpoint raised: {e}")
        return {"error": str(e)}
