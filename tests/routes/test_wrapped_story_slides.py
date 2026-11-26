"""The Wrapped story: models.reading_insights.period_story, the reading
personality rule table, and the slides drawn from them (wrapped_story.py)."""
import io
from datetime import date, datetime, timedelta, timezone

import pytest
from PIL import Image

import wrapped_story
from core import wrapped_slides
from core.database import hide_issue_from_history, mark_issue_read, set_user_preference
from core.wrapped_slides import MONTHLY, YEARLY
from models import reading_insights as ri
from models.reading_insights import (
    ReadEvent,
    period_story,
    reading_personality,
    set_reading_goal,
    top_credits,
)

PNG_MAGIC = bytes([0x89]) + b"PNG"
TODAY = date(2025, 3, 1)       # after the year the fixtures read in


def summary(**overrides):
    """A year of unremarkable reading; each test bends one thing."""
    base = {
        "scope": "yearly", "issues": 60, "active_days": 40, "period_days": 365,
        "longest_streak": 4, "binge_share": 0.1, "top_series": "Batman",
        "top_series_share": 0.2, "distinct_series": 12, "distinct_publishers": 3,
        "top_publisher": "DC Comics", "top_publisher_share": 0.5, "timed_share": 0.9,
        "night_share": 0.1, "morning_share": 0.1, "weekend_share": 0.3,
    }
    base.update(overrides)
    return base


class TestPersonalityRules:
    @pytest.mark.parametrize("overrides,expected", [
        ({"issues": 4}, "starting"),
        ({"active_days": 230}, "ritualist"),
        ({"longest_streak": 30}, "ritualist"),
        ({"binge_share": 0.6}, "binger"),
        ({"top_series_share": 0.5}, "devotee"),
        ({"distinct_series": 40, "distinct_publishers": 5}, "explorer"),
        ({"top_publisher_share": 0.85}, "loyalist"),
        ({"night_share": 0.55}, "night-owl"),
        ({"morning_share": 0.5}, "early-bird"),
        ({"weekend_share": 0.6}, "weekender"),
        ({"issues": 300, "distinct_series": 40}, "marathoner"),
        ({}, "steady"),
    ])
    def test_each_rule(self, overrides, expected):
        assert reading_personality(summary(**overrides))["id"] == expected

    @pytest.mark.parametrize("overrides,expected", [
        # Just short of each threshold falls through to the default.
        ({"active_days": 218, "longest_streak": 29}, "steady"),
        ({"binge_share": 0.49}, "steady"),
        ({"top_series_share": 0.49}, "steady"),
        ({"distinct_series": 40, "distinct_publishers": 4}, "steady"),
        ({"top_publisher_share": 0.85, "distinct_series": 4}, "steady"),
        ({"weekend_share": 0.59}, "steady"),
        ({"issues": 299, "distinct_series": 40}, "steady"),
    ])
    def test_thresholds(self, overrides, expected):
        assert reading_personality(summary(**overrides))["id"] == expected

    def test_order_habit_before_taste_before_volume(self):
        everything = summary(issues=400, active_days=300, binge_share=0.9,
                             top_series_share=0.9, weekend_share=0.9)
        assert reading_personality(everything)["id"] == "ritualist"
        assert reading_personality(summary(top_series_share=0.9, weekend_share=0.9))["id"] == "devotee"

    def test_a_binge_needs_long_quiet_spells_too(self):
        # Five a day, every day, is a routine rather than a binge.
        assert reading_personality(summary(binge_share=0.9, active_days=150))["id"] != "binger"

    def test_the_clock_is_only_trusted_when_most_reads_are_timed(self):
        untimed = summary(night_share=0.9, timed_share=0.5)
        assert reading_personality(untimed)["id"] == "steady"

    def test_monthly_thresholds_are_smaller(self):
        month = summary(scope="monthly", issues=30, period_days=30, active_days=10,
                        distinct_series=20)
        assert reading_personality(month)["id"] == "marathoner"
        assert reading_personality(summary(scope="monthly", period_days=30, active_days=10,
                                           longest_streak=14))["id"] == "ritualist"

    def test_result_carries_label_blurb_and_evidence(self):
        result = reading_personality(summary(top_series_share=0.62, top_series="Saga"))
        assert result["label"] == "The Devotee"
        assert result["blurb"]
        assert result["evidence"] == "62% of your reading was Saga"

    def test_every_personality_has_a_label_and_a_blurb(self):
        assert all(label and blurb for label, blurb in ri.PERSONALITIES.values())

    def test_empty_summary_does_not_raise(self):
        assert reading_personality({})["id"] == "starting"


