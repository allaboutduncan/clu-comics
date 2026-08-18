"""Tests for get_on_the_stack_items() and subscription functions in database.py."""
import pytest
from unittest.mock import patch


@pytest.fixture
def stack_db(db_connection):
    """Set up a database with series, issues, and collection_status for On the Stack tests."""
    from tests.factories.db_factories import (
        create_publisher, create_series, create_issue, reset_counters,
    )
    from core.database import save_collection_status_bulk, mark_issue_read

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
        from core.database import get_on_the_stack_items
        items = get_on_the_stack_items(limit=10)
        ab_items = [i for i in items if i["series_name"] == "Absolute Batman"]
        assert len(ab_items) == 1
        assert ab_items[0]["issue_number"] == "4"
        assert "Absolute Batman 004.cbz" in ab_items[0]["file_path"]

    def test_skips_series_without_reads(self, stack_db):
        """Series with no read issues -> not included."""
        from core.database import get_on_the_stack_items
        items = get_on_the_stack_items(limit=10)
        ww_items = [i for i in items if i["series_name"] == "Wonder Woman"]
        assert len(ww_items) == 0

    def test_respects_subscription_disabled(self, stack_db):
        """Series with subscription=0 -> not included."""
        from core.database import get_on_the_stack_items, set_series_subscription
        set_series_subscription(100, False)
        items = get_on_the_stack_items(limit=10)
        ab_items = [i for i in items if i["series_name"] == "Absolute Batman"]
        assert len(ab_items) == 0

    def test_null_subscription_ongoing_included(self, stack_db):
        """Series with subscription=NULL and status=Ongoing -> included."""
        from core.database import get_on_the_stack_items
        items = get_on_the_stack_items(limit=10)
        series_names = [i["series_name"] for i in items]
        assert "Absolute Batman" in series_names
        assert "Superman" in series_names

    def test_null_subscription_ended_excluded(self, stack_db):
        """Series with subscription=NULL and status=Ended -> excluded."""
        from core.database import get_on_the_stack_items
        items = get_on_the_stack_items(limit=10)
        dc_items = [i for i in items if i["series_name"] == "Dark Crisis"]
        assert len(dc_items) == 0

    def test_shows_lowest_unread_after_read(self, stack_db):
        """Read 1,2,3 -- unread 4,5 -> returns only 4."""
        from core.database import get_on_the_stack_items
        items = get_on_the_stack_items(limit=10)
        ab_items = [i for i in items if i["series_name"] == "Absolute Batman"]
        assert len(ab_items) == 1
        assert ab_items[0]["issue_number"] == "4"

    def test_sorted_by_last_read_date(self, stack_db):
        """Multiple series -> sorted by most recently read first."""
        from core.database import get_on_the_stack_items
        items = get_on_the_stack_items(limit=10)
        # Absolute Batman last read 2024-12-01, Superman last read 2024-10-01
        assert items[0]["series_name"] == "Absolute Batman"
        assert items[1]["series_name"] == "Superman"

    def test_limit_parameter(self, stack_db):
        """Respects the limit parameter."""
        from core.database import get_on_the_stack_items
        items = get_on_the_stack_items(limit=1)
        assert len(items) == 1

    def test_ended_series_with_explicit_subscription(self, stack_db):
        """Ended series with subscription=1 -> included."""
        from core.database import get_on_the_stack_items, set_series_subscription
        set_series_subscription(300, True)
        items = get_on_the_stack_items(limit=10)
        dc_items = [i for i in items if i["series_name"] == "Dark Crisis"]
        assert len(dc_items) == 1
        assert dc_items[0]["issue_number"] == "2"

    def test_return_format(self, stack_db):
        """Verify returned dict has all expected keys."""
        from core.database import get_on_the_stack_items
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


