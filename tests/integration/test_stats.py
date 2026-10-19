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
        from core.database import get_cached_stats

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


class TestLibraryRootsNotHardcoded:
    """Publishers are the top-level folders of each library, not of /data.

    A library at /comics used to report zero publishers and zero root folders,
    because the queries matched ``parent = '/data'`` literally.
    """

    @pytest.fixture
    def comics_library(self, db_connection):
        from core.database import add_file_index_entry, get_db_connection

        # Raw insert rather than add_library(): that normpaths the root, which
        # on a Windows dev box turns '/comics' into '\comics'.
        conn = get_db_connection()
        try:
            conn.execute("DELETE FROM libraries")
            conn.execute(
                "INSERT INTO libraries (name, path, enabled) VALUES ('Comics', '/comics', 1)"
            )
            conn.commit()
        finally:
            conn.close()

        def add(name, path, kind, parent):
            assert add_file_index_entry(
                name=name, path=path, entry_type=kind,
                size=100 if kind == "file" else None, parent=parent,
                modified_at=1_700_000_000.0,
            )

        add("Image", "/comics/Image", "directory", "/comics")
        add("Saga", "/comics/Image/Saga", "directory", "/comics/Image")
        for i in (1, 2):
            add(f"Saga 00{i}.cbz", f"/comics/Image/Saga/Saga 00{i}.cbz",
                "file", "/comics/Image/Saga")
        add("Loose 001.cbz", "/comics/Loose 001.cbz", "file", "/comics")

    def test_root_folders_counted_under_a_non_data_library(self, comics_library):
        from models.stats import get_library_stats

        assert get_library_stats()["root_folders"] == 1

    def test_top_publishers_found_under_a_non_data_library(self, comics_library):
        from models.stats import get_top_publishers

        pubs = get_top_publishers(limit=10)
        assert [p["name"] for p in pubs] == ["Image"]

    def test_top_series_exclude_the_library_root(self, comics_library):
        from models.stats import get_top_series_by_count

        series = get_top_series_by_count(limit=10)
        assert len(series) == 1
        assert series[0]["count"] == 2