class TestTopCredits:
    def _event(self, **kwargs):
        return ReadEvent(path="/x.cbz", at=datetime(2024, 6, 1, tzinfo=timezone.utc), **kwargs)

    def test_counts_reads_not_mentions(self):
        events = [self._event(writers=("Tom King", "tom king")),
                  self._event(writers=("Tom King", "Scott Snyder"))]
        assert top_credits(events)["writers"] == [
            {"name": "Tom King", "count": 2}, {"name": "Scott Snyder", "count": 1}]

    def test_limit_and_ties_by_name(self):
        events = [self._event(characters=("Zatanna", "Aquaman", "Batman"))]
        assert [r["name"] for r in top_credits(events, limit=2)["characters"]] == ["Aquaman", "Batman"]

    def test_publishers_and_blanks(self):
        events = [self._event(publisher="DC Comics"), self._event(publisher="")]
        assert top_credits(events)["publishers"] == [{"name": "DC Comics", "count": 1}]
        assert top_credits(events)["artists"] == []


@pytest.fixture
def year(db_connection):
    """2024: a four-day June streak on Batman, one Saga in July, and one hidden read."""
    for n in range(1, 5):
        mark_issue_read(f"/data/DC/Batman/Batman 00{n}.cbz", read_at=f"2024-06-0{n}T20:00:00",
                        page_count=20, time_spent=1200, writer="Tom King",
                        penciller="David Finch", characters="Batman, Robin",
                        publisher="DC Comics")
    mark_issue_read("/data/Image/Saga/Saga 001.cbz", read_at="2024-07-20T09:00:00",
                    page_count=40, writer="Brian K. Vaughan", penciller="Fiona Staples",
                    characters="Alana, Marko", publisher="Image")
    mark_issue_read("/data/secret.cbz", read_at="2024-06-05T20:00:00", page_count=99,
                    time_spent=9999, writer="Hidden Writer", characters="Hidden Hero")
    hide_issue_from_history("/data/secret.cbz")


