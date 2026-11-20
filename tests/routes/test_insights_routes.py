"""Route tests for routes/insights.py.

Insights, Timeline and the reading-stats API lived in app.py, which cannot be
imported in tests, so none of these routes had a test.
"""
import os
import pathlib
import re
from datetime import datetime
from unittest.mock import patch

import pytest

from core.database import hide_issue_from_history, mark_issue_read

A = "/data/DC/Batman/Batman 001.cbz"
B = "/data/DC/Batman/Batman 002.cbz"
C = "/data/Marvel/X-Men/X-Men 001.cbz"


@pytest.fixture
def reads(db_connection):
    mark_issue_read(A, read_at="2024-06-01T10:00:00", page_count=20, time_spent=600,
                    writer="Tom King", penciller="David Finch",
                    characters="Batman, Robin", publisher="DC Comics")
    mark_issue_read(B, read_at="2024-06-02T10:00:00", page_count=22, time_spent=3660,
                    writer="Tom King", penciller="Mikel Janin",
                    characters="Batman", publisher="DC Comics")
    mark_issue_read(C, read_at="2023-03-05T10:00:00", page_count=30, time_spent=60,
                    writer="Chris Claremont", publisher="Marvel")


class TestInsightsPage:
    def test_renders(self, client, reads):
        r = client.get("/insights")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert "Collection Stats" in body
        assert "Reading Trends" in body
        # The link to the Timeline resolves to the moved endpoint.
        assert 'href="/timeline"' in body

    def test_renders_with_no_reading_history(self, client):
        assert client.get("/insights").status_code == 200

    def test_nothing_is_added_under_the_login_exempt_prefix(self, app):
        # /api/insights is exempt from login on a segment boundary, so any rule
        # below it would be served to anyone.
        rules = [r.rule for r in app.url_map.iter_rules()]
        assert "/api/insights" in rules
        assert [r for r in rules if r.startswith("/api/insights/")] == []


class TestReadingStats:
    def test_all_time(self, client, reads):
        body = client.get("/api/reading-stats").get_json()
        assert body["success"] is True
        assert body["selected_year"] == "all"
        assert body["stats"]["total_read"] == 3
        assert body["stats"]["total_pages"] == 72
        assert body["stats"]["total_time"] == 4320
        assert (body["stats"]["hours"], body["stats"]["minutes"]) == (1, 12)

    def test_one_year(self, client, reads):
        body = client.get("/api/reading-stats?year=2024").get_json()
        assert body["selected_year"] == 2024
        assert body["stats"]["total_read"] == 2

    def test_years_include_the_current_year(self, client, reads):
        years = client.get("/api/reading-stats?year=all").get_json()["years"]
        assert years[0] == datetime.now().year
        assert set(years) >= {2024, 2023}

    def test_invalid_year(self, client):
        r = client.get("/api/reading-stats?year=nope")
        assert r.status_code == 400
        assert r.get_json()["success"] is False


class TestReadingTrends:
    def test_writers_ranked(self, client, reads):
        body = client.get("/api/reading-trends/writer").get_json()
        assert body[0] == {"name": "Tom King", "count": 2}
        assert {"name": "Chris Claremont", "count": 1} in body

    def test_comma_separated_characters_are_split(self, client, reads):
        body = client.get("/api/reading-trends/characters").get_json()
        assert {(row["name"], row["count"]) for row in body} == {("Batman", 2), ("Robin", 1)}

    def test_year_and_limit(self, client, reads):
        body = client.get("/api/reading-trends/publisher?year=2023&limit=1").get_json()
        assert body == [{"name": "Marvel", "count": 1}]

    def test_unknown_field(self, client):
        r = client.get("/api/reading-trends/genre")
        assert r.status_code == 400
        assert "Invalid field" in r.get_json()["error"]


class TestRecentlyRead:
    def test_lists_existing_files_newest_first(self, client, reads):
        with patch("routes.insights.os.path.exists", side_effect=lambda p: p != C):
            body = client.get("/api/recently-read").get_json()
        assert [row["path"] for row in body] == [B, A]
        assert body[0]["name"] == os.path.basename(B)
        assert body[0]["thumbnail_url"].startswith("/api/thumbnail?path=")

    def test_limit(self, client, reads):
        with patch("routes.insights.os.path.exists", return_value=True):
            body = client.get("/api/recently-read?limit=1").get_json()
        assert [row["path"] for row in body] == [B]


class TestTimeline:
    def test_page_renders_with_this_users_years(self, client, reads):
        r = client.get("/timeline")
        assert r.status_code == 200
        body = r.get_data(as_text=True)
        assert '<option value="2024"' in body
        assert '<option value="2023"' in body

    def test_hidden_year_is_not_offered(self, client, reads):
        hide_issue_from_history(C)
        body = client.get("/timeline").get_data(as_text=True)
        assert '<option value="2023"' not in body

    def test_page_filter(self, client, reads):
        r = client.get("/timeline?year=2024&month=6")
        assert r.status_code == 200
        assert '<option value="2024" selected' in r.get_data(as_text=True)

    def test_api_page(self, client, reads):
        body = client.get("/api/timeline?year=2024").get_json()
        assert body["has_more"] is True
        entries = [e for group in body["timeline"] for e in group["entries"]]
        assert {e["issue_path"] for e in entries} == {A, B}
        assert all(e["thumbnail_url"].startswith("/api/thumbnail?path=") for e in entries)
        assert "total_read" in body["stats"]

    def test_api_past_the_end(self, client, reads):
        body = client.get("/api/timeline?offset=500").get_json()
        assert body["timeline"] == []
        assert body["has_more"] is False


def test_app_registers_the_blueprints_and_keeps_no_copy():
    """app.py cannot be imported here, so its half of the move is asserted over
    its source: both blueprints registered, and no second copy of a moved route
    left behind (Flask would serve whichever registered first)."""
    source = (pathlib.Path(__file__).resolve().parents[2] / "app.py").read_text(encoding="utf-8")
    assert "app.register_blueprint(insights_bp)" in source
    assert "app.register_blueprint(wrapped_bp)" in source
    moved = re.findall(
        r'@app\.route\(\s*"(/insights|/timeline|/api/insights|/api/timeline|/api/wrapped[^"]*'
        r'|/api/reading-stats|/api/reading-trends[^"]*|/api/recently-read|/api/mark-comic-read)"',
        source,
    )
    assert moved == []
