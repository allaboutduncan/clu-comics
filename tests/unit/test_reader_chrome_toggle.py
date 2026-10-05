"""Tap/click-to-hide for the comic reader's header and footer.

A tap on the page toggles `.reader-chrome-hidden` on the container. It used to
work only at `max-width: 1024px`, in both the JS (the tap handler returned early)
and the CSS (the hide rules lived inside the media query). A tablet rotated to
landscape is 1180-1366px wide, so turning it pushed the reader past the
breakpoint: the chrome snapped back and tapping no longer hid it.

There is no JS test runner in this repo, so these assert on the assets as text,
as test_reader_pan_controls.py does.
"""
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def read(*parts):
    with open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def reader_js():
    return read("static", "js", "reader.js")


@pytest.fixture(scope="module")
def reader_css():
    return read("static", "css", "reader.css")


def tap_handler(source):
    start = source.index("tap: function (swiper, event)")
    end = source.index("doubleTap:", start)
    return source[start:end]


def top_level_css(css):
    """The stylesheet with every @media block removed."""
    out, depth, i = [], 0, 0
    while i < len(css):
        if depth == 0 and css.startswith("@media", i):
            brace = css.index("{", i)
            depth, i = 1, brace + 1
            while depth:
                depth += {"{": 1, "}": -1}.get(css[i], 0)
                i += 1
            continue
        out.append(css[i])
        i += 1
    return "".join(out)


class TestTapToggle:

    def test_tap_handler_is_not_gated_on_device(self, reader_js):
        body = tap_handler(reader_js)
        assert "isMobileOrTablet" not in body
        assert "toggleReaderChrome()" in body

    def test_tap_still_ignores_zoom_and_nav_buttons(self, reader_js):
        body = tap_handler(reader_js)
        assert "zoom.scale > 1" in body
        assert ".swiper-button-next" in body

    def test_double_tap_cancels_pending_toggle(self, reader_js):
        start = reader_js.index("doubleTap:")
        body = reader_js[start:start + 400]
        assert "clearTimeout(chromeToggleTimeout)" in body


class TestRotation:

    def test_touch_devices_detected_by_input_not_width(self, reader_js):
        match = re.search(
            r"function isMobileOrTablet\(\)\s*\{\s*return window\.matchMedia\('([^']*)'\)",
            reader_js,
        )
        assert match, "isMobileOrTablet must use a single matchMedia query"
        query = match.group(1)
        assert "(hover: none)" in query and "(pointer: coarse)" in query

    def test_hide_rules_are_not_inside_a_media_query(self, reader_css):
        css = top_level_css(reader_css)
        for part in ("header", "footer"):
            selector = ".comic-reader-container.reader-chrome-hidden .comic-reader-%s" % part
            assert selector in css, "%s must apply at every width" % selector

    @pytest.mark.parametrize("part", ["header", "footer"])
    def test_chrome_overlays_at_every_width(self, reader_css, part):
        css = top_level_css(reader_css)
        rules = re.findall(r"\.comic-reader-%s\s*\{([^}]*)\}" % part, css)
        assert any("position: absolute" in r for r in rules)

    def test_modal_uses_dynamic_viewport_height(self, reader_css):
        rule = re.search(r"\.comic-reader-modal\s*\{([^}]*)\}", reader_css).group(1)
        assert "100dvh" in rule
        # The 100vh fallback must come first, or browsers without dvh get nothing.
        assert rule.index("100vh") < rule.index("100dvh")
