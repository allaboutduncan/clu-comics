"""Tests for database schema -- init_db, tables, indexes, WAL, FK, migrations, idempotency."""
import pytest
import sqlite3
from unittest.mock import patch


class TestInitDb:

    def test_init_db_returns_true(self, db_connection):
        """init_db() already ran via the db_connection fixture; verify the DB is usable."""
        cur = db_connection.execute("SELECT 1")
        assert cur.fetchone()[0] == 1

    def test_init_db_idempotent(self, db_path):
        """Calling init_db() twice should not raise or corrupt data."""
        with patch("core.database.get_db_path", return_value=db_path):
            from core.database import init_db
            assert init_db() is True
            assert init_db() is True

    def test_wal_mode_enabled(self, db_connection):
        cur = db_connection.execute("PRAGMA journal_mode")
        mode = cur.fetchone()[0]
        assert mode == "wal"

    def test_foreign_keys_enabled(self, db_connection):
        cur = db_connection.execute("PRAGMA foreign_keys")
        assert cur.fetchone()[0] == 1


class TestTablesExist:

    EXPECTED_TABLES = [
        "thumbnail_jobs",
        "recent_files",
        "file_index",
        "rebuild_schedule",
        "sync_schedule",
        "getcomics_schedule",
        "weekly_packs_config",
        "weekly_packs_history",
        "wanted_issues",
        "browse_cache",
        "favorite_series",
        "reading_lists",
        "reading_list_entries",
        "issues_read",
        "to_read",
        "stats_cache",
        "user_preferences",
        "user_settings",
        "reading_positions",
        "publishers",
        "series",
        "issues",
        "collection_status",
        "issue_manual_status",
        "libraries",
        "provider_credentials",
        "library_providers",
        "provider_cache",
        "komga_sync_config",
        "komga_sync_log",
        "komga_library_mappings",
        "schedules",
        "download_clients",
        "indexers",
        "dcpp_jobs",
    ]

    @pytest.mark.parametrize("table_name", EXPECTED_TABLES)
    def test_table_exists(self, db_connection, table_name):
        cur = db_connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        assert cur.fetchone() is not None, f"Table '{table_name}' does not exist"

    def test_dropped_tables_are_gone(self, db_connection):
        """favorite_publishers should be dropped during migration."""
        cur = db_connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='favorite_publishers'"
        )
        assert cur.fetchone() is None


class TestFileIndexColumns:

    def test_core_columns(self, db_connection):
        cur = db_connection.execute("PRAGMA table_info(file_index)")
        columns = {row[1] for row in cur.fetchall()}
        expected = {
            "id", "name", "path", "type", "size", "parent",
            "has_thumbnail", "modified_at", "last_updated", "first_indexed_at",
            "has_comicinfo",
        }
        assert expected.issubset(columns)

    def test_metadata_columns(self, db_connection):
        cur = db_connection.execute("PRAGMA table_info(file_index)")
        columns = {row[1] for row in cur.fetchall()}
        metadata_cols = {
            "ci_title", "ci_series", "ci_number", "ci_count", "ci_volume",
            "ci_year", "ci_writer", "ci_penciller", "ci_inker", "ci_colorist",
            "ci_letterer", "ci_coverartist", "ci_publisher", "ci_genre",
            "ci_characters", "metadata_scanned_at",
            # Indexed copy of <MetronId>, so the library can be joined against
            # Metron's "issues modified since X" feed without opening archives.
            "ci_metronid",
        }
        assert metadata_cols.issubset(columns)


class TestWeeklyPacksHistoryColumns:
    """download_id links a history row to its live api.download_progress entry,
    which is what lets a stuck 'queued'/'downloading' row be reconciled."""

    def test_core_columns(self, db_connection):
        cur = db_connection.execute("PRAGMA table_info(weekly_packs_history)")
        columns = {row[1] for row in cur.fetchall()}
        expected = {
            "id", "pack_date", "publisher", "format", "download_url",
            "status", "downloaded_at", "download_id",
        }
        assert expected.issubset(columns)

    def test_download_id_migration_is_idempotent(self, db_path):
        """The ALTER TABLE must not run twice on an existing install."""
        with patch("core.database.get_db_path", return_value=db_path):
            from core.database import init_db
            assert init_db() is True

        conn = sqlite3.connect(db_path)
        try:
            cols = [row[1] for row in conn.execute("PRAGMA table_info(weekly_packs_history)")]
        finally:
            conn.close()
        assert cols.count("download_id") == 1


