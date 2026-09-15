"""Maintenance and salvage against a real SQLite database.

Covers the three things that were previously unreachable from the app at all --
checkpointing, compaction and salvage -- plus the regression test for the
backup rewrite.
"""
import os
import sqlite3
import threading
import time

import pytest

from core import database
from core.db_maintenance import (
    checkpoint_wal, compact_database, get_page_stats, optimize_database,
)


def _churn(db_path, rows=3000):
    """Insert then delete, leaving a large freelist for Compact to reclaim."""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("CREATE TABLE IF NOT EXISTS junk (id INTEGER PRIMARY KEY, blob TEXT)")
    conn.executemany(
        "INSERT INTO junk (blob) VALUES (?)",
        [("x" * 900,) for _ in range(rows)],
    )
    conn.commit()
    conn.execute("DELETE FROM junk")
    conn.commit()
    conn.close()


class TestPageStats:
    def test_reports_pages_and_reclaimable(self, db_path, db_connection):
        _churn(db_path)
        stats = get_page_stats(db_path)
        assert stats["error"] is None
        assert isinstance(stats["page_count"], int) and stats["page_count"] > 0
        assert isinstance(stats["page_size"], int) and stats["page_size"] > 0
        assert stats["reclaimable_bytes"] == (
            stats["freelist_count"] * stats["page_size"]
        )

    def test_reports_the_pragmas_actually_in_force(self, db_path, db_connection):
        stats = get_page_stats(db_path)
        assert str(stats["journal_mode"]).lower() == "wal"
        # synchronous is per-connection, so this is the file's own setting --
        # reported so the page can say what is really running.
        assert stats["synchronous"] is not None

    def test_missing_database_is_not_an_error(self, tmp_path):
        stats = get_page_stats(str(tmp_path / "nope.db"))
        assert stats["error"] is None
        assert stats["page_count"] is None


class TestCheckpoint:
    def test_truncates_the_wal(self, db_path, db_connection):
        _churn(db_path, rows=1500)
        wal = db_path + "-wal"
        assert os.path.exists(wal), "fixture DB should be in WAL mode"

        result = checkpoint_wal(truncate=True, db_path=db_path)
        assert result["error"] is None
        assert result["busy"] == 0, "no other reader should be holding the log"
        assert result["wal_size_after"] <= result["wal_size_before"]
        assert os.path.getsize(wal) == 0

    def test_reports_busy_rather_than_hanging(self, db_path, db_connection):
        """With a reader holding an open transaction, TRUNCATE cannot complete.
        That is a normal outcome with this many threads, and the UI has to be
        able to say so -- so it must come back promptly with busy=1, not block.
        """
        _churn(db_path, rows=500)
        reader = sqlite3.connect(db_path, timeout=5)
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM junk").fetchone()
        try:
            started = time.monotonic()
            result = checkpoint_wal(
                truncate=True, db_path=db_path, busy_timeout_ms=500
            )
            elapsed = time.monotonic() - started
            assert elapsed < 10, "checkpoint must not hang on a live reader"
            assert result["error"] is None
            assert result["busy"] in (0, 1)
        finally:
            reader.rollback()
            reader.close()


class TestOptimize:
    def test_runs(self, db_path, db_connection):
        result = optimize_database(db_path)
        assert result["error"] is None
        assert result["analyzed"] is True
        assert result["optimized"] is True


