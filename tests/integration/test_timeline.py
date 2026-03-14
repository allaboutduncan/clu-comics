"""Tests for models/timeline.py -- reading timeline and streak calculation."""
import pytest
from tests.factories.db_factories import create_issue_read


class TestReadingTimeline:

    def test_returns_structure(self, populated_db):
        from models.timeline import get_reading_timeline

        result = get_reading_timeline(limit=50)
        assert result is not None
        assert "stats" in result
        assert "timeline" in result
        assert "total_read" in result["stats"]
        assert "streak" in result["stats"]

    def test_total_read_count(self, populated_db):
        from models.timeline import get_reading_timeline

        result = get_reading_timeline()
        assert result["stats"]["total_read"] >= 3  # 3 reads in populated_db

    def test_timeline_has_entries(self, populated_db):
        from models.timeline import get_reading_timeline

        result = get_reading_timeline()
        # Should have at least one date group
        assert len(result["timeline"]) >= 1
        # Each group should have entries
        for group in result["timeline"]:
            assert "date" in group
            assert "entries" in group

    def test_respects_limit(self, populated_db):
        from models.timeline import get_reading_timeline

        result = get_reading_timeline(limit=1)
        assert result is not None

    def test_empty_db(self, db_connection):
        from models.timeline import get_reading_timeline

        result = get_reading_timeline()
        assert result is not None
        assert result["stats"]["total_read"] == 0
        assert result["timeline"] == []

    def test_hidden_entries_excluded(self, db_connection):
        from models.timeline import get_reading_timeline
        from core.database import hide_issue_from_history

        path1 = create_issue_read(issue_path="/data/A.cbz")
        path2 = create_issue_read(issue_path="/data/B.cbz")
        path3 = create_issue_read(issue_path="/data/C.cbz")

        # All 3 should appear
        result = get_reading_timeline()
        assert result["stats"]["total_read"] == 3

        # Hide one
        hide_issue_from_history(path2)

        result = get_reading_timeline()
        assert result["stats"]["total_read"] == 2
        all_paths = [
            entry["issue_path"]
            for group in result["timeline"]
            for entry in group["entries"]
        ]
        assert path2 not in all_paths
        assert path1 in all_paths
        assert path3 in all_paths


class TestHiddenEntriesInStats:
    """Verify that stats queries still include hidden entries."""

    def test_reading_totals_include_hidden(self, db_connection):
        from core.database import get_reading_totals, hide_issue_from_history

        create_issue_read(issue_path="/data/A.cbz", page_count=24, time_spent=600)
        create_issue_read(issue_path="/data/B.cbz", page_count=30, time_spent=800)

        hide_issue_from_history("/data/A.cbz")

        totals = get_reading_totals()
        # Stats should still include hidden entries
        assert totals["total_pages"] == 54
        assert totals["total_time"] == 1400

    def test_reading_stats_by_year_include_hidden(self, db_connection):
        from core.database import get_reading_stats_by_year, hide_issue_from_history

        create_issue_read(issue_path="/data/A.cbz", page_count=24)
        create_issue_read(issue_path="/data/B.cbz", page_count=30)

        hide_issue_from_history("/data/A.cbz")

        stats = get_reading_stats_by_year()
        assert stats["total_read"] == 2
        assert stats["total_pages"] == 54
