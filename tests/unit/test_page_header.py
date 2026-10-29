"""Every page opens with the one page header, inside the one page shell.

There were six page-header dialects (display-4 titles with KPI tiles, teal
h2s, a centred h1, bare h2s, and pages with no title at all), each page copied
from whichever neighbour it was written next to. The shared pieces now live in
`templates/partials/page_header.html` (the macro) and `templates/base.html`
(the shell), and this test keeps new pages from growing a seventh dialect:

- every page that extends base.html calls `page_header` exactly once;
- no page adds its own outer page container (`container`, `container-lg`,
  `container-fluid` with page spacing) or a display-size title — the shell
  owns width and rhythm;
- only the cover-grid pages opt into the full-width shell.

The macro itself is rendered with plain Jinja below (it needs no Flask), so
its rules — zero stats hidden, More menu only when it holds something,
breadcrumb id for pages whose JS builds it — are pinned too.
"""

import pathlib
import re

import jinja2
import pytest

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]
TEMPLATES = PROJECT_ROOT / "templates"

# Pages that extend base.html but have their own shell on purpose: the login
# card, the setup wizards, and index.html (not rendered by any route).
EXEMPT = {"login.html", "setup.html", "setup_owner.html", "index.html"}

# Cover grids: these use the whole window width.
WIDE_PAGES = {
    "collection.html",
    "metadata_browser.html",
    "source_wall.html",
    "releases.html",
    "to_read.html",
}


def _pages():
    for path in sorted(TEMPLATES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        if re.match(r"""\s*\{%\s*extends\s+['"]base\.html['"]""", text) and path.name not in EXEMPT:
            yield path


PAGES = list(_pages())


def _content_block(text):
    """The page's own markup: everything inside {% block content %}."""
    start = text.find("{% block content %}")
    assert start != -1, "page has no content block"
    return text[start:]


def _strip_modals(html):
    """Modal markup carries its own headings and containers; ignore it."""
    return re.sub(r'<div class="modal[\s\S]*?(?=<div class="modal|\Z)', "", html)


def test_found_the_pages():
    names = {p.name for p in PAGES}
    assert {"pull_list.html", "problem_files.html", "config.html", "collection.html"} <= names
    assert len(PAGES) >= 25


@pytest.mark.parametrize("path", PAGES, ids=lambda p: p.name)
def test_page_uses_the_page_header_once(path):
    text = path.read_text(encoding="utf-8")
    assert "import page_header" in text, "import page_header from partials/page_header.html"
    calls = len(re.findall(r"page_header\(", _content_block(text)))
    assert calls == 1, f"expected one page_header(...) call, found {calls}"


@pytest.mark.parametrize("path", PAGES, ids=lambda p: p.name)
def test_page_has_no_title_of_its_own(path):
    body = _strip_modals(_content_block(path.read_text(encoding="utf-8")))
    assert "<h1" not in body, "the page's <h1> comes from page_header"
    assert not re.search(r"<h[1-3][^>]*\bdisplay-[4-6]\b", body), "no display-size page titles"


@pytest.mark.parametrize("path", PAGES, ids=lambda p: p.name)
def test_page_adds_no_outer_container(path):
    body = _content_block(path.read_text(encoding="utf-8"))
    # The first element of the page must not be a page container: base.html's
    # shell already is one.
    first_div = re.search(r"<div\b[^>]*>", re.sub(r"\{#[\s\S]*?#\}", "", body))
    if first_div:
        cls = re.search(r'class="([^"]*)"', first_div.group(0))
        classes = cls.group(1).split() if cls else []
        assert not any(c in ("container", "container-lg", "container-fluid") for c in classes), (
            f"outer wrapper {first_div.group(0)!r} adds its own page container"
        )
        assert "max-width" not in first_div.group(0)


@pytest.mark.parametrize("path", PAGES, ids=lambda p: p.name)
def test_only_cover_grids_are_full_width(path):
    text = path.read_text(encoding="utf-8")
    wide = "{% block page_shell %}" in text
    if path.name in WIDE_PAGES:
        assert "{% block page_shell %}container-fluid clu-shell-wide{% endblock %}" in text
    else:
        assert not wide, "only cover-grid pages override page_shell"


def test_base_owns_the_shell():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    # The page's block is what it wants under page_width=auto; the setting
    # (core/page_width.py) resolves it into _shell, which the shell renders.
    assert "{% if false %}{% block page_shell %}container-lg{% endblock %}{% endif %}" in base
    assert "{% set _shell = page_width_shell(page_width, self.page_shell()) %}" in base
    assert re.search(
        r'<div class="clu-shell \{\{ _shell \}\}">\s*'
        r"\{% block content %\}\{% endblock %\}",
        base,
    )
    # Resolved before the header is included, or the nav cannot follow it.
    assert base.index("{% set _shell") < base.index("{% include 'header.html' %}")
    assert "css/clu-page.css" in base and "js/clu-page.js" in base


@pytest.mark.parametrize("name", ["header.html", "footer.html"])
def test_nav_and_footer_follow_the_shell(name):
    """They used to be plain `container`, a third width matching neither the
    constrained nor the wide shell."""
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    assert "page_width_chrome(_shell)" in text
    assert '<div class="container">' not in text


# ---- the macro ------------------------------------------------------------

@pytest.fixture(scope="module")
def env():
    return jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)), autoescape=True)


