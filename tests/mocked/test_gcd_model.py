"""Tests for models/gcd.py -- runs the ported SQL against a real temp SQLite DB."""
import sqlite3
import pytest
from unittest.mock import patch, MagicMock

from models.gcd import EXPECTED_GCD_TABLES, GCD_CORE_TABLES
from tests.mocked.conftest import build_gcd_sqlite


@pytest.fixture
def not_configured(monkeypatch):
    """No saved credentials and no env var -> GCD is unconfigured."""
    monkeypatch.setattr("models.gcd._get_saved_credentials", lambda: None)
    monkeypatch.delenv("GCD_DATABASE_PATH", raising=False)


class TestDatabaseStatus:

    def test_available_when_file_exists(self, gcd_configured):
        from models.gcd import check_database_status, is_database_available
        status = check_database_status()
        assert status["gcd_available"] is True
        assert status["gcd_path_configured"] is True
        assert is_database_available() is True

    def test_not_available_when_unconfigured(self, not_configured):
        from models.gcd import check_database_status, is_database_available
        status = check_database_status()
        assert status["gcd_available"] is False
        assert status["gcd_path_configured"] is False
        assert is_database_available() is False

    def test_not_available_when_path_missing(self, tmp_path, monkeypatch):
        from models.gcd import check_database_status
        missing = tmp_path / "nope.db"
        monkeypatch.setattr("models.gcd._get_saved_credentials",
                            lambda: {"database_path": str(missing)})
        status = check_database_status()
        assert status["gcd_available"] is False
        # Path is configured but the file is absent.
        assert status["gcd_path_configured"] is True

    def test_env_var_fallback(self, gcd_db_path, monkeypatch):
        from models.gcd import get_connection_params
        monkeypatch.setattr("models.gcd._get_saved_credentials", lambda: None)
        monkeypatch.setenv("GCD_DATABASE_PATH", str(gcd_db_path))
        params = get_connection_params()
        assert params == {"database_path": str(gcd_db_path)}


class TestGetConnection:

    def test_opens_read_only(self, gcd_configured):
        from models.gcd import get_connection
        conn = get_connection()
        assert conn is not None
        try:
            # Read-only: writes must fail.
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE should_fail (x INTEGER)")
        finally:
            conn.close()

    def test_regexp_registered(self, gcd_configured):
        from models.gcd import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            # Case-sensitive by design (search_series lowercases both operands).
            cur.execute("SELECT 'Batman' REGEXP ? AS m", ("Bat",))
            assert cur.fetchone()["m"] == 1
            cur.execute("SELECT 'Batman' REGEXP ? AS m", ("^zzz",))
            assert cur.fetchone()["m"] == 0
        finally:
            conn.close()

    def test_none_when_unconfigured(self, not_configured):
        from models.gcd import get_connection
        assert get_connection() is None


class TestSearchSeries:

    def test_finds_series(self, gcd_configured):
        from models.gcd import search_series
        result = search_series("Batman")
        assert result is not None
        assert result["name"] == "Batman"
        assert result["year_began"] == 1940

    def test_finds_series_with_year(self, gcd_configured):
        from models.gcd import search_series
        result = search_series("Batman", year=1945)
        assert result is not None
        assert result["name"] == "Batman"

    def test_no_match(self, gcd_configured):
        from models.gcd import search_series
        assert search_series("NonexistentSeries") is None

    def test_not_configured(self, not_configured):
        from models.gcd import search_series
        assert search_series("Batman") is None


class TestGetIssueMetadata:

    def test_returns_metadata(self, gcd_configured):
        from models.gcd import get_issue_metadata
        result = get_issue_metadata(200, "1")
        assert result is not None
        assert result["Series"] == "Batman"
        assert result["Number"] == "1"
        assert result["Publisher"] == "DC Comics"
        assert result["Title"] == "The Beginning"
        assert result["Year"] == 1940
        assert result["Month"] == 4
        # Normalized credit: Bob Kane credited as 'script' -> Writer.
        assert result["Writer"] == "Bob Kane"

    def test_issue_not_found(self, gcd_configured):
        from models.gcd import get_issue_metadata
        assert get_issue_metadata(200, "999") is None

    def test_core_only_db_has_no_credits(self, gcd_core_only_db_path, monkeypatch):
        """A dump missing the credit tables still returns core fields."""
        from models.gcd import get_issue_metadata
        monkeypatch.setattr("models.gcd._get_saved_credentials",
                            lambda: {"database_path": str(gcd_core_only_db_path)})
        result = get_issue_metadata(200, "1")
        assert result is not None
        assert result["Series"] == "Batman"
        assert "Writer" not in result  # None values are dropped

    def test_falls_back_to_legacy_text_columns(self, tmp_path, monkeypatch):
        """When gcd_story_credit is absent, legacy text columns on gcd_story win."""
        from models.gcd import get_issue_metadata
        path = build_gcd_sqlite(tmp_path / "legacy.db", core_only=True)
        conn = sqlite3.connect(path)
        conn.execute("UPDATE gcd_story SET script = 'Bill Finger', pencils = 'Bob Kane' WHERE id = 900")
        conn.commit()
        conn.close()
        monkeypatch.setattr("models.gcd._get_saved_credentials",
                            lambda: {"database_path": path})
        result = get_issue_metadata(200, "1")
        assert result is not None
        assert result["Writer"] == "Bill Finger"
        assert result["Penciller"] == "Bob Kane"

    def test_not_configured(self, not_configured):
        from models.gcd import get_issue_metadata
        assert get_issue_metadata(200, "1") is None


