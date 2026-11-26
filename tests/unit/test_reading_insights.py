"""Pure rules in models/reading_insights.py and the local-time helpers they use."""
from datetime import date, datetime, timedelta, timezone

import pytest

from core.user_time import (
    local_date,
    local_period_bounds_utc,
    localise_read_at_sql,
    sql_offset_modifier,
    user_today,
)
from models.reading_insights import compute_streaks, series_from_path

D = date.fromisoformat
TODAY = D("2024-06-10")


def _run(start, length):
    return [D(start) + timedelta(days=n) for n in range(length)]


class TestComputeStreaks:
    def test_no_reading(self):
        assert compute_streaks([], TODAY) == {
            "current": 0, "longest": 0, "longest_start": None, "longest_end": None,
        }

    def test_read_today(self):
        result = compute_streaks(_run("2024-06-08", 3), TODAY)
        assert result["current"] == 3
        assert result["longest"] == 3

    def test_yesterday_keeps_the_streak_alive(self):
        # Reading every evening is not broken by opening the page at lunchtime.
        assert compute_streaks(_run("2024-06-07", 3), TODAY)["current"] == 3

    def test_a_missed_day_ends_it(self):
        result = compute_streaks(_run("2024-06-06", 3), TODAY)
        assert result["current"] == 0
        assert result["longest"] == 3

    def test_longest_is_reported_with_its_dates(self):
        days = _run("2024-01-01", 5) + _run("2024-03-01", 9) + _run("2024-06-09", 2)
        result = compute_streaks(days, TODAY)
        assert result == {
            "current": 2, "longest": 9,
            "longest_start": "2024-03-01", "longest_end": "2024-03-09",
        }

    def test_first_of_two_equal_runs_wins(self):
        days = _run("2024-01-01", 3) + _run("2024-02-01", 3)
        assert compute_streaks(days, TODAY)["longest_start"] == "2024-01-01"

    def test_duplicates_and_order_do_not_matter(self):
        days = [D("2024-06-10"), D("2024-06-09"), D("2024-06-10"), D("2024-06-08")]
        assert compute_streaks(days, TODAY)["current"] == 3

    def test_spans_a_month_and_year_boundary(self):
        result = compute_streaks(_run("2023-12-30", 4), D("2024-01-02"))
        assert (result["current"], result["longest"]) == (4, 4)

    def test_future_days_do_not_extend_the_current_streak(self):
        # A clock-skewed row dated tomorrow is not evidence about today.
        assert compute_streaks([D("2024-06-11")], TODAY)["current"] == 0

    def test_non_dates_are_ignored(self):
        assert compute_streaks([None, "2024-06-10", D("2024-06-10")], TODAY)["current"] == 1


class TestSeriesFromPath:
    @pytest.mark.parametrize("path,expected", [
        ("/data/DC/Batman (2016)/Batman 001.cbz", "Batman (2016)"),
        ("/data/DC/Batman v2016/Batman 001.cbz", "Batman"),
        ("C:\\data\\Marvel\\X-Men\\X-Men 001.cbz", "X-Men"),
        ("Batman 001.cbz", "Unknown"),
        ("", "Unknown"),
        (None, "Unknown"),
    ])
    def test_folder_names_the_series(self, path, expected):
        assert series_from_path(path) == expected


class TestSqlOffsetModifier:
    @pytest.mark.parametrize("hours,expected", [
        (0, "+0.00 hours"), (-5, "-5.00 hours"), (5.5, "+5.50 hours"),
        ("UTC", "+0.00 hours"), ("-3.5", "-3.50 hours"),
        # Not a number, or not a real offset: never reaches the SQL text.
        ("1'); DROP TABLE issues_read;--", "+0.00 hours"), (400, "+0.00 hours"),
    ])
    def test_is_always_a_number_of_hours(self, hours, expected):
        assert sql_offset_modifier(hours) == expected


class TestLocaliseReadAtSql:
    def test_rewrites_every_bucketing_expression(self):
        sql = ("SELECT date(read_at), strftime('%m', read_at) FROM issues_read r"
               " WHERE strftime('%Y', r.read_at) = ?")
        assert localise_read_at_sql(sql, -5) == (
            "SELECT date(datetime(read_at, '-5.00 hours')),"
            " strftime('%m', datetime(read_at, '-5.00 hours')) FROM issues_read r"
            " WHERE strftime('%Y', datetime(r.read_at, '-5.00 hours')) = ?"
        )

    def test_leaves_everything_else_alone(self):
        sql = "SELECT read_at, updated_at FROM t WHERE validate(read_at) ORDER BY read_at DESC"
        assert localise_read_at_sql(sql, 3) == sql

    def test_the_result_runs_and_moves_a_late_read_to_the_local_day(self):
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE issues_read (read_at TEXT)")
        # 02:30 UTC on 1 Jan 2025 is 21:30 on 31 Dec 2024 in UTC-5. Both
        # shapes the column holds have to land in the same bucket.
        conn.executemany("INSERT INTO issues_read VALUES (?)",
                         [("2025-01-01T02:30:00",), ("2025-01-01 02:30:00",)])
        sql = "SELECT strftime('%Y', read_at), date(read_at) FROM issues_read"
        assert conn.execute(sql).fetchall() == [("2025", "2025-01-01")] * 2
        assert conn.execute(localise_read_at_sql(sql, -5)).fetchall() == [
            ("2024", "2024-12-31")] * 2
        conn.close()


class TestLocalDates:
    def test_user_today_is_the_users_date(self):
        now = datetime(2025, 1, 1, 2, 30, tzinfo=timezone.utc)
        assert user_today(now, hours=-5) == D("2024-12-31")
        assert user_today(now, hours=0) == D("2025-01-01")
        assert user_today(datetime(2024, 12, 31, 20, 0, tzinfo=timezone.utc), hours=5.5) == D("2025-01-01")

    def test_local_date_reads_both_stored_shapes(self):
        assert local_date("2025-01-01T02:30:00", hours=-5) == D("2024-12-31")
        assert local_date("2025-01-01 02:30:00", hours=-5) == D("2024-12-31")

    def test_local_date_of_nothing(self):
        assert local_date(None) is None
        assert local_date("not a date") is None

    def test_year_bounds(self):
        assert local_period_bounds_utc(2024, hours=-5) == (
            datetime(2024, 1, 1, 5, 0), datetime(2025, 1, 1, 5, 0))

    def test_month_bounds_roll_over_december(self):
        assert local_period_bounds_utc(2024, 12, hours=5.5) == (
            datetime(2024, 11, 30, 18, 30), datetime(2024, 12, 31, 18, 30))

    def test_month_bounds_are_contiguous(self):
        ends = [local_period_bounds_utc(2024, m, hours=-8) for m in range(1, 13)]
        assert all(a[1] == b[0] for a, b in zip(ends, ends[1:]))
        assert (ends[0][0], ends[-1][1]) == local_period_bounds_utc(2024, hours=-8)
