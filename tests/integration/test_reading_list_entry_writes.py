"""Reading-list writers: batch import writes and lock release (#722)."""

import gc
import sqlite3
from unittest.mock import patch

import pytest


def _can_write_now(db_path):
    """True when a fresh connection can take the write lock without waiting."""
    probe = sqlite3.connect(db_path, timeout=0.5)
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.rollback()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        probe.close()


def _entries(db_connection, list_id):
    return [
        (r[0], r[1])
        for r in db_connection.execute(
            "SELECT issue_number, sort_order FROM reading_list_entries "
            "WHERE reading_list_id = ? ORDER BY sort_order",
            (list_id,),
        ).fetchall()
    ]


class TestAddReadingListEntries:
    def test_writes_in_order_after_existing_entries(self, db_connection):
        from core.database import (
            add_reading_list_entries,
            add_reading_list_entry,
            create_reading_list,
        )

        list_id = create_reading_list("Arc", source="metron://arc/1")
        add_reading_list_entry(list_id, {"series": "S", "issue_number": "1"})

        written = add_reading_list_entries(list_id, [
            {"series": "S", "issue_number": "2", "metron_id": 20},
            {"series": "S", "issue_number": "3", "matched_file_path": "/data/S/S 003.cbz"},
        ])

        assert written == 2
        assert _entries(db_connection, list_id) == [("1", 1), ("2", 2), ("3", 3)]
        row = db_connection.execute(
            "SELECT metron_id FROM reading_list_entries WHERE issue_number = '2'"
        ).fetchone()
        assert row[0] == 20

    def test_empty_import_writes_nothing(self, db_connection):
        from core.database import add_reading_list_entries, create_reading_list

        list_id = create_reading_list("Empty")
        assert add_reading_list_entries(list_id, []) == 0
        assert _entries(db_connection, list_id) == []

    def test_a_failure_writes_no_entries(self, db_connection):
        from core.database import add_reading_list_entries, create_reading_list
        import core.database as database

        list_id = create_reading_list("Arc")

        def failing_executemany(self, sql, *args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        with patch.object(database._GuardedCursor, "executemany", failing_executemany):
            assert add_reading_list_entries(list_id, [{"series": "S", "issue_number": "1"}]) is None
        assert _entries(db_connection, list_id) == []


class TestReadingListWritersReleaseTheLock:
    """A writer that fails after taking the write lock must not keep it: every
    other writer (login included) would wait out its 30s busy timeout."""

    @pytest.mark.parametrize("call", [
        "create", "add_one", "add_many", "description", "source_version",
        "entry_match", "sync",
    ])
    def test_a_failed_write_releases_the_lock(self, db_connection, db_path, call):
        import core.database as database

        list_id = database.create_reading_list("Arc")
        entry_id = database.add_reading_list_entry(
            list_id, {"series": "S", "issue_number": "1"}
        )

        real_execute = database._GuardedCursor.execute
        real_executemany = database._GuardedCursor.executemany

        def _is_write(sql):
            head = sql.lstrip().upper()
            return "READING_LIST" in head and head.startswith(("INSERT", "UPDATE", "DELETE"))

        def _fail_after_locking(cursor):
            real_execute(cursor, "UPDATE reading_lists SET name = name WHERE 0")
            raise sqlite3.IntegrityError("UNIQUE constraint failed: x.y")

        def failing_execute(self, sql, *args, **kwargs):
            if _is_write(sql):
                _fail_after_locking(self)
            return real_execute(self, sql, *args, **kwargs)

        def failing_executemany(self, sql, *args, **kwargs):
            if _is_write(sql):
                _fail_after_locking(self)
            return real_executemany(self, sql, *args, **kwargs)

        calls = {
            "create": lambda: database.create_reading_list("Other"),
            "add_one": lambda: database.add_reading_list_entry(
                list_id, {"series": "S", "issue_number": "2"}),
            "add_many": lambda: database.add_reading_list_entries(
                list_id, [{"series": "S", "issue_number": "2"}]),
            "description": lambda: database.update_reading_list_description(list_id, "d"),
            "source_version": lambda: database.update_reading_list_source_version(list_id, "t"),
            "entry_match": lambda: database.update_reading_list_entry_match(entry_id, "/data/x.cbz"),
            "sync": lambda: database.sync_reading_list_entries(
                list_id, [{"series": "S", "issue_number": "9"}]),
        }
        gc.disable()  # the leak is only visible until a cyclic collection
        try:
            with patch.object(database._GuardedCursor, "execute", failing_execute), \
                 patch.object(database._GuardedCursor, "executemany", failing_executemany):
                assert calls[call]() in (None, False)
            assert _can_write_now(db_path)
        finally:
            gc.enable()
