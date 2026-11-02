"""Folders deleted or moved outside CLU must leave the index (#693).

The reporter reorganised their library in Krusader: series moved out of
top-level publisher folders into combined ones, then the old folders deleted.
Browse hid them, but Missing XML, Library Health duplicates and its thumbnail
failures kept listing thousands of files that were no longer on disk.

Each class pins one half of the fix, and each "keeps" test pins the evidence
rule that stops the fix from emptying the index the first time a NAS sleeps.
"""

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.factories.db_factories import (
    create_directory_entry,
    create_file_index_entry,
    create_reading_list,
    create_reading_list_entry,
    create_reading_position,
)


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    (root / "Marvel").mkdir()  # mounted: a mounted root is never empty
    with patch("helpers.library.get_library_roots", return_value=[root.as_posix()]):
        yield root.as_posix()


def _paths(conn):
    return {r[0] for r in conn.execute("SELECT path FROM file_index").fetchall()}


def _entry_paths(conn, entry_id):
    return conn.execute(
        "SELECT matched_file_path, manual_override_path FROM reading_list_entries "
        "WHERE id = ?", (entry_id,),
    ).fetchone()


class TestIsPathProvablyGone:
    def test_a_file_under_a_deleted_folder_in_a_mounted_library(self, library):
        from helpers.library import is_path_provably_gone

        assert is_path_provably_gone(f"{library}/Gone/Series/X 001.cbz") is True

    def test_an_existing_path_is_not_gone(self, library):
        from helpers.library import is_path_provably_gone

        assert is_path_provably_gone(f"{library}/Marvel") is False

    def test_an_empty_root_proves_nothing(self, tmp_path):
        """An unmounted bind mount is an empty directory, not a deleted library."""
        from helpers.library import is_path_provably_gone

        root = tmp_path / "asleep"
        root.mkdir()
        with patch("helpers.library.get_library_roots", return_value=[root.as_posix()]):
            assert is_path_provably_gone(f"{root.as_posix()}/A/B.cbz") is False

    def test_a_missing_root_proves_nothing(self, tmp_path):
        from helpers.library import is_path_provably_gone

        root = (tmp_path / "unmounted").as_posix()
        with patch("helpers.library.get_library_roots", return_value=[root]):
            assert is_path_provably_gone(f"{root}/A/B.cbz") is False
            assert is_path_provably_gone(root) is False

    def test_outside_a_library_the_parent_must_exist(self, library, tmp_path):
        from helpers.library import is_path_provably_gone

        watch = tmp_path / "watch"
        watch.mkdir()
        assert is_path_provably_gone(f"{watch.as_posix()}/x.cbz") is True
        assert is_path_provably_gone(f"{watch.as_posix()}/gone/x.cbz") is False


class TestPruneVanishedIndexDirs:
    def test_a_deleted_top_level_folder_leaves_with_its_subtree(self, db_connection, library):
        from core.database import prune_vanished_index_dirs

        keep = create_directory_entry(name="Marvel", parent=library)
        top = create_directory_entry(name="Fantographics", parent=library)
        series = create_directory_entry(name="Hate", parent=top)
        comic = create_file_index_entry(name="Hate 001.cbz", parent=series)

        result = prune_vanished_index_dirs()

        assert result == {"dirs": 1, "rows": 3}
        assert _paths(db_connection) == {keep}
        assert comic not in _paths(db_connection)

    def test_a_file_whose_folder_row_is_already_gone(self, db_connection, library):
        """The reported state: browse no longer showed the folder, so only the
        comics' own rows were left. The parent column is still evidence."""
        from core.database import prune_vanished_index_dirs

        comic = create_file_index_entry(
            name="Hate 001.cbz", parent=f"{library}/Fantographics/Hate"
        )
        prune_vanished_index_dirs()
        assert comic not in _paths(db_connection)

    def test_an_underscore_does_not_reach_a_sibling(self, db_connection, library):
        """LIKE reads "_" as any character: /X_Men/% must not match /X-Men/."""
        from core.database import prune_vanished_index_dirs

        os.mkdir(f"{library}/X-Men")
        live = create_file_index_entry(name="X-Men 001.cbz", parent=f"{library}/X-Men")
        create_directory_entry(name="X_Men", parent=library)

        prune_vanished_index_dirs()

        assert live in _paths(db_connection)

    def test_an_empty_root_keeps_everything(self, db_connection, tmp_path):
        from core.database import prune_vanished_index_dirs

        root = tmp_path / "asleep"
        root.mkdir()
        comic = create_file_index_entry(
            name="A 001.cbz", parent=f"{root.as_posix()}/Series"
        )
        with patch("helpers.library.get_library_roots", return_value=[root.as_posix()]):
            assert prune_vanished_index_dirs() == {"dirs": 0, "rows": 0}
        assert comic in _paths(db_connection)

    def test_reading_list_matches_under_it_become_gaps(self, db_connection, library):
        from core.database import prune_vanished_index_dirs

        comic = create_file_index_entry(
            name="Hate 001.cbz", parent=f"{library}/Fantographics/Hate"
        )
        list_id = create_reading_list()
        entry = create_reading_list_entry(list_id, matched_file_path=comic)

        prune_vanished_index_dirs()

        assert _entry_paths(db_connection, entry)[0] is None


