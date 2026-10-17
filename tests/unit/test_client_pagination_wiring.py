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