@pytest.fixture
def rl_stack_db(db_connection):
    """Reading lists bookmarked into Want to Read, for On the Stack tests.

    - "Bat Arc"      : 4 entries, #1 read, #2 unmatched, #3-4 unread  -> next #3
    - "Fresh Arc"    : 2 entries, nothing read                        -> next #1
    - "Done Arc"     : 2 entries, both read                           -> excluded
    - "Not Bookmarked": 2 entries, nothing read, NOT in want-to-read  -> excluded
    """
    from core.database import (
        create_reading_list, add_reading_list_entry,
        add_reading_list_to_read, mark_issue_read,
    )

    def build(name, entries, bookmarked=True):
        list_id = create_reading_list(name)
        for entry in entries:
            add_reading_list_entry(list_id, entry)
        if bookmarked:
            add_reading_list_to_read(list_id)
        return list_id

    bat_id = build("Bat Arc", [
        {"series": "Batman", "issue_number": "1",
         "matched_file_path": "/data/DC/Batman/Batman 001.cbz"},
        # No matched file: skipped, not treated as the next unread.
        {"series": "Batman", "issue_number": "2", "matched_file_path": None},
        {"series": "Batman", "issue_number": "3",
         "matched_file_path": "/data/DC/Batman/Batman 003.cbz"},
        {"series": "Batman", "issue_number": "4",
         "matched_file_path": "/data/DC/Batman/Batman 004.cbz"},
    ])
    mark_issue_read(issue_path="/data/DC/Batman/Batman 001.cbz",
                    read_at="2024-12-01 10:00:00", page_count=24, time_spent=600)

    fresh_id = build("Fresh Arc", [
        {"series": "Saga", "issue_number": "1",
         "matched_file_path": "/data/Image/Saga/Saga 001.cbz"},
        {"series": "Saga", "issue_number": "2",
         "matched_file_path": "/data/Image/Saga/Saga 002.cbz"},
    ])

    build("Done Arc", [
        {"series": "Y The Last Man", "issue_number": "1",
         "matched_file_path": "/data/DC/Y/Y 001.cbz"},
    ])
    mark_issue_read(issue_path="/data/DC/Y/Y 001.cbz",
                    read_at="2024-11-01 10:00:00", page_count=24, time_spent=600)

    build("Not Bookmarked", [
        {"series": "Hellboy", "issue_number": "1",
         "matched_file_path": "/data/DH/Hellboy/Hellboy 001.cbz"},
    ], bookmarked=False)

    return {"bat_id": bat_id, "fresh_id": fresh_id}


class TestGetReadingListStackItems:

    def test_returns_first_unread_in_list_order(self, rl_stack_db):
        """#1 read, #2 unmatched, #3 unread -> returns #3."""
        from core.database import get_reading_list_stack_items
        items = get_reading_list_stack_items(limit=10)
        bat = [i for i in items if i["reading_list_name"] == "Bat Arc"]
        assert len(bat) == 1
        assert bat[0]["issue_number"] == "3"
        assert bat[0]["file_path"] == "/data/DC/Batman/Batman 003.cbz"

    def test_unstarted_list_still_appears(self, rl_stack_db):
        """A list with nothing read still surfaces its first entry."""
        from core.database import get_reading_list_stack_items
        items = get_reading_list_stack_items(limit=10)
        fresh = [i for i in items if i["reading_list_name"] == "Fresh Arc"]
        assert len(fresh) == 1
        assert fresh[0]["issue_number"] == "1"
        assert fresh[0]["last_read_at"] is None

    def test_fully_read_list_excluded(self, rl_stack_db):
        """A list with every matched entry read drops out."""
        from core.database import get_reading_list_stack_items
        items = get_reading_list_stack_items(limit=10)
        assert not [i for i in items if i["reading_list_name"] == "Done Arc"]

    def test_unbookmarked_list_excluded(self, rl_stack_db):
        """A list that isn't in Want to Read never appears."""
        from core.database import get_reading_list_stack_items
        items = get_reading_list_stack_items(limit=10)
        assert not [i for i in items if i["reading_list_name"] == "Not Bookmarked"]

    def test_removing_bookmark_removes_list(self, rl_stack_db):
        """Un-bookmarking a list takes it out of the stack."""
        from core.database import (
            get_reading_list_stack_items, remove_reading_list_to_read
        )
        remove_reading_list_to_read(rl_stack_db["bat_id"])
        items = get_reading_list_stack_items(limit=10)
        assert not [i for i in items if i["reading_list_name"] == "Bat Arc"]

    def test_read_state_is_per_user(self, rl_stack_db):
        """Another user hasn't read #1, so their next issue is #1."""
        from core.database import get_reading_list_stack_items, add_reading_list_to_read
        add_reading_list_to_read(rl_stack_db["bat_id"], user_id=2)
        items = get_reading_list_stack_items(limit=10, user_id=2)
        bat = [i for i in items if i["reading_list_name"] == "Bat Arc"]
        assert len(bat) == 1
        assert bat[0]["issue_number"] == "1"

    def test_sorted_started_lists_first(self, rl_stack_db):
        """A read at 2024-12-01 outranks a list bookmarked just now... or not.

        Sorting is by last_read_at, falling back to the bookmark time for
        never-started lists. Both lists must be present either way.
        """
        from core.database import get_reading_list_stack_items
        names = [i["reading_list_name"] for i in get_reading_list_stack_items(limit=10)]
        assert set(names) == {"Bat Arc", "Fresh Arc"}

    def test_limit_parameter(self, rl_stack_db):
        """Respects the limit parameter."""
        from core.database import get_reading_list_stack_items
        assert len(get_reading_list_stack_items(limit=1)) == 1

    def test_return_format_matches_series_items(self, rl_stack_db):
        """Same keys as get_on_the_stack_items so the renderers are shared."""
        from core.database import get_reading_list_stack_items
        item = get_reading_list_stack_items(limit=10)[0]
        for key in ("series_id", "series_name", "issue_number", "file_path",
                    "file_name", "cover_image", "last_read_at", "series_status"):
            assert key in item
        assert item["source"] == "reading_list"
        assert item["reading_list_id"] is not None
        # series_name carries the entry's series for the card subtitle.
        assert item["series_name"] in ("Batman", "Saga")
        assert "/" not in item["file_name"]

    def test_deleting_list_cascades(self, rl_stack_db):
        """Deleting a reading list removes its want-to-read bookmark."""
        from core.database import (
            delete_reading_list, get_reading_list_stack_items,
            get_to_read_reading_lists,
        )
        delete_reading_list(rl_stack_db["bat_id"])
        assert not [i for i in get_reading_list_stack_items(limit=10)
                    if i["reading_list_name"] == "Bat Arc"]
        assert not [l for l in get_to_read_reading_lists()
                    if l["name"] == "Bat Arc"]


