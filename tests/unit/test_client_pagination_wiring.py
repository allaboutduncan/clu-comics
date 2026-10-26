"""/reading-lists and /pull-list page on the server, with the same bar as
/library (static/js/clu-pager.js). These pin the wiring, which no Python
route test would otherwise see:

* neither page loads the whole list any more -- the routes ask for one page,
  and the browser fetches later pages from a JSON endpoint;
* the first page and the fetched pages render from one partial each;
* both pages drive CLU.createServerPager, and /library draws its bar
  through the shared renderer, so the three cannot drift apart;
* selection spans pages through an ids endpoint, never the DOM, because
  only one page is in the DOM.
"""

import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _between(src, start, end):
    body = src[src.index(start):]
    return body[:body.index(end)]


def _function_source(rel, name):
    src = _read(rel)
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} not found in {rel}")


# --- The pager ------------------------------------------------------------

def test_pager_defines_the_shared_api():
    src = _read("static/js/clu-pager.js")
    assert "CLU.renderPageLinks = function" in src
    assert "CLU.createServerPager = function" in src
    assert "createClientPager" not in src, "the hide-rows-in-the-browser pager is back"
    # Same URL keys as /library
    assert "'page'" in src and "'per_page'" in src


def test_pager_skips_unchanged_url_writes():
    """Safari throws SecurityError after 100 replaceState calls in 10s."""
    src = _read("static/js/clu-pager.js")
    flush = _between(src, "function flushUrl", "\n    }\n")
    assert "canonical(url) === lastUrl" in flush
    write = _between(src, "function writeUrl", "\n    }\n")
    assert "canonical(buildUrl()) === lastUrl" in write
    assert "setTimeout(flushUrl" in write


def test_library_uses_the_shared_renderer():
    js = _read("static/js/metadata_browser.js")
    body = js[js.index("function renderPagination"):]
    body = body[:body.index("\n    }\n")]
    assert "CLU.renderPageLinks" in body
    assert "addItem" not in body, "a private copy of the bar is back"
    assert "js/clu-pager.js" in _read("templates/metadata_browser.html")


# --- The routes ask for one page --------------------------------------------

def test_pull_list_route_loads_one_page():
    body = _function_source("routes/series.py", "pull_list")
    assert "get_pull_list_page" in body
    assert "get_all_mapped_series" not in body, "the page loads every series again"
    assert "get_pull_list_collection_counts" not in body, \
        "the page aggregates every series' issues again"


def test_reading_lists_route_loads_one_page():
    body = _function_source("routes/reading_lists.py", "index")
    assert "get_reading_lists_page" in body
    assert "get_all_reading_lists" not in body, "the grid loads every list again"


def test_first_page_and_fetched_pages_share_a_partial():
    assert 'include "partials/pull_list_rows.html"' in _read("templates/pull_list.html")
    assert "partials/pull_list_rows.html" in _function_source("routes/series.py", "pull_list_rows")
    assert 'include "partials/reading_list_cards.html"' in _read("templates/reading_lists.html")
    assert "partials/reading_list_cards.html" in _function_source(
        "routes/reading_lists.py", "grid_page"
    )


def test_page_sizes_match_between_route_and_select():
    from routes.reading_lists import READING_LIST_PAGE_SIZES, READING_LIST_DEFAULT_PER_PAGE
    # The card grid is 4 (or 5) wide; a size like 50 left a gap at the end.
    assert READING_LIST_PAGE_SIZES == (20, 40, 80, 160, 300)
    assert all(n % 4 == 0 for n in READING_LIST_PAGE_SIZES)
    assert READING_LIST_DEFAULT_PER_PAGE == 40
    # Both selects are drawn from the route's tuple, so they cannot disagree.
    assert "{% for n in page_sizes %}" in _between(
        _read("templates/reading_lists.html"), 'id="rlPerPage"', "</select>"
    )
    assert "{% for n in page_sizes %}" in _between(
        _read("templates/pull_list.html"), 'id="plPerPage"', "</select>"
    )


# --- The pages drive the server pager ----------------------------------------

def test_pull_list_page_is_wired():
    html = _read("templates/pull_list.html")
    assert "js/clu-pager.js" in html
    assert 'id="plPagination"' in html and 'id="plPerPage"' in html
    nav = _between(html, '<nav id="plPagination"', ">\n")
    # Through url_for: CLU runs behind ProxyFix(x_prefix=1).
    assert "url_for('series.pull_list_rows')" in html
    assert "url_for('series.pull_list_ids')" in html
    assert "data-page=" in html and "data-total-pages=" in html
    assert nav
    assert "CLU.createServerPager" in html
    on_data = _between(html, "onData: data => {", "},")
    assert "tbody.innerHTML = data.html" in on_data
    assert "syncRowCheckboxes()" in on_data
    assert "lastCheckedRow = null" in on_data, \
        "a shift-click anchor must not survive the rows it belonged to"


