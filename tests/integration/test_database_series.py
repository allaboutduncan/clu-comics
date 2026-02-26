"""Tests for series/issues -- CRUD, collection status, wanted issues."""
import pytest
from tests.factories.db_factories import create_publisher, create_series, create_issue


class TestPublisherCRUD:

    def test_save_and_get(self, db_connection):
        from database import save_publisher, get_publisher

        save_publisher(publisher_id=1, name="DC Comics", path="/data/DC")
        pub = get_publisher(1)
        assert pub is not None
        assert pub["name"] == "DC Comics"

    def test_get_all_publishers(self, db_connection):
        from database import get_all_publishers

        create_publisher(publisher_id=1, name="DC")
        create_publisher(publisher_id=2, name="Marvel")

        pubs = get_all_publishers()
        assert len(pubs) >= 2

    def test_delete_publisher(self, db_connection):
        from database import save_publisher, delete_publisher, get_publisher

        save_publisher(99, "DeleteMe")
        assert get_publisher(99) is not None

        delete_publisher(99)
        assert get_publisher(99) is None

    def test_update_publisher_logo(self, db_connection):
        from database import save_publisher, update_publisher_logo, get_publisher

        save_publisher(50, "LogoTest")
        update_publisher_logo(50, "/images/logo.png")

        pub = get_publisher(50)
        assert pub["logo"] == "/images/logo.png"


class TestPublisherFavorites:

    def test_set_favorite(self, db_connection):
        from database import save_publisher_path, set_publisher_favorite, is_favorite_publisher

        save_publisher_path("/data/DC", name="DC Comics")
        set_publisher_favorite("/data/DC", favorite=True)
        assert is_favorite_publisher("/data/DC") is True

    def test_remove_favorite(self, db_connection):
        from database import save_publisher_path, set_publisher_favorite, is_favorite_publisher

        save_publisher_path("/data/DC", name="DC")
        set_publisher_favorite("/data/DC", favorite=True)
        set_publisher_favorite("/data/DC", favorite=False)
        assert is_favorite_publisher("/data/DC") is False

    def test_get_favorites(self, db_connection):
        from database import save_publisher_path, set_publisher_favorite, get_favorite_publishers

        save_publisher_path("/data/DC", name="DC")
        save_publisher_path("/data/Marvel", name="Marvel")
        set_publisher_favorite("/data/DC", favorite=True)

        favs = get_favorite_publishers()
        paths = [f["publisher_path"] for f in favs]
        assert "/data/DC" in paths
        assert "/data/Marvel" not in paths


class TestSeriesFavorites:

    def test_add_and_check(self, db_connection):
        from database import add_favorite_series, is_favorite_series

        add_favorite_series("/data/DC/Batman")
        assert is_favorite_series("/data/DC/Batman") is True

    def test_remove(self, db_connection):
        from database import add_favorite_series, remove_favorite_series, is_favorite_series

        add_favorite_series("/data/DC/Batman")
        remove_favorite_series("/data/DC/Batman")
        assert is_favorite_series("/data/DC/Batman") is False

    def test_get_all(self, db_connection):
        from database import add_favorite_series, get_favorite_series

        add_favorite_series("/data/DC/Batman")
        add_favorite_series("/data/Marvel/Spider-Man")

        favs = get_favorite_series()
        assert len(favs) == 2


class TestSeriesMapping:

    def test_save_and_get(self, db_connection):
        from database import get_series_mapping, get_series_by_id

        pub_id = create_publisher(publisher_id=1, name="DC")
        series_id = create_series(
            series_id=100, name="Batman", publisher_id=pub_id,
            mapped_path="/data/DC/Batman",
        )

        mapping = get_series_mapping(100)
        assert mapping is not None

        series = get_series_by_id(100)
        assert series["name"] == "Batman"

    def test_get_all_mapped_series(self, db_connection):
        from database import get_all_mapped_series

        pub_id = create_publisher(publisher_id=1, name="DC")
        create_series(series_id=100, name="Batman", publisher_id=pub_id)
        create_series(series_id=101, name="Superman", publisher_id=pub_id)

        all_series = get_all_mapped_series()
        assert len(all_series) >= 2

    def test_update_series_desc(self, db_connection):
        from database import update_series_desc, get_series_by_id

        pub_id = create_publisher()
        create_series(series_id=500, name="TestSeries", publisher_id=pub_id)

        update_series_desc(500, "A great new description")
        series = get_series_by_id(500)
        assert series["desc"] == "A great new description"

    def test_remove_series_mapping(self, db_connection):
        from database import remove_series_mapping, get_series_by_id

        pub_id = create_publisher()
        create_series(series_id=600, name="RemoveMe", publisher_id=pub_id)

        ok = remove_series_mapping(600)
        assert ok is True
        # remove_series_mapping clears mapped_path but keeps the series row
        series = get_series_by_id(600)
        assert series is not None
        assert series["mapped_path"] is None

    def test_tracked_series_lookup(self, db_connection):
        from database import get_tracked_series_lookup

        pub_id = create_publisher()
        create_series(series_id=100, name="Batman", publisher_id=pub_id, volume=2020)

        lookup = get_tracked_series_lookup()
        assert isinstance(lookup, set)
        # Returns set of (normalized_name, volume) tuples
        names = {name for name, vol in lookup}
        assert "batman" in names


