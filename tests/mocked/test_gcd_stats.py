"""Unit tests for models.gcd.get_database_stats() with mocked MySQL."""
import pytest
from unittest.mock import patch, MagicMock

from models.gcd import get_database_stats


class TestGetDatabaseStats:
    """Test get_database_stats with mocked connections."""

    def test_returns_stats_on_success(self):
        """Returns a dict with mapped table counts."""
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ('gcd_series', 250000),
            ('gcd_issue', 2500000),
            ('gcd_story', 3000000),
            ('gcd_publisher', 50000),
            ('gcd_creator', 400000),
        ]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch('models.gcd.get_connection', return_value=mock_conn):
            stats = get_database_stats()

        assert stats is not None
        assert stats['series'] == 250000
        assert stats['issues'] == 2500000
        assert stats['stories'] == 3000000
        assert stats['publishers'] == 50000
        assert stats['creators'] == 400000
        assert stats['table_count'] == 5
        mock_conn.close.assert_called_once()

    def test_returns_none_when_no_connection(self):
        """Returns None when get_connection() fails."""
        with patch('models.gcd.get_connection', return_value=None):
            stats = get_database_stats()

        assert stats is None

    def test_returns_none_on_query_error(self):
        """Returns None when the SQL query raises an exception."""
        mock_conn = MagicMock()
        mock_conn.cursor.side_effect = Exception("query failed")

        with patch('models.gcd.get_connection', return_value=mock_conn):
            stats = get_database_stats()

        assert stats is None
        mock_conn.close.assert_called_once()

    def test_handles_partial_tables(self):
        """Works when only some expected tables exist."""
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ('gcd_series', 100),
            ('gcd_issue', 200),
        ]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor

        with patch('models.gcd.get_connection', return_value=mock_conn):
            stats = get_database_stats()

        assert stats is not None
        assert stats['series'] == 100
        assert stats['issues'] == 200
        assert 'stories' not in stats
        assert stats['table_count'] == 2
