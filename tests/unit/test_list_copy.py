"""One wording for a list's search box, its count and its "no matches" state.

Search boxes said "Filter…", "Type to filter...", "Search title / series…",
"Enter search term..." and "Search query..."; counts said "No series match
(4,132 total)" on one screen and nothing at all on others; a search that
matched nothing showed a bare "No results", a muted table row, or an empty
grid. They now share:

  search_input(id, label) / CLU.searchInput    placeholder = label + "…"
  result_count() / CLU.countText, CLU.setCount  "Showing 1–60 of 4,132 series"
  no_matches() / CLU.noMatches                  "No series match “bat”" + Clear

DESIGN.md "Filters, search and pagers" has the rules. This file pins the
helpers and is the ratchet that keeps screens on them.
"""

import glob
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
        return fh.read()


def _sources(patterns=("templates/**/*.html", "static/js/*.js")):
    for pat in patterns:
        for path in glob.glob(os.path.join(ROOT, pat), recursive=True):
            rel = os.path.relpath(path, ROOT)
            if "/.omc/" in rel:
                continue
            yield rel, _read(rel)


@pytest.fixture(scope="module")
def env():
    jinja2 = pytest.importorskip("jinja2")
    return jinja2.Environment(loader=jinja2.FileSystemLoader(os.path.join(ROOT, "templates")),
                              autoescape=True)


def _render(env, src, **ctx):
    return env.from_string(src).render(**ctx)


# ---- the search box --------------------------------------------------------

def test_search_placeholder_is_the_label_with_an_ellipsis(env):
    html = _render(env, "{% from 'partials/filters.html' import search_input %}"
                        "{{ search_input('q', 'Search series', value='bat') }}")
    assert 'aria-label="Search series"' in html
    assert 'placeholder="Search series…"' in html
    assert 'value="bat"' in html


def test_search_input_takes_no_placeholder():
    macro = re.search(r"{% macro search_input\(([^)]*)\)", _read("templates/partials/filters.html"))
    assert "placeholder" not in macro.group(1)


def test_script_search_box_derives_its_placeholder():
    js = _read("static/js/clu-filters.js")
    body = js[js.index("CLU.searchInput = function"):js.index("CLU.setSearchLabel = function")]
    assert "opts.placeholder" not in body
    setter = js[js.index("CLU.setSearchLabel = function"):]
    assert "aria-label" in setter and "\\u2026" in setter[:300]


def test_no_template_passes_a_placeholder_to_search_input():
    offenders = []
    for rel, src in _sources(("templates/**/*.html",)):
        for m in re.finditer(r"search_input\(([^)]*)\)", src):
            args = m.group(1)
            if args.startswith("id,"):  # the macro's own signature
                continue
            positional = [a for a in re.split(r",\s*(?![^()]*\))", args) if "=" not in a]
            if "placeholder=" in args or len(positional) > 2:
                offenders.append(f"{rel}: search_input({args})")
    assert not offenders, "\n".join(offenders)


def test_no_script_passes_a_placeholder_to_search_input():
    offenders = []
    for rel, src in _sources(("static/js/*.js",)):
        for m in re.finditer(r"CLU\.searchInput\(\{(.*?)\}\)", src, re.S):
            if "placeholder" in m.group(1):
                offenders.append(rel)
    assert not offenders, offenders


BANNED_PLACEHOLDERS = re.compile(
    r"""placeholder\s*[=:]\s*["'`](Filter|Type to filter|Enter search term|Search query"""
    r"""|Search for file|Refine search term)[^"'`]*["'`]""")


def test_no_search_box_uses_a_retired_wording():
    offenders = [f"{rel}: {m.group(0)}" for rel, src in _sources()
                 for m in BANNED_PLACEHOLDERS.finditer(src)]
    assert not offenders, "\n".join(offenders)


def test_search_placeholders_end_in_an_ellipsis_character():
    offenders = []
    for rel, src in _sources():
        for m in re.finditer(r"""placeholder\s*[=:]\s*["'`](Search[^"'`]*)["'`]""", src):
            if m.group(1).endswith("..."):
                offenders.append(f"{rel}: {m.group(1)}")
    assert not offenders, "\n".join(offenders)