class TestDcppJobsColumns:
    """The crash-recovery ledger for in-flight AirDC++ bundles.

    ``target`` is the load-bearing column: with "remove finished bundles"
    enabled, the last target seen while a bundle was running is the only
    remaining signal of where a completed file went.
    """

    def test_core_columns(self, db_connection):
        cur = db_connection.execute("PRAGMA table_info(dcpp_jobs)")
        columns = {row[1] for row in cur.fetchall()}
        expected = {
            "download_id", "client_type", "client_id", "filename", "series",
            "issue", "status", "error", "percent", "stage", "bytes_total",
            "bytes_downloaded", "target", "created_at", "updated_at",
        }
        assert expected.issubset(columns)

    def test_one_row_per_bundle(self, db_connection):
        # Two tracking ids for the same AirDC++ bundle would double-import it.
        db_connection.execute(
            "INSERT INTO dcpp_jobs (download_id, client_type, client_id, filename) "
            "VALUES ('d1', 'airdcpp', 'b1', 'Batman 1.cbz')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            db_connection.execute(
                "INSERT INTO dcpp_jobs (download_id, client_type, client_id, filename) "
                "VALUES ('d2', 'airdcpp', 'b1', 'Batman 1.cbz')"
            )


class TestIndexesExist:

    EXPECTED_INDEXES = [
        "idx_file_index_name",
        "idx_file_index_parent",
        "idx_file_index_type",
        "idx_file_index_path",
        "idx_file_index_metadata_scan",
        "idx_file_index_characters",
        "idx_file_index_writer",
        "idx_file_index_first_indexed",
        "idx_issues_read_path",
        "idx_reading_positions_path",
        "idx_favorite_series_path",
        "idx_reading_list_entries_list_id",
        "idx_to_read_path",
        "idx_publishers_path",
        "idx_publishers_favorite",
        "idx_series_cv_id",
        "idx_series_gcd_id",
        "idx_series_mapped_path",
        "idx_issues_series_id",
        "idx_issues_store_date",
        "idx_collection_status_series",
        "idx_issue_manual_status_series",
        "idx_libraries_path",
        "idx_libraries_enabled",
        "idx_library_providers_library",
        "idx_provider_cache_lookup",
        "idx_provider_cache_expires",
        "idx_wanted_issues_series",
        "idx_browse_cache_path",
        "idx_komga_sync_book",
        "idx_indexers_priority",
        "idx_dcpp_jobs_status",
    ]

    @pytest.mark.parametrize("index_name", EXPECTED_INDEXES)
    def test_index_exists(self, db_connection, index_name):
        cur = db_connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        )
        assert cur.fetchone() is not None, f"Index '{index_name}' does not exist"


class TestDefaultData:

    def test_rebuild_schedule_default(self, db_connection):
        cur = db_connection.execute("SELECT frequency, time FROM rebuild_schedule WHERE id=1")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "disabled"
        assert row[1] == "02:00"

    def test_sync_schedule_default(self, db_connection):
        cur = db_connection.execute("SELECT frequency FROM sync_schedule WHERE id=1")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "disabled"

    def test_getcomics_schedule_default(self, db_connection):
        cur = db_connection.execute("SELECT frequency FROM getcomics_schedule WHERE id=1")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "disabled"

    def test_weekly_packs_config_default(self, db_connection):
        cur = db_connection.execute("SELECT enabled, format FROM weekly_packs_config WHERE id=1")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0  # disabled
        assert row[1] == "JPG"

    def test_komga_sync_config_default(self, db_connection):
        cur = db_connection.execute("SELECT server_url FROM komga_sync_config WHERE id=1")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == ""

    def test_schedules_table_populated(self, db_connection):
        cur = db_connection.execute("SELECT name FROM schedules ORDER BY name")
        names = [row[0] for row in cur.fetchall()]
        assert "rebuild" in names
        assert "sync" in names
        assert "getcomics" in names
        assert "weekly_packs" in names
        assert "komga" in names


class TestForeignKeys:

    def test_reading_list_cascade_delete(self, db_connection):
        """Deleting a reading_list should cascade to entries."""
        from core.database import create_reading_list, add_reading_list_entry, delete_reading_list

        list_id = create_reading_list("Cascade Test")
        add_reading_list_entry(list_id, {"series": "Batman", "issue_number": "1"})

        cur = db_connection.execute(
            "SELECT COUNT(*) FROM reading_list_entries WHERE reading_list_id=?",
            (list_id,),
        )
        assert cur.fetchone()[0] == 1

        delete_reading_list(list_id)

        cur = db_connection.execute(
            "SELECT COUNT(*) FROM reading_list_entries WHERE reading_list_id=?",
            (list_id,),
        )
        assert cur.fetchone()[0] == 0

    def test_series_delete_cascades_issues(self, db_connection):
        """Deleting a series row should cascade to issues via FK."""
        from core.database import save_publisher, save_series_mapping, save_issue

        save_publisher(999, "CascadePub")
        series_data = {
            "id": 999,
            "name": "Cascade Series",
            "sort_name": "Cascade Series",
            "volume": 2020,
            "status": "Ongoing",
            "publisher": {"id": 999},
            "imprint": None,
            "year_began": 2020,
            "year_end": None,
            "desc": "test",
            "cv_id": None,
            "gcd_id": None,
            "resource_url": None,
        }
        save_series_mapping(series_data, "/data/Cascade")
        save_issue({"id": 9991, "number": "1"}, 999)

        cur = db_connection.execute("SELECT COUNT(*) FROM issues WHERE series_id=999")
        assert cur.fetchone()[0] == 1

        # Direct DELETE triggers ON DELETE CASCADE on issues
        db_connection.execute("PRAGMA foreign_keys=ON")
        db_connection.execute("DELETE FROM series WHERE id=999")
        db_connection.commit()

        cur = db_connection.execute("SELECT COUNT(*) FROM issues WHERE series_id=999")
        assert cur.fetchone()[0] == 0


