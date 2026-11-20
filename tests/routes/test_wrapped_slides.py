"""core/wrapped_slides.py through its routes: the manifest, slides by id, the
PNG cache and the per-user theme."""
import io
import os
import time
import zipfile
from unittest.mock import patch

import pytest
from PIL import Image

from core import wrapped_slides
from core.database import (
    hide_issue_from_history,
    mark_issue_read,
    set_user_preference,
    set_user_setting,
)

PNG_MAGIC = bytes([0x89]) + b"PNG"
FAKE_PNG = PNG_MAGIC + b"-fake-slide"


@pytest.fixture
def reads(db_connection):
    for n in range(1, 4):
        mark_issue_read(f"/data/DC/Batman (2016)/Batman 00{n}.cbz",
                        read_at=f"2024-06-0{n}T10:00:00", page_count=20,
                        time_spent=600, publisher="DC Comics")
    mark_issue_read("/data/Marvel/X-Men/X-Men 001.cbz",
                    read_at="2024-07-04T10:00:00", page_count=30, publisher="Marvel")


@pytest.fixture
def counted():
    """Replace the yearly summary generator with a counter."""
    with patch("wrapped.generate_summary_slide", return_value=FAKE_PNG) as generator:
        yield generator


def _cache_files(tmp_path):
    root = tmp_path / "cache" / "wrapped"
    return sorted(p for p in root.rglob("*.png")) if root.exists() else []


class TestManifest:
    def test_yearly_slides_in_order_with_urls(self, client, reads):
        body = client.get("/api/wrapped/2024/manifest").get_json()
        assert (body["scope"], body["year"], body["month"]) == ("yearly", 2024, None)
        assert body["style"] == "theme"
        assert [s["id"] for s in body["slides"]] == [
            "summary", "most-read-series", "series-highlights", "books-grid"]
        assert [s["filename"] for s in body["slides"]][0] == "01_summary.png"
        first = body["slides"][0]
        assert first["url"] == f"/api/wrapped/2024/slide/summary.png?v={first['version']}"
        assert all(s["title"] for s in body["slides"])

    def test_monthly(self, client, reads):
        body = client.get("/api/wrapped/monthly/2024/6/manifest").get_json()
        assert (body["scope"], body["month"]) == ("monthly", 6)
        assert [s["id"] for s in body["slides"]] == ["recap", "all-issues"]
        assert body["slides"][0]["url"].startswith("/api/wrapped/monthly/2024/6/slide/recap.png?v=")

    def test_a_period_with_no_reads_has_no_slides(self, client, reads):
        assert client.get("/api/wrapped/2019/manifest").get_json()["slides"] == []
        assert client.get("/api/wrapped/monthly/2024/1/manifest").get_json()["slides"] == []

    def test_every_manifest_url_serves_a_png(self, client, reads):
        for url in ("/api/wrapped/2024/manifest", "/api/wrapped/monthly/2024/6/manifest"):
            for slide in client.get(url).get_json()["slides"]:
                r = client.get(slide["url"])
                assert r.status_code == 200, slide["id"]
                assert r.data.startswith(PNG_MAGIC)

    def test_an_unknown_style_falls_back(self, client, reads):
        body = client.get("/api/wrapped/2024/manifest?style=vaporwave").get_json()
        assert body["style"] == "theme"

    @pytest.mark.parametrize("url", [
        "/api/wrapped/1500/manifest", "/api/wrapped/monthly/2024/13/manifest",
        "/api/wrapped/99999/slide/summary.png", "/api/wrapped/monthly/2024/0/slide/recap.png",
    ])
    def test_out_of_range_period(self, client, url):
        assert client.get(url).status_code == 400


class TestSlide:
    def test_unknown_slide_is_404(self, client, reads):
        assert client.get("/api/wrapped/2024/slide/nope.png").status_code == 404
        # A monthly id is not a yearly slide.
        assert client.get("/api/wrapped/2024/slide/recap.png").status_code == 404

    def test_story_sized(self, client, reads):
        r = client.get("/api/wrapped/2024/slide/summary.png")
        assert Image.open(io.BytesIO(r.data)).size == (1080, 1920)

    def test_the_books_grid_is_now_viewable(self, client, reads):
        # It was in the ZIP only; the numbered image route stopped at three.
        assert client.get("/api/wrapped/2024/slide/books-grid.png").status_code == 200

    def test_nothing_to_draw_is_404(self, client, reads):
        with patch("wrapped.generate_books_grid_slide", return_value=None):
            assert client.get("/api/wrapped/2024/slide/books-grid.png").status_code == 404

    def test_a_render_failure_is_a_500(self, client, reads):
        with patch("wrapped.generate_summary_slide", side_effect=RuntimeError("boom")):
            r = client.get("/api/wrapped/2024/slide/summary.png")
        assert (r.status_code, r.get_json()) == (500, {"error": "boom"})

    def test_legacy_numbers_map_to_ids(self, client, reads, counted):
        assert client.get("/api/wrapped/2024/image/1").data == FAKE_PNG
        assert client.get("/api/wrapped/2024/slide/summary.png").data == FAKE_PNG
        assert counted.call_count == 1          # the second came from the cache


