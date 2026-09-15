"""Reading-list mappings must follow a file, and let go of a deleted one.

reading_list_entries.matched_file_path, .manual_override_path and
reading_lists.thumbnail_path are raw path strings with no foreign key to
file_index. A rename orphans them, and the manual one is the worst case:
core.reading_list_match.rematch_entries deliberately skips an entry that has a
manual_override_path, so a hand-picked mapping broken by a rename can never heal
itself. Following the path is the only thing that fixes it.
"""
import ast
import os

import pytest

from core.database import (
    clear_path_references,
    forget_deleted_path,
    forget_deleted_paths,
    get_db_connection,
    get_reading_position,
    mark_issue_read,
    move_path_references,
    save_reading_position,
    update_file_index_entry,
)
from tests.factories.db_factories import (
    create_file_index_entry,
    create_reading_list,
    create_reading_list_entry,
)


OLD = "/data/Batman/Batman 001.cbz"
NEW = "/data/Batman/Batman 001 (2020).cbz"


def _entry(entry_id):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT matched_file_path, manual_override_path "
        "FROM reading_list_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    conn.close()
    return dict(row)


def _thumbnail(list_id):
    conn = get_db_connection()
    row = conn.execute(
        "SELECT thumbnail_path FROM reading_lists WHERE id = ?", (list_id,)
    ).fetchone()
    conn.close()
    return row["thumbnail_path"]


def _set_thumbnail(list_id, path):
    conn = get_db_connection()
    conn.execute(
        "UPDATE reading_lists SET thumbnail_path = ? WHERE id = ?", (path, list_id)
    )
    conn.commit()
    conn.close()


class TestRenameFollowsReadingList:
    def test_auto_match_follows_a_file_rename(self, db_connection):
        list_id = create_reading_list()
        entry_id = create_reading_list_entry(list_id, matched_file_path=OLD)

        assert move_path_references(OLD, NEW) is True

        assert _entry(entry_id)["matched_file_path"] == NEW

    def test_manual_override_follows_a_file_rename(self, db_connection):
        """The whole point of the feature: rematch_entries skips an entry with
        a manual override, so nothing else can ever repair this one."""
        list_id = create_reading_list()
        entry_id = create_reading_list_entry(
            list_id, matched_file_path=OLD, manual_override_path=OLD
        )

        move_path_references(OLD, NEW)

        row = _entry(entry_id)
        assert row["manual_override_path"] == NEW
        assert row["matched_file_path"] == NEW

    def test_list_thumbnail_follows_a_file_rename(self, db_connection):
        list_id = create_reading_list()
        _set_thumbnail(list_id, OLD)

        move_path_references(OLD, NEW)

        assert _thumbnail(list_id) == NEW

    def test_unrelated_entries_are_untouched(self, db_connection):
        other = "/data/Superman/Superman 001.cbz"
        list_id = create_reading_list()
        moved = create_reading_list_entry(list_id, matched_file_path=OLD)
        stayed = create_reading_list_entry(
            list_id, issue_number="2", matched_file_path=other
        )

        move_path_references(OLD, NEW)

        assert _entry(moved)["matched_file_path"] == NEW
        assert _entry(stayed)["matched_file_path"] == other

    def test_rename_choke_point_follows_the_mapping(self, db_connection):
        """update_file_index_entry is what every single-file rename in
        routes/files.py, routes/metadata.py and cbz_ops/smart_rename.py funnels
        through, so the mapping has to follow from there too."""
        list_id = create_reading_list()
        entry_id = create_reading_list_entry(
            list_id, matched_file_path=OLD, manual_override_path=OLD
        )
        create_file_index_entry(
            name="Batman 001.cbz", path=OLD, parent="/data/Batman"
        )

        assert update_file_index_entry(
            OLD, name="Batman 001 (2020).cbz", new_path=NEW
        ) is True

        row = _entry(entry_id)
        assert row["matched_file_path"] == NEW
        assert row["manual_override_path"] == NEW


