"""The problem_files table and its indexes.

A brand-new table, so `CREATE TABLE IF NOT EXISTS` is the whole migration --
but init_db() runs on every start, so it has to be idempotent against a
database that already has the table and rows in it.
"""

from unittest.mock import patch

import pytest


class TestSchema:
    def test_table_has_the_expected_columns(self, db_connection):
        cols = {
            row[1] for row in db_connection.execute("PRAGMA table_info(problem_files)")
        }
        assert cols == {
            "id",
            "path",
            "source",
            "error_class",
            "error_message",
            "first_seen",
            "last_seen",
            "occurrences",
            "file_mtime",
            "dismissed_at",
        }

    def test_path_and_source_are_unique_together(self, db_connection):
        """Identity is (path, source): one file can be broken two ways at once."""
        db_connection.execute(
            "INSERT INTO problem_files (path, source) VALUES ('/data/x.cbz', 'thumbnail')"
        )
        db_connection.execute(
            "INSERT INTO problem_files (path, source) VALUES ('/data/x.cbz', 'rebuild')"
        )
        db_connection.commit()

        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            db_connection.execute(
                "INSERT INTO problem_files (path, source) "
                "VALUES ('/data/x.cbz', 'thumbnail')"
            )

    def test_indexes_exist(self, db_connection):
        names = {
            row[0]
            for row in db_connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='problem_files'"
            )
        }
        assert "idx_problem_files_path" in names
        assert "idx_problem_files_open" in names

    def test_occurrences_defaults_to_one(self, db_connection):
        db_connection.execute(
            "INSERT INTO problem_files (path, source) VALUES ('/data/x.cbz', 'thumbnail')"
        )
        db_connection.commit()
        row = db_connection.execute(
            "SELECT occurrences, dismissed_at FROM problem_files"
        ).fetchone()
        assert row[0] == 1
        assert row[1] is None

    def test_init_db_is_idempotent_over_existing_rows(self, db_connection, db_path):
        db_connection.execute(
            "INSERT INTO problem_files (path, source, error_class) "
            "VALUES ('/data/x.cbz', 'thumbnail', 'BadZipFile')"
        )
        db_connection.commit()

        with patch("core.database.get_db_path", return_value=db_path):
            from core.database import init_db, wait_for_background_analyze

            init_db()
            wait_for_background_analyze()

        row = db_connection.execute(
            "SELECT error_class FROM problem_files WHERE path = '/data/x.cbz'"
        ).fetchone()
        assert row[0] == "BadZipFile"
