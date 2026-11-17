"""One HTML escaper, safe in quoted attributes, and no handler strings.

``CLU.escapeHtml`` (static/js/clu-utils.js) used to be the ``div.textContent``
trick, which never escapes a quote, while about eighteen local copies across
``static/js`` and the inline scripts in ``templates/`` each did something
slightly different. Values escaped by it were put inside ``title="..."``,
``data-*="..."`` and ``src="..."`` attributes all the same.

Now:

* there is exactly one implementation, in clu-utils.js, which base.html loads
  in ``<head>`` for every page; nothing else defines one;
* it escapes ``& < > " '`` (and the backtick), and ``CLU.safeUrl`` sits beside
  it for URLs that came from remote data, because escaping does not change a
  ``javascript:`` URL;
* no inline ``on*="..."`` handler is built by interpolating a value. Escaping
  cannot make that safe: the HTML parser decodes the entities before the
  handler's JavaScript is compiled. Values go in ``data-*`` attributes and a
  fixed handler (or a listener) reads them back. The files that still build
  one from an id or an internal enum are counted in ``HANDLER_BASELINE``; the
  counts may only go down.

Read from source, in the style of ``test_feedback.py``; the behaviour of the
escaper itself is run under node where it is installed.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
JS_DIR = ROOT / "static" / "js"
TEMPLATES = ROOT / "templates"
UTILS = JS_DIR / "clu-utils.js"
NODE = shutil.which("node")


def _js_files():
    return sorted(JS_DIR.glob("*.js"))


def _templates():
    return sorted(TEMPLATES.rglob("*.html"))


def _all_sources():
    return _js_files() + _templates()


def _rel(path):
    return path.relative_to(ROOT).as_posix()


def _read(path):
    return path.read_text(encoding="utf-8")


# --- one implementation -----------------------------------------------------

# A named escaper defined anywhere: `function escapeHtml(`, `escapeHtml = (`,
# `const esc = s => ...replace(`, `_escapeAttr: function`...
DEFINITION_RE = re.compile(
    r"""(?:function\s+(?P<a>[\w$]+)\s*\()"""
    r"""|(?:(?<![\w$])(?P<b>[\w$]+)\s*[:=]\s*(?:function\b|\(?[\w\s,]*\)?\s*=>))""")

# esc, _esc, escapeHtml, _escapeAttr, escHtml, htmlEscape... but not
# escapeRegExp, which escapes for a RegExp, not for HTML.
ESCAPER_NAME_RE = re.compile(r"^_*(?:esc\w*|\w*Escape\w*|\w*escape\w*)$", re.IGNORECASE)

# The two shapes an implementation takes: the entity table, or the DOM trick.
ENTITY_LITERAL_RE = re.compile(r"""['"`]&amp;['"`]""")
DOM_TRICK_RE = re.compile(r"\.textContent\s*=[^;]*;\s*return\s+\w+\.innerHTML\b")


def _definitions(src):
    for m in DEFINITION_RE.finditer(src):
        name = m.group("a") or m.group("b")
        if not ESCAPER_NAME_RE.match(name) or "regexp" in name.lower():
            continue
        yield name, src.count("\n", 0, m.start()) + 1


def test_exactly_one_escape_html_under_static_js():
    found = [(_rel(p), name)
             for p in _js_files() for name, _line in _definitions(_read(p))]
    assert found == [("static/js/clu-utils.js", "escapeHtml")], (
        "Escape through CLU.escapeHtml (clu-utils.js); do not define another:\n  "
        + "\n  ".join(f"{f}: {n}" for f, n in found))


def test_no_escaper_is_defined_in_a_template():
    found = [f"{_rel(p)}:{line}: {name}"
             for p in _templates() for name, line in _definitions(_read(p))]
    assert not found, (
        "base.html loads clu-utils.js for every page; use CLU.escapeHtml:\n  "
        + "\n  ".join(found))


@pytest.mark.parametrize("pattern", [ENTITY_LITERAL_RE, DOM_TRICK_RE],
                         ids=["entity-table", "textContent-innerHTML"])
def test_no_inline_reimplementation(pattern):
    """Catches an escaper written without a recognisable name."""
    found = [f"{_rel(p)}:{_read(p).count(chr(10), 0, m.start()) + 1}"
             for p in _all_sources() if p != UTILS
             for m in pattern.finditer(_read(p))]
    assert not found, "Use CLU.escapeHtml:\n  " + "\n  ".join(found)


def test_every_caller_names_the_shared_escaper():
    """A bare escapeHtml() would only work while some other script happened to
    leave a global of that name behind (files.js used to)."""
    bare = re.compile(r"(?<![\w.$])escapeHtml\b(?!\s*=\s*function)")
    found = [f"{_rel(p)}:{_read(p).count(chr(10), 0, m.start()) + 1}"
             for p in _all_sources() if p != UTILS
             for m in bare.finditer(_read(p))]
    assert not found, "Call CLU.escapeHtml:\n  " + "\n  ".join(found)


def test_base_loads_the_escaper_once_in_head():
    base = _read(TEMPLATES / "base.html")
    head = base[:base.index("</head>")]
    assert head.count("asset('js/clu-utils.js')") == 1
    assert base.count("js/clu-utils.js") == 1


def test_no_page_loads_a_second_copy():
    """clu-utils.js installs document-level listeners (Enter-to-confirm,
    stacked-modal focus); loading it twice would install them twice."""
    offenders = [_rel(p) for p in _templates()
                 if p.name != "base.html" and "js/clu-utils.js" in _read(p)
                 and "{% extends" in _read(p)]
    assert not offenders, offenders


def test_standalone_pages_that_escape_load_the_escaper():
    for p in _templates():
        src = _read(p)
        if "{% extends" in src or p.name == "base.html":
            continue
        if "CLU.escapeHtml" in src or "CLU.safeUrl" in src:
            assert "asset('js/clu-utils.js')" in src, _rel(p)


# --- the escaper itself ------------------------------------------------------

def test_the_table_escapes_both_quotes():
    src = _read(UTILS)
    body = src[src.index("var _HTML_ESCAPES"):src.index("CLU.safeUrl = function")]
    for ch, entity in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"),
                       ('"', "&quot;"), ("'", "&#39;"), ("`", "&#96;")):
        quoted = f"'{ch}'" if ch != "'" else '"\'"'
        assert f"{quoted}: '{entity}'" in body, ch
    assert "replace(/[&<>\"'`]/g" in body
    assert "createElement" not in body, "string replacement, not the DOM"


HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const sandbox = { document: { addEventListener() {} }, window: {}, console };
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox);
const CLU = sandbox.CLU;
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = {
  escape: cases.escape.map(v => CLU.escapeHtml(v === '__undefined__' ? undefined : v)),
  safeUrl: cases.safeUrl.map(v => CLU.safeUrl(v)),
  safeUrlFallback: CLU.safeUrl('javascript:alert(1)', ''),
};
console.log(JSON.stringify(out));
"""

ESCAPE_CASES = [
    ("Devil's Due", "Devil&#39;s Due"),
    ('a "quoted" title', "a &quot;quoted&quot; title"),
    ("<img src=x onerror=alert(1)>", "&lt;img src=x onerror=alert(1)&gt;"),
    ("Tom & Jerry #1 100%", "Tom &amp; Jerry #1 100%"),
    ("&amp;", "&amp;amp;"),
    ("`tick`", "&#96;tick&#96;"),
    (None, ""),
    ("__undefined__", ""),
    (0, "0"),
    (42, "42"),
]

SAFE_URL_CASES = [
    ("https://getcomics.org/x?a=1&b=2", "https://getcomics.org/x?a=1&b=2"),
    ("HTTP://example.com/a.jpg", "HTTP://example.com/a.jpg"),
    ("/api/thumbnail?path=%2Fdata%2Fx.cbz", "/api/thumbnail?path=%2Fdata%2Fx.cbz"),
    ("?page=2", "?page=2"),
    ("javascript:alert(1)", "#"),
    (" JavaScript:alert(1)", "#"),
    ("java\tscript:alert(1)", "#"),
    ("data:text/html,<b>x</b>", "#"),
    ("vbscript:msgbox(1)", "#"),
    ("//evil.example/x", "#"),
    ("/\\evil.example/x", "#"),
    ("", "#"),
    (None, "#"),
]


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_escape_html_and_safe_url_behaviour(tmp_path):
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    cases = tmp_path / "cases.json"
    cases.write_text(json.dumps({
        "escape": [c for c, _ in ESCAPE_CASES],
        "safeUrl": [c for c, _ in SAFE_URL_CASES],
    }), encoding="utf-8")
    out = subprocess.run([NODE, str(harness), str(UTILS), str(cases)],
                         capture_output=True, text=True, encoding="utf-8",
                         timeout=30, check=True)
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["escape"] == [e for _, e in ESCAPE_CASES]
    assert result["safeUrl"] == [e for _, e in SAFE_URL_CASES]
    assert result["safeUrlFallback"] == ""


# --- inline handlers ---------------------------------------------------------

# An on*= attribute whose value is built from an interpolation (`${...}`) or a
# string concatenation (' + x + '). A fixed handler that reads this.dataset is
# fine and is not matched.
INLINE_HANDLER_RE = re.compile(
    r"""(?<![\w-])on[a-z]+\s*=\s*\\?(?:"[^"]*?(?:\$\{|'\s*\+)|'[^']*?(?:\$\{|"\s*\+))""",
    re.IGNORECASE)

# Files that still build a handler from a value. Every remaining one passes a
# numeric id/index or an internal enum (provider/client type), never a name
# or path. Lower a count when you convert a site; never raise one.
HANDLER_BASELINE = {
    "templates/config.html": 24,
    "templates/users.html": 1,
    "static/js/clu-cbz-split.js": 3,
}


def _handler_counts():
    counts = {}
    for p in _all_sources():
        n = len(INLINE_HANDLER_RE.findall(_read(p)))
        if n:
            counts[_rel(p)] = n
    return counts


def test_no_inline_handler_interpolates_a_value():
    over = {f: n for f, n in _handler_counts().items()
            if n > HANDLER_BASELINE.get(f, 0)}
    assert not over, (
        "An inline handler is parsed as HTML and then as JS, and no escaping "
        "covers both. Put the value in a data-* attribute and read it from a "
        "listener or a fixed handler (this.dataset.x):\n  "
        + "\n  ".join(f"{f}: {n} (baseline {HANDLER_BASELINE.get(f, 0)})"
                      for f, n in sorted(over.items())))


def test_handler_baseline_is_not_stale():
    """A baseline above the real count would let a new handler in unnoticed."""
    counts = _handler_counts()
    stale = {f: (n, counts.get(f, 0)) for f, n in HANDLER_BASELINE.items()
             if counts.get(f, 0) < n}
    assert not stale, f"lower HANDLER_BASELINE: {stale}"


def test_the_handler_pattern_itself():
    assert INLINE_HANDLER_RE.search("html = `<a onclick=\"go('${path}')\">`;")
    assert INLINE_HANDLER_RE.search("'<a onclick=\"go(\\'' + path + '\\')\">'")
    assert INLINE_HANDLER_RE.search("'onchange=\"f(' + i + ')\"'")
    assert not INLINE_HANDLER_RE.search(
        "`<a data-path=\"${p}\" onclick=\"go(this.dataset.path)\">`")
    assert not INLINE_HANDLER_RE.search("'<button onclick=\"emptyTrash()\">'")


# --- the converted renderers -------------------------------------------------

@pytest.mark.parametrize("path, needle", [
    # Dashboard swipers: folder names like "Devil's Due" ended the JS string.
    ("static/js/collection.js", "document.addEventListener('click', onDashboardClick)"),
    ("static/js/collection.js", 'data-dash-action="open-folder"'),
    # Scraped GetComics cover.
    ("static/js/clu-source-search.js", "CLU.safeUrl(thumb, '')"),
    # Provider description is text, never markup.
    ("static/js/clu-metadata.js", "CLU.escapeHtml(descriptionText)"),
    ("static/js/clu-metadata.js", "CLU.safeUrl(volume.image_url, '')"),
    ("templates/series.html", "data-candidate-alias="),
])
def test_converted_renderers(path, needle):
    assert needle in _read(ROOT / path)


@pytest.mark.parametrize("path, gone", [
    ("static/js/clu-metadata.js", "'<option value=\"' + key"),
    ("static/js/clu-metadata.js", "+ volume.description +"),
    ("static/js/clu-metadata.js", "'<img src=\"' + volume.image_url"),
    ("static/js/collection.js", "onclick=\"loadDirectory('${pub.path}')\""),
])
def test_raw_provider_values_are_gone(path, gone):
    assert gone not in _read(ROOT / path)