class TestCacheHeaders:
    def test_the_manifest_url_may_be_kept_forever(self, client, reads):
        slide = client.get("/api/wrapped/2024/manifest").get_json()["slides"][0]
        cache = client.get(slide["url"]).headers["Cache-Control"]
        assert "immutable" in cache and "private" in cache and "public" not in cache

    def test_no_version_or_a_stale_one_must_revalidate(self, client, reads):
        for url in ("/api/wrapped/2024/slide/summary.png",
                    "/api/wrapped/2024/slide/summary.png?v=0123456789abcdef"):
            assert client.get(url).headers["Cache-Control"] == "private, no-cache"

    def test_legacy_route_must_revalidate(self, client, reads):
        assert client.get("/api/wrapped/2024/image/1").headers["Cache-Control"] == "private, no-cache"


class TestCache:
    def test_a_second_request_does_not_redraw(self, client, reads, counted, tmp_path):
        for _ in range(3):
            assert client.get("/api/wrapped/2024/slide/summary.png").data == FAKE_PNG
        assert counted.call_count == 1
        (cached,) = _cache_files(tmp_path)
        assert cached.parent.name == "1"            # the user's own directory
        assert cached.read_bytes() == FAKE_PNG

    def test_a_new_read_redraws(self, client, reads, counted):
        client.get("/api/wrapped/2024/slide/summary.png")
        mark_issue_read("/data/new.cbz", read_at="2024-08-01T10:00:00")
        client.get("/api/wrapped/2024/slide/summary.png")
        assert counted.call_count == 2

    def test_a_read_in_another_year_does_not(self, client, reads, counted):
        client.get("/api/wrapped/2024/slide/summary.png")
        mark_issue_read("/data/new.cbz", read_at="2023-08-01T10:00:00")
        client.get("/api/wrapped/2024/slide/summary.png")
        assert counted.call_count == 1

    def test_hiding_a_read_redraws(self, client, reads, counted):
        client.get("/api/wrapped/2024/slide/summary.png")
        hide_issue_from_history("/data/Marvel/X-Men/X-Men 001.cbz")
        client.get("/api/wrapped/2024/slide/summary.png")
        assert counted.call_count == 2

    def test_changing_theme_redraws_and_changes_the_version(self, client, reads, counted):
        before = client.get("/api/wrapped/2024/manifest").get_json()["slides"][0]["version"]
        client.get("/api/wrapped/2024/slide/summary.png")
        set_user_setting("bootstrap_theme", "darkly")
        after = client.get("/api/wrapped/2024/manifest").get_json()["slides"][0]["version"]
        client.get("/api/wrapped/2024/slide/summary.png")
        assert before != after
        assert counted.call_count == 2

    def test_changing_the_timezone_changes_the_version(self, client, reads):
        before = client.get("/api/wrapped/2024/manifest").get_json()["slides"][0]["version"]
        set_user_preference("timezone", "-5")
        after = client.get("/api/wrapped/2024/manifest").get_json()["slides"][0]["version"]
        assert before != after

    def test_an_empty_render_is_not_cached(self, client, reads, tmp_path):
        with patch("wrapped.generate_summary_slide", return_value=None):
            assert client.get("/api/wrapped/2024/slide/summary.png").status_code == 404
        assert _cache_files(tmp_path) == []
        assert client.get("/api/wrapped/2024/slide/summary.png").status_code == 200

    def test_an_unwritable_cache_still_serves_the_slide(self, client, reads, counted):
        with patch("core.wrapped_slides.os.makedirs", side_effect=OSError("read-only")):
            assert client.get("/api/wrapped/2024/slide/summary.png").data == FAKE_PNG

    def test_an_unreadable_database_draws_without_caching(self, client, reads, counted, tmp_path):
        with patch("core.wrapped_slides.period_fingerprint", return_value=None):
            assert client.get("/api/wrapped/2024/slide/summary.png").data == FAKE_PNG
            assert client.get("/api/wrapped/2024/slide/summary.png").data == FAKE_PNG
        assert counted.call_count == 2
        assert _cache_files(tmp_path) == []

    def test_slides_nobody_asked_for_are_pruned(self, tmp_path):
        directory = tmp_path / "cache" / "wrapped" / "1"
        directory.mkdir(parents=True)
        old, fresh, other = directory / "old.png", directory / "fresh.png", directory / "notes.txt"
        for path in (old, fresh, other):
            path.write_bytes(b"x")
        long_ago = time.time() - wrapped_slides.MAX_CACHE_AGE_SECONDS - 60
        os.utime(old, (long_ago, long_ago))
        os.utime(other, (long_ago, long_ago))
        assert wrapped_slides._prune(str(directory)) == 1
        assert not old.exists() and fresh.exists()
        assert other.exists()                       # not ours: left alone

    def test_a_cache_hit_counts_as_use(self, client, reads, counted, tmp_path):
        client.get("/api/wrapped/2024/slide/summary.png")
        (cached,) = _cache_files(tmp_path)
        long_ago = time.time() - 10 * 24 * 3600
        os.utime(cached, (long_ago, long_ago))
        client.get("/api/wrapped/2024/slide/summary.png")
        assert cached.stat().st_mtime > long_ago + 3600


