"""Per-file writers must release the WAL write lock when they fail (#723 shape).

These run once per file across the whole library (metadata scan, ComicInfo
writes, browse cache, reader saves). Each used to commit and close inside its
``try``, so an exception after the first write abandoned the connection with
its implicit transaction open. The connection then lived on in a reference
cycle, holding the write lock, until the cyclic GC ran -- and every other
writer burned its 30s busy_timeout.

Each test makes a real write first, then fails, with the cyclic GC disabled so
a leak cannot be hidden by a lucky collection, and asserts a fresh connection
can take the write lock immediately.
"""

import gc
import sqlite3
from unittest.mock import patch

import pytest

import core.database as database


def _can_write_now(db_path):
    probe = sqlite3.connect(db_path, timeout=0.5)
    try:
        probe.execute("BEGIN IMMEDIATE")
        probe.rollback()
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        probe.close()


def _fail_on(predicate, take_lock_with=None):
    """A _GuardedCursor.execute that raises IntegrityError when *predicate*
    matches. With *take_lock_with*, that write statement runs first so the
    lock is held even when the failing statement is the first write."""
    real_execute = database._GuardedCursor.execute

    def execute(self, sql, *args, **kwargs):
        if predicate(sql.lstrip().upper()):
            if take_lock_with:
                real_execute(self, take_lock_with)
            raise sqlite3.IntegrityError("UNIQUE constraint failed (injected)")
        return real_execute(self, sql, *args, **kwargs)

    return execute


@pytest.fixture
def indexed_file(db_connection):
    path = "/data/Pub/Series/Series 001.cbz"
    database.add_file_index_entry(
        name="Series 001.cbz", path=path, entry_type="file", size=1,
        parent="/data/Pub/Series",
    )
    row = db_connection.execute(
        "SELECT id FROM file_index WHERE path = ?", (path,)
    ).fetchone()
    return row[0], path


def _assert_fails_and_releases(db_path, execute, call, expected):
    gc.disable()  # the leak is only visible until a cyclic collection
    try:
        with patch.object(database._GuardedCursor, "execute", execute):
            assert call() == expected
        assert _can_write_now(db_path)
    finally:
        gc.enable()


def test_update_file_metadata(db_path, indexed_file):
    file_id, _ = indexed_file
    # The UPDATE file_index runs for real; the tag refresh after it fails.
    execute = _fail_on(lambda s: s.startswith("DELETE FROM FILE_METADATA_TAGS"))
    _assert_fails_and_releases(
        db_path, execute,
        lambda: database.update_file_metadata(
            file_id, {"ci_series": "Series", "ci_writer": "A"}, 123.0, 1
        ),
        False,
    )


def test_update_file_index_from_comicinfo(db_path, indexed_file):
    _, path = indexed_file
    execute = _fail_on(lambda s: s.startswith("DELETE FROM FILE_METADATA_TAGS"))
    _assert_fails_and_releases(
        db_path, execute,
        lambda: database.update_file_index_from_comicinfo(
            path, {"Series": "Series", "Number": "1", "Writer": "A"}
        ),
        False,
    )


def test_save_browse_cache(db_path, db_connection):
    # IntegrityError, not "database is locked", so the retry loop is not taken.
    execute = _fail_on(
        lambda s: s.startswith("INSERT OR REPLACE INTO BROWSE_CACHE"),
        take_lock_with="DELETE FROM browse_cache WHERE 0",
    )
    _assert_fails_and_releases(
        db_path, execute,
        lambda: database.save_browse_cache("/data/Pub", {"entries": []}),
        False,
    )


def test_invalidate_browse_cache(db_path, db_connection):
    assert database.save_browse_cache("/data/Pub/Series", {"entries": []})
    calls = {"n": 0}
    real_execute = database._GuardedCursor.execute

    def execute(self, sql, *args, **kwargs):
        # The first DELETE really removes a row; the second one fails.
        if sql.lstrip().upper().startswith("DELETE FROM BROWSE_CACHE"):
            calls["n"] += 1
            if calls["n"] == 2:
                raise sqlite3.IntegrityError("injected")
        return real_execute(self, sql, *args, **kwargs)

    _assert_fails_and_releases(
        db_path, execute,
        lambda: database.invalidate_browse_cache("/data/Pub/Series"),
        False,
    )
    # The failed transaction was rolled back, not half-applied.
    assert db_connection.execute(
        "SELECT COUNT(*) FROM browse_cache WHERE path = ?", ("/data/Pub/Series",)
    ).fetchone()[0] == 1


def test_save_reading_position(db_path, db_connection):
    execute = _fail_on(
        lambda s: s.startswith("INSERT OR REPLACE INTO READING_POSITIONS"),
        take_lock_with="DELETE FROM reading_positions WHERE 0",
    )
    _assert_fails_and_releases(
        db_path, execute,
        lambda: database.save_reading_position("/data/Pub/Series 001.cbz", 3, 20),
        False,
    )


def test_successful_writes_still_report_success(db_connection, indexed_file):
    """The conversion keeps each function's return values."""
    file_id, path = indexed_file
    assert database.update_file_metadata(file_id, {"ci_series": "S"}, 1.0, 1) is True
    assert database.update_file_index_from_comicinfo(path, {"Series": "S"}) is True
    assert database.update_file_index_from_comicinfo("/data/nope.cbz", {}) is False
    assert database.save_browse_cache("/data/Pub", {"entries": []}) is True
    assert database.invalidate_browse_cache("/data/Pub") is True

    # comic_path is stored byte-exact.
    odd = "/data/Pub/Série  01 (2020).CBZ"
    assert database.save_reading_position(odd, 4, 20, time_spent=7) is True
    row = db_connection.execute(
        "SELECT comic_path, page_number, total_pages, time_spent "
        "FROM reading_positions WHERE comic_path = ?", (odd,)
    ).fetchone()
    assert tuple(row) == (odd, 4, 20, 7)