class TestSyncLeavesUnwalkedRootsAlone:
    def test_rows_under_a_skipped_root_survive(self, db_connection):
        from core.database import sync_file_index_incremental

        asleep = create_file_index_entry(name="A.cbz", parent="/nas/Series")
        stale = create_file_index_entry(name="B.cbz", parent="/data/Series")

        result = sync_file_index_incremental([], walked_roots=["/data"])

        assert result["removed"] == 1
        assert _paths(db_connection) == {asleep}
        assert stale not in _paths(db_connection)

    def test_no_walked_root_removes_nothing(self, db_connection):
        from core.database import sync_file_index_incremental

        create_file_index_entry(name="A.cbz", parent="/data/Series")
        assert sync_file_index_incremental([], walked_roots=[])["removed"] == 0


class TestMoveIndexedPath:
    def test_a_folder_move_rewrites_rows_and_references(self, db_connection):
        from core.database import move_indexed_path

        old = create_directory_entry(name="Hate", parent="/data/Fantographics")
        comic = create_file_index_entry(name="Hate 001.cbz", parent=old)
        create_reading_position(comic_path=comic)

        move_indexed_path(old, "/data/Combined/Hate", is_dir=True)

        assert _paths(db_connection) == {
            "/data/Combined/Hate", "/data/Combined/Hate/Hate 001.cbz",
        }
        row = db_connection.execute(
            "SELECT parent FROM file_index WHERE path = ?",
            ("/data/Combined/Hate/Hate 001.cbz",),
        ).fetchone()
        assert row[0] == "/data/Combined/Hate"
        assert db_connection.execute(
            "SELECT comic_path FROM reading_positions"
        ).fetchone()[0] == "/data/Combined/Hate/Hate 001.cbz"

    def test_an_already_indexed_destination_does_not_strand_the_source(self, db_connection):
        """The UNIQUE(path) conflict used to abort the rewrite and keep both."""
        from core.database import move_indexed_path

        old = create_file_index_entry(name="Hate 001.cbz", parent="/data/Old")
        new = create_file_index_entry(name="Hate 001.cbz", parent="/data/New")

        move_indexed_path(old, new, is_dir=False)

        assert _paths(db_connection) == {new}

    def test_a_sibling_with_a_longer_name_is_untouched(self, db_connection):
        from core.database import move_indexed_path

        old = create_directory_entry(name="Batman", parent="/data")
        sibling = create_file_index_entry(name="BB 001.cbz", parent="/data/Batman Beyond")

        move_indexed_path(old, "/data/Batman (2016)", is_dir=True)

        assert sibling in _paths(db_connection)


class TestFileWatcherFollowsMoves:
    def _handler(self):
        from core.file_watcher import DebouncedFileHandler

        handler = DebouncedFileHandler(debounce_seconds=60)
        handler._add_event = lambda path: None
        return handler

    def test_a_file_moved_outside_clu_is_not_indexed_twice(self, db_connection, library):
        old = create_file_index_entry(name="Hate 001.cbz", parent=f"{library}/Old")
        new = f"{library}/New/Hate 001.cbz"

        self._handler().on_moved(
            SimpleNamespace(src_path=old, dest_path=new, is_directory=False)
        )

        assert _paths(db_connection) == {new}

    def test_a_folder_move_carries_its_subtree(self, db_connection, library):
        old = create_directory_entry(name="Hate", parent=f"{library}/Fantographics")
        create_file_index_entry(name="Hate 001.cbz", parent=old)
        new = f"{library}/Combined/Hate"

        self._handler().on_moved(
            SimpleNamespace(src_path=old, dest_path=new, is_directory=True)
        )

        assert _paths(db_connection) == {new, f"{new}/Hate 001.cbz"}

    def test_moving_out_of_the_library_is_a_deletion(self, db_connection, library, tmp_path):
        old = create_file_index_entry(name="Hate 001.cbz", parent=f"{library}/Old")

        self._handler().on_moved(
            SimpleNamespace(
                src_path=old, dest_path=f"{tmp_path.as_posix()}/elsewhere/Hate 001.cbz",
                is_directory=False,
            )
        )

        assert _paths(db_connection) == set()


class TestProblemRowsUnderADeletedFolder:
    def test_prune_once_the_library_is_mounted(self, db_connection, library):
        from core.problem_files import SOURCE_THUMBNAIL, list_problems, record_problem

        record_problem(
            f"{library}/Fantographics/Hate/Hate 001.cbz", SOURCE_THUMBNAIL,
            error_class="BadZipFile", error_message="bad",
        )
        assert list_problems() == []

    def test_kept_while_the_root_is_empty(self, db_connection, tmp_path):
        from core.problem_files import SOURCE_THUMBNAIL, list_problems, record_problem

        root = tmp_path / "asleep"
        root.mkdir()
        with patch("helpers.library.get_library_roots", return_value=[root.as_posix()]):
            record_problem(
                f"{root.as_posix()}/Series/A 001.cbz", SOURCE_THUMBNAIL,
                error_class="BadZipFile", error_message="bad",
            )
            rows = list_problems()
        assert len(rows) == 1
        assert rows[0]["reachable"] is False
