"""Pages rendered under a path prefix keep every URL inside it.

A reverse proxy that serves CLU at ``/clu`` sends ``X-Forwarded-Prefix``;
``ProxyFix`` turns that into ``SCRIPT_NAME``, which is what ``base_url`` sets
here. Anything in the rendered HTML that starts with ``/`` and not with the
prefix is a link, image or script the browser would ask the proxy's root for.

Every argument-free GET page the test app serves is rendered, so a new page
is covered without being added to a list.
"""
import re

PREFIX = "/clu"

# src / href / action attributes holding a path on this origin.
_ATTR = re.compile(r"""\b(?:src|href|action|poster)\s*=\s*(["'])(/[^"']*)\1""")


def _get(client, page):
    return client.get(page, base_url=f"http://localhost{PREFIX}/")


def _attribute_urls(html):
    # Markup inside <script> is a string a page script will insert later; the
    # shim's observer rewrites it when it lands in the document.
    without_scripts = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.S)
    return [m.group(2) for m in _ATTR.finditer(without_scripts)]


def _html_pages(app, client):
    """(rule, html) for every parameterless GET route that renders a layout."""
    pages = []
    for rule in app.url_map.iter_rules():
        if rule.arguments or "GET" not in rule.methods:
            continue
        path = rule.rule
        if path.startswith(("/api/", "/static", "/opds")):
            continue
        try:
            resp = _get(client, path)
        except Exception:
            continue    # a page whose data the fixture app does not stub
        if resp.status_code != 200 or "text/html" not in resp.content_type:
            continue
        html = resp.get_data(as_text=True)
        if "<html" in html:
            pages.append((path, html))
    return pages


def test_no_attribute_leaves_the_prefix(app, client):
    pages = _html_pages(app, client)
    assert len(pages) >= 10, f"only rendered {[p for p, _ in pages]}"
    escaped = {}
    for path, html in pages:
        bad = sorted({u for u in _attribute_urls(html)
                      if not u.startswith("//")
                      and not (u == PREFIX or u.startswith(PREFIX + "/"))})
        if bad:
            escaped[path] = bad
    assert escaped == {}, f"URLs outside {PREFIX}: {escaped}"


def test_every_page_tells_the_shim_its_prefix(app, client):
    for path, html in _html_pages(app, client):
        assert f'<meta name="clu-root" content="{PREFIX}">' in html, path
        first_script = re.search(r"<script\b[^>]*>", html).group(0)
        assert f'src="{PREFIX}/static/js/clu-url.js?v=' in first_script, path


def test_without_a_prefix_the_root_is_empty(client):
    html = client.get("/publishers").get_data(as_text=True)
    assert '<meta name="clu-root" content="">' in html
    assert 'src="/static/js/clu-url.js?v=' in html