class TestCompact:
    def test_reclaims_space_and_keeps_data(self, db_path, db_connection, monkeypatch):
        monkeypatch.setattr(database, "get_db_path", lambda: db_path)
        _churn(db_path, rows=4000)
        # Put a row back so we can prove the data survives the swap.
        conn = sqlite3.connect(db_path)
        conn.execute("INSERT INTO junk (blob) VALUES ('keep me')")
        conn.commit()
        conn.close()

        # Close the fixture handle: os.replace cannot replace a file with an
        # open handle on Windows.
        db_connection.close()

        before = os.path.getsize(db_path)
        result = compact_database()
        assert result["error"] is None, result["error"]
        assert result["success"] is True
        assert result["size_after"] < before
        assert result["reclaimed"] > 0
        assert result["pre_swap_backup"]

        conn = sqlite3.connect(db_path)
        try:
            assert conn.execute(
                "SELECT blob FROM junk"
            ).fetchall() == [("keep me",)]
            # The swap must leave the database in WAL mode: journal mode travels
            # in the file header, and VACUUM INTO output is not WAL.
            assert conn.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0].lower() == "wal"
        finally:
            conn.close()

    def test_refuses_a_corrupt_database(self, db_path, db_connection, monkeypatch):
        """VACUUM rewrites every page. Doing that to a database already showing
        corruption is the most destructive action on this page."""
        monkeypatch.setattr(database, "get_db_path", lambda: db_path)
        db_connection.close()
        for suffix in ("-wal", "-shm"):
            if os.path.exists(db_path + suffix):
                try:
                    os.remove(db_path + suffix)
                except OSError:
                    pass
        with open(db_path, "wb") as f:
            f.write(b"this is not a sqlite database" * 64)

        result = compact_database()
        assert result["success"] is False
        assert "integrity" in result["error"].lower()

    def test_refuses_without_free_space(self, db_path, db_connection, monkeypatch):
        monkeypatch.setattr(database, "get_db_path", lambda: db_path)

        class _Usage:
            free = 1  # one byte

        monkeypatch.setattr("shutil.disk_usage", lambda p: _Usage())
        result = compact_database()
        assert result["success"] is False
        assert "free space" in result["error"].lower()


class TestBackupIsConsistentUnderWriters:
    """The regression test for the backup rewrite.

    Backups used to be a plain ``zipfile.write()`` of the live database plus its
    WAL and SHM, each read at a different instant while dozens of threads wrote
    -- and it ran at boot, at peak write load. A ``.db`` captured mid-checkpoint
    is only consistent with the matching WAL, and a torn pair reads back as
    "database disk image is malformed".
    """

    def test_backup_member_is_valid_while_a_writer_commits(
        self, db_path, db_connection, monkeypatch, tmp_path
    ):
        import zipfile

        monkeypatch.setattr(database, "get_db_path", lambda: db_path)
        _churn(db_path, rows=500)

        stop = threading.Event()
        errors = []

        def _writer():
            conn = sqlite3.connect(db_path, timeout=30)
            conn.execute("PRAGMA busy_timeout=30000")
            try:
                n = 0
                while not stop.is_set():
                    conn.execute(
                        "INSERT INTO junk (blob) VALUES (?)", (f"row-{n}",)
                    )
                    conn.commit()
                    n += 1
            except Exception as e:  # pragma: no cover
                errors.append(e)
            finally:
                conn.close()

        thread = threading.Thread(target=_writer, daemon=True)
        thread.start()
        try:
            time.sleep(0.2)
            name = database.backup_database(max_backups=99, force=True)
        finally:
            stop.set()
            thread.join(timeout=15)

        assert name, f"backup failed; writer errors: {errors}"

        extracted = tmp_path / "from_backup.db"
        with zipfile.ZipFile(os.path.join(os.path.dirname(db_path), name)) as zf:
            names = zf.namelist()
            assert names == ["comic_utils.db"], (
                "the archive must hold exactly the main database: a WAL "
                "captured at a different instant is what made restores unsafe"
            )
            with zf.open("comic_utils.db") as src:
                extracted.write_bytes(src.read())

        ok, message = database.check_integrity(str(extracted), quick=False)
        assert ok, f"backup taken under load is not intact: {message}"


class TestBackupNameCollisions:
    def test_three_backups_in_one_second_do_not_overwrite(
        self, db_path, db_connection, monkeypatch
    ):
        """A manual backup, the pre-swap snapshot and a salvage apply can all
        land inside the same second. Names carry second resolution, so two of
        them silently overwrote each other."""
        monkeypatch.setattr(database, "get_db_path", lambda: db_path)
        names = {
            database.backup_database(max_backups=99, force=True)
            for _ in range(3)
        }
        assert len(names) == 3
        for name in names:
            assert database._BACKUP_FILENAME_RE.match(name), name
