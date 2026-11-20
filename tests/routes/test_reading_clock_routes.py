"""The reading clock and the day view: /api/reading-insights/clock and /day/<d>."""
from datetime import datetime, timedelta, timezone

import pytest

from core.database import hide_issue_from_history, mark_issue_read, set_user_preference
from core.reading_sessions import record_reading_activity
from models.reading_insights import (
    FINISH_TIME_CAP_SECONDS,
    ReadEvent,
    _spread_over_hours,
    reading_clock,
)

UTC = timezone.utc
A = "/data/DC/Batman/Batman 001.cbz"
B = "/data/DC/Batman/Batman 002.cbz"
C = "/data/Image/Saga/Saga 001.cbz"


def dt(text):
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def event(path, at, seconds=0, hidden=False):
    return ReadEvent(path=path, at=dt(at), seconds=seconds, pages=20, series="S", hidden=hidden)


def session(path, start, end, active):
    return {"comic_path": path, "started_at": dt(start), "ended_at": dt(end),
            "active_seconds": active}


class TestSpreadOverHours:
    def test_within_one_hour(self):
        hours = [0.0] * 24
        _spread_over_hours(dt("2024-06-10T20:10:00"), dt("2024-06-10T20:40:00"), 1800, hours)
        assert hours[20] == 1800 and sum(hours) == 1800

    def test_split_in_proportion_across_hours(self):
        hours = [0.0] * 24
        # 20:30 to 22:00 is 30 + 60 minutes: a third, then two thirds.
        _spread_over_hours(dt("2024-06-10T20:30:00"), dt("2024-06-10T22:00:00"), 900, hours)
        assert (hours[20], hours[21]) == (300, 600)

    def test_across_midnight(self):
        hours = [0.0] * 24
        _spread_over_hours(dt("2024-06-10T23:30:00"), dt("2024-06-11T00:30:00"), 600, hours)
        assert (hours[23], hours[0]) == (300, 300)

    def test_a_zero_length_span_goes_to_its_hour(self):
        hours = [0.0] * 24
        _spread_over_hours(dt("2024-06-10T09:15:00"), dt("2024-06-10T09:15:00"), 120, hours)
        assert hours[9] == 120

    def test_nothing_to_add(self):
        hours = [0.0] * 24
        _spread_over_hours(dt("2024-06-10T09:00:00"), dt("2024-06-10T10:00:00"), 0, hours)
        assert sum(hours) == 0


class TestReadingClock:
    def test_no_history(self):
        clock = reading_clock([], [], UTC)
        assert clock["source"] == "none"
        assert clock["hours"] == [0] * 24 and clock["weekdays"] == [0] * 7
        assert clock["peak_hour"] is None and clock["peak_weekday"] is None

    def test_sessions_only(self):
        # Monday 10 June 2024, 20:00-21:00, 40 minutes active.
        clock = reading_clock([], [session(A, "2024-06-10T20:00:00", "2024-06-10T21:00:00", 2400)], UTC)
        assert clock["source"] == "sessions"
        assert clock["hours"][20] == 2400
        assert clock["weekdays"] == [2400, 0, 0, 0, 0, 0, 0]
        assert (clock["peak_hour"], clock["peak_weekday"]) == (20, 0)
        assert clock["total_seconds"] == 2400

    def test_finish_times_are_laid_back_from_the_finish(self):
        # Finished 21:30 after 60 minutes: 20:30-21:30.
        clock = reading_clock([event(A, "2024-06-11T21:30:00", seconds=3600)], [], UTC)
        assert clock["source"] == "finish_times"
        assert (clock["hours"][20], clock["hours"][21]) == (1800, 1800)
        assert clock["weekdays"][1] == 3600

    def test_a_long_total_is_capped(self):
        clock = reading_clock([event(A, "2024-06-11T21:00:00", seconds=10 * 3600)], [], UTC)
        assert clock["total_seconds"] == FINISH_TIME_CAP_SECONDS

    def test_a_comic_with_a_session_is_not_counted_twice(self):
        clock = reading_clock(
            [event(A, "2024-06-10T21:00:00", seconds=3600), event(B, "2024-06-10T09:30:00", seconds=600)],
            [session(A, "2024-06-10T20:00:00", "2024-06-10T21:00:00", 1800)],
            UTC,
        )
        assert clock["source"] == "mixed"
        assert clock["total_seconds"] == 1800 + 600
        assert (clock["sessions"], clock["finishes"]) == (1, 1)

    def test_untimed_reads_say_nothing_about_the_clock(self):
        clock = reading_clock([event(A, "2024-06-10T03:00:00", seconds=0)], [], UTC)
        assert clock["source"] == "none"
        assert clock["untimed"] == 1
        assert sum(clock["hours"]) == 0

    def test_hours_are_the_users(self):
        tz = timezone(timedelta(hours=-5))
        clock = reading_clock([], [session(A, "2024-06-11T01:00:00", "2024-06-11T01:30:00", 600)], tz)
        assert clock["hours"][20] == 600
        assert clock["weekdays"][0] == 600      # still Monday evening locally

    def test_a_session_with_no_active_time_still_claims_its_comic(self):
        clock = reading_clock(
            [event(A, "2024-06-10T21:00:00", seconds=3600)],
            [session(A, "2024-06-10T20:00:00", "2024-06-10T20:00:00", 0)],
            UTC,
        )
        assert clock["total_seconds"] == 0