class TestDirectoryRenameFollowsReadingList:
    def test_descendants_are_rewritten(self, db_connection):
        list_id = create_reading_list()
        flat = create_reading_list_entry(
            list_id, matched_file_path="/data/Batman/Batman 001.cbz"
        )
        nested = create_reading_list_entry(
            list_id,
            issue_number="50",
            manual_override_path="/data/Batman/v2/Batman 050.cbz",
        )
        _set_thumbnail(list_id, "/data/Batman/Batman 001.cbz")

        assert move_path_references(
            "/data/Batman", "/data/Batman (DC)", is_dir=True
        ) is True

        assert _entry(flat)["matched_file_path"] == "/data/Batman (DC)/Batman 001.cbz"
        assert (
            _entry(nested)["manual_override_path"]
            == "/data/Batman (DC)/v2/Batman 050.cbz"
        )
        assert _thumbnail(list_id) == "/data/Batman (DC)/Batman 001.cbz"

    def test_sibling_with_shared_prefix_is_not_rewritten(self, db_connection):
        """The LIKE pattern must be '{old}/%', never '{old}%' -- otherwise
        renaming /data/Batman also rewrites /data/Batman Beyond."""
        sibling = "/data/Batman Beyond/Issue 001.cbz"
        list_id = create_reading_list()
        moved = create_reading_list_entry(
            list_id, matched_file_path="/data/Batman/Batman 001.cbz"
        )
        stayed = create_reading_list_entry(
            list_id, issue_number="2", matched_file_path=sibling
        )

        move_path_references("/data/Batman", "/data/Batman (DC)", is_dir=True)

        assert _entry(moved)["matched_file_path"] == "/data/Batman (DC)/Batman 001.cbz"
        assert _entry(stayed)["matched_file_path"] == sibling


class TestMetadataTagsFollowToo:
    """file_metadata_tags was only rewritten on the single-file branch, and
    without OR REPLACE. Both are fixed by routing through one body."""

    def _tags(self):
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT file_path, kind, value FROM file_metadata_tags "
            "ORDER BY file_path, kind, value"
        ).fetchall()
        conn.close()
        return [(r["file_path"], r["kind"], r["value"]) for r in rows]

    def _tag(self, path, kind="genre", value="Action"):
        conn = get_db_connection()
        conn.execute(
            "INSERT OR REPLACE INTO file_metadata_tags (file_path, kind, value) "
            "VALUES (?, ?, ?)",
            (path, kind, value),
        )
        conn.commit()
        conn.close()

    def test_tags_follow_a_directory_rename(self, db_connection):
        """The directory branch never rewrote this table at all."""
        self._tag("/data/Batman/Batman 001.cbz")

        move_path_references("/data/Batman", "/data/Batman (DC)", is_dir=True)

        assert self._tags() == [
            ("/data/Batman (DC)/Batman 001.cbz", "genre", "Action")
        ]

    def test_rename_onto_existing_tag_rows_does_not_lose_the_rename(
        self, db_connection
    ):
        """PRIMARY KEY (file_path, kind, value): a plain UPDATE raised
        IntegrityError, which aborted the caller's whole transaction --
        including the file_index rename it was there to perform."""
        create_file_index_entry(
            name="Batman 001.cbz", path=OLD, parent="/data/Batman"
        )
        self._tag(OLD)
        self._tag(NEW)

        assert update_file_index_entry(OLD, new_path=NEW) is True

        conn = get_db_connection()
        row = conn.execute(
            "SELECT path FROM file_index WHERE path = ?", (NEW,)
        ).fetchone()
        conn.close()
        assert row is not None, "the file_index rename must survive the collision"
        assert self._tags() == [(NEW, "genre", "Action")]


