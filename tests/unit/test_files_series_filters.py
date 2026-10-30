"""Files, the index root-folder modal, the Series folder modal, Series Search
and the metadata-select modal use the shared filter components.

Each screen used to hand-roll its A-Z bar (String.fromCharCode loops, inline
onclick per letter), mark the GCD / ComicVine sort with a btn-secondary fill,
and build bare text inputs with a bg-white icon box. sortGCDSeries was also
defined twice in files.js, the first copy shadowed by the second.
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _function(src, name):
    m = re.search(r"function %s\([^)]*\) \{(.*?)\n\}" % name, src, re.S)
    assert m, f"{name} not found"
    return m.group(1)


def test_letter_bars_are_drawn_by_clu_letter_bar():
    for path, fn in [("static/js/files.js", "updateFilterBar"),
                     ("static/js/series.js", "updateFilterBar"),
                     ("static/js/index.js", "updateModalFilterBar")]:
        body = _function(_read(path), fn)
        assert "CLU.letterBar(" in body, f"{path}: {fn} does not use CLU.letterBar"
    for path in ["static/js/files.js", "static/js/series.js", "static/js/index.js"]:
        src = _read(path)
        assert "fromCharCode" not in src, path
        assert "filterDirectories(" not in src and "filterModalDirectories(" not in src, path


def test_letter_bar_containers():
    for path in ["templates/files.html", "templates/index.html", "templates/series.html"]:
        src = _read(path)
        assert "grid-filter-buttons" not in src, path
        assert 'class="clu-letter-bar' in src, path
    assert "grid-filter-buttons" not in _read("static/css/files.css")
    assert "#source-directory-filter .btn" not in _read("templates/base.html")


def test_gcd_sort_is_defined_once_and_marks_active():
    src = _read("static/js/files.js")
    assert src.count("function sortGCDSeries(") == 1
    assert src.count("function renderSeriesList(") == 1
    assert "btn-secondary" not in _function(src, "markGCDSort")
    assert "aria-pressed" in _function(src, "markGCDSort")
    for path in ["templates/partials/modal_metadata_select.html", "templates/series.html"]:
        tpl = _read(path)
        assert 'data-value="series" aria-pressed="false"' in tpl, path
        assert 'data-value="year" aria-pressed="false"' in tpl, path


def test_comicvine_sort_marks_active_not_btn_secondary():
    src = _read("static/js/clu-metadata.js")
    body = _function(src, "_wireCVSortAndFilter").replace("\n  ", "\n")
    assert "btn-secondary" not in body
    assert "_markCVSort(" in body
    assert "aria-pressed" in src[src.index("function _markCVSort"):src.index("function _wireCVSortAndFilter")]


def test_search_boxes():
    modal = _read("templates/partials/modal_metadata_select.html")
    assert "search_input('cvFilterInput', 'Search volumes'" in modal
    assert re.search(r'<input type="search"[^>]*id="cvRefineSearchInput"[^>]*aria-label="Search series"', modal)
    ss = _read("templates/series_search.html")
    assert "search_input('filterInput', 'Search these series'" in ss
    assert re.search(r'<input type="search"[^>]*id="searchInput"', ss)
    assert "CLU.bindSearch(document.getElementById('filterInput')" in ss
    files = _function(_read("static/js/files.js"), "updateFilterBar")
    # Both panes get the box (the left one had none), over files and folders.
    assert "id: panel + '-directory-search'" in files
    assert "panel === 'destination'" not in files
    assert "label: 'Search files and folders'" in files
    assert "CLU.bindSearch(" in files and "delay: 0" in files
    for path in ["templates/series_search.html", "templates/series.html",
                 "templates/partials/modal_metadata_select.html", "static/js/files.js"]:
        assert "bg-white" not in _read(path), path