def test_scripts_rename_a_search_box_with_set_search_label():
    src = _read("static/js/collection.js")
    assert not re.search(r"searchInput\.placeholder\s*=", src)


# ---- the count -------------------------------------------------------------

@pytest.mark.parametrize("args,expected", [
    ("4132, 'series'", "4,132 series"),
    ("1, 'publishers', one='publisher'", "1 publisher"),
    ("245, 'publishers', one='publisher', shown=12", "Showing 12 of 245 publishers"),
    ("245, 'publishers', shown=245", "245 publishers"),
    ("245, 'publishers', shown=0", "Showing 0 of 245 publishers"),
    ("4132, 'series', first=1, last=60", "Showing 1–60 of 4,132 series"),
    ("4132, 'series', first=4081, last=4132", "Showing 4,081–4,132 of 4,132 series"),
    ("40, 'series', first=1, last=40", "40 series"),
])
def test_count_text_wording(env, args, expected):
    html = _render(env, "{% from 'partials/filters.html' import count_text %}"
                        "{{ count_text(" + args + ") }}")
    assert html.strip() == expected


def test_result_count_is_the_toolbar_count(env):
    html = _render(env, "{% from 'partials/filters.html' import result_count %}"
                        "{{ result_count('resultCount', 60, 'series', first=1, last=60) }}")
    assert html == '<span class="clu-toolbar-count" id="resultCount" aria-live="polite">60 series</span>'


def test_script_count_says_what_the_macro_says():
    js = _read("static/js/clu-filters.js")
    body = js[js.index("CLU.countText = function"):js.index("CLU.setCount = function")]
    assert "'Showing '" in body and "\\u2013" in body and "' of '" in body
    assert "CLU.formatCount" in body or "f(total)" in body


HAND_WRITTEN_COUNT = re.compile(r"No [a-z ]+ match \(")


def test_no_count_is_hand_written_as_no_match():
    offenders = [rel for rel, src in _sources() if HAND_WRITTEN_COUNT.search(src)]
    assert not offenders, offenders


# ---- no matches ------------------------------------------------------------

def test_no_matches_for_a_search(env):
    html = _render(env, "{% from 'partials/empty_state.html' import no_matches %}"
                        "{{ no_matches('series', query='bat', clear_href='/pull-list') }}")
    assert "No series match “bat”" in html
    assert "Check the spelling" in html
    assert '<a class="btn btn-outline-secondary" href="/pull-list">Clear search</a>' in html
    assert "bi-search" in html and "<h2" in html


def test_no_matches_with_filters(env):
    html = _render(env, "{% from 'partials/empty_state.html' import no_matches %}"
                        "{{ no_matches('series', query='bat', filtered=True, clear_id='plClear') }}")
    assert "No series match these filters" in html
    assert 'id="plClear">Clear filters</button>' in html
    assert "bi-funnel" in html


def test_no_matches_escapes_the_query(env):
    html = _render(env, "{% from 'partials/empty_state.html' import no_matches %}"
                        "{{ no_matches('series', query=q) }}", q='<b>x</b>')
    assert "<b>x</b>" not in html and "&lt;b&gt;" in html


def test_script_no_matches_words_it_the_same():
    js = _read("static/js/clu-page.js")
    body = js[js.index("CLU.noMatchesText = function"):]
    for words in ("' match \\u201c'", "Check the spelling, or search for part of a name.",
                  "' match these filters'", "'Clear search'", "'Clear filters'"):
        assert words in body, words
    tpl = _read("templates/partials/empty_state.html")
    assert "Check the spelling, or search for part of a name." in tpl
    assert "Change or clear a filter to see more." in body and "Change or clear a filter to see more." in tpl


def test_script_empty_state_actions_are_safe_buttons():
    js = _read("static/js/clu-page.js")
    body = js[js.index("CLU.emptyState = function"):js.index("CLU.noMatchesText = function")]
    assert "createTextNode(a.label)" in body
    assert "innerHTML" not in body
    assert "'btn-primary' : 'btn-outline-secondary'" in body


