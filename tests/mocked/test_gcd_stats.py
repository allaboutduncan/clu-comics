"""Tests for models.gcd.get_database_stats() against a real temp SQLite DB."""
import pytest
from unittest.mock import patch, MagicMock

from models.gcd import (
    get_database_stats,
    EXPECTED_GCD_TABLES,
    GCD_CORE_TABLES,
)


class TestGetDatabaseStats:

    def test_returns_stats_on_success(self, gcd_configured):
        """Counts every mapped table via COUNT(*) against the test DB."""
        stats = get_database_stats()
        assert stats is not None
        assert stats['series'] == 1
        assert stats['issues'] == 4
        assert stats['stories'] == 1
        assert stats['publishers'] == 1
        assert stats['creators'] == 1
        assert stats['table_count'] == len(EXPECTED_GCD_TABLES)
        assert stats['missing_tables'] == []
        assert stats['core_ok'] is True

    def test_returns_none_when_no_connection(self):
        with patch('models.gcd.get_connection', return_value=None):
            assert get_database_stats() is None

    def test_returns_none_on_query_error(self):
        """Returns None (and closes) when the COUNT query raises."""
        mock_conn = MagicMock()
        mock_conn.cursor.side_effect = Exception("query failed")
        with patch('models.gcd.get_connection', return_value=mock_conn), \
             patch('models.gcd.get_available_gcd_tables', return_value=set(EXPECTED_GCD_TABLES)):
            assert get_database_stats() is None
        mock_conn.close.assert_called_once()

    def test_reports_missing_tables_when_dump_partial(self, gcd_core_only_db_path, monkeypatch):
        """A core-only dump surfaces the missing auxiliary tables."""
        monkeypatch.setattr("models.gcd._get_saved_credentials",
                            lambda: {"database_path": str(gcd_core_only_db_path)})
        stats = get_database_stats()
        assert stats is not None
        assert stats['series'] == 1
        # gcd_creator absent → creators defaults to 0 (key still present)
        assert stats['creators'] == 0
        assert 'gcd_creator' in stats['missing_tables']
        assert 'gcd_story_credit' in stats['missing_tables']
        assert stats['core_ok'] is True  # core tables still present

    def test_core_ok_false_when_core_tables_missing(self, gcd_configured):
        """core_ok is False when a core table is reported missing."""
        available = set(EXPECTED_GCD_TABLES) - {'gcd_issue'}
        with patch('models.gcd.get_available_gcd_tables', return_value=available):
            stats = get_database_stats()
        assert stats is not None
        assert stats['core_ok'] is False
        assert 'gcd_issue' in stats['missing_tables']
