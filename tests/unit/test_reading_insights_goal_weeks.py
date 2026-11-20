"""Goal pace, week comparison, the five-week stack and the shelf: pure rules."""
from datetime import date, datetime, timedelta, timezone

import pytest

from models.reading_insights import (
    OTHER_SERIES,
    ReadEvent,
    finished_shelf,
    five_week_series_stack,
    goal_progress,
    week_compare,
    week_start,
)

D = date.fromisoformat


def ev(day, series="Batman", hidden=False, pages=20, seconds=600, n=[0]):
    n[0] += 1
    at = datetime.combine(D(day), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=20)
    return ReadEvent(path=f"/data/{series}/{series} {n[0]:03d}.cbz", at=at, pages=pages,
                     seconds=seconds, series=series, hidden=hidden)


class TestGoalProgress:
    def test_halfway_through_a_leap_year_on_pace(self):
        # 1 July 2024 is day 183 of 366: half the year, half the goal.
        result = goal_progress(50, 100, 2024, D("2024-07-01"))
        assert result["expected"] == 50
        assert result["delta"] == 0
        assert result["on_track"] is True
        assert result["projected"] == 100
        assert result["pct"] == 50.0
        assert result["reached"] is False

    def test_behind_pace_says_what_it_takes(self):
        result = goal_progress(20, 100, 2023, D("2023-07-02"))   # day 183 of 365
        assert result["delta"] == -30
        assert result["on_track"] is False
        assert result["projected"] == 40
        # 80 issues in 182 days = 26 weeks.
        assert result["per_week_needed"] == pytest.approx(3.1)

    def test_ahead_of_pace(self):
        result = goal_progress(80, 100, 2023, D("2023-07-02"))
        assert result["delta"] == 30
        assert result["on_track"] is True

    def test_reached(self):
        result = goal_progress(120, 100, 2023, D("2023-07-02"))
        assert result["reached"] is True
        assert result["on_track"] is True
        assert result["pct"] == 120.0
        assert result["per_week_needed"] is None

    def test_a_past_year_is_complete(self):
        result = goal_progress(90, 100, 2022, D("2024-03-01"))
        assert result["complete"] is True
        assert result["projected"] == 90
        assert result["expected"] == 100
        assert result["delta"] == -10
        assert result["days_remaining"] == 0
        assert result["per_week_needed"] is None

    def test_a_future_year_has_no_pace(self):
        result = goal_progress(0, 100, 2030, D("2024-03-01"))
        assert result["projected"] == 0
        assert result["expected"] == 0
        assert result["complete"] is False

    def test_no_goal_still_projects(self):
        result = goal_progress(50, None, 2024, D("2024-07-01"))
        assert result["goal"] is None
        assert result["projected"] == 100
        assert result["pct"] is None and result["delta"] is None and result["on_track"] is None

    def test_first_day_of_the_year(self):
        result = goal_progress(1, 365, 2023, D("2023-01-01"))
        assert result["projected"] == 365
        assert result["delta"] == 0

    def test_last_day_needs_nothing_more_per_week(self):
        result = goal_progress(99, 100, 2023, D("2023-12-31"))
        assert result["complete"] is True
        assert result["per_week_needed"] is None


class TestWeekCompare:
    # Wednesday 12 June 2024; the week began Monday 10 June.
    TODAY = D("2024-06-12")

    def test_week_starts_on_monday(self):
        assert week_start(self.TODAY) == D("2024-06-10")
        assert week_start(D("2024-06-10")) == D("2024-06-10")
        assert week_start(D("2024-06-16")) == D("2024-06-10")

    def test_counts_and_weekday_buckets(self):
        events = [ev("2024-06-10"), ev("2024-06-12"), ev("2024-06-12"),
                  ev("2024-06-03"), ev("2024-06-07"), ev("2024-06-09"), ev("2024-06-09")]
        result = week_compare(events, self.TODAY)
        assert result["week_start"] == "2024-06-10"
        assert result["weekday"] == 2
        assert result["this_week"]["issues"] == 3
        assert result["this_week"]["by_day"] == [1, 0, 2, 0, 0, 0, 0]
        assert result["last_week"]["issues"] == 4
        assert result["last_week"]["by_day"] == [1, 0, 0, 0, 1, 0, 2]

    def test_to_date_stops_at_the_same_weekday(self):
        # Last week had 1 read by Wednesday and 3 after it.
        events = [ev("2024-06-03"), ev("2024-06-07"), ev("2024-06-09"), ev("2024-06-09")]
        assert week_compare(events, self.TODAY)["last_week_to_date"] == 1

    def test_pages_and_time_are_summed(self):
        events = [ev("2024-06-10", pages=22, seconds=300), ev("2024-06-11", pages=30, seconds=900)]
        this_week = week_compare(events, self.TODAY)["this_week"]
        assert (this_week["pages"], this_week["seconds"]) == (52, 1200)

    def test_older_and_future_reads_are_in_neither_week(self):
        events = [ev("2024-06-02"), ev("2024-06-13")]
        result = week_compare(events, self.TODAY)
        assert result["this_week"]["issues"] == 0
        assert result["last_week"]["issues"] == 0

    def test_empty(self):
        result = week_compare([], self.TODAY)
        assert result["this_week"] == {"issues": 0, "pages": 0, "seconds": 0, "by_day": [0] * 7}
        assert result["last_week_to_date"] == 0


