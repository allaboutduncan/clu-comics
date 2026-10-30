"""The list toolbar is one set of components, and nothing builds its own.

An audit found seven A-Z letter bars, three ways of showing the selected
filter button (.active, a btn-primary fill, btn-secondary), search boxes with
white icon boxes on dark themes, five pagers and three sets of page sizes
(Collection's "20" loaded 21 covers). They now share:

  templates/partials/filters.html   filter_bar, letter_bar, search_input,
                                    per_page_select, pager_nav
  static/js/clu-filters.js          CLU.filterBar / renderFilterOptions /
                                    letterBar / bindSearch / searchInput
  static/js/clu-pager.js            CLU.renderPageLinks, the only page bar
  helpers/paging.py                 PAGE_SIZES, snap_per_page, per_page_from

This file pins the components and is the ratchet that keeps screens on them.
"""

import glob
import os
import re

import pytest

from helpers.paging import (
    COLLECTION_DEFAULT_PER_PAGE,
    DEFAULT_PER_PAGE,
    PAGE_SIZES,
    per_page_from,
    snap_per_page,
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SHARED_JS = {"static/js/clu-filters.js", "static/js/clu-pager.js"}


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _sources(patterns=("templates/**/*.html", "static/js/*.js")):
    for pat in patterns:
        for path in glob.glob(os.path.join(ROOT, pat), recursive=True):
            rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
            if "/.omc/" in rel:
                continue
            yield rel, _read(rel)


# ---- page sizes ------------------------------------------------------------

def test_page_sizes_fill_whole_rows_of_the_cover_grid():
    assert PAGE_SIZES == (24, 60, 120, 240)
    for n in PAGE_SIZES:
        assert all(n % cols == 0 for cols in (2, 3, 4, 6)), n
    assert DEFAULT_PER_PAGE in PAGE_SIZES
    assert COLLECTION_DEFAULT_PER_PAGE in PAGE_SIZES


@pytest.mark.parametrize("old,new", [
    # Collection's old values, Library / Pull List's, Reading Lists'.
    (21, 24), (49, 60), (98, 120), (252, 240),
    (50, 60), (100, 120), (250, 240),
    (20, 24), (40, 24), (80, 60), (160, 120), (300, 240),
    (24, 24), (60, 60), (1, 24), (10_000, 240),
])
def test_a_size_saved_under_an_old_set_snaps_to_the_nearest(old, new):
    assert snap_per_page(old) == new
    assert snap_per_page(str(old)) == new


@pytest.mark.parametrize("junk", [None, "", "abc", "0", "-5"])
def test_junk_falls_back_to_the_default(junk):
    assert snap_per_page(junk) == DEFAULT_PER_PAGE
    assert snap_per_page(junk, default=24) == 24


def test_per_page_from_takes_the_first_value_that_is_set():
    assert per_page_from(None, "250") == 240            # no URL value: the cookie
    assert per_page_from("24", "250") == 24             # the URL wins
    assert per_page_from("", None) == DEFAULT_PER_PAGE
    # Junk falls through to the next candidate (#681).
    assert per_page_from("junk", "120") == 120
    assert per_page_from("junk", "junk") == DEFAULT_PER_PAGE
    assert per_page_from("0", "-5") == DEFAULT_PER_PAGE


def test_no_page_size_list_outside_the_one_set():
    # A <select> of page sizes is per_page_select; a JS array of sizes is read
    # from that select instead. Collection's 21/49/98/252 and the other old
    # sets must not come back.
    old = re.compile(r'<option value="(21|49|98|252)"|PAGE_SIZES\s*=\s*\[')
    hits = [f"{rel}:{src[:m.start()].count(chr(10)) + 1}"
            for rel, src in _sources() for m in old.finditer(src)]
    assert not hits, "hand-written page-size list:\n  " + "\n  ".join(hits)


def test_every_per_page_select_is_the_shared_one():
    hits = []
    for rel, src in _sources(("templates/**/*.html",)):
        if rel.endswith("partials/filters.html"):
            continue
        for m in re.finditer(r'<label[^>]*>\s*Per page\s*</label>', src):
            hits.append(f"{rel}:{src[:m.start()].count(chr(10)) + 1}")
    assert not hits, "use per_page_select():\n  " + "\n  ".join(hits)


# ---- the macros ------------------------------------------------------------

@pytest.fixture
def macros():
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(os.path.join(ROOT, "templates")),
                             autoescape=True)
    env.globals["clu_page_sizes"] = lambda: PAGE_SIZES
    env.globals["clu_default_per_page"] = (
        lambda kind=None: COLLECTION_DEFAULT_PER_PAGE if kind == "collection" else DEFAULT_PER_PAGE)
    return env.get_template("partials/filters.html").module