class TestGetAvailableGcdTables:
    """The cached helper that detects which expected GCD tables exist."""

    def test_full_db_reports_all_tables(self, gcd_configured):
        from models.gcd import get_available_gcd_tables
        assert get_available_gcd_tables() == set(EXPECTED_GCD_TABLES)

    def test_core_only_db_reports_core(self, gcd_core_only_db_path, monkeypatch):
        from models.gcd import get_available_gcd_tables
        monkeypatch.setattr("models.gcd._get_saved_credentials",
                            lambda: {"database_path": str(gcd_core_only_db_path)})
        present = get_available_gcd_tables()
        assert GCD_CORE_TABLES.issubset(present)
        assert 'gcd_story_credit' not in present

    def _mock_conn(self, table_names):
        cursor = MagicMock()
        cursor.fetchall.return_value = [{'name': t} for t in table_names]
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn, cursor

    def test_caches_after_first_call(self):
        from models.gcd import get_available_gcd_tables, invalidate_gcd_table_cache
        invalidate_gcd_table_cache()
        conn, cursor = self._mock_conn(['gcd_series', 'gcd_issue'])
        with patch("models.gcd.get_connection", return_value=conn):
            first = get_available_gcd_tables()
            second = get_available_gcd_tables()
        assert first == second == {'gcd_series', 'gcd_issue'}
        assert cursor.execute.call_count == 1

    def test_force_refresh_requeries(self):
        from models.gcd import get_available_gcd_tables, invalidate_gcd_table_cache
        invalidate_gcd_table_cache()
        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [{'name': 'gcd_series'}],
            [{'name': 'gcd_series'}, {'name': 'gcd_issue'}],
        ]
        conn = MagicMock()
        conn.cursor.return_value = cursor
        with patch("models.gcd.get_connection", return_value=conn):
            first = get_available_gcd_tables()
            second = get_available_gcd_tables(force_refresh=True)
        assert first == {'gcd_series'}
        assert second == {'gcd_series', 'gcd_issue'}
        assert cursor.execute.call_count == 2

    def test_returns_empty_set_on_error(self):
        from models.gcd import get_available_gcd_tables, invalidate_gcd_table_cache
        invalidate_gcd_table_cache()
        conn = MagicMock()
        conn.cursor.side_effect = Exception("boom")
        with patch("models.gcd.get_connection", return_value=conn):
            assert get_available_gcd_tables() == set()

    def test_returns_empty_set_when_no_connection(self):
        from models.gcd import get_available_gcd_tables, invalidate_gcd_table_cache
        invalidate_gcd_table_cache()
        with patch("models.gcd.get_connection", return_value=None):
            assert get_available_gcd_tables() == set()

    def test_warns_once_when_tables_missing(self, caplog):
        import logging
        from models.gcd import get_available_gcd_tables, invalidate_gcd_table_cache
        invalidate_gcd_table_cache()
        present = sorted(set(EXPECTED_GCD_TABLES) - {'gcd_creator', 'gcd_issue_credit'})
        conn, cursor = self._mock_conn(present)
        with caplog.at_level(logging.WARNING, logger="app_logger"):
            with patch("models.gcd.get_connection", return_value=conn):
                get_available_gcd_tables()
                get_available_gcd_tables()
                get_available_gcd_tables()
        warnings = [r for r in caplog.records if "missing from dump" in r.getMessage()]
        assert len(warnings) == 1


class TestGetDatabaseStats:

    def test_counts(self, gcd_configured):
        from models.gcd import get_database_stats
        stats = get_database_stats()
        assert stats is not None
        assert stats["series"] == 1
        assert stats["issues"] == 4
        assert stats["stories"] == 1
        assert stats["publishers"] == 1
        assert stats["creators"] == 1
        assert stats["core_ok"] is True
        assert stats["missing_tables"] == []

    def test_none_when_unconfigured(self, not_configured):
        from models.gcd import get_database_stats
        assert get_database_stats() is None


class TestValidateIssue:

    def test_valid_issue(self, gcd_configured):
        from models.gcd import validate_issue
        result = validate_issue(200, "1")
        assert result["success"] is True
        assert result["valid"] is True

    def test_invalid_issue(self, gcd_configured):
        from models.gcd import validate_issue
        result = validate_issue(200, "999")
        assert result["success"] is True
        assert result["valid"] is False

    def test_missing_args(self):
        from models.gcd import validate_issue
        result = validate_issue(None, None)
        assert result["success"] is False