class TestTheme:
    def test_the_users_own_theme_is_used(self, client, reads, counted):
        set_user_preference("bootstrap_theme", "flatly")       # the site's
        set_user_setting("bootstrap_theme", "darkly")           # theirs
        assert client.get("/api/wrapped/2024/manifest").get_json()["theme"] == "darkly"
        client.get("/api/wrapped/2024/slide/summary.png")
        assert counted.call_args.args == (2024, "darkly")

    def test_the_site_theme_is_the_fallback(self, client, reads):
        set_user_preference("bootstrap_theme", "flatly")
        assert client.get("/api/wrapped/2024/manifest").get_json()["theme"] == "flatly"

    def test_an_unknown_stored_theme_reads_as_default(self, client, reads):
        set_user_setting("bootstrap_theme", "not-a-theme")
        assert client.get("/api/wrapped/2024/manifest").get_json()["theme"] == "default"


class TestZip:
    def test_holds_exactly_the_manifest(self, client, reads):
        manifest = client.get("/api/wrapped/2024/manifest").get_json()["slides"]
        r = client.get("/api/wrapped/2024/download")
        with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
            assert sorted(zf.namelist()) == sorted(
                f"wrapped_2024/{s['filename']}" for s in manifest)

    def test_reuses_slides_the_viewer_already_drew(self, client, reads, counted):
        client.get("/api/wrapped/2024/slide/summary.png")
        client.get("/api/wrapped/2024/download")
        assert counted.call_count == 1

    def test_a_slide_with_nothing_to_draw_is_left_out(self, client, reads):
        with patch("wrapped.generate_books_grid_slide", return_value=None):
            r = client.get("/api/wrapped/2024/download")
        with zipfile.ZipFile(io.BytesIO(r.data)) as zf:
            assert "wrapped_2024/04_books_grid.png" not in zf.namelist()
            assert len(zf.namelist()) == 3

    @pytest.mark.parametrize("url", ["/api/wrapped/2019/download",
                                     "/api/wrapped/monthly/2024/1/download"])
    def test_an_empty_period_is_a_404_not_an_empty_zip(self, client, reads, url):
        r = client.get(url)
        assert r.status_code == 404
        assert "error" in r.get_json()


class TestPerUser:
    def test_fingerprint_and_cache_are_per_user(self, reads):
        mark_issue_read("/data/other.cbz", read_at="2024-06-01T10:00:00", user_id=2)
        assert wrapped_slides.period_fingerprint(2024, user_id=1) != \
            wrapped_slides.period_fingerprint(2024, user_id=2)
        assert wrapped_slides._cache_dir(1) != wrapped_slides._cache_dir(2)

    def test_no_route_takes_a_user_id(self, app):
        rules = [r.rule for r in app.url_map.iter_rules() if r.rule.startswith("/api/wrapped")]
        assert rules and not any("user" in rule for rule in rules)