def _render(env, body):
    return env.from_string(
        "{% from 'partials/page_header.html' import page_header %}" + body
    ).render()


def test_plain_header_has_one_h1_and_no_menu(env):
    html = _render(env, "{{ page_header('Users', 'bi-people') }}")
    assert html.count("<h1") == 1
    assert 'class="bi bi-people"' in html and 'aria-hidden="true"' in html
    assert "data-clu-actions" not in html  # no actions, no action bar


def test_stats_hide_zero_and_none_and_format_thousands(env):
    html = _render(env, """{{ page_header('Pull List', stats=[
        {'value': 4115, 'label': 'tracked'},
        {'value': 0, 'label': 'missing'},
        {'value': None, 'label': 'upcoming'},
        {'value': 0, 'label': 'from lists', 'keep_zero': True, 'id': 'rl'}]) }}""")
    assert "4,115" in html and "tracked" in html
    assert "missing" not in html and "upcoming" not in html
    assert 'id="rl"' in html and "from lists" in html


def test_more_menu_hidden_until_it_has_items(env):
    only_actions = _render(env, """{% call(slot) page_header('X') %}{% if slot == 'actions' %}
        <button class="btn btn-sm btn-outline-secondary" id="a">A</button>{% endif %}{% endcall %}""")
    assert "clu-page-more-empty" in only_actions  # rendered, so phones can fold into it

    with_more = _render(env, """{% call(slot) page_header('X') %}{% if slot == 'more' %}
        <li><button class="dropdown-item" id="b">B</button></li>{% endif %}{% endcall %}""")
    assert "clu-page-more-empty" not in with_more
    assert 'id="b"' in with_more and "More" in with_more
    assert "visually-hidden" in with_more  # "More actions" for screen readers


def test_breadcrumb_slot_and_js_built_breadcrumb(env):
    crumbs = _render(env, """{% call(slot) page_header('Weapon X') %}{% if slot == 'crumbs' %}
        <li class="breadcrumb-item"><a href="/pull-list">Pull List</a></li>{% endif %}{% endcall %}""")
    assert 'aria-label="breadcrumb"' in crumbs and "Pull List" in crumbs
    assert crumbs.index("breadcrumb") < crumbs.index("<h1")

    js_built = _render(env, "{{ page_header('Library', crumbs_id='breadcrumb', title_id='t') }}")
    assert 'id="breadcrumb"' in js_built and 'id="t"' in js_built


def test_no_crumbs_no_nav(env):
    assert "breadcrumb" not in _render(env, "{{ page_header('Logs', 'bi-journal-text') }}")


def test_help_folds_behind_a_toggle(env):
    html = _render(env, """{% call(slot) page_header('Schedules', subtitle='Times are in UTC.') %}{% if slot == 'help' %}
        <p id="why">Jobs are stored in UTC.</p>{% endif %}{% endcall %}""")
    assert '<details class="clu-page-help">' in html and "How this works" in html
    assert html.index("clu-page-sub") < html.index("clu-page-help") < html.index('id="why"')
    assert "data-clu-actions" not in html  # help alone adds no action bar
    assert "clu-page-help" not in _render(env, "{{ page_header('Logs') }}")


def test_header_secondaries_are_neutral_on_every_theme():
    """Minty's secondary is pink; the header's grey buttons must not follow it."""
    css = (TEMPLATES.parent / "static" / "css" / "clu-page.css").read_text(encoding="utf-8")
    rule = css[css.index(".clu-page-actions .btn-outline-secondary,"):]
    rule = rule[:rule.index("}")]
    assert "--bs-btn-color: var(--bs-body-color)" in rule
    assert "--bs-secondary)" not in rule
    # Bootswatch sets color on the class itself, so the variable alone loses.
    assert "color: var(--bs-btn-color);" in rule