class TestFiveWeekStack:
    TODAY = D("2024-06-12")

    def test_five_monday_weeks_ending_this_week(self):
        result = five_week_series_stack([], self.TODAY)
        assert [w["start"] for w in result["weeks"]] == [
            "2024-05-13", "2024-05-20", "2024-05-27", "2024-06-03", "2024-06-10"]
        assert result["weeks"][0]["label"] == "May 13"
        assert result["weeks"][3]["label"] == "Jun 3"
        assert result["series"] == []
        assert result["totals"] == [0] * 5

    def test_each_series_gets_a_band_per_week(self):
        events = [ev("2024-05-13", "Saga"), ev("2024-06-10", "Saga"), ev("2024-06-11", "Saga"),
                  ev("2024-06-04", "Batman")]
        result = five_week_series_stack(events, self.TODAY)
        assert result["series"] == [
            {"name": "Saga", "counts": [1, 0, 0, 0, 2]},
            {"name": "Batman", "counts": [0, 0, 0, 1, 0]},
        ]
        assert result["totals"] == [1, 0, 0, 1, 2]

    def test_series_beyond_the_top_n_fold_into_other(self):
        events = []
        for index, name in enumerate(["A", "B", "C", "D"]):
            events += [ev("2024-06-10", name) for _ in range(4 - index)]
        result = five_week_series_stack(events, self.TODAY, top_n=2)
        assert [row["name"] for row in result["series"]] == ["A", "B", OTHER_SERIES]
        assert result["series"][-1]["counts"][-1] == 3
        assert result["totals"][-1] == 10

    def test_ties_are_ordered_by_name(self):
        events = [ev("2024-06-10", "Zatanna"), ev("2024-06-10", "aquaman")]
        names = [row["name"] for row in five_week_series_stack(events, self.TODAY)["series"]]
        assert names == ["aquaman", "Zatanna"]

    def test_a_hidden_read_is_counted_but_never_named(self):
        events = [ev("2024-06-10", "Secret Series", hidden=True), ev("2024-06-10", "Batman")]
        result = five_week_series_stack(events, self.TODAY)
        assert [row["name"] for row in result["series"]] == ["Batman", OTHER_SERIES]
        assert result["totals"][-1] == 2

    def test_reads_outside_the_window_are_ignored(self):
        events = [ev("2024-05-12", "Old"), ev("2024-06-13", "Tomorrow")]
        assert five_week_series_stack(events, self.TODAY)["series"] == []

    def test_a_series_really_called_other_merges_with_the_fold(self):
        events = [ev("2024-06-10", OTHER_SERIES), ev("2024-06-10", "X", hidden=True)]
        result = five_week_series_stack(events, self.TODAY)
        assert result["series"] == [{"name": OTHER_SERIES, "counts": [0, 0, 0, 0, 2]}]


class TestFinishedShelf:
    def test_newest_first_and_limited(self):
        events = [ev(f"2024-06-{day:02d}") for day in range(1, 11)]
        shelf = finished_shelf(events, limit=3)
        assert [item["read_at"][:10] for item in shelf] == ["2024-06-10", "2024-06-09", "2024-06-08"]
        assert shelf[0]["name"].endswith(".cbz") and "/" not in shelf[0]["name"]
        assert shelf[0]["series"] == "Batman"

    def test_hidden_reads_are_not_shown(self):
        events = [ev("2024-06-01"), ev("2024-06-02", hidden=True)]
        assert [item["read_at"][:10] for item in finished_shelf(events)] == ["2024-06-01"]

    def test_windows_paths(self):
        event = ReadEvent(path="C:\\data\\DC\\Batman\\Batman 001.cbz",
                          at=datetime(2024, 6, 1, tzinfo=timezone.utc))
        assert finished_shelf([event])[0]["name"] == "Batman 001.cbz"
