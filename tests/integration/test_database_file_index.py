"""Tests for file_index CRUD -- add, search, sync, directory children, path counts."""
import pytest
import time
from tests.factories.db_factories import create_file_index_entry, create_directory_entry


class TestAddFileIndexEntry:

    def test_add_and_retrieve(self, db_connection):
        from database import add_file_index_entry, get_file_index_entry_by_path

        ok = add_file_index_entry(
            name="Batman 001.cbz",
            path="/data/DC/Batman/Batman 001.cbz",
            entry_type="file",
            size=50_000_000,
            parent="/data/DC/Batman",
        )
        assert ok is True

        entry = get_file_index_entry_by_path("/data/DC/Batman/Batman 001.cbz")
        assert entry is not None
        assert entry["path"] == "/data/DC/Batman/Batman 001.cbz"

    def test_upsert_preserves_first_indexed(self, db_connection):
        """Second add on same path should preserve first_indexed_at."""
        from database import add_file_index_entry

        add_file_index_entry(
            name="Comic.cbz", path="/data/X/Comic.cbz",
            entry_type="file", size=100, parent="/data/X",
        )

        cur = db_connection.execute(
            "SELECT first_indexed_at FROM file_index WHERE path=?",
            ("/data/X/Comic.cbz",),
        )
        first_time = cur.fetchone()[0]

        time.sleep(0.05)

        add_file_index_entry(
            name="Comic.cbz", path="/data/X/Comic.cbz",
            entry_type="file", size=200, parent="/data/X",
        )

        cur = db_connection.execute(
            "SELECT first_indexed_at, size FROM file_index WHERE path=?",
            ("/data/X/Comic.cbz",),
        )
        row = cur.fetchone()
        assert row[0] == first_time  # first_indexed_at unchanged
        assert row[1] == 200  # size updated

    def test_add_directory(self, db_connection):
        from database import add_file_index_entry

        ok = add_file_index_entry(
            name="Batman", path="/data/DC/Batman",
            entry_type="directory", parent="/data/DC",
        )
        assert ok is True

    def test_factory_creates_entry(self, db_connection):
        path = create_file_index_entry(name="Test.cbz")
        cur = db_connection.execute(
            "SELECT name FROM file_index WHERE path=?", (path,)
        )
        assert cur.fetchone()[0] == "Test.cbz"


class TestDeleteFileIndexEntry:

    def test_delete_single(self, db_connection):
        from database import add_file_index_entry, delete_file_index_entry

        add_file_index_entry("X.cbz", "/data/X.cbz", "file", parent="/data")
        delete_file_index_entry("/data/X.cbz")

        cur = db_connection.execute(
            "SELECT COUNT(*) FROM file_index WHERE path='/data/X.cbz'"
        )
        assert cur.fetchone()[0] == 0

    def test_delete_directory_cascades_children(self, db_connection):
        from database import add_file_index_entry, delete_file_index_entry

        add_file_index_entry("Series", "/data/Series", "directory", parent="/data")
        add_file_index_entry("A.cbz", "/data/Series/A.cbz", "file", parent="/data/Series")
        add_file_index_entry("B.cbz", "/data/Series/B.cbz", "file", parent="/data/Series")

        delete_file_index_entry("/data/Series")

        cur = db_connection.execute("SELECT COUNT(*) FROM file_index WHERE parent='/data/Series'")
        assert cur.fetchone()[0] == 0

    def test_delete_nonexistent_returns_false(self, db_connection):
        from database import delete_file_index_entry

        ok = delete_file_index_entry("/data/no/such/path.cbz")
        assert ok is False


class TestBatchDelete:

    def test_delete_multiple(self, db_connection):
        from database import add_file_index_entry, delete_file_index_entries

        for i in range(5):
            add_file_index_entry(f"f{i}.cbz", f"/data/f{i}.cbz", "file", parent="/data")

        count = delete_file_index_entries([f"/data/f{i}.cbz" for i in range(3)])
        assert count == 3

        cur = db_connection.execute("SELECT COUNT(*) FROM file_index WHERE parent='/data'")
        assert cur.fetchone()[0] == 2

    def test_empty_list(self, db_connection):
        from database import delete_file_index_entries

        assert delete_file_index_entries([]) == 0


