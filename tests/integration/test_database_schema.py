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
        with patch("database.get_db_path", return_value=db_path):
            from database import init_db
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
        }
        assert metadata_cols.issubset(columns)


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
        from database import create_reading_list, add_reading_list_entry, delete_reading_list

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
        from database import save_publisher, save_series_mapping, save_issue

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
