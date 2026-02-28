"""Tests for get_on_the_stack_items() and subscription functions in database.py."""
import pytest
from unittest.mock import patch


@pytest.fixture
def stack_db(db_connection):
    """Set up a database with series, issues, and collection_status for On the Stack tests."""
    from tests.factories.db_factories import (
        create_publisher, create_series, create_issue, reset_counters,
    )
    from database import save_collection_status_bulk, mark_issue_read

    reset_counters()
    pub_id = create_publisher(publisher_id=10, name="DC Comics")

    # Series A: Ongoing, issues 1-5, read 1-3, unread 4-5
    create_series(series_id=100, name="Absolute Batman", volume=2024,
                  publisher_id=pub_id, mapped_path="/data/DC/Absolute Batman",
                  status="Ongoing")
    for i in range(1, 6):
        create_issue(issue_id=1000 + i, series_id=100, number=str(i),
                     cover_date=f"2024-{i:02d}-15", store_date=f"2024-{i:02d}-10",
                     image=f"https://example.com/ab{i}.jpg")
    save_collection_status_bulk([
        {"series_id": 100, "issue_id": 1000 + i, "issue_number": str(i),
         "found": 1, "file_path": f"/data/DC/Absolute Batman/Absolute Batman {i:03d}.cbz",
         "file_mtime": 1700000000.0 + i, "matched_via": "exact"}
        for i in range(1, 6)
    ])
    mark_issue_read(issue_path="/data/DC/Absolute Batman/Absolute Batman 001.cbz",
                    read_at="2024-11-01 10:00:00", page_count=24, time_spent=600)
    mark_issue_read(issue_path="/data/DC/Absolute Batman/Absolute Batman 002.cbz",
                    read_at="2024-11-15 10:00:00", page_count=24, time_spent=600)
    mark_issue_read(issue_path="/data/DC/Absolute Batman/Absolute Batman 003.cbz",
                    read_at="2024-12-01 10:00:00", page_count=24, time_spent=600)

    # Series B: Ongoing, issues 1-3, read 1, unread 2-3
    create_series(series_id=200, name="Superman", volume=2024,
                  publisher_id=pub_id, mapped_path="/data/DC/Superman",
                  status="Ongoing")
    for i in range(1, 4):
        create_issue(issue_id=2000 + i, series_id=200, number=str(i),
                     cover_date=f"2024-{i:02d}-15", store_date=f"2024-{i:02d}-10")
    save_collection_status_bulk([
        {"series_id": 200, "issue_id": 2000 + i, "issue_number": str(i),
         "found": 1, "file_path": f"/data/DC/Superman/Superman {i:03d}.cbz",
         "file_mtime": 1700000000.0 + i, "matched_via": "exact"}
        for i in range(1, 4)
    ])
    mark_issue_read(issue_path="/data/DC/Superman/Superman 001.cbz",
                    read_at="2024-10-01 10:00:00", page_count=24, time_spent=600)

    # Series C: Ended, issues 1-2, read 1, unread 2
    create_series(series_id=300, name="Dark Crisis", volume=2022,
                  publisher_id=pub_id, mapped_path="/data/DC/Dark Crisis",
                  status="Ended")
    for i in range(1, 3):
        create_issue(issue_id=3000 + i, series_id=300, number=str(i),
                     cover_date=f"2022-{i:02d}-15", store_date=f"2022-{i:02d}-10")
    save_collection_status_bulk([
        {"series_id": 300, "issue_id": 3000 + i, "issue_number": str(i),
         "found": 1, "file_path": f"/data/DC/Dark Crisis/Dark Crisis {i:03d}.cbz",
         "file_mtime": 1700000000.0 + i, "matched_via": "exact"}
        for i in range(1, 3)
    ])
    mark_issue_read(issue_path="/data/DC/Dark Crisis/Dark Crisis 001.cbz",
                    read_at="2024-09-01 10:00:00", page_count=24, time_spent=600)

    # Series D: Ongoing, no issues read
    create_series(series_id=400, name="Wonder Woman", volume=2024,
                  publisher_id=pub_id, mapped_path="/data/DC/Wonder Woman",
                  status="Ongoing")
    for i in range(1, 3):
        create_issue(issue_id=4000 + i, series_id=400, number=str(i),
                     cover_date=f"2024-{i:02d}-15", store_date=f"2024-{i:02d}-10")
    save_collection_status_bulk([
        {"series_id": 400, "issue_id": 4000 + i, "issue_number": str(i),
         "found": 1, "file_path": f"/data/DC/Wonder Woman/Wonder Woman {i:03d}.cbz",
         "file_mtime": 1700000000.0 + i, "matched_via": "exact"}
        for i in range(1, 3)
    ])

    return db_connection