class TestPeriodStory:
    def test_yearly_numbers(self, year):
        story = period_story(YEARLY, 2024, today=TODAY)
        assert (story["issues"], story["pages"], story["seconds"]) == (5, 120, 4800)
        assert (story["active_days"], story["period_days"]) == (5, 366)
        assert story["series"] == 2
        assert story["busiest_day_issues"] == 1

    def test_hidden_reads_are_left_out_entirely(self, year):
        story = period_story(YEARLY, 2024, today=TODAY)
        # Without the hidden read on the 5th the streak is four days, not five.
        assert story["longest_streak"] == 4
        assert (story["longest_streak_start"], story["longest_streak_end"]) == (
            "2024-06-01", "2024-06-04")
        names = [row["name"] for rows in story["credits"].values() for row in rows]
        assert "Hidden Writer" not in names and "Hidden Hero" not in names

    def test_credits(self, year):
        credits = period_story(YEARLY, 2024, today=TODAY)["credits"]
        assert credits["writers"][0] == {"name": "Tom King", "count": 4}
        assert credits["artists"][0] == {"name": "David Finch", "count": 4}
        assert {r["name"] for r in credits["characters"]} == {"Batman", "Robin", "Alana", "Marko"}
        assert credits["publishers"][0] == {"name": "DC Comics", "count": 4}

    def test_clock_uses_timed_reads_only(self, year):
        clock = period_story(YEARLY, 2024, today=TODAY)["clock"]
        assert clock["source"] == "finish_times"
        assert clock["peak_hour"] == 19          # 19:40-20:00, four evenings
        assert clock["untimed"] == 1             # the Saga issue carries no time

    def test_personality(self, year):
        personality = period_story(YEARLY, 2024, today=TODAY)["personality"]
        assert personality["id"] == "devotee"
        assert personality["evidence"] == "80% of your reading was Batman"

    def test_goal_only_when_set_and_only_yearly(self, year):
        assert period_story(YEARLY, 2024, today=TODAY)["goal"] is None
        set_reading_goal(2024, 10)
        goal = period_story(YEARLY, 2024, today=TODAY)["goal"]
        # The visible reads against the goal: the hidden one is not shown.
        assert (goal["read"], goal["goal"], goal["pct"], goal["complete"]) == (5, 10, 50.0, True)
        assert period_story(MONTHLY, 2024, 6, today=TODAY)["goal"] is None

    def test_month(self, year):
        story = period_story(MONTHLY, 2024, 6, today=TODAY)
        assert (story["issues"], story["period_days"], story["longest_streak"]) == (4, 30, 4)
        assert story["credits"]["writers"] == [{"name": "Tom King", "count": 4}]

    def test_a_period_in_progress_is_judged_on_its_days_so_far(self, year):
        assert period_story(MONTHLY, 2024, 6, today=date(2024, 6, 10))["period_days"] == 10
        assert period_story(YEARLY, 2024, today=date(2024, 1, 31))["period_days"] == 31

    def test_a_streak_does_not_reach_outside_the_period(self, db_connection):
        for day in ("2024-05-30", "2024-05-31", "2024-06-01", "2024-06-02"):
            mark_issue_read(f"/data/{day}.cbz", read_at=f"{day}T12:00:00")
        assert period_story(MONTHLY, 2024, 6, today=TODAY)["longest_streak"] == 2

    def test_local_days(self, db_connection):
        set_user_preference("timezone", "-5")
        # 02:30 UTC on 1 July is the evening of 30 June in UTC-5.
        mark_issue_read("/data/a.cbz", read_at="2024-07-01T02:30:00", time_spent=600)
        assert period_story(MONTHLY, 2024, 6, today=TODAY)["issues"] == 1
        assert period_story(MONTHLY, 2024, 7, today=TODAY)["issues"] == 0

    def test_empty_period(self, db_connection):
        story = period_story(YEARLY, 2019, today=TODAY)
        assert (story["issues"], story["longest_streak"], story["busiest_day"]) == (0, 0, None)
        assert story["clock"]["source"] == "none"
        assert story["personality"]["id"] == "starting"

    def test_per_user(self, year):
        mark_issue_read("/data/other.cbz", read_at="2024-06-10T10:00:00", user_id=2)
        assert period_story(YEARLY, 2024, user_id=2, today=TODAY)["issues"] == 1
        assert period_story(YEARLY, 2024, user_id=1, today=TODAY)["issues"] == 5


class TestManifestFollowsTheStory:
    def _ids(self, client, url):
        return [s["id"] for s in client.get(url).get_json()["slides"]]

    def test_full_year(self, client, year):
        set_reading_goal(2024, 10)
        assert self._ids(client, "/api/wrapped/2024/manifest") == [
            "summary", "goal", "streaks", "time-pages", "most-read-series",
            "series-highlights", "creators", "characters", "reading-clock",
            "personality", "books-grid"]

    def test_full_month(self, client, year):
        assert self._ids(client, "/api/wrapped/monthly/2024/6/manifest") == [
            "recap", "numbers", "creators", "reading-clock", "all-issues"]

    def test_no_goal_no_goal_slide(self, client, year):
        assert "goal" not in self._ids(client, "/api/wrapped/2024/manifest")

    def test_reads_with_no_metadata_or_time(self, client, db_connection):
        mark_issue_read("/data/a/a 001.cbz", read_at="2024-06-01T10:00:00")
        ids = self._ids(client, "/api/wrapped/2024/manifest")
        for absent in ("goal", "time-pages", "creators", "characters", "reading-clock"):
            assert absent not in ids
        assert {"summary", "streaks", "personality", "books-grid"} <= set(ids)

    def test_filenames_are_numbered_in_story_order(self, client, year):
        set_reading_goal(2024, 10)
        names = [s["filename"] for s in client.get("/api/wrapped/2024/manifest").get_json()["slides"]]
        assert names == sorted(names)
        assert len(set(names)) == len(names)

    def test_a_backfilled_credit_redraws_the_slide(self, client, db_connection):
        mark_issue_read("/data/a/a 001.cbz", read_at="2024-06-01T10:00:00", time_spent=600)
        before = client.get("/api/wrapped/2024/manifest").get_json()["slides"][0]["version"]
        mark_issue_read("/data/a/a 001.cbz", read_at="2024-06-01T10:00:00", time_spent=600,
                        writer="Tom King")
        after = client.get("/api/wrapped/2024/manifest").get_json()
        assert after["slides"][0]["version"] != before
        assert "creators" in [s["id"] for s in after["slides"]]


