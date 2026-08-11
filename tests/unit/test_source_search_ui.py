"""The keep-open contract of the shared source-search modal.

Each result in the modal offers two buttons: queue it, or queue it and keep the
results up so the user can grab more than one file from a single search. Which
one was pressed reaches the page as `keepOpen` on the onQueued payload, and
closing the modal is the page's decision — so the module and all three of its
callers have to stay in lockstep.

There is no JS test runner in this repo, so these assert on the assets as text,
following the precedent in test_themes.py. They are a wiring guard, not a
behaviour test: they catch a caller that goes back to closing unconditionally,
or a card renderer that loses one of the two buttons.
"""
import os

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
STATIC_JS = os.path.join(REPO_ROOT, "static", "js")
TEMPLATE_DIR = os.path.join(REPO_ROOT, "templates")

# Every surface that hosts the shared modal. A new page using
# CLU.createSourceSearch belongs here too.
CALLERS = [
    os.path.join(TEMPLATE_DIR, "series.html"),
    os.path.join(TEMPLATE_DIR, "wanted.html"),
    os.path.join(STATIC_JS, "reading_list.js"),
]


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def module_js():
    return read(os.path.join(STATIC_JS, "clu-source-search.js"))


class TestGrabButtons:

    def test_renders_both_a_close_and_a_keep_open_button(self, module_js):
        assert "function grabButtons(" in module_js
        # Two grab buttons per result, exactly one of them marked keep-open.
        assert module_js.count("clu-src-grab") >= 2
        assert 'data-keep="1"' in module_js

    def test_every_card_renderer_uses_the_shared_pair(self, module_js):
        # Each source must go through grabButtons() — a hand-rolled single
        # button would silently drop the keep-open option for that source.
        for source in ("getcomics", "usenet", "dcpp"):
            assert "grabButtons('%s'" % source in module_js

    def test_buttons_carry_an_identity_key(self, module_js):
        # markQueued() and the dedupe set both key off data-key.
        assert "data-key=" in module_js
        assert "function markQueued(" in module_js

    def test_error_restore_reads_the_per_button_icon(self, module_js):
        # The keep-open button's idle icon differs from its source's, so a
        # failed grab must restore from the button, not from a per-source icon.
        assert "dataset.idle" in module_js
        assert "idleIcon:" not in module_js

    def test_a_row_moves_as_a_unit(self, module_js):
        # Otherwise a grab in flight on one button leaves its sibling live and
        # the same release can be queued twice.
        assert "function rowButtons(" in module_js
        assert "function releaseRow(" in module_js


class TestKeepOpenContract:

    def test_module_reports_which_button_was_used(self, module_js):
        assert "keepOpen: !!btn.dataset.keep" in module_js

    def test_queued_releases_survive_a_re_render(self, module_js):
        # None of the grab endpoints are idempotent, so re-searching must not
        # re-arm a row the user already took.
        assert "queuedKeys" in module_js
        assert "queuedKeys.forEach(markQueued)" in module_js

    @pytest.mark.parametrize("path", CALLERS, ids=lambda p: os.path.basename(p))
    def test_caller_guards_the_hide_on_keep_open(self, path):
        source = read(path)
        assert "createSourceSearch" in source, "caller no longer uses the shared module"
        assert "keepOpen" in source
        # No caller may hide the modal unconditionally from onQueued.
        for line in source.splitlines():
            if "getcomicsModal.hide()" in line and "keepOpen" not in line:
                pytest.fail("unguarded modal hide in %s: %s" % (os.path.basename(path), line.strip()))


class TestReadingListMigration:

    def test_legacy_getcomics_modal_is_gone(self):
        source = read(os.path.join(STATIC_JS, "reading_list.js"))
        # The page-local copy of the modal predated clu-source-search.js and
        # only ever searched GetComics.
        for dead in ("downloadFromGetComics", "sortResultsGC", "escapeJsGC"):
            assert dead not in source, "%s should have been removed" % dead

    def test_page_loads_the_shared_module(self):
        template = read(os.path.join(TEMPLATE_DIR, "reading_list_view.html"))
        assert "js/clu-source-search.js" in template

    def test_search_is_seeded_with_the_issue_year(self):
        template = read(os.path.join(TEMPLATE_DIR, "reading_list_view.html"))
        # Without a year, "#8" matches every volume that ever had one.
        assert "entry.year or ''" in template

    def test_toast_severity_is_adapted(self):
        source = read(os.path.join(STATIC_JS, "reading_list.js"))
        # showToast() here only knows 'success'/'error'; the shared module
        # emits Bootstrap's 'danger', which would render blue.
        assert "'danger' ? 'error'" in source

    def test_modal_is_built_lazily(self):
        source = read(os.path.join(STATIC_JS, "reading_list.js"))
        # The modal is inside {% if _can_manage %}, so a viewer without manage
        # rights has no #getcomicsResults to bind to.
        assert "function ensureSourceSearch(" in source
        assert "function ensureGetComicsModal(" in source
