"""Tests for models/gcd.py -- mocked MySQL connection."""
import pytest
from unittest.mock import patch, MagicMock
from tests.mocked.conftest import make_mock_mysql_connection

from models.gcd import EXPECTED_GCD_TABLES


class TestIsMysqlAvailable:

    def test_returns_bool(self):
        from models.gcd import is_mysql_available
        assert isinstance(is_mysql_available(), bool)


class TestCheckMysqlStatus:

    @patch("models.gcd.get_connection_params",
           return_value={"host": "localhost", "port": 3306,
                         "database": "gcd", "username": "root", "password": ""})
    def test_available(self, mock_params):
        from models.gcd import check_mysql_status

        status = check_mysql_status()
        assert status["gcd_mysql_available"] is True

    @patch("models.gcd.get_connection_params", return_value=None)
    def test_not_available(self, mock_params):
        from models.gcd import check_mysql_status

        status = check_mysql_status()
        assert status["gcd_mysql_available"] is False


class TestSearchSeries:

    @patch("models.gcd.MYSQL_AVAILABLE", True)
    @patch("models.gcd.get_connection")
    def test_finds_series(self, mock_conn):
        from models.gcd import search_series

        conn, cursor = make_mock_mysql_connection(rows=[
            {"id": 200, "name": "Batman", "year_began": 1940,
             "year_ended": None, "publisher_id": 10, "publisher_name": "DC Comics"},
        ])
        mock_conn.return_value = conn

        result = search_series("Batman")
        assert result is not None
        assert result["name"] == "Batman"

    @patch("models.gcd.MYSQL_AVAILABLE", True)
    @patch("models.gcd.get_connection")
    def test_no_match(self, mock_conn):
        from models.gcd import search_series

        conn, cursor = make_mock_mysql_connection(rows=[])
        mock_conn.return_value = conn

        assert search_series("NonexistentSeries") is None

    @patch("models.gcd.MYSQL_AVAILABLE", False)
    def test_mysql_unavailable(self):
        from models.gcd import search_series
        assert search_series("Batman") is None


class TestGetIssueMetadata:

    @patch("models.gcd.MYSQL_AVAILABLE", True)
    @patch("models.gcd.get_available_gcd_tables", return_value=set(EXPECTED_GCD_TABLES))
    @patch("models.gcd.get_connection")
    def test_returns_metadata(self, mock_conn, mock_tables):
        from models.gcd import get_issue_metadata

        # get_issue_metadata uses a single cursor for three sequential queries:
        # 1. series query → fetchone (series info)
        # 2. issue query  → fetchone (issue info)
        # 3. credits query → fetchall (credits list)
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            # Series query result
            {"id": 200, "name": "Batman", "year_began": 1940, "publisher_name": "DC Comics"},
            # Issue query result
            {"id": 1, "number": "1", "volume": "1", "title": "Origin",
             "summary": "The origin of Batman", "year": 1940, "month": 4},
        ]
        cursor.fetchall.return_value = [
            {"credit_type": "pencils", "creator_name": "Bob Kane"},
            {"credit_type": "script", "creator_name": "Bill Finger"},
        ]
        conn.cursor.return_value = cursor
        mock_conn.return_value = conn

        result = get_issue_metadata(200, "1")
        assert result is not None
        assert result["Series"] == "Batman"
        assert result["Number"] == "1"
        assert result["Writer"] == "Bill Finger"

    @patch("models.gcd.MYSQL_AVAILABLE", True)
    @patch("models.gcd.get_available_gcd_tables", return_value=set(EXPECTED_GCD_TABLES))
    @patch("models.gcd.get_connection")
    def test_issue_not_found(self, mock_conn, mock_tables):
        from models.gcd import get_issue_metadata

        conn, cursor = make_mock_mysql_connection(fetchone_result=None)
        mock_conn.return_value = conn

        assert get_issue_metadata(200, "999") is None

    @patch("models.gcd.MYSQL_AVAILABLE", True)
    @patch("models.gcd.get_connection")
    def test_falls_back_to_legacy_when_story_credit_missing(self, mock_conn):
        """When gcd_story_credit is absent the normalized query is skipped and
        the legacy text-column fallback runs against gcd_story."""
        from models.gcd import get_issue_metadata

        partial = set(EXPECTED_GCD_TABLES) - {'gcd_story_credit', 'gcd_creator', 'gcd_issue_credit'}
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            # Series query
            {"id": 200, "name": "Batman", "year_began": 1940, "publisher_name": "DC Comics"},
            # Issue query
            {"id": 1, "number": "1", "volume": "1", "title": "Origin",
             "summary": "Origin", "year": 1940, "month": 4},
            # Legacy text-column fallback row
            {"script": "Bill Finger", "pencils": "Bob Kane", "inks": None,
             "colors": None, "letters": None, "editing": None},
        ]
        conn.cursor.return_value = cursor
        mock_conn.return_value = conn

        with patch("models.gcd.get_available_gcd_tables", return_value=partial):
            result = get_issue_metadata(200, "1")

        assert result is not None
        assert result["Writer"] == "Bill Finger"
        assert result["Penciller"] == "Bob Kane"

    @patch("models.gcd.MYSQL_AVAILABLE", True)
    @patch("models.gcd.get_connection")
    def test_no_credits_when_story_table_missing(self, mock_conn):
        """When gcd_story is itself absent, no credits at all are returned and
        no 500 is raised."""
        from models.gcd import get_issue_metadata

        partial = {'gcd_series', 'gcd_issue', 'gcd_publisher', 'stddata_language'}
        conn = MagicMock()
        cursor = MagicMock()
        cursor.fetchone.side_effect = [
            {"id": 200, "name": "Batman", "year_began": 1940, "publisher_name": "DC Comics"},
            {"id": 1, "number": "1", "volume": "1", "title": "Origin",
             "summary": "Origin", "year": 1940, "month": 4},
        ]
        conn.cursor.return_value = cursor
        mock_conn.return_value = conn

        with patch("models.gcd.get_available_gcd_tables", return_value=partial):
            result = get_issue_metadata(200, "1")

        assert result is not None
        # Core fields still populated; credit fields are absent (None values dropped from result).
        assert result["Series"] == "Batman"
        assert "Writer" not in result

    @patch("models.gcd.MYSQL_AVAILABLE", False)
    def test_mysql_unavailable(self):
        from models.gcd import get_issue_metadata
        assert get_issue_metadata(200, "1") is None


