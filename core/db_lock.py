"""A cross-process advisory lock, used to serialise ``init_db()``.

``init_db()`` is not a read-only "create tables if missing" pass. It runs
``_migrate_db_to_config_dir()`` -- a ``shutil.move`` of the database file and
its WAL/SHM sidecars, which degrades to a non-atomic copy+unlink across
volumes -- and then four ``_rebuild_add_user_scope()`` table rebuilds, each
``CREATE __new`` -> ``INSERT ... SELECT`` -> ``DROP TABLE`` -> ``RENAME``, plus
several hand-rolled drops. None of it is inside a transaction, and
``busy_timeout`` serialises individual statements, not a four-step rebuild.

Two processes doing that to one file at the same time is a corruption
mechanism, not a theory. The window is small but it is reachable:

- ``monitor.py`` is a separate OS process (``app.py`` Popen's it when
  ``MONITOR=yes``) and calls ``init_db()`` at import.
- More importantly, gunicorn kills a worker that exceeds ``--timeout 120`` and
  respawns it. That re-imports ``app.py`` -> a second ``init_db()`` -> a second
  ``monitor.py``, while the orphaned first one is still alive and was never
  reaped. *That* is the case where two full ``init_db()`` runs genuinely
  overlap.

Fails open on purpose. A lock that raises at import would take the whole app
down, and the thing it guards is a rare race -- degrading to today's behaviour
is strictly better than not starting.
"""
import os
import time
from contextlib import contextmanager

from core.app_logging import app_logger

try:  # POSIX (the container)
    import fcntl
except ImportError:  # pragma: no cover - Windows dev boxes
    fcntl = None

try:  # Windows (local development and the test suite)
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None


@contextmanager
def exclusive_lock(lock_path, timeout=120.0, poll=0.25):
    """Hold an exclusive advisory lock on ``lock_path`` for the block.

    Yields True when the lock was taken, False when it could not be (timed out,
    or the platform offers no usable locking). The caller proceeds either way --
    see the module docstring on failing open -- but a False lets it log.

    The lock file is created next to the database and is never deleted:
    unlinking it races another process that already has the old inode open.
    """
    handle = None
    acquired = False
    try:
        try:
            handle = open(lock_path, "a+b")
        except OSError as e:
            app_logger.warning(
                f"Could not open DB lock file {lock_path}: {e}. Continuing "
                "without cross-process locking."
            )
            yield False
            return

        acquired = _acquire(handle, timeout, poll)
        if not acquired:
            app_logger.warning(
                f"Timed out after {timeout:.0f}s waiting for the database lock "
                f"({lock_path}). Continuing anyway."
            )
        yield acquired
    finally:
        if handle is not None:
            if acquired:
                _release(handle)
            try:
                handle.close()
            except OSError:
                pass


def _acquire(handle, timeout, poll):
    """Block up to ``timeout`` seconds for an exclusive lock. False on failure."""
    if fcntl is not None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(poll)

    if msvcrt is not None:
        # msvcrt.locking needs a byte range and raises rather than blocking, so
        # it gets the same poll loop. One byte at offset 0 is enough for an
        # advisory lock.
        deadline = time.monotonic() + timeout
        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(poll)

    return False  # pragma: no cover - no locking primitive available


def _release(handle):
    try:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError as e:  # pragma: no cover
        app_logger.debug(f"Could not release DB lock: {e}")


def db_lock_path(db_path):
    """The lock file for a given database path."""
    return db_path + ".initlock"