class TestCollectionStatusBoundaryPurge:
    """One-time purge of caches written by the pre-boundary issue matcher.

    generate_filename_pattern had no leading digit boundary, so issue #1 matched
    "Nightwing 051 (2016).cbz". The cached rows survive the code fix, so init_db
    drops them once, guarded by a user_preferences marker.
    """

    def _seed_poisoned_cache(self, conn):
        from tests.factories.db_factories import create_series, create_issue

        series_id = create_series(name="Nightwing", mapped_path="/data/Nightwing")
        issue_id = create_issue(series_id=series_id, number="1")

        conn.execute(
            "INSERT INTO collection_status "
            "(series_id, issue_id, issue_number, found, file_path, matched_via) "
            "VALUES (?, ?, '1', 1, '/data/Nightwing/Nightwing 051 (2016).cbz', 'pattern')",
            (series_id, issue_id),
        )
        conn.execute(
            "INSERT INTO wanted_issues (series_id, issue_id, issue_number) "
            "VALUES (?, ?, '2')",
            (series_id, issue_id),
        )
        conn.execute(
            "DELETE FROM user_preferences WHERE key = 'collection_status_boundary_purge'"
        )
        conn.commit()
        return series_id, issue_id

    def _counts(self, conn):
        cs = conn.execute("SELECT COUNT(*) FROM collection_status").fetchone()[0]
        wi = conn.execute("SELECT COUNT(*) FROM wanted_issues").fetchone()[0]
        return cs, wi

    def test_purges_poisoned_rows_and_sets_marker(self, db_connection, db_path):
        self._seed_poisoned_cache(db_connection)

        with patch("core.database.get_db_path", return_value=db_path):
            from core.database import init_db
            assert init_db() is True

        assert self._counts(db_connection) == (0, 0)
        marker = db_connection.execute(
            "SELECT value FROM user_preferences WHERE key='collection_status_boundary_purge'"
        ).fetchone()
        assert marker is not None

    def test_does_not_repurge_once_marked(self, db_connection, db_path):
        series_id, issue_id = self._seed_poisoned_cache(db_connection)

        with patch("core.database.get_db_path", return_value=db_path):
            from core.database import init_db
            init_db()

            # Rows written *after* the purge are freshly matched and must survive
            # a later startup.
            db_connection.execute(
                "INSERT INTO collection_status "
                "(series_id, issue_id, issue_number, found, file_path, matched_via) "
                "VALUES (?, ?, '51', 1, '/data/Nightwing/Nightwing 051 (2016).cbz', 'pattern')",
                (series_id, issue_id),
            )
            db_connection.execute(
                "INSERT INTO wanted_issues (series_id, issue_id, issue_number) "
                "VALUES (?, ?, '1')",
                (series_id, issue_id),
            )
            db_connection.commit()

            init_db()

        assert self._counts(db_connection) == (1, 1)


class TestDownloadClientGroupMigration:
    """download_clients.client_group was added after the table shipped."""

    def test_column_exists(self, db_connection):
        cols = [r[1] for r in
                db_connection.execute("PRAGMA table_info(download_clients)").fetchall()]
        assert "client_group" in cols

    def test_migration_backfills_existing_rows_as_usenet(self, db_connection):
        from core.database import init_db

        # Rebuild the table as it existed before DC++ support, with a row in it.
        db_connection.execute("DROP TABLE download_clients")
        db_connection.execute("""
            CREATE TABLE download_clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_type TEXT NOT NULL UNIQUE,
                config_encrypted BLOB NOT NULL,
                config_nonce BLOB NOT NULL,
                is_active INTEGER DEFAULT 0,
                is_valid INTEGER DEFAULT 0,
                last_tested TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        db_connection.execute(
            "INSERT INTO download_clients (client_type, config_encrypted, config_nonce, is_active)"
            " VALUES ('sabnzbd', X'00', X'00', 1)"
        )
        db_connection.commit()

        init_db()

        row = db_connection.execute(
            "SELECT client_group, is_active FROM download_clients WHERE client_type='sabnzbd'"
        ).fetchone()
        # The pre-existing Usenet client keeps working and lands in the right group.
        assert row["client_group"] == "usenet"
        assert row["is_active"] == 1