class TestSlidesDraw:
    @pytest.mark.parametrize("slide", [
        "goal", "streaks", "time-pages", "creators", "characters", "reading-clock", "personality"])
    def test_yearly(self, client, year, slide):
        set_reading_goal(2024, 10)
        r = client.get(f"/api/wrapped/2024/slide/{slide}.png")
        assert r.status_code == 200
        assert Image.open(io.BytesIO(r.data)).size == (1080, 1920)

    @pytest.mark.parametrize("slide", ["numbers", "creators", "reading-clock"])
    def test_monthly(self, client, year, slide):
        r = client.get(f"/api/wrapped/monthly/2024/6/slide/{slide}.png")
        assert r.status_code == 200
        assert Image.open(io.BytesIO(r.data)).size == (1080, 1920)

    def test_long_names_and_big_numbers_do_not_break_layout(self, db_connection):
        long_name = "The Extraordinarily Long-Named Writer Of Many Hyphenated-Surnames III"
        for n in range(12):
            mark_issue_read(f"/data/s/s {n:03d}.cbz", read_at=f"2024-06-{n + 1:02d}T22:30:00",
                            page_count=99999, time_spent=86400, writer=long_name,
                            penciller=long_name, characters=", ".join(f"{long_name} {i}" for i in range(8)),
                            publisher=long_name)
        set_reading_goal(2024, 100000)
        for spec in wrapped_slides.slides_for(YEARLY):
            if spec.module == "wrapped_story":
                data = wrapped_slides.render_slide(YEARLY, spec.id, 2024, user_id=1)
                assert data and data.startswith(PNG_MAGIC), spec.id

    def test_a_goal_already_passed(self, year):
        set_reading_goal(2024, 3)
        assert wrapped_slides.render_slide(YEARLY, "goal", 2024, user_id=1).startswith(PNG_MAGIC)


class TestFormatting:
    @pytest.mark.parametrize("seconds,expected", [
        (0, "0m"), (59, "1m"), (2280, "38m"), (3600, "1h 0m"), (152100, "42h 15m"),
        (3_600_000, "1,000h 0m"),
    ])
    def test_duration(self, seconds, expected):
        assert wrapped_story.format_duration(seconds) == expected

    @pytest.mark.parametrize("hour,expected", [
        (0, "12am"), (1, "1am"), (12, "12pm"), (17, "5pm"), (23, "11pm"), (24, "12am")])
    def test_hour(self, hour, expected):
        assert wrapped_story.format_hour(hour) == expected

    def test_day_has_no_leading_zero(self):
        assert wrapped_story.format_day("2024-06-03") == "Jun 3"
        assert wrapped_story.format_day("2024-06-03", with_year=True) == "Jun 3, 2024"
        assert wrapped_story.format_day(None) == ""

    def test_personality_name_splits_onto_two_lines(self):
        assert wrapped_story._two_lines("The Daily Ritualist") == ["The Daily", "Ritualist"]
        assert wrapped_story._two_lines("Marathoner") == ["Marathoner"]