class TestDeleteClearsTheMapping:
    def test_both_columns_and_the_thumbnail_are_cleared(self, db_connection):
        list_id = create_reading_list()
        entry_id = create_reading_list_entry(
            list_id, matched_file_path=OLD, manual_override_path=OLD
        )
        _set_thumbnail(list_id, OLD)

        assert clear_path_references(OLD) is True

        row = _entry(entry_id)
        assert row["matched_file_path"] is None
        assert row["manual_override_path"] is None
        assert _thumbnail(list_id) is None

    def test_deleting_a_folder_clears_every_descendant(self, db_connection):
        """No is_dir flag: at delete time the path is already gone, so the
        exact match and the prefix sweep both run unconditionally."""
        list_id = create_reading_list()
        flat = create_reading_list_entry(
            list_id, matched_file_path="/data/Batman/Batman 001.cbz"
        )
        nested = create_reading_list_entry(
            list_id,
            issue_number="50",
            manual_override_path="/data/Batman/v2/Batman 050.cbz",
        )

        clear_path_references("/data/Batman")

        assert _entry(flat)["matched_file_path"] is None
        assert _entry(nested)["manual_override_path"] is None

    def test_a_sibling_with_a_shared_prefix_survives(self, db_connection):
        sibling = "/data/Batman Beyond/Issue 001.cbz"
        list_id = create_reading_list()
        stayed = create_reading_list_entry(list_id, matched_file_path=sibling)

        clear_path_references("/data/Batman")

        assert _entry(stayed)["matched_file_path"] == sibling

    def test_reading_history_is_deliberately_kept(self, db_connection):
        """'I read this' is a fact about the user, not about the file -- and a
        trashed comic can be restored."""
        save_reading_position(OLD, page_number=7, total_pages=30)
        mark_issue_read(OLD)

        forget_deleted_path(OLD)

        assert get_reading_position(OLD)["page_number"] == 7
        conn = get_db_connection()
        rows = conn.execute(
            "SELECT issue_path FROM issues_read WHERE issue_path = ?", (OLD,)
        ).fetchall()
        conn.close()
        assert len(rows) == 1

    def test_forget_deleted_path_also_drops_the_index_row(self, db_connection):
        list_id = create_reading_list()
        entry_id = create_reading_list_entry(list_id, matched_file_path=OLD)
        create_file_index_entry(
            name="Batman 001.cbz", path=OLD, parent="/data/Batman"
        )

        forget_deleted_path(OLD)

        conn = get_db_connection()
        row = conn.execute(
            "SELECT path FROM file_index WHERE path = ?", (OLD,)
        ).fetchone()
        conn.close()
        assert row is None
        assert _entry(entry_id)["matched_file_path"] is None

    def test_batch_delete_clears_files_and_folders(self, db_connection):
        list_id = create_reading_list()
        a = create_reading_list_entry(list_id, matched_file_path=OLD)
        b = create_reading_list_entry(
            list_id, issue_number="9", matched_file_path="/data/Gone/Issue 009.cbz"
        )

        forget_deleted_paths([OLD], ["/data/Gone"])

        assert _entry(a)["matched_file_path"] is None
        assert _entry(b)["matched_file_path"] is None


class TestClearedEntryComesBackAsWanted:
    def test_a_deleted_comic_re_wants_its_issue(self, db_connection):
        """Nothing is stored to make this happen: core.wanted_reading_lists
        derives 'unmatched' from these two columns being NULL, which is exactly
        the state clear_path_references leaves behind."""
        from core.wanted_reading_lists import get_reading_list_wanted_items

        list_id = create_reading_list()
        create_reading_list_entry(
            list_id, series="Batman", issue_number="1",
            year=2020, matched_file_path=OLD,
        )
        conn = get_db_connection()
        conn.execute(
            "UPDATE reading_lists SET track_wanted = 1 WHERE id = ?", (list_id,)
        )
        conn.commit()
        conn.close()

        assert get_reading_list_wanted_items() == []

        forget_deleted_path(OLD)

        wanted = get_reading_list_wanted_items()
        assert [(w["series"], str(w["issue_number"])) for w in wanted] == [
            ("Batman", "1")
        ]


class TestConversionIsNotADeletion:
    """CBR->CBZ deletes the old index row and adds a new one. It is a rename
    wearing a delete's clothes, so it must call delete_file_index_entry and
    follow the paths -- never forget_deleted_path, which would throw the
    reading-list mapping away mid-conversion.
    """

    def test_the_mapping_survives_a_conversion(self, db_connection):
        from core.database import add_file_index_entry, delete_file_index_entry

        cbr = "/data/Batman/Batman 001.cbr"
        cbz = "/data/Batman/Batman 001.cbz"
        list_id = create_reading_list()
        entry_id = create_reading_list_entry(list_id, manual_override_path=cbr)
        create_file_index_entry(
            name="Batman 001.cbr", path=cbr, parent="/data/Batman"
        )

        delete_file_index_entry(cbr)
        add_file_index_entry(
            name="Batman 001.cbz", path=cbz, entry_type="file",
            parent="/data/Batman",
        )
        move_path_references(cbr, cbz)

        assert _entry(entry_id)["manual_override_path"] == cbz

    def test_single_file_still_calls_the_plain_delete(self):
        """Pinned structurally so nobody 'tidies' it into forget_deleted_path."""
        source = open(
            os.path.join(
                os.path.dirname(__file__), "..", "..", "cbz_ops", "single_file.py"
            ),
            encoding="utf-8",
        ).read()
        called = {
            node.func.id
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "delete_file_index_entry" in called
        assert "forget_deleted_path" not in called
        assert "move_path_references" in called
