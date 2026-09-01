"""Keyboard/wheel panning of the zoomed comic reader.

Zooming always centres on the page, so without a way to move the zoom window
the reader can only ever show the middle of the artwork. The pan controls live
entirely in `static/js/reader.js`.

There is no JS test runner in this repo, so these assert on the assets as text,
following the precedent in test_source_search_ui.py and test_themes.py. They are
a wiring guard, not a behaviour test, and they exist because of two mistakes
that are easy to make again:

- **The pan writer must never emit `scale()`.** Swiper splits the zoom across
  two elements: `translate3d()` on the `.swiper-zoom-container` and `scale()` on
  the `<img>` inside it. Writing a `scale()` of our own onto the container
  multiplies against the image's, so the first pan press blows the page up to
  scale-squared. That is precisely what the original implementation did.
- **Bare arrows must keep their existing meaning.** Left/Right turn the page and
  Up/Down zoom; panning is on modified arrows and W/A/S/D instead. A pan binding
  that swallows an unmodified arrow silently breaks page turning.
"""
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATIC_JS = os.path.join(REPO_ROOT, "static", "js")
STATIC_CSS = os.path.join(REPO_ROOT, "static", "css")
TEMPLATE_DIR = os.path.join(REPO_ROOT, "templates")

# Every page that hosts the comic reader modal. The markup is duplicated across
# all four, so anything documented in one has to be documented in all of them.
READER_TEMPLATES = [
    "collection.html",
    "metadata_browser.html",
    "reading_list_view.html",
    "source_wall.html",
]


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def function_body(source, name):
    """Slice one top-level `function name(...)` out of a JS file."""
    start = source.index("function %s(" % name)
    end = source.find("\nfunction ", start + 1)
    return source[start:end if end != -1 else len(source)]


@pytest.fixture(scope="module")
def reader_js():
    return read(os.path.join(STATIC_JS, "reader.js"))


@pytest.fixture(scope="module")
def reader_css():
    return read(os.path.join(STATIC_CSS, "reader.css"))


class TestPanTransform:

    def test_pan_writer_never_emits_scale(self, reader_js):
        # The <img> carries the scale; the container carries only the offset.
        body = function_body(reader_js, "applyZoomPan")
        assert "translate3d(" in body
        assert "scale(" not in body, (
            "applyZoomPan must not write scale() onto the zoom container -- it "
            "multiplies against the scale Swiper puts on the <img>."
        )

    def test_pan_reads_its_offset_back_off_the_zoom_container(self, reader_js):
        # Swiper re-reads the container's translate on touchstart, so reading and
        # writing the same property is what keeps keyboard and drag panning in
        # step instead of each jumping to the other's idea of the offset.
        body = function_body(reader_js, "getZoomPanContext")
        assert "translate3d" in body and "match(" in body

    def test_pan_offset_is_clamped_to_the_scaled_image(self, reader_js):
        body = function_body(reader_js, "applyZoomPan")
        assert "Math.max(-ctx.maxX, Math.min(ctx.maxX" in body
        assert "Math.max(-ctx.maxY, Math.min(ctx.maxY" in body

    def test_pan_is_a_noop_until_actually_zoomed(self, reader_js):
        body = function_body(reader_js, "getZoomPanContext")
        assert "if (scale <= 1) return null;" in body