class TestSearchFileIndex:

    def test_finds_by_partial_name(self, db_connection):
        from database import search_file_index

        create_file_index_entry(name="Batman 001 (2020).cbz")
        create_file_index_entry(name="Spider-Man 001 (2018).cbz")

        results = search_file_index("Batman")
        assert len(results) == 1
        assert results[0]["name"] == "Batman 001 (2020).cbz"

    def test_case_insensitive(self, db_connection):
        from database import search_file_index

        create_file_index_entry(name="BATMAN 001.cbz")
        results = search_file_index("batman")
        assert len(results) == 1

    def test_respects_limit(self, db_connection):
        from database import search_file_index

        for i in range(20):
            create_file_index_entry(name=f"Comic {i:03d}.cbz")

        results = search_file_index("Comic", limit=5)
        assert len(results) == 5

    def test_empty_query_returns_all(self, db_connection):
        from database import search_file_index

        create_file_index_entry(name="A.cbz")
        create_file_index_entry(name="B.cbz")

        results = search_file_index("")
        assert len(results) >= 2

    def test_no_match_returns_empty(self, db_connection):
        from database import search_file_index

        create_file_index_entry(name="Batman.cbz")
        results = search_file_index("xyznonexistent")
        assert results == []

    def test_result_has_expected_keys(self, db_connection):
        from database import search_file_index

        create_file_index_entry(name="Test.cbz", size=12345)
        results = search_file_index("Test")
        assert len(results) == 1
        r = results[0]
        assert "name" in r
        assert "path" in r
        assert "type" in r
        assert "parent" in r


class TestGetDirectoryChildren:

    def test_returns_dirs_and_files(self, db_connection):
        from database import get_directory_children

        create_directory_entry(name="SubDir", path="/data/Parent/SubDir", parent="/data/Parent")
        create_file_index_entry(name="A.cbz", path="/data/Parent/A.cbz", parent="/data/Parent")
        create_file_index_entry(name="B.cbz", path="/data/Parent/B.cbz", parent="/data/Parent")

        dirs, files = get_directory_children("/data/Parent")
        assert len(dirs) == 1
        assert dirs[0]["name"] == "SubDir"
        assert len(files) == 2

    def test_empty_directory(self, db_connection):
        from database import get_directory_children

        dirs, files = get_directory_children("/data/empty")
        assert dirs == []
        assert files == []

    def test_directory_entries_have_has_thumbnail(self, db_connection):
        from database import get_directory_children

        create_directory_entry(name="WithThumb", path="/data/X/WithThumb", parent="/data/X")

        dirs, _ = get_directory_children("/data/X")
        assert "has_thumbnail" in dirs[0]

    def test_file_entries_have_size(self, db_connection):
        from database import get_directory_children

        create_file_index_entry(name="F.cbz", path="/data/Y/F.cbz", parent="/data/Y", size=999)

        _, files = get_directory_children("/data/Y")
        assert files[0]["size"] == 999


class TestSyncFileIndexIncremental:

    def test_adds_new_entries(self, db_connection):
        from database import sync_file_index_incremental

        entries = [
            {"name": "A.cbz", "path": "/data/A.cbz", "type": "file",
             "size": 100, "parent": "/data", "has_thumbnail": 0, "modified_at": time.time()},
            {"name": "B.cbz", "path": "/data/B.cbz", "type": "file",
             "size": 200, "parent": "/data", "has_thumbnail": 0, "modified_at": time.time()},
        ]

        result = sync_file_index_incremental(entries)
        assert result["added"] == 2
        assert result["removed"] == 0

    def test_removes_orphaned_entries(self, db_connection):
        from database import sync_file_index_incremental

        create_file_index_entry(name="Old.cbz", path="/data/Old.cbz", parent="/data")

        # Sync with empty list -> Old.cbz should be removed
        result = sync_file_index_incremental([])
        assert result["removed"] == 1
        assert result["added"] == 0

    def test_preserves_existing_entries(self, db_connection):
        from database import sync_file_index_incremental

        create_file_index_entry(name="Keep.cbz", path="/data/Keep.cbz", parent="/data")

        entries = [
            {"name": "Keep.cbz", "path": "/data/Keep.cbz", "type": "file",
             "size": 100, "parent": "/data", "has_thumbnail": 0, "modified_at": time.time()},
        ]

        result = sync_file_index_incremental(entries)
        assert result["unchanged"] == 1
        assert result["added"] == 0
        assert result["removed"] == 0

    def test_mixed_add_remove_keep(self, db_connection):
        from database import sync_file_index_incremental

        create_file_index_entry(name="Keep.cbz", path="/data/Keep.cbz", parent="/data")
        create_file_index_entry(name="Remove.cbz", path="/data/Remove.cbz", parent="/data")

        entries = [
            {"name": "Keep.cbz", "path": "/data/Keep.cbz", "type": "file",
             "size": 100, "parent": "/data", "has_thumbnail": 0, "modified_at": time.time()},
            {"name": "New.cbz", "path": "/data/New.cbz", "type": "file",
             "size": 300, "parent": "/data", "has_thumbnail": 0, "modified_at": time.time()},
        ]

        result = sync_file_index_incremental(entries)
        assert result["added"] == 1
        assert result["removed"] == 1
        assert result["unchanged"] == 1


