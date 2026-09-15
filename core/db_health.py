"""Database health: corruption detection, an error ledger, and storage facts.

Three corruptions in 24 hours produced one ERROR line per failing query and
nothing else. The app kept serving for 78 minutes with a malformed database,
reporting success over writes that never landed, because every helper in
``core/database.py`` swallows to ``[]``/``0``/``False``.

This module is the other half of that: a place for a swallowed
``sqlite3.DatabaseError`` to be *recorded* even when the caller goes on to
return an empty list.

Three responsibilities:

1. ``is_corruption_error`` / ``note_db_error`` -- classify and latch. Called
   from the guarded connection factory in ``core.database``, so every one of
   the ~350 ``get_db_connection()`` call sites is covered without being edited.
2. ``recent_db_errors`` -- a ring buffer the Database tab renders, so "07:35,
   malformed, get_files_needing_metadata_scan" is visible rather than buried in
   a log file.
3. ``describe_storage`` -- which filesystem the database actually sits on.
   SQLite's own documentation says POSIX advisory locking is unreliable over
   network filesystems, and a database on one is the single most common cause
   of "database disk image is malformed". No amount of tooling fixes that, so
   the page has to be able to say it.

Deliberately in memory only. The corruption ledger must not be written to the
database, because the database is the thing that is broken -- that is how the
alarm gets lost exactly when it matters. It does NOT follow
``metron_auth_blocked``'s persist-to-``user_preferences`` pattern for that
reason.

This module must not import ``core.database`` at module level: that module
imports this one. ``core.app_state`` is safe (its own ``core.database`` import
is lazy, inside a function).
"""
import os
import re
import sqlite3
import threading
import time

from core.app_logging import app_logger
import core.app_state as app_state

# ---------------------------------------------------------------------------
# Corruption classification
# ---------------------------------------------------------------------------

# Substrings that mean the file is damaged, not merely busy.
#
# The filter matters more than it looks. sqlite3.OperationalError -- which is
# what "database is locked" and "no such table" raise -- IS a subclass of
# sqlite3.DatabaseError. Latching on the base class alone would fire the
# corruption notification on every busy timeout, and this app has 8 gunicorn
# threads, ~10 APScheduler threads and a separate monitor.py process all
# contending for one file. An alarm that cries wolf gets muted, and then the
# real corruption is invisible again.
CORRUPTION_MARKERS = (
    "database disk image is malformed",
    "file is not a database",
    "malformed database schema",
    "database corruption",
    "corrupt",
)

# Not corruption, but the same "act now" urgency and the same silent-failure
# shape, so the page reports them in the same ledger under their own kind.
DISK_MARKERS = (
    "database or disk is full",
    "disk i/o error",
    "unable to open database file",
    "attempt to write a readonly database",
)

MAX_RECENT_ERRORS = 50


def classify_db_error(exc):
    """Return ``"corruption"``, ``"disk"`` or ``None`` for a SQLite exception.

    ``None`` means "ordinary failure" -- a lock, a programming error, a missing
    table on a fresh database. Those must never latch.
    """
    if not isinstance(exc, sqlite3.Error):
        # helpers/trash.py-style wrappers occasionally re-raise as RuntimeError
        # carrying the original text, so fall through to a text match rather
        # than insisting on the type.
        text = str(exc).lower()
        if any(m in text for m in CORRUPTION_MARKERS):
            return "corruption"
        return None

    text = str(exc).lower()
    if any(m in text for m in CORRUPTION_MARKERS):
        return "corruption"
    if any(m in text for m in DISK_MARKERS):
        return "disk"
    return None


def is_corruption_error(exc):
    """True only for genuine file damage. See ``classify_db_error``."""
    return classify_db_error(exc) == "corruption"


# ---------------------------------------------------------------------------
# The error ledger
# ---------------------------------------------------------------------------

_errors = []
_errors_lock = threading.Lock()
# True once a corruption error has been reported and not yet cleared by a
# passing integrity check. Used so the notification fires once per episode
# rather than once per failing query -- a corrupt file fails every 30s poll.
_latched = False