def test_filter_bar_marks_the_selection_for_everyone(macros):
    html = str(macros.filter_bar("st", [("", "All"), {"value": "m", "label": "Missing", "count": 3}],
                                 active="m", label="Status"))
    assert 'role="group"' in html and 'aria-labelledby="st-label"' in html
    assert 'data-value="m" aria-pressed="true"' in html
    assert 'data-value="" aria-pressed="false"' in html
    assert 'class="clu-filter-count">3<' in html
    assert "btn-primary" not in html and "btn-outline-primary" not in html
    assert "clu-filter-scroll" in html


def test_filter_bar_selects_values_that_need_escaping(macros):
    # A tag like Writer's must match the active tag and be escaped once.
    html = str(macros.filter_bar("t", [("", "All"), ("Writer's", "Writer's"), ("A&B", "A&B")],
                                 active=["Writer's", "A&B"], multi=True))
    assert html.count('aria-pressed="true"') == 2
    assert 'data-value="Writer&#39;s"' in html and 'data-value="A&amp;B"' in html
    assert "&amp;#39;" not in html and "&amp;amp;" not in html


def test_filter_bar_multi_selects_several_and_none_means_all(macros):
    html = str(macros.filter_bar("t", [("", "All"), ("X", "X"), ("Y", "Y")],
                                 active=["X", "Y"], multi=True, aria_label="Tags"))
    assert html.count('aria-pressed="true"') == 2 and 'data-clu-filter="multi"' in html
    none = str(macros.filter_bar("t", [("", "All"), ("X", "X")], active=[], multi=True))
    assert 'data-value="" aria-pressed="true"' in none


def test_letter_bar_puts_hash_first_and_toggles_off(macros):
    html = str(macros.letter_bar("ab", ["B", "#", "A"], active="B"))
    assert re.findall(r'data-value="([^"]*)"', html) == ["", "#", "A", "B"]
    assert "data-clu-toggle-off" in html
    assert 'data-value="B" aria-pressed="true"' in html


def test_search_input_is_named_typed_and_theme_safe(macros):
    html = str(macros.search_input("q", "Search series", value="bat"))
    assert 'type="search"' in html and 'aria-label="Search series"' in html
    assert "bg-body" in html and "bg-white" not in html
    assert 'value="bat"' in html


def test_per_page_select_offers_the_one_set(macros):
    html = str(macros.per_page_select("pp", 60))
    assert re.findall(r'<option value="(\d+)"', html) == [str(n) for n in PAGE_SIZES]
    assert '<option value="60" selected>' in html


def test_select_field_puts_the_label_inside_the_control(macros):
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(os.path.join(ROOT, "templates")),
                             autoescape=True)
    html = env.from_string(
        "{% from 'partials/filters.html' import select_field %}"
        "{% call select_field('pub', 'Publisher') %}<option value=\"\">All</option>{% endcall %}"
    ).render()
    assert '<label class="input-group-text" for="pub">Publisher</label>' in html
    assert '<select class="form-select" id="pub">' in html and '<option value="">All</option>' in html
    assert "clu-select" in html and "input-group-sm" in html


def test_per_page_select_carries_the_server_default(macros):
    # CLU.defaultPerPage reads this, so no page script hard-codes the number.
    assert f'data-default-per-page="{DEFAULT_PER_PAGE}"' in str(macros.per_page_select("pp", 24))
    assert 'data-default-per-page="24"' in str(macros.per_page_select("pp", 60, default=24))


