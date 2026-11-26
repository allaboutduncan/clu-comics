"""Route tests for routes/wrapped.py (yearly and monthly Wrapped).

These routes lived in app.py and had no test at all. The slide generators are
real here: a year with reads renders PNGs through Pillow.
"""
import io
import zipfile
from datetime import datetime
from unittest.mock import patch

import pytest
from PIL import Image

from core.database import mark_issue_read

PNG_MAGIC = bytes([0x89]) + b"PNG"


@pytest.fixture
def reads(db_connection):
    for n in range(1, 4):
        mark_issue_read(f"/data/DC/Batman (2016)/Batman 00{n}.cbz",
                        read_at=f"2024-06-0{n}T10:00:00", page_count=20,
                        time_spent=600, publisher="DC Comics")
    mark_issue_read("/data/Marvel/X-Men/X-Men 001.cbz",
                    read_at="2024-07-04T10:00:00", page_count=30, publisher="Marvel")


def _png_size(data):
    assert data.startswith(PNG_MAGIC)
    return Image.open(io.BytesIO(data)).size


class TestYears:
    def test_lists_years_with_reads(self, client, reads):
        assert client.get("/api/wrapped/years").get_json() == {"years": [2024]}

    def test_defaults_to_the_current_year(self, client):
        assert client.get("/api/wrapped/years").get_json() == {"years": [datetime.now().year]}


class TestYearly:
    def test_stats(self, client, reads):
        body = client.get("/api/wrapped/2024").get_json()
        assert body["year"] == 2024
        assert body["total_read"] == 4
        assert body["most_read_series"][0]["count"] == 3
        assert "Batman" in body["most_read_series"][0]["name"]

    @pytest.mark.parametrize("slide", [1, 2, 3])
    def test_each_slide_is_a_story_sized_png(self, client, reads, slide):
        r = client.get(f"/api/wrapped/2024/image/{slide}")
        assert r.status_code == 200
        assert r.mimetype == "image/png"
        assert _png_size(r.data) == (1080, 1920)

    def test_unknown_slide(self, client, reads):
        assert client.get("/api/wrapped/2024/image/4").status_code == 400

    def test_download_holds_every_slide(self, client, reads):
        r = client.get("/api/wrapped/2024/download")
        assert r.status_code == 200
        assert r.mimetype == "application/zip"
        assert "wrapped_2024.zip" in r.headers["Content-Disposition"]
        with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
            names = sorted(zf.namelist())
            # These reads carry no credits, characters or goal, so those
            # slides are left out and the rest keep their numbered names.
            assert names == [
                "wrapped_2024/01_summary.png",
                "wrapped_2024/03_streaks.png",
                "wrapped_2024/04_time_and_pages.png",
                "wrapped_2024/05_most_read_series.png",
                "wrapped_2024/06_series_highlights.png",
                "wrapped_2024/09_reading_clock.png",
                "wrapped_2024/10_personality.png",
                "wrapped_2024/11_books_grid.png",
            ]
            assert all(zf.read(n).startswith(PNG_MAGIC) for n in names)

    def test_download_for_a_year_with_no_reads(self, client, reads):
        # There are no slides to pack. This was a 500 (None written into the
        # ZIP); an archive of nothing would be no better.
        r = client.get("/api/wrapped/2019/download")
        assert r.status_code == 404

    def test_a_render_failure_is_a_500_with_a_message(self, client, reads):
        with patch("wrapped.generate_summary_slide", side_effect=RuntimeError("boom")):
            r = client.get("/api/wrapped/2024/image/1")
        assert r.status_code == 500
        assert r.get_json() == {"error": "boom"}


class TestMonthly:
    def test_stats(self, client, reads):
        body = client.get("/api/wrapped/monthly/2024/6").get_json()
        assert (body["year"], body["month"], body["month_name"]) == (2024, 6, "June")
        assert body["total_read"] == 3
        assert body["total_pages"] == 60
        assert body["top_publisher"] == "DC Comics"

    @pytest.mark.parametrize("url", [
        "/api/wrapped/monthly/2024/13",
        "/api/wrapped/monthly/2024/0/image/1",
        "/api/wrapped/monthly/2024/13/download",
        "/api/wrapped/monthly/2024/6/image/3",
    ])
    def test_out_of_range_is_400(self, client, url):
        assert client.get(url).status_code == 400

    @pytest.mark.parametrize("slide", [1, 2])
    def test_each_slide_is_a_png(self, client, reads, slide):
        r = client.get(f"/api/wrapped/monthly/2024/6/image/{slide}")
        assert r.status_code == 200
        assert r.mimetype == "image/png"
        assert _png_size(r.data)[0] == 1080

    def test_download(self, client, reads):
        r = client.get("/api/wrapped/monthly/2024/6/download")
        assert r.status_code == 200
        assert "monthly_wrapped_2024_06.zip" in r.headers["Content-Disposition"]
        with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
            assert sorted(zf.namelist()) == [
                "monthly_wrapped_2024_06/01_monthly_recap.png",
                "monthly_wrapped_2024_06/02_month_in_numbers.png",
                "monthly_wrapped_2024_06/04_reading_clock.png",
                "monthly_wrapped_2024_06/05_all_issues.png",
            ]