class TestGetPathCounts:

    def test_counts_files_and_dirs(self, db_connection):
        from database import get_path_counts

        create_directory_entry(name="Series1", path="/data/Pub/Series1", parent="/data/Pub")
        create_file_index_entry(name="A.cbz", path="/data/Pub/Series1/A.cbz", parent="/data/Pub/Series1")
        create_file_index_entry(name="B.cbz", path="/data/Pub/Series1/B.cbz", parent="/data/Pub/Series1")

        folders, files = get_path_counts("/data/Pub")
        assert folders == 1
        assert files == 2

    def test_empty_path(self, db_connection):
        from database import get_path_counts

        folders, files = get_path_counts("/data/nonexistent")
        assert folders == 0
        assert files == 0

    def test_batch_counts(self, db_connection):
        from database import get_path_counts_batch

        create_directory_entry(name="S1", path="/data/A/S1", parent="/data/A")
        create_file_index_entry(name="f.cbz", path="/data/A/S1/f.cbz", parent="/data/A/S1")
        create_file_index_entry(name="g.cbz", path="/data/B/g.cbz", parent="/data/B")

        result = get_path_counts_batch(["/data/A", "/data/B"])
        assert result["/data/A"] == (1, 1)
        assert result["/data/B"] == (0, 1)


class TestClearFileIndex:

    def test_clears_all(self, db_connection):
        from database import clear_file_index_from_db

        create_file_index_entry(name="A.cbz")
        create_file_index_entry(name="B.cbz")

        ok = clear_file_index_from_db()
        assert ok is True

        cur = db_connection.execute("SELECT COUNT(*) FROM file_index")
        assert cur.fetchone()[0] == 0


class TestSaveFileIndexBulk:

    def test_bulk_save(self, db_connection):
        from database import save_file_index_to_db

        entries = [
            {"name": f"C{i}.cbz", "path": f"/data/C{i}.cbz", "type": "file",
             "size": i * 100, "parent": "/data", "has_thumbnail": 0}
            for i in range(10)
        ]

        ok = save_file_index_to_db(entries)
        assert ok is True

        cur = db_connection.execute("SELECT COUNT(*) FROM file_index")
        assert cur.fetchone()[0] == 10

    def test_bulk_save_replaces_existing(self, db_connection):
        from database import save_file_index_to_db

        create_file_index_entry(name="Old.cbz")

        entries = [
            {"name": "New.cbz", "path": "/data/New.cbz", "type": "file",
             "size": 100, "parent": "/data"},
        ]

        save_file_index_to_db(entries)

        cur = db_connection.execute("SELECT COUNT(*) FROM file_index")
        assert cur.fetchone()[0] == 1  # Old entry cleared

        cur = db_connection.execute("SELECT name FROM file_index")
        assert cur.fetchone()[0] == "New.cbz"


class TestFileMetadata:

    def test_update_file_metadata(self, db_connection):
        from database import add_file_index_entry, update_file_metadata

        add_file_index_entry("Test.cbz", "/data/Test.cbz", "file", parent="/data")

        cur = db_connection.execute("SELECT id FROM file_index WHERE path='/data/Test.cbz'")
        file_id = cur.fetchone()[0]

        ok = update_file_metadata(
            file_id,
            {"ci_title": "Batman", "ci_writer": "Tom King", "ci_publisher": "DC"},
            scanned_at=time.time(),
            has_comicinfo=1,
        )
        assert ok is True

        cur = db_connection.execute(
            "SELECT ci_title, ci_writer, ci_publisher, has_comicinfo FROM file_index WHERE id=?",
            (file_id,),
        )
        row = cur.fetchone()
        assert row[0] == "Batman"
        assert row[1] == "Tom King"
        assert row[2] == "DC"
        assert row[3] == 1
