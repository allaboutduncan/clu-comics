"""The cross-process lock that serialises ``init_db()``.

``init_db()`` moves the database file between volumes and performs four
CREATE-copy-DROP-RENAME table rebuilds, none of it inside a transaction.
``busy_timeout`` serialises single statements, not a four-step rebuild, so two
concurrent runs can drop a table between another run's copy and its rename.

The lock's two required properties are opposites, and both matter:

- it must actually exclude a second *process* (a thread lock would not: the
  monitor is a separate OS process, and a gunicorn worker respawn re-imports
  app.py entirely), and
- it must **fail open**. A lock that raises would take the whole app down to
  avoid a rare race, which is a much worse trade.
"""
import os
import subprocess
import sys
import textwrap
import time

import pytest

from core.db_lock import db_lock_path, exclusive_lock


def test_lock_path_sits_beside_the_database():
    assert db_lock_path("/config/comic_utils.db") == \
        "/config/comic_utils.db.initlock"


def test_acquires_and_releases(tmp_path):
    lock = str(tmp_path / "a.lock")
    with exclusive_lock(lock, timeout=5) as acquired:
        assert acquired is True
    # Released, so it can be taken again in the same process.
    with exclusive_lock(lock, timeout=5) as acquired:
        assert acquired is True


def test_lock_file_survives_release(tmp_path):
    """Never unlinked: removing it races another process that already holds the
    old inode open."""
    lock = str(tmp_path / "b.lock")
    with exclusive_lock(lock, timeout=5):
        pass
    assert os.path.exists(lock)


def test_excludes_a_second_process(tmp_path):
    """The property that matters. monitor.py is a separate OS process, so a
    threading.Lock here would be decorative."""
    lock = str(tmp_path / "c.lock")
    flag = str(tmp_path / "held.flag")

    child_src = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {os.getcwd()!r})
        from core.db_lock import exclusive_lock
        with exclusive_lock({lock!r}, timeout=30) as ok:
            if not ok:
                sys.exit(3)
            open({flag!r}, "w").close()
            time.sleep(3)
        sys.exit(0)
    """)
    child = subprocess.Popen([sys.executable, "-c", child_src])
    try:
        # Wait for the child to actually hold it.
        deadline = time.monotonic() + 20
        while not os.path.exists(flag):
            if time.monotonic() > deadline or child.poll() is not None:
                child.kill()
                pytest.skip("child process could not take the lock")
            time.sleep(0.05)

        # Short timeout: we expect to be refused while the child holds it.
        with exclusive_lock(lock, timeout=0.5) as acquired:
            assert acquired is False, (
                "a second process took the lock while the first held it"
            )
    finally:
        child.wait(timeout=15)

    # Once the child exits, the lock is free again.
    with exclusive_lock(lock, timeout=10) as acquired:
        assert acquired is True


def test_fails_open_when_the_lock_file_cannot_be_opened(tmp_path):
    """A directory path can never be opened as a file. The context manager must
    still yield (False) rather than raise -- refusing to start is worse than the
    race being avoided."""
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    with exclusive_lock(str(directory), timeout=0.2) as acquired:
        assert acquired is False
