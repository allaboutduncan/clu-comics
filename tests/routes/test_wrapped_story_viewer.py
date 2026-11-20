"""The shared Wrapped story viewer (partials/wrapped_story.html,
static/js/clu-wrapped-story.js) and its wiring into Insights and Timeline.

There is no JS test runner in this repo, so the script's contract is asserted
over its source, the same way the reader and the issue badge are.
"""
import pathlib
import re

import pytest

from core.database import mark_issue_read

ROOT = pathlib.Path(__file__).resolve().parents[2]
PARTIAL = (ROOT / "templates" / "partials" / "wrapped_story.html").read_text(encoding="utf-8")
STORY_JS = (ROOT / "static" / "js" / "clu-wrapped-story.js").read_text(encoding="utf-8")
INSIGHTS_JS = (ROOT / "static" / "js" / "insights.js").read_text(encoding="utf-8")
INSIGHTS_HTML = (ROOT / "templates" / "insights.html").read_text(encoding="utf-8")
TIMELINE_HTML = (ROOT / "templates" / "timeline.html").read_text(encoding="utf-8")


@pytest.fixture
def reads(db_connection):
    mark_issue_read("/data/DC/Batman/Batman 001.cbz", read_at="2024-06-01T10:00:00")


class TestPagesCarryTheViewer:
    @pytest.mark.parametrize("url", ["/insights", "/timeline"])
    def test_rendered_once_with_its_assets(self, client, reads, url):
        body = client.get(url).get_data(as_text=True)
        assert body.count('id="wrappedStoryModal"') == 1
        assert body.count('id="editorCanvas"') == 1
        assert "/static/css/wrapped-story.css?v=" in body
        assert "/static/js/clu-wrapped-story.js?v=" in body
        # A ZIP that includes edited slides is built in the browser.
        assert "jszip@3.10.1" in body

    def test_the_viewer_loads_before_the_page_script_that_opens_it(self, client, reads):
        body = client.get("/insights").get_data(as_text=True)
        assert body.index("clu-wrapped-story.js") < body.index("js/insights.js")


class TestMarkupMatchesScript:
    def test_every_element_the_script_looks_up_is_in_the_partial(self):
        ids = set(re.findall(r"byId\('([^']+)'\)", STORY_JS))
        assert len(ids) > 20
        assert sorted(i for i in ids if f'id="{i}"' not in PARTIAL) == []

    def test_controls_are_labelled(self):
        for control in ("wrappedStoryPause", "wrappedStoryPrev", "wrappedStoryNext"):
            tag = re.search(rf'<button[^>]*id="{control}"[^>]*>', PARTIAL, re.S).group(0)
            assert "aria-label=" in tag
        assert 'data-bs-dismiss="modal"' in PARTIAL


class TestStoryScript:
    def test_slides_are_keyed_by_id_not_counted(self):
        # The pages used to hard-code two and three slides.
        assert "TOTAL_SLIDES" not in STORY_JS + INSIGHTS_JS + TIMELINE_HTML
        assert "state.edited[slide.id]" in STORY_JS
        assert "manifest.slides" in STORY_JS

    def test_no_page_uses_the_numbered_image_routes(self):
        for source in (STORY_JS, INSIGHTS_JS, TIMELINE_HTML, INSIGHTS_HTML):
            assert "/image/" not in source

    def test_both_pages_open_the_story_from_a_manifest(self):
        assert "CLU.wrappedStory.open(" in INSIGHTS_JS
        assert "/api/wrapped/${year}/manifest" in INSIGHTS_JS
        assert "CLU.wrappedStory.open(" in TIMELINE_HTML
        assert "/api/wrapped/monthly/${year}/${month}/manifest" in TIMELINE_HTML

    def test_the_timer_waits_for_the_image(self):
        # `ready` is set in the image's onload and the tick stands still
        # without it: a slide the server is still drawing loses no screen time.
        onload = STORY_JS[STORY_JS.index("img.onload = function () {"):]
        assert "state.ready = true;" in onload[:onload.index("img.onerror")]
        assert "if (state.paused || !state.ready || state.tall)" in STORY_JS

    def test_only_the_next_slide_is_preloaded(self):
        assert STORY_JS.count("preload(index + 1)") == 1
        assert not re.search(r"slides\.(forEach|map)\([^)]*preload", STORY_JS)

    def test_reduced_motion_starts_paused(self):
        assert "prefers-reduced-motion: reduce" in STORY_JS
        assert "setPaused(reducedMotion(), reducedMotion())" in STORY_JS

    def test_keyboard_and_visibility(self):
        for key in ("ArrowRight", "ArrowLeft"):
            assert f"e.key === '{key}'" in STORY_JS
        assert "visibilitychange" in STORY_JS

    def test_download_is_the_server_zip_unless_a_slide_was_edited(self):
        body = STORY_JS[STORY_JS.index("async function downloadAll(e)"):]
        body = body[:body.index("\n    }\n")]
        assert body.index("if (edited.length === 0) return;") < body.index("e.preventDefault();")
        assert "folder.file(slide.filename, blob)" in body

    def test_the_editor_redraws_over_the_servers_slide(self):
        # Reopening an edited slide must not bake overlays into a copy of a copy.
        opener = STORY_JS[STORY_JS.index("function openEditor()"):]
        opener = opener[:opener.index("\n    }\n")]
        assert "img.src = slide.url;" in opener
        assert "state.overlays[slide.id]" in opener

    def test_the_saved_image_has_no_selection_box(self):
        done = STORY_JS[STORY_JS.index("byId('editorDone')"):]
        assert done.index("drawEditor(false);") < done.index("canvas.toDataURL('image/png')")

    def test_closing_the_modal_stops_the_timer(self):
        hidden = STORY_JS[STORY_JS.index("'hidden.bs.modal'"):]
        assert "cancelAnimationFrame(state.frame)" in hidden[:300]
        assert "state = null;" in hidden[:300]

    def test_no_native_dialogs(self):
        assert not re.search(r"\b(alert|confirm|prompt)\(", STORY_JS)
