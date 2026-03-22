"""Tests for sync_reading_list_entries database function."""
import pytest
from unittest.mock import patch, MagicMock


class TestSyncReadingListEntries:

    @patch("core.database.get_db_connection")
    def test_sync_adds_new_entries(self, mock_conn):
        from core.database import sync_reading_list_entries

        # Mock existing entries (empty)
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_connection = MagicMock()
        mock_connection.row_factory = None
        mock_connection.cursor.return_value = mock_cursor
        mock_conn.return_value = mock_connection

        new_entries = [
            {"series": "Batman", "issue_number": "1", "volume": "2016", "year": "2016", "matched_file_path": "/data/Batman 001.cbz"},
            {"series": "Batman", "issue_number": "2", "volume": "2016", "year": "2016", "matched_file_path": None},
        ]

        result = sync_reading_list_entries(1, new_entries)
        assert result is not None
        assert result["added"] == 2
        assert result["removed"] == 0

    @patch("core.database.get_db_connection")
    def test_sync_removes_old_entries(self, mock_conn):
        from core.database import sync_reading_list_entries

        # Use plain dicts that dict() can handle
        existing = [
            {"id": 10, "series": "Batman", "issue_number": "1", "volume": "2016", "year": "2016",
             "manual_override_path": None, "sort_order": 0},
            {"id": 11, "series": "Batman", "issue_number": "2", "volume": "2016", "year": "2016",
             "manual_override_path": None, "sort_order": 1},
        ]
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = existing

        mock_connection = MagicMock()
        mock_connection.cursor.return_value = mock_cursor
        mock_conn.return_value = mock_connection

        # New entries: only issue 1, issue 2 should be removed
        new_entries = [
            {"series": "Batman", "issue_number": "1", "volume": "2016", "year": "2016", "matched_file_path": None},
        ]

        result = sync_reading_list_entries(1, new_entries)
        assert result is not None
        assert result["removed"] == 1

    @patch("core.database.get_db_connection")
    def test_sync_preserves_manual_overrides(self, mock_conn):
        from core.database import sync_reading_list_entries

        existing = [
            {"id": 10, "series": "Batman", "issue_number": "1", "volume": "2016", "year": "2016",
             "manual_override_path": "/data/custom/Batman 001.cbz", "sort_order": 0},
        ]
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = existing

        mock_connection = MagicMock()
        mock_connection.cursor.return_value = mock_cursor
        mock_conn.return_value = mock_connection

        # New entries: empty - would normally remove Batman #1, but it has manual override
        result = sync_reading_list_entries(1, [], preserve_manual=True)
        assert result is not None
        assert result["removed"] == 0
