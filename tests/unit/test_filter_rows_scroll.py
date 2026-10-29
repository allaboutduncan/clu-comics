"""Filter rows scroll sideways on a phone instead of wrapping.

An audit found the Releases pills, the Reading Lists tags and the Pull List
status and A-Z rows wrapping into blocks three or four lines tall on a phone,
pushing the results a screen down, while /collection's letter bar stays on
one line and scrolls. They now share .clu-filter-scroll (clu-page.css, phone
only) and its helper in clu-page.js, which base.html loads on every page.
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


# Each row is built by a shared builder (partials/filters.html or
# clu-filters.js), and every builder puts .clu-filter-scroll on its row.
ROWS = [
    ("templates/pull_list.html", "letter_bar('alphabetFilter'"),
    ("templates/pull_list.html", "filter_bar('statusFilter'"),
    ("templates/reading_lists.html", "filter_bar('rlTags'"),
    ("templates/releases.html", "filter_bar('publisher-pills'"),
    ("templates/releases.html", "filter_bar('subscription-pills'"),
    ("static/js/series.js", "CLU.letterBar("),
]


def test_filter_rows_use_a_shared_builder():
    missing = [f"{path}: {call}" for path, call in ROWS if call not in _read(path)]
    assert not missing, "filter row not built by a shared builder:\n  " + "\n  ".join(missing)


def test_every_shared_builder_applies_the_scroller():
    macros = _read("templates/partials/filters.html")
    # filter_bar's options row and letter_bar's group.
    assert macros.count("clu-filter-scroll") >= 2
    assert "'clu-filter-scroll'" in _read("static/js/clu-filters.js")


def test_the_scroller_is_phone_only_and_beats_flex_wrap():
    css = _read("static/css/clu-page.css")
    m = re.search(r"@media \(max-width: 767\.98px\) \{(.*?)\n\}", css[css.index(".clu-filter-scroll") - 600:], re.S)
    assert m and ".clu-filter-scroll {" in m.group(1)
    body = m.group(1)
    # The rows carry Bootstrap's .flex-wrap, which is !important.
    assert "flex-wrap: nowrap !important" in body
    assert "overflow-x: auto" in body


def test_the_helper_ships_on_every_page():
    assert ".clu-filter-scroll" in _read("static/js/clu-page.js")
    assert "js/clu-page.js" in _read("templates/base.html")
