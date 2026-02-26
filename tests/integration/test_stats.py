"""Tests for models/stats.py -- stats queries against populated DB."""
import pytest


class TestLibraryStats:

    def test_returns_stats_dict(self, populated_db):
        from models.stats import get_library_stats

        stats = get_library_stats()
        assert stats is not None
        assert "total_files" in stats
        assert "total_directories" in stats
        assert "total_read" in stats
        assert stats["total_files"] >= 10  # 5 batman + 5 spidey
        assert stats["total_read"] >= 3

    def test_caches_result(self, populated_db):
        from models.stats import get_library_stats
        from database import get_cached_stats

        get_library_stats()
        cached = get_cached_stats("library_stats")
        assert cached is not None


class TestFileTypeDistribution:

    def test_returns_distribution(self, populated_db):
        from models.stats import get_file_type_distribution

        dist = get_file_type_distribution()
        assert dist is not None


class TestTopPublishers:

    def test_returns_list(self, populated_db):
        from models.stats import get_top_publishers

        pubs = get_top_publishers(limit=10)
        assert pubs is not None


class TestReadingHistoryStats:

    def test_returns_history(self, populated_db):
        from models.stats import get_reading_history_stats

        stats = get_reading_history_stats()
        assert stats is not None


class TestLargestComics:

    def test_returns_list(self, populated_db):
        from models.stats import get_largest_comics

        comics = get_largest_comics(limit=5)
        assert comics is not None
        assert len(comics) <= 5


class TestTopSeriesByCount:

    def test_returns_list(self, populated_db):
        from models.stats import get_top_series_by_count

        series = get_top_series_by_count(limit=5)
        assert series is not None