RETIRED_NO_MATCH = re.compile(
    r"No matching entries|No entries match this filter|No series match these filters\.</td>"
    r"|No folders match your search|>No results<|'No results'|>No publishers found<")


def test_no_list_hand_writes_its_no_match_state():
    offenders = [f"{rel}: {m.group(0)}" for rel, src in _sources()
                 for m in RETIRED_NO_MATCH.finditer(src)]
    assert not offenders, "\n".join(offenders)


# ---- the second batch of screens -------------------------------------------

@pytest.mark.parametrize("tpl,count_id,label", [
    ("templates/metadata_browser.html", "mbCount", "Search title or series"),
    ("templates/source_wall.html", "swCount", "Search folders"),
    ("templates/timeline.html", "timelineCount", "Search reading history"),
    ("templates/metadata_history.html", "auditCount", "Search file and series names"),
    ("templates/problem_files.html", "pf-summary", "Search file paths"),
    ("templates/status.html", "downloadsCount", None),
    ("templates/import_mylar.html", "mi-series-count", "Search series"),
    ("templates/partials/modal_metadata_select.html", "cvCount", "Search volumes"),
])
def test_screen_has_a_shared_count_and_label(tpl, count_id, label):
    src = _read(tpl)
    assert f"result_count('{count_id}'" in src
    if label:
        assert f"'{label}')" in src


@pytest.mark.parametrize("rel", [
    "templates/timeline.html", "templates/metadata_history.html",
    "templates/status.html", 
    "static/js/metadata_browser.js", "static/js/source_wall.js",
    "static/js/problem_files.js", "static/js/import-mylar.js",
    "static/js/clu-metadata.js",
])
def test_screen_draws_the_shared_no_matches_state(rel):
    assert "CLU.noMatches(" in _read(rel)


# ---- the final polish pass -------------------------------------------------

def test_count_is_never_repeated_or_shown_beside_an_empty_state():
    # Download Status: no "0 downloads" over "Nothing is downloading".
    status = _read("templates/status.html")
    assert "toolbar('Download list', id='downloads-toolbar', classes='d-none')" in status
    assert "getElementById('downloads-toolbar').classList.toggle('d-none', !hasRows)" in status
    # Source Wall: no "0 items" before a library is picked.
    assert 'class="clu-toolbar-end d-none" id="swCountEnd"' in _read("templates/source_wall.html")
    # Collection: the sticky bar says where you are; the toolbar says how many.
    col = _read("static/js/collection.js")
    assert "readout.textContent = `Page ${CLU.formatCount(currentPage)} of ${CLU.formatCount(totalPages)}`;" in col
    assert "· page ${currentPage}" not in col


def test_narrowed_counts_name_the_whole_list():
    # A server-paged list sends the total from before the search (all_total)
    # with every page to say "Showing 3 of 76". The page must not remember it
    # itself: a stored total goes stale once files are indexed.
    mb = _read("static/js/metadata_browser.js")
    assert "data.all_total" in mb and "unnarrowedTotals" not in mb
    col = _read("static/js/collection.js")
    assert "allBooksUnnarrowed = typeof data.all_total === 'number'" in col
    assert "allBooksUnnarrowed[" not in col
    rl = _read("static/js/reading_list.js")
    assert "if (total === 0 || (narrowed && (data.total_pages || 1) <= 1)) {" in rl


def test_collection_shelves_step_aside_while_narrowed():
    assert "shelves.classList.toggle('clu-narrowing'" in _read("static/js/collection.js")
    assert "#dashboard-sections.clu-narrowing .dashboard-section:not(#library-section)" in \
        _read("static/css/collection.css")


def test_file_manager_view_buttons_are_small_secondary_and_fit_a_phone():
    html = _read("templates/files.html")
    views = html[html.index('files-source-views'):html.index('source-directory-filter')]
    assert "btn-outline-primary" not in views and "btn-outline-danger" not in views
    assert views.count("btn btn-sm btn-outline-secondary") == 4
    assert 'Recent<span class="d-none d-sm-inline"> Files</span>' in views
    assert ".files-source-views .btn {\n    white-space: nowrap;" in _read("static/css/files.css")