class TestGetOnTheStackItems:

    def test_returns_next_unread_issue(self, stack_db):
        """Series with issues 1-3 read, issue 4 unread -> returns issue 4."""
        from database import get_on_the_stack_items
        items = get_on_the_stack_items(limit=10)
        ab_items = [i for i in items if i["series_name"] == "Absolute Batman"]
        assert len(ab_items) == 1
        assert ab_items[0]["issue_number"] == "4"
        assert "Absolute Batman 004.cbz" in ab_items[0]["file_path"]

    def test_skips_series_without_reads(self, stack_db):
        """Series with no read issues -> not included."""
        from database import get_on_the_stack_items
        items = get_on_the_stack_items(limit=10)
        ww_items = [i for i in items if i["series_name"] == "Wonder Woman"]
        assert len(ww_items) == 0

    def test_respects_subscription_disabled(self, stack_db):
        """Series with subscription=0 -> not included."""
        from database import get_on_the_stack_items, set_series_subscription
        set_series_subscription(100, False)
        items = get_on_the_stack_items(limit=10)
        ab_items = [i for i in items if i["series_name"] == "Absolute Batman"]
        assert len(ab_items) == 0

    def test_null_subscription_ongoing_included(self, stack_db):
        """Series with subscription=NULL and status=Ongoing -> included."""
        from database import get_on_the_stack_items
        items = get_on_the_stack_items(limit=10)
        series_names = [i["series_name"] for i in items]
        assert "Absolute Batman" in series_names
        assert "Superman" in series_names

    def test_null_subscription_ended_excluded(self, stack_db):
        """Series with subscription=NULL and status=Ended -> excluded."""
        from database import get_on_the_stack_items
        items = get_on_the_stack_items(limit=10)
        dc_items = [i for i in items if i["series_name"] == "Dark Crisis"]
        assert len(dc_items) == 0

    def test_shows_lowest_unread_after_read(self, stack_db):
        """Read 1,2,3 -- unread 4,5 -> returns only 4."""
        from database import get_on_the_stack_items
        items = get_on_the_stack_items(limit=10)
        ab_items = [i for i in items if i["series_name"] == "Absolute Batman"]
        assert len(ab_items) == 1
        assert ab_items[0]["issue_number"] == "4"

    def test_sorted_by_last_read_date(self, stack_db):
        """Multiple series -> sorted by most recently read first."""
        from database import get_on_the_stack_items
        items = get_on_the_stack_items(limit=10)
        # Absolute Batman last read 2024-12-01, Superman last read 2024-10-01
        assert items[0]["series_name"] == "Absolute Batman"
        assert items[1]["series_name"] == "Superman"

    def test_limit_parameter(self, stack_db):
        """Respects the limit parameter."""
        from database import get_on_the_stack_items
        items = get_on_the_stack_items(limit=1)
        assert len(items) == 1

    def test_ended_series_with_explicit_subscription(self, stack_db):
        """Ended series with subscription=1 -> included."""
        from database import get_on_the_stack_items, set_series_subscription
        set_series_subscription(300, True)
        items = get_on_the_stack_items(limit=10)
        dc_items = [i for i in items if i["series_name"] == "Dark Crisis"]
        assert len(dc_items) == 1
        assert dc_items[0]["issue_number"] == "2"

    def test_return_format(self, stack_db):
        """Verify returned dict has all expected keys."""
        from database import get_on_the_stack_items
        items = get_on_the_stack_items(limit=10)
        assert len(items) > 0
        item = items[0]
        assert "series_id" in item
        assert "series_name" in item
        assert "issue_number" in item
        assert "file_path" in item
        assert "file_name" in item
        assert "cover_image" in item
        assert "last_read_at" in item
        assert "series_status" in item


class TestSeriesSubscription:

    def test_set_and_get_subscription_enabled(self, stack_db):
        """Setting subscription to True returns True."""
        from database import set_series_subscription, get_series_subscription
        set_series_subscription(100, True)
        assert get_series_subscription(100) is True

    def test_set_and_get_subscription_disabled(self, stack_db):
        """Setting subscription to False returns False."""
        from database import set_series_subscription, get_series_subscription
        set_series_subscription(100, False)
        assert get_series_subscription(100) is False

    def test_null_subscription_ongoing_defaults_true(self, stack_db):
        """NULL subscription on Ongoing series defaults to True."""
        from database import get_series_subscription
        # Series 100 is Ongoing with NULL subscription
        assert get_series_subscription(100) is True

    def test_null_subscription_ended_defaults_false(self, stack_db):
        """NULL subscription on Ended series defaults to False."""
        from database import get_series_subscription
        # Series 300 is Ended with NULL subscription
        assert get_series_subscription(300) is False

    def test_nonexistent_series_returns_false(self, stack_db):
        """Nonexistent series returns False."""
        from database import get_series_subscription
        assert get_series_subscription(99999) is False
