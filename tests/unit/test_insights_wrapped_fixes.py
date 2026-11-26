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


class TestBrandLogoStaysOutOfTheWay:
    """The CLU logo was pasted 400px wide from y=80 to y=480, over the big
    number on the summary slide and the heading on the most-read slide."""

    BACKGROUND = (10, 20, 30)

    def _branded(self):
        import os

        from PIL import Image, ImageDraw

        assert os.path.exists(wrapped.ImageUtils.get_logo_path()), "no logo: this test proves nothing"
        img = Image.new("RGB", (wrapped.IMAGE_WIDTH, wrapped.IMAGE_HEIGHT), self.BACKGROUND)
        wrapped.add_branding(img, ImageDraw.Draw(img), wrapped.get_theme_colors("default"), 2024)
        return img

    def _untouched(self, img, box):
        region = img.crop(box)
        return region.getcolors() == [(region.width * region.height, self.BACKGROUND)]

    def test_the_logo_is_drawn(self):
        img = self._branded()
        left = (wrapped.IMAGE_WIDTH - wrapped.BRAND_LOGO_WIDTH) // 2
        box = (left, wrapped.BRAND_LOGO_TOP, left + wrapped.BRAND_LOGO_WIDTH,
               wrapped.BRAND_LOGO_TOP + wrapped.BRAND_LOGO_WIDTH)
        assert not self._untouched(img, box)

    def test_it_is_small_and_ends_above_the_first_line_of_content(self):
        # The earliest content on a slide that carries the logo is the
        # most-read heading at y=230.
        assert wrapped.BRAND_LOGO_WIDTH <= 140
        img = self._branded()
        assert self._untouched(img, (0, 200, wrapped.IMAGE_WIDTH, 1700))

    def test_nothing_beside_it(self):
        img = self._branded()
        left = (wrapped.IMAGE_WIDTH - wrapped.BRAND_LOGO_WIDTH) // 2
        assert self._untouched(img, (0, 0, left - 4, 200))
        assert self._untouched(img, (left + wrapped.BRAND_LOGO_WIDTH + 4, 0, wrapped.IMAGE_WIDTH, 200))

