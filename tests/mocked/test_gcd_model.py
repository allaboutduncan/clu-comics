"""Tests for models/gcd.py -- mocked MySQL connection."""
import pytest
from unittest.mock import patch, MagicMock
from tests.mocked.conftest import make_mock_mysql_connection


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
    @patch("models.gcd.get_connection")
    def test_returns_metadata(self, mock_conn):
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
    @patch("models.gcd.get_connection")
    def test_issue_not_found(self, mock_conn):
        from models.gcd import get_issue_metadata

        conn, cursor = make_mock_mysql_connection(fetchone_result=None)
        mock_conn.return_value = conn

        assert get_issue_metadata(200, "999") is None

    @patch("models.gcd.MYSQL_AVAILABLE", False)
    def test_mysql_unavailable(self):
        from models.gcd import get_issue_metadata
        assert get_issue_metadata(200, "1") is None


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
