"""Tests for reading tracking -- mark_read, positions, continue_reading, totals, trends."""
import pytest
from tests.factories.db_factories import create_issue_read, create_reading_position


class TestMarkIssueRead:

    def test_mark_and_check(self, db_connection):
        from database import mark_issue_read, is_issue_read

        ok = mark_issue_read("/data/DC/Batman/Batman 001.cbz", page_count=24)
        assert ok is True
        assert is_issue_read("/data/DC/Batman/Batman 001.cbz") is True

    def test_not_read_by_default(self, db_connection):
        from database import is_issue_read

        assert is_issue_read("/data/nonexistent.cbz") is False

    def test_unmark_issue_read(self, db_connection):
        from database import mark_issue_read, unmark_issue_read, is_issue_read

        mark_issue_read("/data/X.cbz")
        assert is_issue_read("/data/X.cbz") is True

        unmark_issue_read("/data/X.cbz")
        assert is_issue_read("/data/X.cbz") is False

    def test_custom_read_at(self, db_connection):
        from database import mark_issue_read, get_issue_read_date

        mark_issue_read("/data/A.cbz", read_at="2024-06-15T10:00:00")
        date = get_issue_read_date("/data/A.cbz")
        assert date is not None
        assert "2024-06-15" in date

    def test_read_with_metadata(self, db_connection):
        from database import mark_issue_read, get_issues_read

        mark_issue_read(
            "/data/B.cbz",
            page_count=30,
            time_spent=900,
            writer="Stan Lee",
            penciller="Jack Kirby",
            characters="Hulk, Thor",
            publisher="Marvel",
        )

        reads = get_issues_read()
        assert len(reads) == 1
        assert reads[0]["page_count"] == 30
        assert reads[0]["time_spent"] == 900


class TestGetIssuesRead:

    def test_returns_list(self, db_connection):
        from database import get_issues_read

        create_issue_read(issue_path="/data/A.cbz")
        create_issue_read(issue_path="/data/B.cbz")

        reads = get_issues_read()
        assert len(reads) == 2
        # Ordered by read_at DESC
        paths = [r["issue_path"] for r in reads]
        assert "/data/A.cbz" in paths
        assert "/data/B.cbz" in paths

    def test_empty_when_nothing_read(self, db_connection):
        from database import get_issues_read

        assert get_issues_read() == []


class TestReadingTotals:

    def test_sums_pages_and_time(self, db_connection):
        from database import get_reading_totals

        create_issue_read(issue_path="/data/A.cbz", page_count=24, time_spent=600)
        create_issue_read(issue_path="/data/B.cbz", page_count=30, time_spent=800)

        totals = get_reading_totals()
        assert totals["total_pages"] == 54
        assert totals["total_time"] == 1400

    def test_zero_when_empty(self, db_connection):
        from database import get_reading_totals

        totals = get_reading_totals()
        assert totals["total_pages"] == 0
        assert totals["total_time"] == 0


class TestReadingStatsByYear:

    def test_all_time(self, db_connection):
        from database import get_reading_stats_by_year

        create_issue_read(issue_path="/data/A.cbz", page_count=24, time_spent=600)
        create_issue_read(issue_path="/data/B.cbz", page_count=30, time_spent=800)

        stats = get_reading_stats_by_year()
        assert stats["total_read"] == 2
        assert stats["total_pages"] == 54