def test_no_page_script_hard_codes_the_default_page_size():
    # helpers/paging.py is the one copy; the scripts read the select's
    # data-default-per-page. A literal here drifts the day the default moves.
    pat = re.compile(r"DEFAULT_PER_PAGE\s*=\s*\d|\|\|\s*(?:24|60)\b|itemsPerPage\s*=\s*\d")
    hits = []
    for rel in ("static/js/metadata_browser.js", "static/js/reading_list.js",
                "static/js/collection.js", "templates/metadata_history.html"):
        src = _read(rel)
        hits += [f"{rel}:{src[:m.start()].count(chr(10)) + 1}" for m in pat.finditer(src)]
    assert not hits, "hard-coded default page size:\n  " + "\n  ".join(hits)
    for rel in ("templates/collection.html", "templates/metadata_browser.html",
                "templates/metadata_history.html"):
        assert re.search(r"per_page_select\('\w+', \d", _read(rel)) is None, rel


def test_per_page_select_is_a_select_field(macros):
    html = str(macros.per_page_select("pp", 24))
    assert "clu-select" in html and "clu-per-page" in html and ">Per page</label>" in html


def test_toolbar_is_one_labelled_flat_panel():
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(os.path.join(ROOT, "templates")),
                             autoescape=True)
    html = env.from_string(
        "{% from 'partials/filters.html' import toolbar %}"
        "{% call toolbar('Filter things', id='tb') %}<div class=\"clu-toolbar-row\">x</div>{% endcall %}"
    ).render()
    assert html.startswith('<section class="clu-toolbar" id="tb" aria-label="Filter things">')
    assert '<div class="clu-toolbar-row">x</div>' in html
    for banned in ("card", "shadow", "border"):
        assert banned not in html


def test_pager_nav_is_labelled_and_hidden_until_needed(macros):
    html = str(macros.pager_nav("pg", "Pull list pages", {"data-page": 2}))
    assert 'aria-label="Pull list pages"' in html and 'data-page="2"' in html
    assert 'style="display: none;"' in html and "pagination-sm" in html


# ---- the ratchet -----------------------------------------------------------

def test_no_white_icon_boxes():
    # .bg-white is a white block on Darkly, Cyborg, Slate, Superhero...
    hits = [f"{rel}:{src[:m.start()].count(chr(10)) + 1}"
            for rel, src in _sources() for m in re.finditer(r"input-group-text[^\"']*\bbg-white\b", src)]
    assert not hits, "use bg-body:\n  " + "\n  ".join(hits)


def test_no_hand_built_letter_bars():
    # Every A-Z bar is letter_bar() or CLU.letterBar(), whose buttons carry
    # data-value. The old builders each tagged theirs data-letter (or read
    # dataset.letter), or spelled the alphabet out.
    pat = re.compile(r"data-letter\b|dataset\.letter\b|ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    hits = [f"{rel}:{src[:m.start()].count(chr(10)) + 1}"
            for rel, src in _sources(("static/js/*.js", "templates/**/*.html"))
            if rel not in SHARED_JS for m in pat.finditer(src)]
    assert not hits, "letter bar not built with CLU.letterBar:\n  " + "\n  ".join(hits)


def test_selection_is_never_a_colour_swap():
    # The selected filter button is .active + aria-pressed. Swapping
    # btn-primary / btn-secondary in for the selected one is how three screens
    # did it; it skips aria-pressed and changes colour per screen.
    # Only a ternary on a selection-like condition counts: action buttons that
    # pick btn-primary for the recommended action (Problem Files) are emphasis,
    # not selection state.
    sel = r"\w*(?:[aA]ctive|[sS]elected|[cC]urrent|[fF]ilter)\w*"
    pat = re.compile(
        r"(?:\b" + sel + r"|===\s*" + sel + r")\s*\?\s*['\"](?:btn btn-sm |btn )?btn-(?:primary|secondary)['\"]"
        r"""|classList\.(?:add|toggle)\(\s*['"]btn-secondary['"]""")
    hits = [f"{rel}:{src[:m.start()].count(chr(10)) + 1}"
            for rel, src in _sources(("static/js/*.js", "templates/**/*.html"))
            for m in pat.finditer(src)]
    assert not hits, "mark the selection with .active / aria-pressed:\n  " + "\n  ".join(hits)