def test_reading_lists_page_is_wired():
    html = _read("templates/reading_lists.html")
    assert "js/clu-pager.js" in html
    assert 'id="rlPagination"' in html and 'id="rlPerPage"' in html
    assert html.index("js/clu-pager.js") < html.index("js/reading_list.js")
    assert "url_for('reading_lists.grid_page')" in html
    assert "url_for('reading_lists.grid_ids')" in html

    js = _read("static/js/reading_list.js")
    assert "CLU.createServerPager" in js
    assert "createClientPager" not in js
    on_data = _between(js, "onData: data => {", "},")
    assert "grid.innerHTML = data.html" in on_data
    # A swapped-in card needs its tick, tag fit and bookmark state again.
    init = _between(js, "function initListCards", "\n}\n")
    for call in ("syncListCheckboxes()", "observeCardTags()", "loadWantToReadState()"):
        assert call in init


# --- Selection spans pages through the server ----------------------------------

def test_pull_list_select_all_matching_comes_from_the_server():
    """Only one page is in the DOM, so "every matching series" has to be
    asked for; a selection read off the DOM would act on 50 of 300."""
    html = _read("templates/pull_list.html")
    assert 'id="bulkSelectMatchingBtn"' in html
    handler = _between(html, "selectMatchingBtn.addEventListener('click'", "\n        }\n")
    assert "fetchMatchingIds()" in handler
    assert "nav.dataset.idsEndpoint" in _between(html, "function fetchMatchingIds", "\n        }\n")
    # A filter change while the ids are in flight makes them stale.
    assert "if (seq !== filterSeq) return;" in handler
    bulk = _between(html, "function bulkSetMonitored", "\n        }\n")
    assert "Array.from(selected)" in bulk


def test_pull_list_reports_selected_rows_on_other_pages():
    html = _read("templates/pull_list.html")
    assert 'id="bulkOffPage"' in html
    assert "on other pages" in _between(html, "function updateBulkUI", "\n        }\n")


def test_filter_change_drops_selections_it_excludes():
    html = _read("templates/pull_list.html")
    body = _between(html, "function applyFilters", "\n        }\n")
    assert "fetchMatchingIds()" in body and "selected.delete(id)" in body
    assert "pager.load({ resetPage: true })" in body

    js = _read("static/js/reading_list.js")
    body = _between(js, "function applyTagFilters", "\n}\n")
    assert "fetchMatchingListIds()" in body and "selectedListIds.delete(id)" in body
    assert "readingListPager.load({ resetPage: true })" in body


def test_reading_lists_select_all_comes_from_the_server():
    js = _read("static/js/reading_list.js")
    body = _between(js, "function selectAllVisibleLists", "\n}\n")
    assert "fetchMatchingListIds()" in body
    assert "if (seq !== tagFilterSeq) return;" in body
    assert "on other pages" in _between(js, "function updateBulkBar", "\n}\n")
    assert "selectedListsOffPage()" in _between(js, "function bulkDeleteSelected", "\n}\n")
    # "Delete All" counts every list, not the cards on this page.
    delete_all = _between(js, "function confirmDeleteAll", "\n}\n")
    assert "dataset.totalLists" in delete_all
    assert "querySelectorAll('.reading-list-card').length" not in delete_all


def test_a_failed_selection_check_clears_the_selection():
    """If the ids for the new filter cannot be fetched, nobody knows which
    selected rows still match; a bulk action must not reach a hidden one."""
    html = _read("templates/pull_list.html")
    body = _between(html, "function applyFilters", "pager.load({ resetPage: true })")
    assert "selected.clear()" in _between(body, ".catch(", "});")
    js = _read("static/js/reading_list.js")
    body = _between(js, "function applyTagFilters", "readingListPager.load(")
    assert "selectedListIds.clear()" in _between(body, ".catch(", "});")


def test_tag_chips_are_read_before_the_pager_writes_the_url():
    """The pager writes its state to the URL when created; reading the chips
    after that would strip ?tag= from the address bar."""
    js = _read("static/js/reading_list.js")
    init = _between(js, "document.addEventListener('DOMContentLoaded', () => {\n    // Before the pager",
                    "CLU.createServerPager({")
    assert "initTagFilters();" in init
    assert "document.addEventListener('DOMContentLoaded', initTagFilters)" not in js


def test_pull_list_search_is_debounced():
    html = _read("templates/pull_list.html")
    assert "searchInput.addEventListener('input', applyFilters)" not in html
    assert "setTimeout(applyFilters" in html


def test_pull_list_reports_errors_without_native_dialogs():
    html = _read("templates/pull_list.html")
    script = html[html.index("<script"):]
    assert not re.search(r"\balert\(", script)
    assert "partials/toast_container.html" in html
    assert "js/clu-utils.js" in html


def test_body_background_selects_also_take_body_text():
    """bg-body puts the page background behind a .form-select, whose text
    colour some themes keep dark (Cyborg: both rgb(33,37,41)), so the value
    was invisible. text-body pairs it with the theme's own text colour."""
    templates = os.path.join(ROOT, "templates")
    offenders = []
    for name in os.listdir(templates):
        if not name.endswith(".html"):
            continue
        for cls in re.findall(r'class="([^"]*form-select[^"]*)"', _read(f"templates/{name}")):
            if "bg-body" in cls.split() and "text-body" not in cls.split():
                offenders.append(f"{name}: {cls}")
    assert not offenders, offenders
