"""Reading stats bucket by the user's local day, everywhere.

``read_at`` is UTC. A comic finished at 21:30 on 31 December in UTC-5 is stored
as 02:30 on 1 January; bucketed raw it belonged to next year's Wrapped, the
January heatmap cell and the wrong Timeline filter, while Insights' reading
chart (which did apply the offset) put it in December.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import models.stats as stats
import models.timeline as timeline
import wrapped
from core.database import (
    get_db_connection,
    get_reading_stats_by_year,
    get_reading_trends,
    hide_issue_from_history,
    mark_issue_read,
    set_user_preference,
)
from core.user_time import local_period_bounds_utc
from models.reading_insights import load_read_events

NYE = "/data/DC/Batman (2016)/Batman 050.cbz"       # 21:30 local, 31 Dec 2024
JAN = "/data/DC/Batman (2016)/Batman 051.cbz"       # 10:00 local, 2 Jan 2025


@pytest.fixture
def utc_minus_5(db_connection):
    set_user_preference("timezone", "-5")
    mark_issue_read(NYE, read_at="2025-01-01T02:30:00", page_count=20,
                    time_spent=600, writer="Tom King", publisher="DC Comics")
    mark_issue_read(JAN, read_at="2025-01-02 15:00:00", page_count=22,
                    writer="Tom King", publisher="DC Comics")


class TestWrappedUsesLocalTime:
    def test_new_years_eve_read_belongs_to_the_old_year(self, utc_minus_5):
        assert wrapped.get_yearly_total_read(2024) == 1
        assert wrapped.get_yearly_total_read(2025) == 1
        assert wrapped.get_years_with_reading_data() == [2025, 2024]

    def test_busiest_day_is_the_local_day(self, utc_minus_5):
        assert wrapped.get_busiest_day(2024)["date_short"] == "Dec 31"

    def test_monthly_stats(self, utc_minus_5):
        assert wrapped.get_monthly_stats(2024, 12)["total_read"] == 1
        assert wrapped.get_monthly_stats(2025, 1)["total_read"] == 1
        assert wrapped.get_monthly_read_issues(2024, 12) == [NYE]

    def test_utc_is_unchanged(self, db_connection):
        mark_issue_read(NYE, read_at="2025-01-01T02:30:00")
        assert wrapped.get_yearly_total_read(2025) == 1
        assert wrapped.get_yearly_total_read(2024) == 0


class TestInsightsUsesLocalTime:
    def test_heatmap_cell(self, utc_minus_5):
        heatmap = stats.get_reading_heatmap_data()
        assert heatmap["2024"][11] == 1
        assert heatmap["2025"][0] == 1

    def test_heatmap_cache_follows_the_offset(self, utc_minus_5):
        assert stats.get_reading_heatmap_data()["2025"][0] == 1
        # Changing the offset moves reads between cells; a cached answer for
        # the old offset must not be served.
        set_user_preference("timezone", "UTC")
        assert stats.get_reading_heatmap_data()["2025"][0] == 2

    def test_stats_by_year(self, utc_minus_5):
        assert get_reading_stats_by_year(2024)["total_read"] == 1
        assert get_reading_stats_by_year(2025)["total_read"] == 1
        assert get_reading_stats_by_year()["total_read"] == 2

    def test_trends_by_year(self, utc_minus_5):
        assert get_reading_trends("writer", year=2024) == [{"name": "Tom King", "count": 1}]


class TestTimelineUsesLocalTime:
    def test_year_and_month_filters(self, utc_minus_5):
        december = timeline.get_reading_timeline(year=2024, month=12)
        paths = [e["issue_path"] for g in december["timeline"] for e in g["entries"]]
        assert paths == [NYE]
        assert december["timeline"][0]["date"] == "DEC 31, 2024"
        assert december["stats"]["total_read"] == 1

    def test_streak_counts_local_days_up_to_the_users_today(self, db_connection):
        set_user_preference("timezone", "-5")
        # Three consecutive local evenings (02:30 UTC the following mornings),
        # the last of them "yesterday" for a user whose clock reads 12:00.
        now = datetime(2024, 6, 10, 17, 0, tzinfo=timezone.utc)   # 12:00 local, 10 Jun
        for n, days_ago in enumerate((1, 2, 3)):
            stamp = datetime(2024, 6, 11, 2, 30) - timedelta(days=days_ago)
            mark_issue_read(f"/data/x/{n}.cbz", read_at=stamp.isoformat())
        with patch("core.user_time.utc_now", return_value=now):
            assert timeline.get_reading_timeline()["stats"]["streak"] == 3

    def test_streak_ignores_hidden_reads(self, db_connection):
        now = datetime(2024, 6, 10, 12, 0, tzinfo=timezone.utc)
        mark_issue_read("/data/x/a.cbz", read_at="2024-06-10T09:00:00")
        mark_issue_read("/data/x/b.cbz", read_at="2024-06-09T09:00:00")
        hide_issue_from_history("/data/x/b.cbz")
        with patch("core.user_time.utc_now", return_value=now):
            assert timeline.get_reading_timeline()["stats"]["streak"] == 1


class TestLoadReadEvents:
    def test_events_carry_local_time_and_parsed_credits(self, utc_minus_5):
        events = load_read_events()
        assert [e.path for e in events] == [NYE, JAN]
        first = events[0]
        assert first.day.isoformat() == "2024-12-31"
        assert first.at.hour == 21
        assert first.series == "Batman (2016)"
        assert first.writers == ("Tom King",)
        assert (first.pages, first.seconds, first.timed) == (20, 600, True)
        assert events[1].timed is False

    def test_period_bounds_select_the_local_year(self, utc_minus_5):
        start, end = local_period_bounds_utc(2024)
        assert [e.path for e in load_read_events(start_utc=start, end_utc=end)] == [NYE]
        start, end = local_period_bounds_utc(2025, 1)
        assert [e.path for e in load_read_events(start_utc=start, end_utc=end)] == [JAN]

    def test_hidden_rows_are_optional(self, utc_minus_5):
        hide_issue_from_history(NYE)
        assert [e.hidden for e in load_read_events()] == [True, False]
        assert [e.path for e in load_read_events(include_hidden=False)] == [JAN]

    def test_per_user(self, utc_minus_5):
        mark_issue_read("/data/other.cbz", read_at="2025-01-03T10:00:00", user_id=2)
        assert [e.path for e in load_read_events(user_id=2)] == ["/data/other.cbz"]
        assert len(load_read_events(user_id=1)) == 2

    def test_series_prefers_comicinfo(self, utc_minus_5):
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO file_index (name, path, type, ci_series) VALUES (?, ?, 'file', ?)",
            ("Batman 050.cbz", NYE, "Batman"),
        )
        conn.commit()
        conn.close()
        assert load_read_events()[0].series == "Batman"


def test_period_index_exists(db_connection):
    conn = get_db_connection()
    names = {row[1] for row in conn.execute("PRAGMA index_list(issues_read)")}
    conn.close()
    assert "idx_issues_read_user_read_at" in names
