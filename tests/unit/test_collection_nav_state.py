"""The /collection grid keeps folder, view, letter, search and page in the URL.

There is no JS test runner in this repo, so the wiring is asserted
structurally against ``static/js/collection.js`` (same approach as
``test_issue_badge.py``). The behaviour was checked in a real browser:
filter -> open folder -> Back restores the filter, Forward still works, a
reload keeps All Books + letter, and the first load adds no history entry.
"""
import os
import re

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
COLLECTION_JS = os.path.join(REPO, "static", "js", "collection.js")
COLLECTION_HTML = os.path.join(REPO, "templates", "collection.html")


@pytest.fixture(scope="module")
def js_src():
    with open(COLLECTION_JS, encoding="utf-8") as fh:
        return fh.read()


def _code_only(block):
    """Drop // comments so an assertion cannot match prose about the code."""
    return "\n".join(line.split("//", 1)[0] for line in block.splitlines())


def _function_body(src, name):
    """Source of a top-level JS function, up to the next top-level declaration."""
    m = re.search(rf"^(?:async )?function {name}\(.*?(?=^(?:async )?function |\Z)", src, re.S | re.M)
    assert m, f"function {name} not found -- was it renamed?"
    return _code_only(m.group(0))


def _popstate_handler(src):
    m = re.search(r"window\.onpopstate = .*?\n\};", src, re.S)
    assert m, "onpopstate handler not found"
    return _code_only(m.group(0))


def test_only_sync_nav_url_pushes_history(js_src):
    """A push anywhere else (the old loadDirectory did one per call) adds a
    duplicate entry on first load and wipes Forward when Back re-loads."""
    pushes = [m.start() for m in re.finditer(r"history\.pushState\(", _code_only(js_src))]
    assert len(pushes) == 1
    assert "history.pushState(" in _function_body(js_src, "syncNavUrl")


def test_popstate_restores_without_loading_directly(js_src):
    m = re.search(r"window\.onpopstate = .*?\n\};", js_src, re.S)
    assert m, "onpopstate handler not found"
    handler = _code_only(m.group(0))
    assert "restoreNavState(" in handler
    assert "loadDirectory(" not in handler


def test_restore_suppresses_history_writes(js_src):
    body = _function_body(js_src, "restoreNavState")
    assert "navRestoring = true" in body
    assert "navRestoring = false" in body
    assert "if (navRestoring) return;" in _function_body(js_src, "syncNavUrl")


@pytest.mark.parametrize("name", [
    "loadDirectory", "loadAllBooks", "fetchAllBooksPage", "loadMissingXml",
    "filterItems", "changePage", "changeItemsPerPage",
])
def test_state_changes_write_the_url(js_src, name):
    assert "syncNavUrl()" in _function_body(js_src, name)


@pytest.mark.parametrize("name", ["loadDirectory", "loadAllBooks", "loadMissingXml"])
def test_scroll_saved_before_the_grid_is_torn_down(js_src, name):
    assert "rememberScroll()" in _function_body(js_src, name)


def test_url_params_are_validated(js_src):
    body = _function_body(js_src, "readNavStateFromUrl")
    assert "NAV_VIEWS.includes(view)" in body
    assert "/^[A-Z]$/" in body
    assert "page > 0" in body


def test_popstate_without_state_reads_the_url_not_the_first_folder(js_src):
    """A stateless entry used to load INITIAL_PATH -- the folder the page
    first opened on -- while the address bar showed a different one."""
    handler = _popstate_handler(js_src)
    assert "readNavPathFromUrl()" in handler
    assert "INITIAL_PATH" not in handler


def test_url_path_parsing_matches_the_server_route(js_src):
    body = _function_body(js_src, "readNavPathFromUrl")
    assert "get('path')" in body
    assert "decodeURIComponent(" in body
    assert "collectionBaseUrl()" in body


@pytest.mark.parametrize("name", ["syncNavUrl", "rememberScroll"])
def test_unsynced_views_leave_the_folder_entry_alone(js_src, name):
    """Recently Added / Continue Reading / On the Stack have no URL view, so
    their letter/page/scroll must not be written onto the folder's entry."""
    assert "inUnsyncedView()" in _function_body(js_src, name)
    body = _function_body(js_src, "inUnsyncedView")
    for flag in ("isRecentlyAddedMode", "isContinueReadingMode", "isOnTheStackMode"):
        assert flag in body


def test_folder_segments_are_encoded(js_src):
    """'Batman #1 Variants' must not turn everything after '#' into a fragment."""
    assert ".map(encodeURIComponent)" in _function_body(js_src, "buildCollectionUrl")


def test_history_urls_keep_the_subpath_prefix(js_src):
    """CLU runs behind ProxyFix(x_prefix=1); a literal '/collection' drops it."""
    assert "'/collection'" not in _function_body(js_src, "buildCollectionUrl")
    assert "window.COLLECTION_URL" in _function_body(js_src, "collectionBaseUrl")
    with open(COLLECTION_HTML, encoding="utf-8") as fh:
        html = fh.read()
    assert "window.COLLECTION_URL = {{ url_for('collection.collection')" in html


def test_scroll_is_saved_while_scrolling(js_src):
    """popstate fires after the entry switched, so Forward needs the outgoing
    entry's scrollY saved beforehand."""
    code = _code_only(js_src)
    m = re.search(r"window\.addEventListener\('scroll',.*?\n\}", code, re.S)
    assert m, "debounced scroll listener not found"
    assert "rememberScroll" in m.group(0)
    assert "clearTimeout(navScrollSaveTimer)" in _popstate_handler(js_src)


def test_all_books_clamp_refetches_inline(js_src):
    """A timer-based retry let restoreNavState scroll before the real page
    rendered; and an empty result must drop a restored page number."""
    body = _function_body(js_src, "fetchAllBooksPage")
    assert "await fetchAllBooksPage(opts)" in body
    assert "setTimeout(() => fetchAllBooksPage" not in body
    assert re.search(r"if \(allBooksTotal === 0\) \{\s*currentPage = 1;", body)


def test_restoring_a_view_skips_the_folder_listing(js_src):
    body = _function_body(js_src, "applyNavState")
    view_branch = body.split("} else {", 1)[0]
    assert "enterFolderForView(path)" in view_branch
    assert "loadDirectory(" not in view_branch
    assert "fetch(" not in _function_body(js_src, "enterFolderForView")


def test_search_typing_debounces_the_url_write(js_src):
    body = _function_body(js_src, "onGridSearch")
    assert "syncNavUrlSoon()" in body
    assert "syncNavUrl()" not in body


def test_restores_are_serialized(js_src):
    """Independent retry timers let two restores overlap, and the first one's
    finally cleared navRestoring under the second."""
    body = _function_body(js_src, "restoreNavState")
    assert "pendingRestore" in body
    assert "if (restoreRunning) return;" in body
    assert "setTimeout(() => restoreNavState" not in body
