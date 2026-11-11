"""The asset() template global and the cache headers on the static endpoint.

Only a static URL carrying the file's current content hash may be cached for a
year. Everything else -- an unversioned URL, a stale token from a page cached
before an upgrade, an image, a 404 -- must keep werkzeug's ``no-cache``, or a
browser would hold old bytes under a URL it never asks about again.
"""
import re

from core.version import __version__

ASSET_URL = re.compile(r'/static/(?:js|css)/[^"\']+')


def _asset(app, filename, **ctx):
    with app.test_request_context(**ctx):
        return app.jinja_env.globals["asset"](filename)


def test_asset_url_is_stable_and_versioned(app):
    first = _asset(app, "js/clu-toast.js")
    assert first == _asset(app, "js/clu-toast.js")
    assert first.startswith("/static/js/clu-toast.js?v=")


def test_asset_url_honours_the_script_root(app):
    url = _asset(app, "js/clu-toast.js", base_url="http://localhost/clu/")
    assert url.startswith("/clu/static/js/clu-toast.js?v=")


def test_unknown_asset_falls_back_to_the_release(app):
    url = _asset(app, "js/does-not-exist.js")
    assert url.endswith(f"?v={__version__}")


def test_rendered_pages_reference_the_same_versioned_urls(client):
    bodies = [client.get("/account").get_data(as_text=True)
              for _ in range(2)]
    urls = [set(ASSET_URL.findall(body)) for body in bodies]
    assert urls[0], "no /static/js|css URL rendered"
    assert urls[0] == urls[1]
    assert all("?v=" in u for u in urls[0])


class TestStaticCacheHeaders:

    def test_versioned_url_is_immutable(self, app, client):
        resp = client.get(_asset(app, "js/clu-toast.js"))
        assert resp.status_code == 200
        cc = resp.headers["Cache-Control"]
        assert "public" in cc
        assert "max-age=31536000" in cc
        assert "immutable" in cc
        assert "no-cache" not in cc
        assert resp.headers.get("ETag")

    def test_revalidation_keeps_the_long_max_age(self, app, client):
        url = _asset(app, "js/clu-toast.js")
        etag = client.get(url).headers["ETag"]
        resp = client.get(url, headers={"If-None-Match": etag})
        assert resp.status_code == 304
        cc = resp.headers["Cache-Control"]
        assert "max-age=31536000" in cc and "immutable" in cc

    def test_unversioned_url_is_no_cache(self, client):
        cc = client.get("/static/js/clu-toast.js").headers["Cache-Control"]
        assert "no-cache" in cc and "max-age=31536000" not in cc

    def test_stale_token_is_no_cache(self, client):
        cc = client.get("/static/js/clu-toast.js?v=deadbeef00").headers["Cache-Control"]
        assert "no-cache" in cc and "max-age=31536000" not in cc

    def test_images_stay_no_cache(self, client):
        cc = client.get("/static/images/loading.svg").headers["Cache-Control"]
        assert "no-cache" in cc and "max-age=31536000" not in cc

    def test_missing_file_gets_no_max_age(self, client):
        resp = client.get("/static/js/nope.js?v=x")
        assert resp.status_code == 404
        assert "max-age=31536000" not in resp.headers.get("Cache-Control", "")

    def test_pages_are_not_long_cached(self, client):
        resp = client.get("/login")
        assert "max-age=31536000" not in resp.headers.get("Cache-Control", "")