@pytest.fixture
def day(db_connection):
    """10 June 2024 in UTC-5: an evening sitting, a timed read with no
    session, a manual mark, and a read just after local midnight."""
    set_user_preference("timezone", "-5")
    # Sitting on A: 20:00-20:30 local = 01:00-01:30 UTC on the 11th.
    start = datetime(2024, 6, 11, 1, 0, tzinfo=UTC)
    for minute, page in ((0, 2), (4, 8), (8, 14), (30, 22)):
        record_reading_activity(A, page, 22, session_id="day-sitting-0001",
                                now=start + timedelta(minutes=minute),
                                finished=(page == 22))
    mark_issue_read(A, read_at="2024-06-11T01:40:00", page_count=22, time_spent=1500)
    # B: finished 12:30 local after 20 minutes, no session.
    mark_issue_read(B, read_at="2024-06-10T17:30:00", page_count=20, time_spent=1200)
    # C: marked read by hand at 09:00 local.
    mark_issue_read(C, read_at="2024-06-10T14:00:00")
    # The next local day.
    mark_issue_read("/data/next.cbz", read_at="2024-06-11T06:00:00", time_spent=300)


class TestDayRoute:
    def test_invalid_date(self, client):
        r = client.get("/api/reading-insights/day/yesterday")
        assert r.status_code == 400

    def test_empty_day(self, client, db_connection):
        body = client.get("/api/reading-insights/day/2024-06-10").get_json()
        assert body["success"] is True
        assert (body["sessions"], body["reads"], body["issues"]) == ([], [], 0)

    def test_the_local_day(self, client, day):
        body = client.get("/api/reading-insights/day/2024-06-10").get_json()
        assert body["date"] == "2024-06-10"
        assert body["issues"] == 3
        assert {r["name"] for r in body["reads"]} == {"Batman 001.cbz", "Batman 002.cbz", "Saga 001.cbz"}

    def test_session_is_placed_on_the_ruler_in_local_time(self, client, day):
        (sitting,) = client.get("/api/reading-insights/day/2024-06-10").get_json()["sessions"]
        assert sitting["name"] == "Batman 001.cbz"
        assert sitting["start"].startswith("2024-06-10T20:00:00")
        assert sitting["end"].startswith("2024-06-10T20:30:00")
        assert sitting["minutes"] == [20 * 60, 20 * 60 + 30]
        # 4 + 4 minutes in full, then a 22-minute gap capped at 5.
        assert sitting["active_seconds"] == 240 + 240 + 300
        assert sitting["pages"] == 21
        assert sitting["finished"] is True
        assert sitting["thumbnail_url"].startswith("/api/thumbnail?path=")

    def test_reads_say_how_they_are_drawn(self, client, day):
        reads = {r["name"]: r for r in
                 client.get("/api/reading-insights/day/2024-06-10").get_json()["reads"]}
        # Described by its session: listed, not drawn again.
        assert reads["Batman 001.cbz"]["has_session"] is True
        assert reads["Batman 001.cbz"]["minutes"] is None
        # No session: laid back 20 minutes from a 12:30 finish.
        assert reads["Batman 002.cbz"]["minutes"] == [12 * 60 + 10, 12 * 60 + 30]
        # A manual mark has no place on a clock.
        assert reads["Saga 001.cbz"]["timed"] is False
        assert reads["Saga 001.cbz"]["minutes"] is None

    def test_totals(self, client, day):
        body = client.get("/api/reading-insights/day/2024-06-10").get_json()
        assert body["pages"] == 42
        assert body["active_seconds"] == 780 + 1200

    def test_hidden_reads_are_left_out_and_counted(self, client, day):
        hide_issue_from_history(A)
        body = client.get("/api/reading-insights/day/2024-06-10").get_json()
        assert body["sessions"] == []
        assert "Batman 001.cbz" not in {r["name"] for r in body["reads"]}
        assert body["hidden"] == 1
        assert body["issues"] == 2

    def test_per_user(self, client, day):
        mark_issue_read("/data/other.cbz", read_at="2024-06-10T17:00:00", user_id=2)
        assert client.get("/api/reading-insights/day/2024-06-10").get_json()["issues"] == 3


class TestClockRoute:
    def test_all_time_and_one_year(self, client, day):
        body = client.get("/api/reading-insights/clock").get_json()
        assert body["success"] is True and body["year"] is None
        assert body["source"] == "mixed"
        assert body["hours"][20] == 780                 # the evening sitting, local
        assert body["hours"][12] == 1200                # B, 12:10-12:30
        assert body["untimed"] == 1
        assert client.get("/api/reading-insights/clock?year=2023").get_json()["source"] == "none"
        assert client.get("/api/reading-insights/clock?year=2024").get_json()["hours"][20] == 780
        assert client.get("/api/reading-insights/clock?year=all").get_json()["year"] is None

    @pytest.mark.parametrize("year", ["soon", "1500"])
    def test_invalid_year(self, client, year):
        assert client.get(f"/api/reading-insights/clock?year={year}").status_code == 400