def test_one_page_bar():
    # Every .pagination list is drawn by CLU.renderPageLinks; no screen builds
    # page-item <li>s itself.
    hits = [f"{rel}:{src[:m.start()].count(chr(10)) + 1}"
            for rel, src in _sources(("static/js/*.js", "templates/**/*.html"))
            if rel not in SHARED_JS
            for m in re.finditer(r"""['"]page-item\b""", src)]
    assert not hits, "draw page links with CLU.renderPageLinks:\n  " + "\n  ".join(hits)


def test_shared_scripts_ship_on_every_page():
    base = _read("templates/base.html")
    for js in ("js/clu-filters.js", "js/clu-pager.js"):
        assert js in base
    # ...and no page loads a second copy.
    for rel, src in _sources(("templates/**/*.html",)):
        if rel != "templates/base.html":
            assert "js/clu-pager.js" not in src and "js/clu-filters.js" not in src, rel


def test_reading_lists_css_has_no_hard_coded_colours():
    css = _read("static/css/reading_lists.css")
    for rule in (".tag-filter-btn", ".sort-btn", ".tag-filter-bar", ".sort-bar"):
        assert rule not in css, rule


# Every screen with an A-Z bar, and the shared builder it must use.
LETTER_BAR_HOSTS = [
    ("static/js/collection.js", "CLU.letterBar("),
    ("static/js/metadata_browser.js", "CLU.letterBar("),
    ("static/js/files.js", "CLU.letterBar("),
    ("static/js/index.js", "CLU.letterBar("),
    ("static/js/series.js", "CLU.letterBar("),
    ("static/js/source_wall.js", "CLU.letterBar("),
    ("templates/pull_list.html", "letter_bar("),
]


@pytest.mark.parametrize("path,call", LETTER_BAR_HOSTS)
def test_every_letter_bar_is_the_shared_one(path, call):
    assert call in _read(path), f"{path} must build its A-Z bar with {call}"


# Every list screen's controls sit in the one toolbar() panel.
TOOLBAR_HOSTS = [
    "templates/collection.html",
    "templates/metadata_browser.html",
    "templates/source_wall.html",
    "templates/pull_list.html",
    "templates/releases.html",
    "templates/publishers.html",
    "templates/reading_lists.html",
    "templates/problem_files.html",
    "templates/timeline.html",
    "templates/metadata_history.html",
    "templates/status.html",
    "templates/series_search.html",
]


@pytest.mark.parametrize("path", TOOLBAR_HOSTS)
def test_every_list_toolbar_is_the_shared_one(path):
    src = _read(path)
    assert "call toolbar(" in src, f"{path} must put its list controls in toolbar()"
    # The hand-built panels it replaced.
    assert "bg-body-tertiary p-3 rounded-3 shadow-sm" not in src, path


def test_filter_labels_carry_no_icon():
    # Releases and Reading Lists had label icons and Pull List didn't; the
    # shared language is a plain label. (Option icons, e.g. status squares, stay.)
    hits = [f"{rel}:{src[:m.start()].count(chr(10)) + 1}"
            for rel, src in _sources(("templates/**/*.html",))
            for m in re.finditer(r"label='[^']*',\s*icon='bi-", src)]
    assert not hits, "drop icon= from filter_bar labels:\n  " + "\n  ".join(hits)


def test_no_select_uses_its_first_option_as_a_label():
    # select_field labels the control; "All Publishers" as option text was the
    # old pattern.
    hits = [f"{rel}:{src[:m.start()].count(chr(10)) + 1}"
            for rel, src in _sources()
            for m in re.finditer(r"<option value=\"\">All (Publishers|Years|Months|Providers)<", src)]
    assert not hits, "use select_field and an 'All' option:\n  " + "\n  ".join(hits)


def test_letter_buckets_come_from_sort_letters():
    # "First character, or '#'" is CLU.sortLetters' rule. index.js, series.js
    # and files.js each had their own copy of it.
    copy = re.compile(r"letters\.add\([^)]*\?\s*\w+\s*:\s*'#'\)")
    hits = [f"{rel}:{src[:m.start()].count(chr(10)) + 1}"
            for rel, src in _sources() for m in copy.finditer(src)]
    assert not hits, "use CLU.sortLetters:\n  " + "\n  ".join(hits)
