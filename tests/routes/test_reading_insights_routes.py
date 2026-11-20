"""/api/reading-insights/summary and /api/account/reading-goal (routes/insights.py)."""
import pathlib
import re
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from core.database import (
    get_user_setting_override,
    hide_issue_from_history,
    mark_issue_read,
    save_reading_position,
)
from models.reading_insights import get_reading_goal, set_reading_goal

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Wednesday 12 June 2024, midday UTC.
NOW = datetime(2024, 6, 12, 12, 0, tzinfo=timezone.utc)


def _read(path, days_ago, **kwargs):
    stamp = (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")
    mark_issue_read(path, read_at=stamp, **kwargs)


@pytest.fixture
def frozen_now():
    with patch("core.user_time.utc_now", return_value=NOW):
        yield


@pytest.fixture
def history(db_connection, frozen_now):
    _read("/data/DC/Batman/Batman 001.cbz", 0, page_count=20, time_spent=600)
    _read("/data/DC/Batman/Batman 002.cbz", 1, page_count=20, time_spent=600)
    _read("/data/Image/Saga/Saga 001.cbz", 2, page_count=30, time_spent=900)
    _read("/data/Image/Saga/Saga 002.cbz", 8)
    _read("/data/Image/Saga/Saga 003.cbz", 200)          # 25 Nov 2023
    save_reading_position("/data/DC/Batman/Batman 003.cbz", page_number=6,
                          total_pages=24, time_spent=120)


class TestSummary:
    def test_shape(self, client, history):
        body = client.get("/api/reading-insights/summary").get_json()
        assert body["success"] is True
        assert body["today"] == "2024-06-12"
        assert set(body) >= {"goal", "streak", "weeks", "five_weeks", "shelf"}

    def test_goal_defaults_to_this_year_with_no_goal(self, client, history):
        goal = client.get("/api/reading-insights/summary").get_json()["goal"]
        assert (goal["year"], goal["read"], goal["goal"]) == (2024, 4, None)
        assert goal["projected"] > 4

    def test_goal_for_another_year(self, client, history):
        goal = client.get("/api/reading-insights/summary?year=2023").get_json()["goal"]
        assert (goal["year"], goal["read"], goal["complete"]) == (2023, 1, True)

    def test_goal_progress_once_set(self, client, history):
        set_reading_goal(2024, 8)
        goal = client.get("/api/reading-insights/summary").get_json()["goal"]
        assert goal["goal"] == 8
        assert goal["pct"] == 50.0
        assert goal["reached"] is False

    def test_streak_and_weeks(self, client, history):
        body = client.get("/api/reading-insights/summary").get_json()
        assert body["streak"]["current"] == 3
        assert body["streak"]["longest"] == 3
        assert body["weeks"]["this_week"]["issues"] == 3
        assert body["weeks"]["last_week"]["issues"] == 1
        assert body["weeks"]["this_week"]["by_day"] == [1, 1, 1, 0, 0, 0, 0]

    def test_five_weeks_are_split_by_series(self, client, history):
        five = client.get("/api/reading-insights/summary").get_json()["five_weeks"]
        assert {row["name"]: sum(row["counts"]) for row in five["series"]} == {"Batman": 2, "Saga": 2}
        assert sum(five["totals"]) == 4

    def test_shelf(self, client, history):
        shelf = client.get("/api/reading-insights/summary").get_json()["shelf"]
        assert [item["name"] for item in shelf["finished"]][:2] == ["Batman 001.cbz", "Batman 002.cbz"]
        assert shelf["finished"][0]["thumbnail_url"].startswith("/api/thumbnail?path=")
        (open_item,) = shelf["open"]
        assert (open_item["page"], open_item["total_pages"], open_item["percent"]) == (6, 24, 25)
        assert open_item["thumbnail_url"].startswith("/api/thumbnail?path=")

    def test_hidden_reads_count_but_are_not_shown(self, client, history):
        hide_issue_from_history("/data/DC/Batman/Batman 001.cbz")
        body = client.get("/api/reading-insights/summary").get_json()
        # Counted: the streak still reaches today and the goal still has 4.
        assert body["streak"]["current"] == 3
        assert body["goal"]["read"] == 4
        assert sum(body["five_weeks"]["totals"]) == 4
        # Not named: off the shelf, and folded into "Other" on the chart.
        assert "Batman 001.cbz" not in [i["name"] for i in body["shelf"]["finished"]]
        assert {row["name"] for row in body["five_weeks"]["series"]} == {"Batman", "Saga", "Other"}

    def test_empty_history(self, client, db_connection, frozen_now):
        body = client.get("/api/reading-insights/summary").get_json()
        assert body["goal"]["read"] == 0
        assert body["streak"] == {
            "current": 0, "longest": 0, "longest_start": None, "longest_end": None}
        assert body["five_weeks"]["series"] == []
        assert body["shelf"] == {"finished": [], "open": []}

    def test_days_follow_the_users_timezone(self, client, db_connection, frozen_now):
        from core.database import set_user_preference

        set_user_preference("timezone", "-5")
        # 02:30 UTC on the 12th is the evening of the 11th in UTC-5.
        mark_issue_read("/data/x.cbz", read_at="2024-06-12T02:30:00")
        body = client.get("/api/reading-insights/summary").get_json()
        assert body["today"] == "2024-06-12"
        assert body["weeks"]["this_week"]["by_day"] == [0, 1, 0, 0, 0, 0, 0]

    def test_per_user(self, client, history):
        mark_issue_read("/data/other.cbz", read_at="2024-06-12T09:00:00", user_id=2)
        assert client.get("/api/reading-insights/summary").get_json()["goal"]["read"] == 4

    @pytest.mark.parametrize("year", ["1500", "99999"])
    def test_invalid_year(self, client, year):
        assert client.get(f"/api/reading-insights/summary?year={year}").status_code == 400


class TestSaveGoal:
    def _save(self, client, **body):
        return client.post("/api/account/reading-goal", json=body)

    def test_set_then_change(self, client, db_connection):
        r = self._save(client, year=2024, goal=150)
        assert r.get_json() == {"success": True, "year": 2024, "goal": 150}
        assert get_reading_goal(2024) == 150
        self._save(client, year=2024, goal=200)
        assert get_reading_goal(2024) == 200

    def test_years_are_independent(self, client, db_connection):
        self._save(client, year=2024, goal=150)
        self._save(client, year=2025, goal=90)
        assert (get_reading_goal(2024), get_reading_goal(2025)) == (150, 90)
        assert get_user_setting_override("reading_goals") == {"2024": 150, "2025": 90}

    @pytest.mark.parametrize("cleared", [0, None])
    def test_clear(self, client, db_connection, cleared):
        self._save(client, year=2024, goal=150)
        self._save(client, year=2025, goal=90)
        r = self._save(client, year=2024, goal=cleared)
        assert r.get_json() == {"success": True, "year": 2024, "goal": None}
        assert get_reading_goal(2024) is None
        assert get_reading_goal(2025) == 90

    def test_goal_is_personal(self, client, db_connection):
        self._save(client, year=2024, goal=150)
        assert get_reading_goal(2024, user_id=2) is None

    @pytest.mark.parametrize("body", [
        {"year": 2024, "goal": -1},
        {"year": 2024, "goal": 100001},
        {"year": 2024, "goal": "lots"},
        {"year": "soon", "goal": 10},
        {"goal": 10},
        {"year": 1500, "goal": 10},
    ])
    def test_rejects_bad_input(self, client, db_connection, body):
        r = client.post("/api/account/reading-goal", json=body)
        assert r.status_code == 400
        assert r.get_json()["success"] is False
        assert get_user_setting_override("reading_goals") is None

    def test_a_failed_write_is_a_500(self, client, db_connection):
        with patch("routes.insights.set_reading_goal", return_value=False):
            r = self._save(client, year=2024, goal=10)
        assert r.status_code == 500

    def test_a_corrupt_stored_goal_reads_as_unset(self, db_connection):
        from core.database import set_user_setting

        set_user_setting("reading_goals", {"2024": "many", "2025": 0, "2026": 10 ** 9})
        assert [get_reading_goal(y) for y in (2024, 2025, 2026)] == [None, None, None]
        set_user_setting("reading_goals", "not a dict")
        assert get_reading_goal(2024) is None
        assert set_reading_goal(2024, 5) is True
        assert get_reading_goal(2024) == 5


class TestPageWiring:
    """The page's script moved from the template into static/js/insights.js."""

    HTML = (PROJECT_ROOT / "templates" / "insights.html").read_text(encoding="utf-8")
    JS = (PROJECT_ROOT / "static" / "js" / "insights.js").read_text(encoding="utf-8")

    def test_page_renders_the_new_cards_and_data_island(self, client, history):
        body = client.get("/insights").get_data(as_text=True)
        for element_id in ("goalDialProgress", "statCurrentStreak", "statLongestStreak",
                           "weekCompareChart", "fiveWeekChart", "open-now-grid",
                           "recently-read-grid", "goalModal"):
            assert f'id="{element_id}"' in body
        assert '<script type="application/json" id="insightsData">' in body
        assert "/static/js/insights.js?v=" in body

    def test_template_has_no_inline_script(self):
        scripts = re.findall(r"<script(?![^>]*\bsrc=)(?![^>]*application/json)[^>]*>", self.HTML)
        assert scripts == []

    def test_every_element_the_script_looks_up_exists(self):
        ids = set(re.findall(r"getElementById\('([^']+)'\)", self.JS))
        assert len(ids) > 30
        missing = sorted(i for i in ids if f'id="{i}"' not in self.HTML
                         and f"id='{i}'" not in self.HTML)
        assert missing == []

    def test_new_endpoints_are_not_under_the_login_exempt_prefix(self):
        assert "/api/reading-insights/summary" in self.JS
        assert "/api/insights/" not in self.JS
