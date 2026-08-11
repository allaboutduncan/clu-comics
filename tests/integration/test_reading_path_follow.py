"""
Reading data must follow a file when it is renamed, moved or converted.

reading_positions.comic_path and issues_read.issue_path are raw path strings
with no foreign key to file_index, so any path change orphans a user's bookmark
and read history unless it is followed explicitly.
"""
import pytest

from core.database import (
    create_user,
    get_db_connection,
    get_reading_position,
    get_user_by_username,
    mark_issue_read,
    move_reading_data,
    save_reading_position,
    update_file_index_entry,
)
from tests.factories.db_factories import create_file_index_entry


OLD = "/data/Batman/Batman 001.cbz"
NEW = "/data/Batman/Batman 001 (2020).cbz"


def _positions():
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT user_id, comic_path, page_number, total_pages, time_spent "
        "FROM reading_positions ORDER BY comic_path, user_id"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _read_paths():
    conn = get_db_connection()
    rows = conn.execute(
        "SELECT issue_path FROM issues_read ORDER BY issue_path"
    ).fetchall()
    conn.close()
    return [r["issue_path"] for r in rows]


class TestMoveReadingDataFile:
    def test_position_follows_and_keeps_its_data(self, db_connection):
        save_reading_position(OLD, page_number=7, total_pages=30, time_spent=450)

        assert move_reading_data(OLD, NEW) is True

        assert get_reading_position(OLD) is None
        moved = get_reading_position(NEW)
        assert moved is not None
        assert moved["page_number"] == 7
        assert moved["total_pages"] == 30
        assert moved["time_spent"] == 450

    def test_issues_read_follows(self, db_connection):
        mark_issue_read(OLD)
        assert _read_paths() == [OLD]

        move_reading_data(OLD, NEW)
        assert _read_paths() == [NEW]

    def test_every_users_rows_follow(self, db_connection):
        """A rename is a filesystem fact -- it must not be user-scoped."""
        create_user("alice", password="pw", role="reader")
        create_user("bob", password="pw", role="reader")
        alice = get_user_by_username("alice")["id"]
        bob = get_user_by_username("bob")["id"]

        save_reading_position(OLD, page_number=3, total_pages=30, user_id=alice)
        save_reading_position(OLD, page_number=11, total_pages=30, user_id=bob)

        move_reading_data(OLD, NEW)

        assert get_reading_position(NEW, user_id=alice)["page_number"] == 3
        assert get_reading_position(NEW, user_id=bob)["page_number"] == 11
        assert get_reading_position(OLD, user_id=alice) is None
        assert get_reading_position(OLD, user_id=bob) is None

    def test_move_onto_an_existing_row_does_not_raise(self, db_connection):
        """UNIQUE(user_id, comic_path) would make a plain UPDATE raise
        IntegrityError when the destination already has a bookmark."""
        save_reading_position(OLD, page_number=4, total_pages=30)
        save_reading_position(NEW, page_number=25, total_pages=30)

        assert move_reading_data(OLD, NEW) is True

        rows = _positions()
        assert len(rows) == 1
        # The row that moved wins; the stale destination row is dropped.
        assert rows[0]["comic_path"] == NEW
        assert rows[0]["page_number"] == 4

    def test_noop_moves_are_rejected(self, db_connection):
        assert move_reading_data(OLD, OLD) is False
        assert move_reading_data("", NEW) is False
        assert move_reading_data(OLD, None) is False

    def test_unrelated_rows_are_untouched(self, db_connection):
        other = "/data/Superman/Superman 001.cbz"
        save_reading_position(OLD, page_number=2, total_pages=30)
        save_reading_position(other, page_number=9, total_pages=30)

        move_reading_data(OLD, NEW)

        assert get_reading_position(other)["page_number"] == 9


class TestMoveReadingDataDirectory:
    def test_descendants_are_rewritten(self, db_connection):
        save_reading_position("/data/Batman/Batman 001.cbz", page_number=2,
                              total_pages=30)
        save_reading_position("/data/Batman/v2/Batman 050.cbz", page_number=8,
                              total_pages=30)
        mark_issue_read("/data/Batman/Batman 002.cbz")

        assert move_reading_data("/data/Batman", "/data/Batman (DC)",
                                 is_dir=True) is True

        assert get_reading_position(
            "/data/Batman (DC)/Batman 001.cbz")["page_number"] == 2
        assert get_reading_position(
            "/data/Batman (DC)/v2/Batman 050.cbz")["page_number"] == 8
        assert _read_paths() == ["/data/Batman (DC)/Batman 002.cbz"]

    def test_sibling_with_shared_prefix_is_not_rewritten(self, db_connection):
        """The LIKE pattern must be '{old}/%', never '{old}%' -- otherwise
        renaming /data/Batman also rewrites /data/Batman Beyond."""
        sibling = "/data/Batman Beyond/Issue 001.cbz"
        save_reading_position("/data/Batman/Batman 001.cbz", page_number=2,
                              total_pages=30)
        save_reading_position(sibling, page_number=5, total_pages=30)

        move_reading_data("/data/Batman", "/data/Batman (DC)", is_dir=True)

        assert get_reading_position(sibling)["page_number"] == 5
        assert get_reading_position(
            "/data/Batman (DC)/Batman 001.cbz")["page_number"] == 2

    def test_directory_move_onto_existing_rows_does_not_raise(self, db_connection):
        save_reading_position("/data/Batman/Batman 001.cbz", page_number=2,
                              total_pages=30)
        save_reading_position("/data/Batman (DC)/Batman 001.cbz",
                              page_number=20, total_pages=30)

        assert move_reading_data("/data/Batman", "/data/Batman (DC)",
                                 is_dir=True) is True

        rows = _positions()
        assert len(rows) == 1
        assert rows[0]["page_number"] == 2


class TestRenameChokePoint:
    """update_file_index_entry is the single choke point every file rename in
    routes/files.py, routes/metadata.py and cbz_ops/smart_rename.py funnels
    through, so following the path there covers all of them at once.
    """

    def test_file_index_rename_follows_reading_position(self, db_connection):
        create_file_index_entry(name="Batman 001.cbz", path=OLD,
                                parent="/data/Batman")
        save_reading_position(OLD, page_number=6, total_pages=30, time_spent=90)
        mark_issue_read(OLD)

        assert update_file_index_entry(
            OLD, name="Batman 001 (2020).cbz", new_path=NEW
        ) is True

        assert get_reading_position(OLD) is None
        assert get_reading_position(NEW)["page_number"] == 6
        assert get_reading_position(NEW)["time_spent"] == 90
        assert _read_paths() == [NEW]

    def test_update_without_a_rename_leaves_the_position_alone(self, db_connection):
        create_file_index_entry(name="Batman 001.cbz", path=OLD,
                                parent="/data/Batman")
        save_reading_position(OLD, page_number=6, total_pages=30)

        update_file_index_entry(OLD, size=4242)

        assert get_reading_position(OLD)["page_number"] == 6

    def test_rename_onto_an_existing_row_does_not_raise(self, db_connection):
        create_file_index_entry(name="Batman 001.cbz", path=OLD,
                                parent="/data/Batman")
        save_reading_position(OLD, page_number=6, total_pages=30)
        save_reading_position(NEW, page_number=28, total_pages=30)

        assert update_file_index_entry(OLD, new_path=NEW) is True

        rows = _positions()
        assert len(rows) == 1
        assert rows[0]["page_number"] == 6
