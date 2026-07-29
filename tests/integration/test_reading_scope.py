"""
PR3: per-user scoping of reading data (issues_read, reading_positions, to_read).

Verifies two users keep independent read history, progress, and to-read queues,
that the composite (user_id, path) uniqueness prevents cross-user clobbering,
and that the one-time table rebuild backfills existing rows to the owner.
"""
import sqlite3
from unittest.mock import patch

import pytest

from core.database import (
    add_to_read,
    get_continue_reading_items,
    get_issues_read,
    get_reading_position,
    get_reading_totals,
    get_to_read_items,
    init_db,
    is_issue_read,
    is_to_read,
    mark_issue_read,
    remove_to_read,
    save_reading_position,
    unmark_issue_read,
)

A, B = 1, 2  # two distinct user ids


# ---------------------------------------------------------------------------
# issues_read
# ---------------------------------------------------------------------------
class TestReadHistoryIsolation:
    def test_read_status_is_per_user(self, db_connection):
        path = "/data/x/Book 001.cbz"
        mark_issue_read(path, page_count=20, user_id=A)
        assert is_issue_read(path, user_id=A) is True
        assert is_issue_read(path, user_id=B) is False

        mark_issue_read(path, page_count=20, user_id=B)
        assert is_issue_read(path, user_id=B) is True

    def test_unmark_does_not_affect_other_user(self, db_connection):
        path = "/data/x/Book 002.cbz"
        mark_issue_read(path, user_id=A)
        mark_issue_read(path, user_id=B)
        unmark_issue_read(path, user_id=A)
        assert is_issue_read(path, user_id=A) is False
        assert is_issue_read(path, user_id=B) is True  # B's row survives

    def test_get_issues_read_scoped(self, db_connection):
        mark_issue_read("/data/a.cbz", user_id=A)
        mark_issue_read("/data/b.cbz", user_id=B)
        assert [i["issue_path"] for i in get_issues_read(user_id=A)] == ["/data/a.cbz"]
        assert [i["issue_path"] for i in get_issues_read(user_id=B)] == ["/data/b.cbz"]

    def test_reading_totals_scoped(self, db_connection):
        mark_issue_read("/data/a.cbz", page_count=10, time_spent=100, user_id=A)
        mark_issue_read("/data/b.cbz", page_count=5, time_spent=50, user_id=B)
        assert get_reading_totals(user_id=A)["total_pages"] == 10
        assert get_reading_totals(user_id=B)["total_pages"] == 5

    def test_same_path_two_users_coexist(self, db_connection):
        path = "/data/shared.cbz"
        mark_issue_read(path, page_count=1, user_id=A)
        mark_issue_read(path, page_count=2, user_id=B)
        rows = db_connection.execute(
            "SELECT user_id, page_count FROM issues_read WHERE issue_path = ? ORDER BY user_id",
            (path,),
        ).fetchall()
        assert [(r[0], r[1]) for r in rows] == [(A, 1), (B, 2)]


# ---------------------------------------------------------------------------
# reading_positions
# ---------------------------------------------------------------------------
class TestReadingPositionIsolation:
    def test_positions_are_per_user(self, db_connection):
        path = "/data/c.cbz"
        save_reading_position(path, 5, 20, user_id=A)
        save_reading_position(path, 15, 20, user_id=B)
        assert get_reading_position(path, user_id=A)["page_number"] == 5
        assert get_reading_position(path, user_id=B)["page_number"] == 15

    def test_continue_reading_scoped(self, db_connection):
        save_reading_position("/data/c.cbz", 5, 20, user_id=A)
        assert len(get_continue_reading_items(user_id=A)) == 1
        assert len(get_continue_reading_items(user_id=B)) == 0


# ---------------------------------------------------------------------------
# to_read
# ---------------------------------------------------------------------------
class TestToReadIsolation:
    def test_to_read_is_per_user(self, db_connection):
        path = "/data/d.cbz"
        add_to_read(path, user_id=A)
        assert is_to_read(path, user_id=A) is True
        assert is_to_read(path, user_id=B) is False
        assert len(get_to_read_items(user_id=A)) == 1
        assert len(get_to_read_items(user_id=B)) == 0

    def test_remove_scoped(self, db_connection):
        path = "/data/e.cbz"
        add_to_read(path, user_id=A)
        add_to_read(path, user_id=B)
        remove_to_read(path, user_id=A)
        assert is_to_read(path, user_id=A) is False
        assert is_to_read(path, user_id=B) is True


# ---------------------------------------------------------------------------
# One-time migration / backfill from a pre-multi-user schema
# ---------------------------------------------------------------------------
class TestMigrationBackfill:
    def _build_legacy_db(self, db_path):
        """Create a pre-migration issues_read/reading_positions/to_read schema
        (path-keyed, no user_id) with a couple of rows."""
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE issues_read (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "issue_path TEXT NOT NULL UNIQUE, read_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
            "page_count INTEGER DEFAULT 0, time_spent INTEGER DEFAULT 0)"
        )
        conn.execute(
            "INSERT INTO issues_read (issue_path, page_count) VALUES ('/old/read.cbz', 12)"
        )
        conn.execute(
            "CREATE TABLE reading_positions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "comic_path TEXT NOT NULL UNIQUE, page_number INTEGER NOT NULL, "
            "total_pages INTEGER, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO reading_positions (comic_path, page_number, total_pages) "
            "VALUES ('/old/pos.cbz', 3, 22)"
        )
        conn.execute(
            "CREATE TABLE to_read (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "path TEXT NOT NULL UNIQUE, type TEXT NOT NULL, "
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute("INSERT INTO to_read (path, type) VALUES ('/old/want.cbz', 'file')")
        conn.commit()
        conn.close()

    def test_backfill_assigns_existing_rows_to_owner(self, db_path):
        self._build_legacy_db(db_path)
        with patch("core.database.get_db_path", return_value=db_path):
            assert init_db() is True

            conn = sqlite3.connect(db_path)
            try:
                # user_id column now exists on all three tables.
                for table, path_col, path_val in [
                    ("issues_read", "issue_path", "/old/read.cbz"),
                    ("reading_positions", "comic_path", "/old/pos.cbz"),
                    ("to_read", "path", "/old/want.cbz"),
                ]:
                    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
                    assert "user_id" in cols, f"{table} missing user_id"
                    row = conn.execute(
                        f"SELECT user_id FROM {table} WHERE {path_col} = ?", (path_val,)
                    ).fetchone()
                    assert row is not None and row[0] == 1, f"{table} not backfilled to owner"
            finally:
                conn.close()

    def test_migration_is_idempotent(self, db_path):
        self._build_legacy_db(db_path)
        with patch("core.database.get_db_path", return_value=db_path):
            assert init_db() is True
            assert init_db() is True  # second run must not error or duplicate

            conn = sqlite3.connect(db_path)
            try:
                n = conn.execute(
                    "SELECT COUNT(*) FROM issues_read WHERE issue_path = '/old/read.cbz'"
                ).fetchone()[0]
                assert n == 1
            finally:
                conn.close()