class TestReadingTrends:

    def test_writer_trends(self, db_connection):
        from database import get_reading_trends

        create_issue_read(issue_path="/data/1.cbz", writer="Stan Lee")
        create_issue_read(issue_path="/data/2.cbz", writer="Stan Lee")
        create_issue_read(issue_path="/data/3.cbz", writer="Tom King")

        trends = get_reading_trends("writer")
        assert len(trends) >= 2
        assert trends[0]["name"] == "Stan Lee"
        assert trends[0]["count"] == 2

    def test_splits_comma_separated(self, db_connection):
        from database import get_reading_trends

        create_issue_read(issue_path="/data/1.cbz", characters="Batman, Robin")
        create_issue_read(issue_path="/data/2.cbz", characters="Batman, Joker")

        trends = get_reading_trends("characters")
        names = {t["name"] for t in trends}
        assert "Batman" in names
        assert "Robin" in names
        assert "Joker" in names

        batman = next(t for t in trends if t["name"] == "Batman")
        assert batman["count"] == 2

    def test_invalid_field_returns_empty(self, db_connection):
        from database import get_reading_trends

        assert get_reading_trends("invalid_field") == []

    def test_respects_limit(self, db_connection):
        from database import get_reading_trends

        for i in range(15):
            create_issue_read(
                issue_path=f"/data/{i}.cbz",
                writer=f"Writer {i}",
            )

        trends = get_reading_trends("writer", limit=5)
        assert len(trends) == 5


class TestReadingPositions:

    def test_save_and_get(self, db_connection):
        from database import save_reading_position, get_reading_position

        save_reading_position("/data/Comic.cbz", page_number=5, total_pages=24, time_spent=300)

        pos = get_reading_position("/data/Comic.cbz")
        assert pos is not None
        assert pos["page_number"] == 5
        assert pos["total_pages"] == 24
        assert pos["time_spent"] == 300

    def test_update_position(self, db_connection):
        from database import save_reading_position, get_reading_position

        save_reading_position("/data/Comic.cbz", page_number=5, total_pages=24)
        save_reading_position("/data/Comic.cbz", page_number=10, total_pages=24)

        pos = get_reading_position("/data/Comic.cbz")
        assert pos["page_number"] == 10

    def test_delete_position(self, db_connection):
        from database import save_reading_position, delete_reading_position, get_reading_position

        save_reading_position("/data/X.cbz", page_number=3, total_pages=20)
        delete_reading_position("/data/X.cbz")

        assert get_reading_position("/data/X.cbz") is None

    def test_get_nonexistent(self, db_connection):
        from database import get_reading_position

        assert get_reading_position("/data/nope.cbz") is None

    def test_get_all_positions(self, db_connection):
        from database import get_all_reading_positions

        create_reading_position(comic_path="/data/A.cbz", page_number=5, total_pages=20)
        create_reading_position(comic_path="/data/B.cbz", page_number=10, total_pages=30)

        positions = get_all_reading_positions()
        assert len(positions) == 2


class TestContinueReading:

    def test_returns_in_progress_items(self, db_connection):
        from database import get_continue_reading_items

        # In-progress: page 5 of 24
        create_reading_position(comic_path="/data/InProgress.cbz", page_number=5, total_pages=24)
        # Also add to file_index so it can join
        from tests.factories.db_factories import create_file_index_entry
        create_file_index_entry(name="InProgress.cbz", path="/data/InProgress.cbz", parent="/data")

        # Finished: page 22 of 24 (page < total - 1 is the condition)
        create_reading_position(comic_path="/data/Done.cbz", page_number=23, total_pages=24)
        create_file_index_entry(name="Done.cbz", path="/data/Done.cbz", parent="/data")

        items = get_continue_reading_items()
        paths = [item["comic_path"] for item in items]
        assert "/data/InProgress.cbz" in paths


class TestToRead:

    def test_add_and_check(self, db_connection):
        from database import add_to_read, is_to_read

        add_to_read("/data/WantToRead.cbz", item_type="file")
        assert is_to_read("/data/WantToRead.cbz") is True

    def test_remove(self, db_connection):
        from database import add_to_read, remove_to_read, is_to_read

        add_to_read("/data/X.cbz")
        remove_to_read("/data/X.cbz")
        assert is_to_read("/data/X.cbz") is False

    def test_get_items(self, db_connection):
        from database import add_to_read, get_to_read_items

        add_to_read("/data/A.cbz")
        add_to_read("/data/B.cbz")

        items = get_to_read_items()
        assert len(items) >= 2
