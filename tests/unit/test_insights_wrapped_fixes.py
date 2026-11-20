"""Small Insights / Wrapped regressions that have no route test of their own."""
import pathlib
import re
from unittest.mock import patch

import wrapped

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
TEMPLATES = sorted((PROJECT_ROOT / "templates").glob("**/*.html"))


class TestYearlyZipSkipsEmptySlides:
    """The books grid returns None for a year with no reads. Writing None into
    the ZIP raised, so "Download All" answered 500."""

    def test_unrendered_slides_are_left_out(self):
        with patch.object(wrapped, "generate_summary_slide", return_value=b"a"), \
             patch.object(wrapped, "generate_most_read_series_slide", return_value=b"b"), \
             patch.object(wrapped, "generate_series_highlights_slide", return_value=None), \
             patch.object(wrapped, "generate_books_grid_slide", return_value=None):
            slides = wrapped.generate_all_wrapped_images(2020, "default")
        assert [name for name, _ in slides] == ["01_summary.png", "02_most_read_series.png"]
        assert all(data for _, data in slides)

    def test_full_year_keeps_every_slide_in_order(self):
        with patch.object(wrapped, "generate_summary_slide", return_value=b"a"), \
             patch.object(wrapped, "generate_most_read_series_slide", return_value=b"b"), \
             patch.object(wrapped, "generate_series_highlights_slide", return_value=b"c"), \
             patch.object(wrapped, "generate_books_grid_slide", return_value=b"d"):
            slides = wrapped.generate_all_wrapped_images(2020, "default")
        assert [name for name, _ in slides] == [
            "01_summary.png", "02_most_read_series.png",
            "03_series_highlights.png", "04_books_grid.png",
        ]


def test_every_stat_icon_colour_used_has_a_rule():
    """`stat-icon danger` was used by the template with no CSS behind it."""
    html = (PROJECT_ROOT / "templates" / "insights.html").read_text(encoding="utf-8")
    css = (PROJECT_ROOT / "static" / "css" / "insights.css").read_text(encoding="utf-8")
    used = set(re.findall(r'class="stat-icon (\w+)"', html))
    assert used, "no stat icons found: the pattern no longer matches the template"
    missing = sorted(c for c in used if f".stat-icon.{c}" not in css)
    assert missing == []


def test_cdn_scripts_are_version_pinned():
    """An unversioned CDN URL (`/npm/chart.js`) follows the package's latest
    release, so a major version can change under the page without a deploy."""
    assert len(TEMPLATES) > 10, "template glob matched almost nothing"
    rx = re.compile(r'<script[^>]+src="(https?://[^"]+)"')
    urls = [
        (p.relative_to(PROJECT_ROOT).as_posix(), url)
        for p in TEMPLATES
        for url in rx.findall(p.read_text(encoding="utf-8"))
    ]
    assert urls, "no CDN scripts found: the pattern no longer matches the templates"
    unpinned = [(p, u) for p, u in urls if not re.search(r"@\d", u)]
    assert unpinned == []