class TestIssueCRUD:

    def test_save_and_get(self, db_connection):
        from database import get_issue_by_id

        pub_id = create_publisher()
        series_id = create_series(publisher_id=pub_id)
        issue_id = create_issue(series_id=series_id, number="5")

        issue = get_issue_by_id(issue_id)
        assert issue is not None
        assert issue["number"] == "5"

    def test_get_issues_for_series(self, db_connection):
        from database import get_issues_for_series

        pub_id = create_publisher()
        series_id = create_series(publisher_id=pub_id)
        for i in range(1, 6):
            create_issue(series_id=series_id, number=str(i))

        issues = get_issues_for_series(series_id)
        assert len(issues) == 5

    def test_save_issues_bulk(self, db_connection):
        from database import save_issues_bulk, get_issues_for_series

        pub_id = create_publisher()
        series_id = create_series(publisher_id=pub_id)

        issues = [
            {"id": 9000 + i, "number": str(i), "issue_name": f"Issue {i}",
             "cover_date": "2020-01-01", "store_date": "2020-01-01"}
            for i in range(1, 11)
        ]

        count = save_issues_bulk(issues, series_id)
        assert count == 10

        saved = get_issues_for_series(series_id)
        assert len(saved) == 10

    def test_delete_issues_for_series(self, db_connection):
        from database import delete_issues_for_series, get_issues_for_series

        pub_id = create_publisher()
        series_id = create_series(publisher_id=pub_id)
        create_issue(series_id=series_id, number="1")
        create_issue(series_id=series_id, number="2")

        delete_issues_for_series(series_id)
        assert get_issues_for_series(series_id) == []


class TestCollectionStatus:

    def test_save_and_get(self, db_connection):
        from database import save_collection_status_bulk, get_collection_status_for_series

        pub_id = create_publisher()
        series_id = create_series(publisher_id=pub_id)
        issue_id = create_issue(series_id=series_id, number="1")

        entries = [{
            "series_id": series_id,
            "issue_id": issue_id,
            "issue_number": "1",
            "found": 1,
            "file_path": "/data/Comic.cbz",
            "file_mtime": 1234567890.0,
            "matched_via": "filename",
        }]

        ok = save_collection_status_bulk(entries)
        assert ok is True

        status = get_collection_status_for_series(series_id)
        assert status is not None
        assert len(status) >= 1

    def test_invalidate_for_series(self, db_connection):
        from database import (
            save_collection_status_bulk,
            invalidate_collection_status_for_series,
            get_collection_status_for_series,
        )

        pub_id = create_publisher()
        series_id = create_series(publisher_id=pub_id)
        issue_id = create_issue(series_id=series_id, number="1")

        save_collection_status_bulk([{
            "series_id": series_id,
            "issue_id": issue_id,
            "issue_number": "1",
            "found": 0,
        }])

        invalidate_collection_status_for_series(series_id)
        status = get_collection_status_for_series(series_id)
        assert status is None or len(status) == 0


class TestWantedIssues:

    def test_save_and_get(self, db_connection):
        from database import save_wanted_issues_for_series, get_cached_wanted_issues

        pub_id = create_publisher()
        series_id = create_series(publisher_id=pub_id)

        wanted = [
            {"id": 5001, "number": "5", "name": "Missing",
             "store_date": "2020-05-01", "cover_date": "2020-05-01", "image": None},
        ]

        save_wanted_issues_for_series(series_id, "TestSeries", 2020, wanted)
        cached = get_cached_wanted_issues()
        assert len(cached) >= 1

    def test_clear_wanted_for_series(self, db_connection):
        from database import (
            save_wanted_issues_for_series,
            clear_wanted_cache_for_series,
            get_cached_wanted_issues,
        )

        pub_id = create_publisher()
        series_id = create_series(publisher_id=pub_id)

        save_wanted_issues_for_series(series_id, "X", 2020, [
            {"id": 7001, "number": "1", "name": "X",
             "store_date": None, "cover_date": None, "image": None},
        ])

        clear_wanted_cache_for_series(series_id)
        cached = get_cached_wanted_issues()
        # Should be empty or not contain this series
        for item in cached:
            assert item["series_id"] != series_id

    def test_clear_all(self, db_connection):
        from database import save_wanted_issues_for_series, clear_wanted_cache_all, get_cached_wanted_issues

        pub_id = create_publisher()
        series_id = create_series(publisher_id=pub_id)
        save_wanted_issues_for_series(series_id, "X", 2020, [
            {"id": 8001, "number": "1", "name": "X",
             "store_date": None, "cover_date": None, "image": None},
        ])

        clear_wanted_cache_all()
        assert get_cached_wanted_issues() == []


class TestManualIssueStatus:

    def test_set_and_get(self, db_connection):
        from database import set_manual_status, get_manual_status_for_series

        pub_id = create_publisher()
        series_id = create_series(publisher_id=pub_id)

        set_manual_status(series_id, "5", "owned", notes="Have physical copy")
        statuses = get_manual_status_for_series(series_id)
        assert statuses is not None
        assert "5" in statuses

    def test_clear_status(self, db_connection):
        from database import set_manual_status, clear_manual_status, get_manual_status_for_series

        pub_id = create_publisher()
        series_id = create_series(publisher_id=pub_id)

        set_manual_status(series_id, "3", "skipped")
        clear_manual_status(series_id, "3")

        statuses = get_manual_status_for_series(series_id)
        assert statuses is None or "3" not in statuses

    def test_bulk_set(self, db_connection):
        from database import bulk_set_manual_status, get_manual_status_for_series

        pub_id = create_publisher()
        series_id = create_series(publisher_id=pub_id)

        count = bulk_set_manual_status(series_id, ["1", "2", "3"], "owned")
        assert count == 3

        statuses = get_manual_status_for_series(series_id)
        assert len(statuses) == 3