def note_db_error(exc, context=""):
    """Record a SQLite failure; latch and notify on the first corruption.

    Safe to call from anywhere, including an ``except`` block that is about to
    re-raise. Never raises: a fault in the reporting path must not replace the
    fault being reported.

    ``context`` is the calling function's name, used only for display.
    """
    global _latched
    try:
        kind = classify_db_error(exc)
        if kind is None:
            return

        entry = {
            "ts": time.time(),
            "kind": kind,
            "type": type(exc).__name__,
            "message": str(exc)[:500],
            "context": context or "",
        }
        with _errors_lock:
            _errors.append(entry)
            if len(_errors) > MAX_RECENT_ERRORS:
                del _errors[:-MAX_RECENT_ERRORS]
            first = not _latched
            if kind == "corruption":
                _latched = True

        if kind != "corruption":
            app_logger.error(
                f"Database {kind} error in {context or 'unknown'}: {exc}"
            )
            return

        app_state.set_db_integrity(False, str(exc))

        if not first:
            return

        # First transition to corrupt in this process: say so once, loudly.
        app_logger.error(
            f"DATABASE CORRUPTION detected in {context or 'unknown'}: {exc}. "
            "Open Config -> Database to check integrity, salvage the file or "
            "restore a backup."
        )
        app_state.add_notification(
            "Database corruption detected. Open Config → Database to "
            "salvage the file or restore a backup.",
            level="error",
        )
        _notify_corruption(str(exc), context)
    except Exception:  # pragma: no cover - reporting must never break a caller
        pass


def _notify_corruption(message, context):
    """Fire the push notification. Imported lazily and fully swallowed."""
    try:
        from core.notifications import notify_async, EVENT_DB_CORRUPT

        notify_async(
            EVENT_DB_CORRUPT,
            "Database corruption detected",
            f"{message}\n\nDetected in: {context or 'unknown'}\n\n"
            "Open Config → Database to salvage the file or restore a backup.",
        )
    except Exception:
        pass


def recent_db_errors(limit=20):
    """Newest-first list of recorded failures, for the Database tab."""
    with _errors_lock:
        return list(reversed(_errors[-limit:]))


def error_summary():
    """``{total, corruption, disk, latched, last_ts}`` for the health endpoint."""
    with _errors_lock:
        corruption = sum(1 for e in _errors if e["kind"] == "corruption")
        return {
            "total": len(_errors),
            "corruption": corruption,
            "disk": len(_errors) - corruption,
            "latched": _latched,
            "last_ts": _errors[-1]["ts"] if _errors else None,
        }


def clear_db_errors():
    """Drop the ledger and unlatch. Called after a successful swap or a
    passing integrity check -- the file that was broken is no longer in use."""
    global _latched
    with _errors_lock:
        _errors.clear()
        _latched = False


def is_latched():
    with _errors_lock:
        return _latched


# ---------------------------------------------------------------------------
# Storage diagnostics
# ---------------------------------------------------------------------------

# SQLite on a network filesystem is the most common cause of the corruption
# this module exists to report. The docs are explicit that POSIX advisory locks
# are unreliable over NFS and that SMB/CIFS locking is worse. A user whose
# /config is a CIFS share does not have a bug we can fix in code, and telling
# them so is worth more than every other button on the page.
RISKY_FSTYPES = {
    "cifs": ("danger", "SMB/CIFS network share"),
    "smb3": ("danger", "SMB network share"),
    "smbfs": ("danger", "SMB network share"),
    "nfs": ("danger", "NFS network share"),
    "nfs4": ("danger", "NFS network share"),
    "fuse.sshfs": ("danger", "sshfs mount"),
    "fuse.rclone": ("danger", "rclone mount"),
    "fuse.mergerfs": ("danger", "mergerfs union mount"),
    "fuse.glusterfs": ("danger", "GlusterFS mount"),
    "afs": ("danger", "AFS network share"),
    "9p": ("danger", "9p host share"),
    "lustre": ("danger", "Lustre network filesystem"),
    "ceph": ("danger", "CephFS network filesystem"),
    "virtiofs": ("warn", "virtiofs host share"),
    "fuseblk": ("warn", "NTFS/exFAT via FUSE"),
    "vboxsf": ("danger", "VirtualBox shared folder"),
    "overlay": ("warn", "container overlay filesystem"),
    "tmpfs": ("danger", "RAM disk — contents are lost on restart"),
}

_SAFE_NOTE = "local filesystem — safe for SQLite"


