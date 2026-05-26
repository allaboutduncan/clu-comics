"""Unit tests for models.gcd.get_database_stats() with mocked MySQL."""
import pytest
from unittest.mock import patch, MagicMock

from models.gcd import (
    get_database_stats,
    EXPECTED_GCD_TABLES,
    GCD_CORE_TABLES,
)


class TestGetDatabaseStats:
    """Test get_database_stats with mocked connections."""

    def _make_conn(self, fetchall_rows):
        cursor = MagicMock()
        cursor.fetchall.return_value = fetchall_rows
        conn = MagicMock()
        conn.cursor.return_value = cursor
        return conn

    def test_returns_stats_on_success(self):
        """Returns a dict with mapped table counts when all tables present."""
        rows = [
            ('gcd_series', 250000),
            ('gcd_issue', 2500000),
            ('gcd_story', 3000000),
            ('gcd_publisher', 50000),
            ('gcd_creator', 400000),
        ]
        mock_conn = self._make_conn(rows)

        with patch('models.gcd.get_connection', return_value=mock_conn), \
             patch('models.gcd.get_available_gcd_tables', return_value=set(EXPECTED_GCD_TABLES)):
            stats = get_database_stats()

        assert stats is not None
        assert stats['series'] == 250000
        assert stats['issues'] == 2500000
        assert stats['stories'] == 3000000
        assert stats['publishers'] == 50000
        assert stats['creators'] == 400000
        assert stats['table_count'] == len(EXPECTED_GCD_TABLES)
        assert stats['missing_tables'] == []
        assert stats['core_ok'] is True
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

        with patch('models.gcd.get_connection', return_value=mock_conn), \
             patch('models.gcd.get_available_gcd_tables', return_value=set(EXPECTED_GCD_TABLES)):
            stats = get_database_stats()

        assert stats is None
        mock_conn.close.assert_called_once()

    def test_reports_missing_tables_when_dump_partial(self):
        """Surfaces missing_tables / core_ok when the dump excludes tables."""
        # Simulate the May 2026 GCD dump: gcd_creator and gcd_issue_credit absent.
        available = set(EXPECTED_GCD_TABLES) - {'gcd_creator', 'gcd_issue_credit'}
        rows = [
            ('gcd_series', 100),
            ('gcd_issue', 200),
            ('gcd_story', 300),
            ('gcd_publisher', 50),
        ]
        mock_conn = self._make_conn(rows)

        with patch('models.gcd.get_connection', return_value=mock_conn), \
             patch('models.gcd.get_available_gcd_tables', return_value=available):
            stats = get_database_stats()

        assert stats is not None
        assert stats['series'] == 100
        # gcd_creator missing → creators defaults to 0 (key still present)
        assert stats['creators'] == 0
        assert 'gcd_creator' in stats['missing_tables']
        assert 'gcd_issue_credit' in stats['missing_tables']
        assert stats['core_ok'] is True  # core tables (series/issue/publisher/story/stddata_language) still here

    def test_core_ok_false_when_core_tables_missing(self):
        """core_ok is False when a core table (gcd_issue) is absent."""
        available = set(EXPECTED_GCD_TABLES) - {'gcd_issue'}
        mock_conn = self._make_conn([])

        with patch('models.gcd.get_connection', return_value=mock_conn), \
             patch('models.gcd.get_available_gcd_tables', return_value=available):
            stats = get_database_stats()

        assert stats is not None
        assert stats['core_ok'] is False
        assert 'gcd_issue' in stats['missing_tables']
