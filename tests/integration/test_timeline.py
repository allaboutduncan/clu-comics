"""Tests for models/timeline.py -- reading timeline and streak calculation."""
import pytest


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