def run_scheduled_health_check():
    """Periodic integrity check and WAL checkpoint.

    The body lives here rather than in ``app.py`` for the reason
    ``scheduled_reading_list_sync`` does: app.py cannot be imported in tests,
    so anything left there is only reachable through AST assertions.

    Two jobs in one because they belong together and both are cheap:

    - ``quick_check`` is what closes the 78-minute gap. Corruption used to be
      discovered only at startup, so a database that broke at 07:35 was still
      being written to at 08:53 with nothing said.
    - The checkpoint keeps the WAL from growing without bound. Nothing else in
      the app ever checkpoints, and a leaked read connection blocks SQLite's
      automatic one indefinitely.

    Never raises: it runs on the APScheduler thread, where an exception is
    swallowed and the job quietly stops being useful.
    """
    from core.database import check_integrity, get_db_path
    from core.db_maintenance import checkpoint_wal

    result = {"ok": None, "message": None, "checkpoint": None}
    try:
        ok, message = check_integrity(get_db_path(), quick=True)
        result["ok"], result["message"] = ok, message

        if ok:
            app_state.set_db_integrity(True, None)
            # A passing check means the file in use now is sound. Anything in
            # the ledger describes a database that has since been replaced.
            if is_latched():
                app_logger.info(
                    "Database integrity check passed; clearing the previous "
                    "corruption alert."
                )
                clear_db_errors()
        else:
            app_logger.error(
                f"Scheduled database integrity check FAILED: {message}"
            )
            note_db_error(
                sqlite3.DatabaseError(message), "scheduled integrity check"
            )

        result["checkpoint"] = checkpoint_wal(truncate=True)
        return result
    except Exception as e:  # pragma: no cover - scheduler thread
        app_logger.error(f"Scheduled database health check failed: {e}")
        result["message"] = str(e)
        return result


def _read_mountinfo(path="/proc/self/mountinfo"):
    """Parse /proc/self/mountinfo into ``[(mountpoint, fstype, source)]``.

    Returns ``[]`` on any platform without it (Windows dev, macOS), which makes
    every caller degrade to "unknown" rather than raising.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []

    out = []
    for line in lines:
        # <id> <parent> <maj:min> <root> <mountpoint> <opts> [tags...] - <fstype> <source> <sopts>
        if " - " not in line:
            continue
        left, _, right = line.partition(" - ")
        left_parts = left.split()
        right_parts = right.split()
        if len(left_parts) < 5 or len(right_parts) < 2:
            continue
        mountpoint = _unescape_mount(left_parts[4])
        fstype = right_parts[0]
        source = _unescape_mount(right_parts[1])
        out.append((mountpoint, fstype, source))
    return out


def _unescape_mount(value):
    """mountinfo octal-escapes space, tab, newline and backslash."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), value)


def describe_storage(path):
    """Identify the filesystem holding ``path`` and judge it for SQLite.

    Returns ``{fstype, mountpoint, source, risk, note}`` where ``risk`` is one
    of ``"ok"``, ``"warn"``, ``"danger"`` or ``"unknown"``. Never raises.
    """
    result = {
        "fstype": None,
        "mountpoint": None,
        "source": None,
        "risk": "unknown",
        "note": "could not determine the filesystem",
    }
    try:
        mounts = _read_mountinfo()
        if not mounts:
            return result

        # mountinfo paths are POSIX. Keep an already-absolute POSIX path as it
        # is rather than sending it through abspath, which on Windows turns
        # "/config" into "C:\\config" and stops it matching anything.
        target = path if path.startswith("/") else os.path.abspath(path)
        target = target.replace("\\", "/")
        # Longest matching mountpoint wins -- /config must beat /.
        best = None
        for mountpoint, fstype, source in mounts:
            if target == mountpoint or target.startswith(
                mountpoint.rstrip("/") + "/"
            ):
                if best is None or len(mountpoint) > len(best[0]):
                    best = (mountpoint, fstype, source)
        if best is None:
            return result

        mountpoint, fstype, source = best
        result.update(
            {"fstype": fstype, "mountpoint": mountpoint, "source": source}
        )

        risk, label = RISKY_FSTYPES.get(fstype, ("ok", None))
        result["risk"] = risk
        if risk == "ok":
            result["note"] = f"{fstype} — {_SAFE_NOTE}"
        elif fstype == "overlay":
            # Not a locking problem: it means /config was never mounted, so the
            # database lives in the container's writable layer and dies with it.
            result["note"] = (
                "the database is inside the container's writable layer, not a "
                "mounted volume — it will be lost when the container is "
                "recreated"
            )
        elif risk == "danger":
            result["note"] = (
                f"{label} — SQLite is not safe here. File locking over "
                "network filesystems is unreliable and is the most common "
                "cause of “database disk image is malformed”. Move "
                "/config to local storage or a Docker named volume."
            )
        else:
            result["note"] = (
                f"{label} — SQLite can be unreliable here; prefer local "
                "storage or a Docker named volume for /config."
            )
        return result
    except Exception as e:  # pragma: no cover - diagnostics must never raise
        app_logger.debug(f"describe_storage failed for {path}: {e}")
        return result
