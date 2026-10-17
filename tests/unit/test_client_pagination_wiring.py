"""/reading-lists and /pull-list page in the browser with the same bar as
/library (static/js/clu-pager.js). These pin the wiring, which no Python
test would otherwise see:

* both pages load the pager and carry the nav + per-page select it drives;
* /library draws its bar through the shared renderer, so the three cannot
  drift apart;
* filters mark items with data-match and leave style.display to the pager --
  a filter that set display itself would show rows from every page at once.
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def test_pager_defines_the_shared_api():
    src = _read("static/js/clu-pager.js")
    assert "CLU.renderPageLinks = function" in src
    assert "CLU.createClientPager = function" in src
    # Same URL keys as /library
    assert "'page'" in src and "'per_page'" in src


def test_reading_lists_page_is_wired():
    html = _read("templates/reading_lists.html")
    assert "js/clu-pager.js" in html
    assert 'id="rlPagination"' in html and 'id="rlPerPage"' in html
    assert html.index("js/clu-pager.js") < html.index("js/reading_list.js")

    js = _read("static/js/reading_list.js")
    assert "CLU.createClientPager" in js
    body = js[js.index("function applyTagFilters"):js.index("let readingListPager")]
    assert "dataset.match" in body
    assert "style.display" not in body, "the tag filter must leave display to the pager"


def test_pull_list_page_is_wired():
    html = _read("templates/pull_list.html")
    assert "js/clu-pager.js" in html
    assert 'id="plPagination"' in html and 'id="plPerPage"' in html
    body = html[html.index("function applyFilters"):html.index("// ── Multi-select")]
    assert "dataset.match" in body
    assert not re.search(r"row\.style\.display\s*=", body), \
        "the filters must leave display to the pager"
    assert "pager.refresh" in body


def test_library_uses_the_shared_renderer():
    js = _read("static/js/metadata_browser.js")
    body = js[js.index("function renderPagination"):]
    body = body[:body.index("\n    }\n")]
    assert "CLU.renderPageLinks" in body
    assert "addItem" not in body, "a private copy of the bar is back"
    assert "js/clu-pager.js" in _read("templates/metadata_browser.html")


def test_reading_list_page_sizes_fill_whole_rows():
    """The card grid is 4 (or 5) wide; a size like 50 left a gap at the end
    of every page."""
    html = _read("templates/reading_lists.html")
    select = html[html.index('id="rlPerPage"'):]
    select = select[:select.index("</select>")]
    sizes = [int(v) for v in re.findall(r'value="(\d+)"', select)]
    assert sizes == [20, 40, 80, 160, 300]
    assert all(n % 4 == 0 for n in sizes)
    assert "defaultPerPage: 40" in _read("static/js/reading_list.js")


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


# --- Review of #642: selection scope, URL state, URL write rate ----------

_READS_DISPLAY = re.compile(r"\.style\.display\s*[!=]==")


def _between(src, start, end):
    body = src[src.index(start):]
    return body[:body.index(end)]


def test_pull_list_select_all_covers_every_matching_row():
    """style.display means "on this page". Select-all, the header checkbox
    and bulk actions must use the filtered set, or select-all -> Unmonitor
    acts on 50 of 300 matching series (#1)."""
    html = _read("templates/pull_list.html")
    script = html[html.index("// ── Multi-select"):]
    assert not _READS_DISPLAY.search(_between(script, "// ── Multi-select", "const rowStatusClasses")), \
        "selection must not read the pager's display state"
    assert "pager.matching()" in script
    select_all = _between(script, "selectAll.addEventListener('change'", "});")
    assert "matchingRows()" in select_all
    header = _between(script, "function updateBulkUI", "rows.forEach(row =>")
    assert "matchingRows()" in header


def test_pull_list_reports_selected_rows_on_other_pages():
    """Bulk Monitor/Unmonitor reaches every selected row; rows on another
    page are counted in the toolbar rather than acted on silently (#3)."""
    html = _read("templates/pull_list.html")
    assert 'id="bulkOffPage"' in html
    assert "on other pages" in _between(html, "function updateBulkUI", "rows.forEach(row =>")


def test_pull_list_shift_click_anchor_is_reset_off_page():
    """indexOf(anchor) is -1 once the anchor's page is gone, which silently
    turned a range into a single toggle (#4)."""
    html = _read("templates/pull_list.html")
    on_render = _between(html, "onRender: ({ total, start, end })", "updateBulkUI();")
    assert "lastCheckedRow = null" in on_render and "isOnPage(lastCheckedRow)" in on_render
    assert "const vis = pageRows();" in html


def test_reading_lists_select_all_covers_every_matching_card():
    """Select all -> Delete selected deleted 40 of 120 tag-matching lists (#2)."""
    js = _read("static/js/reading_list.js")
    body = _between(js, "function selectAllVisibleLists", "\n}\n")
    assert not _READS_DISPLAY.search(body)
    assert "matching()" in body
    assert "on other pages" in _between(js, "function updateBulkBar", "\n}\n")
    assert "selectedListsOffPage()" in _between(js, "function bulkDeleteSelected", "\n}\n")


def test_reading_lists_tag_filter_drops_hidden_selections():
    js = _read("static/js/reading_list.js")
    body = _between(js, "function applyTagFilters", "\n}\n")
    assert "onListCheckboxChange(cb)" in body


def test_page_is_restored_with_its_sort_and_filters():
    """?page=4 restored without the sort and filters it came from shows page
    4 of a different list (#5). Both pages put that state in the URL and
    restore it before the first render, which keeps the page number."""
    html = _read("templates/pull_list.html")
    assert "urlState: () => ({" in html
    for key in ("q:", "publisher:", "letter:", "status:", "sort:", "dir:"):
        assert key in _between(html, "urlState: () => ({", "}),")
    restore = _between(html, "function restoreFromUrl", "\n        }\n")
    assert "applyFilters({ resetPage: false })" in restore
    assert "pager.refresh();" not in html, "a bare first render ignores the URL's filters"

    js = _read("static/js/reading_list.js")
    assert "urlState: () => ({" in js
    restore = _between(js, "function restoreListStateFromUrl", "\n}\n")
    assert "applyTagFilters({ resetPage: false })" in restore
    assert "applySort(sortBtn, { render: false })" in restore


def test_pager_skips_unchanged_url_writes():
    """Safari throws SecurityError after 100 replaceState calls in 10s, and
    the pager renders on every keystroke (#6)."""
    src = _read("static/js/clu-pager.js")
    flush = _between(src, "function flushUrl", "\n    }\n")
    assert "url === lastUrl" in flush
    write = _between(src, "function writeUrl", "\n    }\n")
    assert "buildUrl() === lastUrl" in write
    assert "setTimeout(flushUrl" in write
    html = _read("templates/pull_list.html")
    assert "searchInput.addEventListener('input', applyFilters)" not in html, \
        "the search box should be debounced"