class TestKeyBindings:

    def test_wasd_and_arrows_are_both_pan_keys(self, reader_js):
        pan_keys = reader_js[reader_js.index("const PAN_KEYS = {"):]
        pan_keys = pan_keys[:pan_keys.index("};")]
        for key in ("ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"):
            assert key in pan_keys
        for letter in ("w:", "W:", "a:", "A:", "s:", "S:", "d:", "D:"):
            assert letter in pan_keys, "missing WASD pan key %r" % letter

    def test_arrow_panning_requires_a_modifier(self, reader_js):
        body = function_body(reader_js, "handleZoomKeyboard")
        assert "event.shiftKey || event.ctrlKey" in body, (
            "bare arrows must stay page-turn/zoom; panning needs Shift or Ctrl"
        )

    def test_browser_shortcut_modifiers_are_left_alone(self, reader_js):
        # Alt+arrow is browser history and Cmd/Ctrl+letter are browser shortcuts.
        body = function_body(reader_js, "handleZoomKeyboard")
        assert "!event.altKey && !event.metaKey" in body
        assert "!(event.ctrlKey || event.altKey || event.metaKey)" in body

    def test_unmodified_arrows_still_turn_pages_and_zoom(self, reader_js):
        body = function_body(reader_js, "handleZoomKeyboard")
        assert "stepZoom('in')" in body and "stepZoom('out')" in body
        assert "slidePrev()" in body and "slideNext()" in body

    def test_recentre_key_is_bound(self, reader_js):
        body = function_body(reader_js, "handleZoomKeyboard")
        assert "'0'" in body and "'Home'" in body
        assert "recenterZoomedView()" in body

    def test_form_controls_keep_their_own_keys(self, reader_js):
        # The reader binds on `document`, and its footer holds a <select>.
        # Without this guard, W/A/S/D would eat the page selector's typeahead
        # and Space would stop it opening.
        assert "function _isFormControlTarget(" in reader_js
        for handler in ("handleZoomKeyboard", "handleComicReaderKeydown"):
            body = function_body(reader_js, handler)
            assert "_isFormControlTarget(" in body, (
                "%s must skip keys aimed at a form control" % handler
            )


class TestWheelPanning:

    def test_wheel_pans_the_page_while_zoomed(self, reader_js):
        # Swiper's mousewheel module is disabled and its zoom module ignores the
        # wheel, so the wheel was simply dead once the reader was zoomed in.
        body = function_body(reader_js, "initializeMousewheelHandler")
        assert "panZoomedView(" in body
        assert "event.shiftKey" in body, "Shift+wheel should pan horizontally"

    def test_wheel_deltas_are_normalised_to_pixels(self, reader_js):
        # Firefox reports lines (deltaMode 1); untranslated, a delta of 3 reads
        # as a 3px pan and the wheel feels dead.
        assert "function normalizeWheelDelta(" in reader_js
        body = function_body(reader_js, "initializeMousewheelHandler")
        assert "normalizeWheelDelta(" in body

    def test_page_turning_by_wheel_survives_at_1x(self, reader_js):
        body = function_body(reader_js, "initializeMousewheelHandler")
        assert "slideNext()" in body and "slidePrev()" in body


class TestPanHint:

    def test_hint_is_built_in_js_not_markup(self, reader_js):
        # The reader modal is duplicated across four templates; building the
        # hint here is what stops those copies drifting.
        assert "function ensurePanHintElement(" in reader_js
        assert "reader-pan-hint" in reader_js
        for name in READER_TEMPLATES:
            html = read(os.path.join(TEMPLATE_DIR, name))
            assert "reader-pan-hint" not in html, (
                "%s should not hand-roll the pan hint" % name
            )

    def test_hint_is_styled(self, reader_css):
        assert ".reader-pan-hint" in reader_css
        assert ".reader-pan-hint.visible" in reader_css

    def test_hint_stops_nagging_after_a_few_showings(self, reader_js):
        assert "PAN_HINT_MAX_SHOWS" in reader_js
        body = function_body(reader_js, "maybeShowPanHint")
        assert "seen >= PAN_HINT_MAX_SHOWS" in body
        # Blocked storage (private mode) must not break zooming.
        assert "try {" in body and "catch" in body

    def test_hint_is_skipped_on_touch_layouts(self, reader_js):
        body = function_body(reader_js, "maybeShowPanHint")
        assert "isMobileOrTablet()" in body


class TestTooltipsDocumentThePanKeys:

    @pytest.mark.parametrize("name", READER_TEMPLATES)
    def test_zoom_in_button_documents_panning(self, name):
        html = read(os.path.join(TEMPLATE_DIR, name))
        match = re.search(r'id="zoomInBtn"[^>]*title="([^"]*)"', html)
        assert match, "%s has no titled zoom-in button" % name
        title = match.group(1)
        assert "Shift+Arrows" in title and "WASD" in title, (
            "%s: the zoom-in tooltip is the only place the pan keys are "
            "advertised once the hint stops showing (got %r)" % (name, title)
        )

    @pytest.mark.parametrize("name", READER_TEMPLATES)
    def test_zoom_out_button_documents_its_key(self, name):
        html = read(os.path.join(TEMPLATE_DIR, name))
        match = re.search(r'id="zoomOutBtn"[^>]*title="([^"]*)"', html)
        assert match, "%s has no titled zoom-out button" % name
        assert "Arrow Down" in match.group(1)
