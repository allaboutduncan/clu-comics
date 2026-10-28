"""One toast, one empty state, no native dialogs for messages.

The app had eight local showToast copies with three argument orders (four of
them white boxes that ignored the theme), thirteen pages including their own
toast container, a File Manager script calling a showToast that did not exist
on its page, and 75 alert() calls despite CLAUDE.md forbidding them. The
shared pieces now are:

- static/js/clu-toast.js (CLU.toast / CLU.showToast), loaded by base.html;
- partials/toast_container.html, rendered once by base.html;
- partials/empty_state.html (empty_state) and its JS twin CLU.emptyState.

This keeps a ninth copy from appearing.
"""

import pathlib
import re

import jinja2
import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
JS = ROOT / "static" / "js"


def _sources():
    for path in sorted(list(TEMPLATES.rglob("*.html")) + list(JS.glob("*.js"))):
        yield path, path.read_text(encoding="utf-8")


def _code_lines(text):
    """Lines that are not comments (good enough for a ratchet)."""
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith(("//", "*", "/*", "{#", "<!--")):
            continue
        yield n, line


def test_base_renders_the_one_container_and_loads_the_toast():
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert "{% include 'partials/toast_container.html' %}" in base
    toast = base.index("js/clu-toast.js")
    assert base.index("bootstrap.bundle.min.js") < toast < base.index("{% block scripts %}")


def test_no_page_includes_its_own_toast_container():
    offenders = [p.name for p, t in _sources()
                 if p.suffix == ".html" and p.name != "base.html"
                 and "partials/toast_container.html" in t]
    assert offenders == []


def test_no_page_builds_a_toast_container():
    offenders = [f"{p.relative_to(ROOT)}:{n}" for p, t in _sources()
                 if p.name not in ("toast_container.html", "clu-toast.js")
                 for n, line in _code_lines(t)
                 if re.search(r"""class=["'][^"']*\btoast-container\b""", line)
                 or "className = 'toast-container" in line]
    assert offenders == []


def test_no_local_show_toast():
    offenders = [f"{p.relative_to(ROOT)}:{n}" for p, t in _sources()
                 for n, line in _code_lines(t)
                 if re.search(r"function\s+showToast(GC)?\s*\(", line)
                 or re.search(r"(?<![\w.$])showToast(GC)?\(", line)]
    assert offenders == [], "use CLU.toast(message, type) or CLU.showToast(title, message, type)"


def test_no_native_alert():
    offenders = [f"{p.relative_to(ROOT)}:{n}" for p, t in _sources()
                 for n, line in _code_lines(t)
                 if re.search(r"(?<![\w.$])alert\(", line)]
    assert offenders == [], "CLAUDE.md: messages are toasts (CLU.toast), never alert()"


def test_toast_never_renders_html():
    src = (JS / "clu-toast.js").read_text(encoding="utf-8")
    assert "innerHTML" not in src and "insertAdjacentHTML" not in src
    assert "alert(" not in src.replace("role', urgent ? 'alert'", "")
    # 'danger' is what Bootstrap-minded callers pass; it must not render as info.
    assert "type === 'danger'" in src


def test_no_legacy_empty_state_class():
    offenders = [f"{p.relative_to(ROOT)}:{n}" for p, t in _sources()
                 for n, line in _code_lines(t)
                 if re.search(r"""class=["']empty-state\b""", line)]
    assert offenders == [], "use empty_state() from partials/empty_state.html"


# ---- the macro ------------------------------------------------------------

@pytest.fixture(scope="module")
def env():
    return jinja2.Environment(loader=jinja2.FileSystemLoader(str(TEMPLATES)), autoescape=True)


def _render(env, body):
    return env.from_string(
        "{% from 'partials/empty_state.html' import empty_state %}" + body).render(name="<b>x</b>")


def test_empty_state_basics(env):
    html = _render(env, "{{ empty_state('No publishers', 'Add one.', 'bi-building') }}")
    assert '<h2 class="clu-empty-title">No publishers</h2>' in html
    assert 'class="clu-empty-text">Add one.</p>' in html
    assert 'aria-hidden="true"' in html
    assert "clu-empty-actions" not in html  # no caller, no action row
    assert "display: none" not in html


def test_empty_state_hidden_actions_and_escaping(env):
    html = _render(env, """{% call empty_state('Nothing', 'Matching ' ~ name, id='e', hidden=True) %}
        <button class="btn btn-primary" id="go">Go</button>{% endcall %}""")
    assert 'id="e"' in html and 'style="display: none;"' in html
    assert 'id="go"' in html and "clu-empty-actions" in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html  # a plain string is escaped

    markup = _render(env, "{% set m %}Has <code>series.json</code>{% endset %}{{ empty_state('T', m) }}")
    assert "<code>series.json</code>" in markup  # a {% set %} block is Markup


def test_status_page_says_when_nothing_is_running():
    html = (TEMPLATES / "status.html").read_text(encoding="utf-8")
    assert "id='downloads-empty'" in html and "Nothing is downloading" in html
    # An unreachable status endpoint must not read as an empty queue.
    assert "'unreachable'" in html and "data === null" in html


def test_file_manager_says_when_a_listing_is_empty():
    """A Downloads folder with nothing in it rendered a blank panel."""
    src = (JS / "files.js").read_text(encoding="utf-8")
    assert "function appendEmptyListing(" in src
    downloads = src[src.index("function loadDownloads("):]
    downloads = downloads[:downloads.index("\n}\n")]
    assert "'No downloads waiting'" in downloads
    assert "if (data.error)" in downloads  # a server error is not an empty folder
    listing = src[src.index("function renderDirectoryListing("):]
    listing = listing[:listing.index("\n}\n")]
    assert "'This folder is empty'" in listing