class TestGetAvailableGcdTables:
    """The cached helper that detects which expected GCD tables exist."""

    def test_caches_after_first_call(self):
        """Second call must not re-query information_schema."""
        from models.gcd import get_available_gcd_tables, invalidate_gcd_table_cache
        invalidate_gcd_table_cache()

        cursor = MagicMock()
        cursor.fetchall.return_value = [('gcd_series',), ('gcd_issue',)]
        conn = MagicMock()
        conn.cursor.return_value = cursor

        with patch("models.gcd.get_connection", return_value=conn):
            first = get_available_gcd_tables()
            second = get_available_gcd_tables()

        assert first == second == {'gcd_series', 'gcd_issue'}
        # Helper queries information_schema only once across two calls.
        assert cursor.execute.call_count == 1

    def test_force_refresh_requeries(self):
        from models.gcd import get_available_gcd_tables, invalidate_gcd_table_cache
        invalidate_gcd_table_cache()

        cursor = MagicMock()
        cursor.fetchall.side_effect = [
            [('gcd_series',)],
            [('gcd_series',), ('gcd_issue',)],
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
            result = get_available_gcd_tables()

        assert result == set()

    def test_returns_empty_set_when_no_connection(self):
        from models.gcd import get_available_gcd_tables, invalidate_gcd_table_cache
        invalidate_gcd_table_cache()

        with patch("models.gcd.get_connection", return_value=None):
            result = get_available_gcd_tables()

        assert result == set()

    def test_warns_once_when_tables_missing(self, caplog):
        """A single warning should be logged the first time we detect missing tables."""
        import logging
        from models.gcd import get_available_gcd_tables, invalidate_gcd_table_cache
        invalidate_gcd_table_cache()

        cursor = MagicMock()
        # Return a partial schema — gcd_creator and gcd_issue_credit missing.
        present = sorted(set(EXPECTED_GCD_TABLES) - {'gcd_creator', 'gcd_issue_credit'})
        cursor.fetchall.return_value = [(t,) for t in present]
        conn = MagicMock()
        conn.cursor.return_value = cursor

        with caplog.at_level(logging.WARNING, logger="app_logger"):
            with patch("models.gcd.get_connection", return_value=conn):
                get_available_gcd_tables()
                get_available_gcd_tables()
                get_available_gcd_tables()

        warnings = [r for r in caplog.records if "missing from dump" in r.getMessage()]
        assert len(warnings) == 1


class TestValidateIssue:

    @patch("models.gcd.MYSQL_AVAILABLE", True)
    @patch("models.gcd.get_connection")
    def test_valid_issue(self, mock_conn):
        from models.gcd import validate_issue

        conn, cursor = make_mock_mysql_connection(
            fetchone_result={"id": 1, "number": "1", "title": "Origin"}
        )
        mock_conn.return_value = conn

        result = validate_issue(200, "1")
        assert result["success"] is True
        assert result["valid"] is True

    @patch("models.gcd.MYSQL_AVAILABLE", True)
    @patch("models.gcd.get_connection")
    def test_invalid_issue(self, mock_conn):
        from models.gcd import validate_issue

        conn, cursor = make_mock_mysql_connection(fetchone_result=None)
        mock_conn.return_value = conn

        result = validate_issue(200, "999")
        assert result["success"] is True
        assert result["valid"] is False
