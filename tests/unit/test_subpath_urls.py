"""CLU served under a path prefix: page scripts must keep the prefix.

Behind ``X-Forwarded-Prefix: /clu`` Flask builds every ``url_for`` with the
prefix (``ProxyFix(x_prefix=1)``). Page scripts did not: about 390 absolute
``fetch('/api/...')`` calls and 20 hardcoded ``/static/`` URLs left it, so a
subpath install rendered a page whose every request went to the proxy's root.

``static/js/clu-url.js`` fixes that in one place. These tests pin the three
things that keep it true: it loads first, it decides "already prefixed"
correctly, and nobody writes the two things it cannot intercept (a literal
``/static/`` URL in markup rendered before it, and ``location.href = '/...'``).
"""
import json
import pathlib
import re
import shutil
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SHIM = ROOT / "static" / "js" / "clu-url.js"
LAYOUTS = ("base.html", "login.html")   # the only templates that are whole documents


def _sources():
    for folder, pattern in (("static/js", "*.js"), ("templates", "*.html")):
        for path in sorted((ROOT / folder).rglob(pattern)):
            if path == SHIM:
                continue
            yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")


def _offenders(pattern):
    rx = re.compile(pattern)
    return [f"{rel}:{text.count(chr(10), 0, m.start()) + 1}"
            for rel, text in _sources() for m in rx.finditer(text)]


def test_no_hardcoded_static_url():
    """Templates use url_for('static', ...) / asset(); scripts use CLU.staticUrl()."""
    assert _offenders(r"""["'`(]/static/""") == []


def test_no_unprefixed_navigation():
    """The browser lets nothing intercept a location change, so it is explicit."""
    assert _offenders(r"""location(\.href)?\s*=\s*["'`]/""") == []
    assert _offenders(r"""location\.(assign|replace)\(\s*["'`]/""") == []


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_shim_is_the_first_script(layout):
    html = (ROOT / "templates" / layout).read_text(encoding="utf-8")
    first = re.search(r"<script\b[^>]*>", html)
    assert first and "asset('js/clu-url.js')" in first.group(0), (
        f"{layout}: clu-url.js must load before every other script, or a fetch "
        "made while the page parses leaves the path prefix"
    )
    meta = html.index('<meta name="clu-root" content="{{ request.script_root }}">')
    assert meta < first.start(), "the shim reads the meta tag when it loads"


def test_only_the_layouts_are_whole_documents():
    """A template that renders its own <html> would need the shim too."""
    whole = sorted(p.name for p in (ROOT / "templates").rglob("*.html")
                   if "<html" in p.read_text(encoding="utf-8"))
    assert whole == sorted(LAYOUTS)


def test_the_manifest_is_relative():
    """A static file cannot know the prefix; relative URLs resolve against the
    manifest's own address, which carries it."""
    manifest = json.loads((ROOT / "static" / "manifest.json").read_text(encoding="utf-8"))
    assert not manifest["start_url"].startswith("/")
    assert all(not icon["src"].startswith("/") for icon in manifest["icons"])


def test_with_no_prefix_nothing_is_wrapped():
    """The usual install: the shim defines its helpers and stops."""
    src = SHIM.read_text(encoding="utf-8")
    assert src.index("if (!root) return;") < src.index("window.fetch = function")


# ── Behaviour, under node when it is installed ─────────────────────────────

_HARNESS = r"""
const fs = require('fs');
function load(rootValue) {
  const calls = [];
  const win = {
    location: { origin: 'http://host' },
    fetch: function (input) { calls.push(['fetch', String(input)]); },
    open: function (url) { calls.push(['open', url]); },
    URL: URL,
    navigator: { sendBeacon: function (url) { calls.push(['beacon', url]); return true; } },
    XMLHttpRequest: function () {},
    EventSource: function (url) { calls.push(['es', url]); },
  };
  win.XMLHttpRequest.prototype.open = function (m, url) { calls.push(['xhr', url]); };
  const doc = {
    querySelector: function () { return { getAttribute: function () { return rootValue; } }; },
    documentElement: {},
  };
  new Function('window', 'document', fs.readFileSync(process.argv[2], 'utf8') + '\n')(win, doc);
  return { win: win, calls: calls };
}
const out = {};
const bare = load('');
out.bare = ['/api/x', 'images/a.png'].map(v => bare.win.CLU.url(v));
out.bareStatic = bare.win.CLU.staticUrl('images/loading.svg');
out.bareFetchWrapped = bare.win.fetch.toString().indexOf('nativeFetch') !== -1;
const pre = load('/clu/');
const u = pre.win.CLU.url;
out.urls = ['/api/x', '/clu/api/x', '/clu', '/clu?x=1', '/clubhouse', '//cdn/x', 'https://a/b',
            'relative/x', '/', '', '#top', 'data:image/png;base64,AA'].map(v => u(v));
out.nonString = [u(undefined), u(null), u(5)];
out.staticUrl = pre.win.CLU.staticUrl('/images/loading.svg');
pre.win.fetch('/api/a');
pre.win.fetch(new URL('http://host/api/b?q=1'));
pre.win.fetch(new URL('http://other/api/c'));
new pre.win.XMLHttpRequest().open('GET', '/api/d');
new pre.win.EventSource('/stream/x');
pre.win.navigator.sendBeacon('/api/e');
pre.win.open('/reader?path=x');
out.calls = pre.calls;
console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def shim(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    harness = tmp_path_factory.mktemp("shim") / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    done = subprocess.run([node, str(harness), str(SHIM)], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_no_prefix_is_the_identity(shim):
    assert shim["bare"] == ["/api/x", "images/a.png"]
    assert shim["bareStatic"] == "/static/images/loading.svg"
    assert shim["bareFetchWrapped"] is False


def test_prefix_is_added_once_and_only_to_local_paths(shim):
    assert shim["urls"] == [
        "/clu/api/x",          # the common case
        "/clu/api/x",          # already prefixed (a url_for URL from the server)
        "/clu",                # the prefix itself
        "/clu?x=1",
        "/clu/clubhouse",      # shares the prefix's letters, not its segment
        "//cdn/x",             # protocol-relative: another host
        "https://a/b",
        "relative/x",
        "/clu/",
        "",
        "#top",
        "data:image/png;base64,AA",
    ]
    assert shim["nonString"] == [None, None, 5]
    assert shim["staticUrl"] == "/clu/static/images/loading.svg"


def test_every_request_api_is_wrapped(shim):
    assert shim["calls"] == [
        ["fetch", "/clu/api/a"],
        ["fetch", "/clu/api/b?q=1"],
        ["fetch", "http://other/api/c"],
        ["xhr", "/clu/api/d"],
        ["es", "/clu/stream/x"],
        ["beacon", "/clu/api/e"],
        ["open", "/clu/reader?path=x"],
    ]