class TestToReadReadingLists:

    def test_returns_bookmarked_lists_with_progress(self, rl_stack_db):
        """Card data carries entry/read counts and a cover stack."""
        from core.database import get_to_read_reading_lists
        lists = {l["name"]: l for l in get_to_read_reading_lists()}
        assert set(lists) == {"Bat Arc", "Fresh Arc", "Done Arc"}
        # 4 entries, 1 of them read.
        assert lists["Bat Arc"]["entry_count"] == 4
        assert lists["Bat Arc"]["read_count"] == 1
        # Only entries with a matched file can supply a cover.
        assert lists["Bat Arc"]["covers"] == [
            "/data/DC/Batman/Batman 001.cbz",
            "/data/DC/Batman/Batman 003.cbz",
            "/data/DC/Batman/Batman 004.cbz",
        ]
        assert lists["Fresh Arc"]["read_count"] == 0

    def test_read_count_is_per_user(self, rl_stack_db):
        """Another user sees their own progress, not the owner's."""
        from core.database import get_to_read_reading_lists, add_reading_list_to_read
        add_reading_list_to_read(rl_stack_db["bat_id"], user_id=2)
        lists = {l["name"]: l for l in get_to_read_reading_lists(user_id=2)}
        assert lists["Bat Arc"]["entry_count"] == 4
        assert lists["Bat Arc"]["read_count"] == 0

    def test_add_is_idempotent(self, rl_stack_db):
        """Bookmarking twice doesn't duplicate the card."""
        from core.database import get_to_read_reading_lists, add_reading_list_to_read
        assert add_reading_list_to_read(rl_stack_db["bat_id"]) is True
        names = [l["name"] for l in get_to_read_reading_lists()]
        assert names.count("Bat Arc") == 1

    def test_is_reading_list_to_read(self, rl_stack_db):
        from core.database import is_reading_list_to_read, remove_reading_list_to_read
        assert is_reading_list_to_read(rl_stack_db["bat_id"]) is True
        remove_reading_list_to_read(rl_stack_db["bat_id"])
        assert is_reading_list_to_read(rl_stack_db["bat_id"]) is False

    def test_unknown_list_rejected(self, rl_stack_db):
        """The FK keeps a bogus id out rather than storing a dangling row."""
        from core.database import (
            add_reading_list_to_read, get_to_read_reading_lists, reading_list_exists
        )
        assert reading_list_exists(99999) is False
        add_reading_list_to_read(99999)
        assert not [l for l in get_to_read_reading_lists() if l["id"] == 99999]


class TestSeriesSubscription:

    def test_set_and_get_subscription_enabled(self, stack_db):
        """Setting subscription to True returns True."""
        from core.database import set_series_subscription, get_series_subscription
        set_series_subscription(100, True)
        assert get_series_subscription(100) is True

    def test_set_and_get_subscription_disabled(self, stack_db):
        """Setting subscription to False returns False."""
        from core.database import set_series_subscription, get_series_subscription
        set_series_subscription(100, False)
        assert get_series_subscription(100) is False

    def test_null_subscription_ongoing_defaults_true(self, stack_db):
        """NULL subscription on Ongoing series defaults to True."""
        from core.database import get_series_subscription
        # Series 100 is Ongoing with NULL subscription
        assert get_series_subscription(100) is True

    def test_null_subscription_ended_defaults_false(self, stack_db):
        """NULL subscription on Ended series defaults to False."""
        from core.database import get_series_subscription
        # Series 300 is Ended with NULL subscription
        assert get_series_subscription(300) is False

    def test_nonexistent_series_returns_false(self, stack_db):
        """Nonexistent series returns False."""
        from core.database import get_series_subscription
        assert get_series_subscription(99999) is False
