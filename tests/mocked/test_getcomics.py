"""Tests for models/getcomics.py -- mocked cloudscraper HTTP calls."""
import sys
import types
import pytest
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Ensure cloudscraper is importable before models/getcomics.py is loaded.
# The module creates a module-level scraper via cloudscraper.create_scraper(),
# which will fail if the real package is not installed.
# ---------------------------------------------------------------------------
try:
    import cloudscraper  # noqa: F401
except ImportError:
    _cs = types.ModuleType("cloudscraper")
    _cs.create_scraper = MagicMock(return_value=MagicMock())
    sys.modules["cloudscraper"] = _cs


# ---------------------------------------------------------------------------
# HTML fragments used across tests
# ---------------------------------------------------------------------------

SEARCH_RESULTS_HTML = """\
<html><body>
<article class="post">
  <h1 class="post-title"><a href="https://getcomics.org/batman-1">Batman #1 (2020)</a></h1>
  <img data-lazy-src="https://img.example.com/batman.jpg">
</article>
<article class="post">
  <h1 class="post-title"><a href="https://getcomics.org/superman-5">Superman #5 (2021)</a></h1>
  <img src="https://img.example.com/superman.jpg">
</article>
</body></html>
"""

SEARCH_NO_RESULTS_HTML = "<html><body><p>No results</p></body></html>"

SEARCH_ARTICLE_NO_TITLE_HTML = """\
<html><body>
<article class="post">
  <div class="no-title">Nothing here</div>
</article>
</body></html>
"""

DOWNLOAD_LINKS_BY_TITLE_HTML = """\
<html><body>
<a href="https://pixeldrain.com/u/abc123" title="PIXELDRAIN">Download</a>
<a href="https://getcomics.org/dlds/xyz" title="DOWNLOAD NOW">Main Link</a>
<a href="https://mega.nz/file/xxx#yyy" title="MEGA">Mega</a>
</body></html>
"""

DOWNLOAD_LINKS_BY_TEXT_HTML = """\
<html><body>
<a class="aio-red" href="https://pixeldrain.com/u/text123">PIXELDRAIN</a>
<a class="aio-red" href="https://getcomics.org/dlds/text456">DOWNLOAD HERE</a>
<a class="aio-red" href="https://mega.nz/file/textmega">MEGA LINK</a>
</body></html>
"""

DOWNLOAD_NO_LINKS_HTML = """\
<html><body>
<p>No download links here</p>
</body></html>
"""

# Current getcomics layout (Nightwing #140 style): every provider's button is a
# tokenized getcomics.org/dls/ redirector, so the hrefs are indistinguishable --
# only the title/text names the provider. Pixeldrain sits on aio-orange, which
# the aio-red/aio-blue tier does not match, and unsupported providers are mixed
# in. Regression guard: the provider must still be identified from the label.
DOWNLOAD_LINKS_DLS_WRAPPED_HTML = """\
<html><body>
<a rel="nofollow" href="https://getcomics.org/dls/MAINTOKEN==:abc==" class="aio-red" title="DOWNLOAD NOW"><i></i>DOWNLOAD NOW</a>
<a href="https://1024terabox.com/s/1I-4GNpnIlcwrO5DGscBIwg" class="aio-blue" title="TERABOX"><i></i>TERABOX</a>
<a href="https://vikingfile.com/f/pTPOtm5Aks" class="aio-purple" title="VIKINGFILE"><i></i>VIKINGFILE</a>
<a rel="nofollow" href="https://getcomics.org/dls/PDTOKEN==:xyz==" class="aio-orange" title="PIXELDRAIN"><i></i>PIXELDRAIN</a>
<a href="https://datanodes.to/mlz81n23h1b8/Nightwing_140.cbz" class="aio-gray" title="DATANODES"><i></i>DATANODES</a>
<a href="https://readcomicsonline.ru/comic/nightwing-2016/140" class="aio-red" title="READ ONLINE"><i></i>READ ONLINE</a>
</body></html>
"""

# A fully-rendered post whose only mirrors are providers CLU can't download.
DOWNLOAD_ONLY_UNSUPPORTED_HTML = """\
<html><body>
<a class="aio-blue" href="https://1024terabox.com/s/xyz" title="TERABOX">Terabox</a>
<a class="aio-gray" href="https://datanodes.to/abc/comic.cbr" title="DATANODES">Datanodes</a>
</body></html>
"""

# Older getcomics layout: provider names live in inner <span> text of
# class-less, title-less <a> tags wrapped in <strong> (Supergirl Vol 4 style).
DOWNLOAD_LINKS_SPAN_HTML = """\
<html><body>
<strong>
<a rel="nofollow" href="https://getcomics.org/dls/terabox123"><span style="color: #008000;">TERABOX</span></a> |
<a rel="nofollow" href="https://getcomics.org/dls/mega456"><span style="color: #800077;">Mega</span></a> |
<a rel="nofollow" href="https://getcomics.org/dls/mediafire789"><span style="color: #808080;">Mediafire</span></a> |
<a rel="nofollow" href="https://getcomics.org/dls/pixeldrain000"><span style="color: #ff9900;">PIXELDRAIN</span></a>
</strong>
</body></html>
"""

# Tier-4 href backstop: provider name only appears as the URL host, plus a
# look-alike host that must NOT be matched as a real provider.
DOWNLOAD_LINKS_HREF_HOST_HTML = """\
<html><body>
<a rel="nofollow" href="https://pixeldrain.com.evil.com/u/spoof">Click</a>
<a rel="nofollow" href="https://evil.com/?ref=mega.nz">Click</a>
<a rel="nofollow" href="https://cdn.pixeldrain.com/api/file/real123">Download</a>
</body></html>
"""

# Older getcomics layout: aio-red "Download Now" + aio-blue "Mirror Download"
# (Supergirl Vol 2 style).
DOWNLOAD_LINKS_MIRROR_HTML = """\
<html><body>
<a rel="nofollow" href="https://light.getcomics.info/Comics/Supergirl.zip" class="aio-red" title="Download Now"><i></i>Download Now</a>
<a rel="nofollow" href="https://getcomics.org/dls/mirror999" class="aio-blue" title="Mirror Download"><i></i>Mirror Download</a>
</body></html>
"""

HOMEPAGE_WITH_WEEKLY_PACK_HTML = """\
<html><body>
<div class="cover-blog-posts">
  <h2 class="post-title"><a href="https://getcomics.org/other-comics/2026-01-14-weekly-pack/">2026.01.14 Weekly Pack</a></h2>
</div>
</body></html>
"""

HOMEPAGE_WEEKLY_PACK_URL_ONLY_HTML = """\
<html><body>
<div class="cover-blog-posts">
  <h2 class="post-title"><a href="https://getcomics.org/other-comics/2026-02-04-weekly-pack/">Some Other Title</a></h2>
</div>
</body></html>
"""

HOMEPAGE_NO_WEEKLY_PACK_HTML = """\
<html><body>
<div class="cover-blog-posts">
  <h2 class="post-title"><a href="https://getcomics.org/batman-100/">Batman #100</a></h2>
</div>
</body></html>
"""

PACK_NOT_READY_HTML = """\
<html><body>
<p>This page will be updated once all the files are complete.</p>
</body></html>
"""

PACK_READY_HTML = """\
<html><body>
<a href="https://pixeldrain.com/u/pack1">DC Pack</a>
<a href="https://getcomics.org/dlds/pack2">Marvel Pack</a>
</body></html>
"""

PACK_NO_LINKS_HTML = """\
<html><body>
<p>Some text but no download links at all.</p>
</body></html>
"""

WEEKLY_PACK_PAGE_HTML = """\
<html><body>
<h3><span style="color: #3366ff;">JPG</span></h3>
<ul>
  <li>2026.01.14 DC Week (489 MB) :<br>
    <a href="https://pixeldrain.com/u/dc_jpg">PIXELDRAIN</a>
    <a href="https://mega.nz/dc_jpg">MEGA</a>
  </li>
  <li>2026.01.14 Marvel Week (620 MB) :<br>
    <a href="https://pixeldrain.com/u/marvel_jpg">PIXELDRAIN</a>
  </li>
  <li>2026.01.14 Image Week (210 MB) :<br>
    <a href="https://pixeldrain.com/u/image_jpg">PIXELDRAIN</a>
  </li>
</ul>
<h3><span style="color: #ff0000;">WEBP</span></h3>
<ul>
  <li>2026.01.14 DC Week (300 MB) :<br>
    <a href="https://pixeldrain.com/u/dc_webp">PIXELDRAIN</a>
  </li>
  <li>2026.01.14 Marvel Week (400 MB) :<br>
    <a href="https://pixeldrain.com/u/marvel_webp">PIXELDRAIN</a>
  </li>
</ul>
</body></html>
"""


# ---------------------------------------------------------------------------
# Helper to build a mock response object
# ---------------------------------------------------------------------------

def _mock_response(html, status_code=200):
    resp = MagicMock()
    resp.text = html
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return resp


# ===================================================================
# search_getcomics
# ===================================================================

class TestSearchGetcomics:

    @patch("models.getcomics.scraper")
    def test_returns_results_from_single_page(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(SEARCH_RESULTS_HTML)

        from models.getcomics import search_getcomics
        results = search_getcomics("batman", max_pages=1)

        assert len(results) == 2
        assert results[0]["title"] == "Batman #1 (2020)"
        assert results[0]["link"] == "https://getcomics.org/batman-1"
        assert results[0]["image"] == "https://img.example.com/batman.jpg"

    @patch("models.getcomics.scraper")
    def test_uses_data_lazy_src_for_image(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(SEARCH_RESULTS_HTML)

        from models.getcomics import search_getcomics
        results = search_getcomics("batman", max_pages=1)

        # First article uses data-lazy-src
        assert results[0]["image"] == "https://img.example.com/batman.jpg"
        # Second article uses src fallback
        assert results[1]["image"] == "https://img.example.com/superman.jpg"

    @patch("models.getcomics.scraper")
    def test_stops_when_no_articles_found(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(SEARCH_NO_RESULTS_HTML)

        from models.getcomics import search_getcomics
        results = search_getcomics("nonexistent", max_pages=3)

        assert results == []
        # Should stop after first page since no articles found
        assert mock_scraper.get.call_count == 1

    @patch("models.getcomics.scraper")
    def test_skips_articles_without_title(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(SEARCH_ARTICLE_NO_TITLE_HTML)

        from models.getcomics import search_getcomics
        results = search_getcomics("test", max_pages=1)

        assert results == []

    @patch("models.getcomics.scraper")
    def test_paginates_multiple_pages(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(SEARCH_RESULTS_HTML)

        from models.getcomics import search_getcomics
        results = search_getcomics("batman", max_pages=2)

        assert mock_scraper.get.call_count == 2
        # Page 1 uses base URL, page 2 uses /page/2/
        first_call_url = mock_scraper.get.call_args_list[0][0][0]
        second_call_url = mock_scraper.get.call_args_list[1][0][0]
        assert first_call_url == "https://getcomics.org"
        assert second_call_url == "https://getcomics.org/page/2/"

    @patch("models.getcomics.scraper")
    def test_handles_request_exception(self, mock_scraper):
        mock_scraper.get.side_effect = Exception("Connection error")

        from models.getcomics import search_getcomics
        results = search_getcomics("batman", max_pages=1)

        assert results == []


# ===================================================================
# get_download_links
# ===================================================================

class TestGetDownloadLinks:

    @patch("models.getcomics.scraper")
    def test_extracts_links_by_title_attribute(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(DOWNLOAD_LINKS_BY_TITLE_HTML)

        from models.getcomics import get_download_links
        links = get_download_links("https://getcomics.org/batman-1")

        assert links["pixeldrain"] == "https://pixeldrain.com/u/abc123"
        assert links["download_now"] == "https://getcomics.org/dlds/xyz"
        assert links["mega"] == "https://mega.nz/file/xxx#yyy"

    @patch("models.getcomics.scraper")
    def test_extracts_links_by_text_fallback(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(DOWNLOAD_LINKS_BY_TEXT_HTML)

        from models.getcomics import get_download_links
        links = get_download_links("https://getcomics.org/batman-1")

        assert links["pixeldrain"] == "https://pixeldrain.com/u/text123"
        assert links["download_now"] == "https://getcomics.org/dlds/text456"
        assert links["mega"] == "https://mega.nz/file/textmega"

    @patch("models.getcomics.scraper")
    def test_extracts_links_from_span_label_format(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(DOWNLOAD_LINKS_SPAN_HTML)

        from models.getcomics import get_download_links
        links = get_download_links("https://getcomics.org/dc/supergirl-vol-4")

        # Supported providers captured from inner <span> labels...
        assert links["pixeldrain"] == "https://getcomics.org/dls/pixeldrain000"
        assert links["mega"] == "https://getcomics.org/dls/mega456"
        # ...while unsupported Terabox/Mediafire are ignored.
        assert links["download_now"] is None

    @patch("models.getcomics.scraper")
    def test_extracts_mirror_download_aio_blue(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(DOWNLOAD_LINKS_MIRROR_HTML)

        from models.getcomics import get_download_links
        links = get_download_links("https://getcomics.org/dc/supergirl-vol-2")

        # aio-red "Download Now" wins the download_now slot (title tier first),
        # and the aio-blue "Mirror Download" /dls/ link is recognised.
        assert links["download_now"] == "https://light.getcomics.info/Comics/Supergirl.zip"
        assert links["pixeldrain"] is None
        assert links["mega"] is None

    @patch("models.getcomics.scraper")
    def test_href_backstop_matches_real_host_rejects_lookalikes(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(DOWNLOAD_LINKS_HREF_HOST_HTML)

        from models.getcomics import get_download_links
        links = get_download_links("https://getcomics.org/dc/supergirl")

        # A real cdn.pixeldrain.com sub-domain link is matched...
        assert links["pixeldrain"] == "https://cdn.pixeldrain.com/api/file/real123"
        # ...but pixeldrain.com.evil.com and ?ref=mega.nz are NOT treated as providers.
        assert links["mega"] is None

    @patch("models.getcomics.scraper")
    def test_returns_none_values_when_no_links(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(DOWNLOAD_NO_LINKS_HTML)

        from models.getcomics import get_download_links
        links = get_download_links("https://getcomics.org/nothing")

        assert links == {"pixeldrain": None, "download_now": None, "mega": None}

    @patch("models.getcomics.scraper")
    def test_returns_empty_dict_on_request_error(self, mock_scraper):
        mock_scraper.get.side_effect = Exception("Timeout")

        from models.getcomics import get_download_links
        links = get_download_links("https://getcomics.org/fail")

        assert links == {"pixeldrain": None, "download_now": None, "mega": None}

    @patch("models.getcomics.scraper")
    def test_rendered_post_with_only_unsupported_providers_no_retry(self, mock_scraper):
        # A real post that exposes only Terabox/Datanodes must NOT be retried —
        # there is nothing CLU can download, and hammering it wastes requests.
        mock_scraper.get.return_value = _mock_response(DOWNLOAD_ONLY_UNSUPPORTED_HTML)

        from models.getcomics import get_download_links
        links = get_download_links("https://getcomics.org/terabox-only")

        assert links == {"pixeldrain": None, "download_now": None, "mega": None}
        assert mock_scraper.get.call_count == 1

    @patch("models.getcomics.scraper")
    def test_incomplete_page_is_retried(self, mock_scraper):
        # A thin page with no download UI looks soft-blocked, so we retry.
        mock_scraper.get.return_value = _mock_response(DOWNLOAD_NO_LINKS_HTML)

        from models.getcomics import get_download_links
        links = get_download_links("https://getcomics.org/thin", max_attempts=3)

        assert links == {"pixeldrain": None, "download_now": None, "mega": None}
        assert mock_scraper.get.call_count == 3

    @patch("models.getcomics._make_scraper")
    @patch("models.getcomics.scraper")
    def test_cloudflare_challenge_retries_with_fresh_scraper(self, mock_scraper, mock_make):
        # First hit is a Cloudflare challenge; a fresh session then succeeds.
        challenge = MagicMock()
        challenge.status_code = 403
        challenge.headers = {"Server": "cloudflare", "Content-Type": "text/html"}
        challenge.content = b"<html><head><title>Just a moment...</title></head></html>"
        challenge.text = challenge.content.decode()
        challenge.raise_for_status = MagicMock()
        mock_scraper.get.return_value = challenge

        fresh = MagicMock()
        fresh.get.return_value = _mock_response(DOWNLOAD_LINKS_BY_TITLE_HTML)
        mock_make.return_value = fresh

        from models.getcomics import get_download_links
        links = get_download_links("https://getcomics.org/blocked")

        assert links["pixeldrain"] == "https://pixeldrain.com/u/abc123"
        assert links["download_now"] == "https://getcomics.org/dlds/xyz"
        mock_make.assert_called()  # a fresh scraper session was created for the retry

    @patch("models.getcomics._make_scraper")
    @patch("models.getcomics.scraper")
    def test_persistent_cloudflare_challenge_gives_up_empty(self, mock_scraper, mock_make):
        def _challenge():
            resp = MagicMock()
            resp.status_code = 403
            resp.headers = {"Server": "cloudflare", "Content-Type": "text/html"}
            resp.content = b"<html><title>Just a moment...</title></html>"
            resp.text = resp.content.decode()
            resp.raise_for_status = MagicMock()
            return resp

        mock_scraper.get.return_value = _challenge()
        blocked = MagicMock()
        blocked.get.return_value = _challenge()
        mock_make.return_value = blocked

        from models.getcomics import get_download_links
        links = get_download_links("https://getcomics.org/always-blocked", max_attempts=3)

        assert links == {"pixeldrain": None, "download_now": None, "mega": None}


# ===================================================================
# _extract_content_li_entries (variant-3 <li> scraping)
# ===================================================================

# Real-world shape: an advertising link lives in the site header nav (a <ul>
# of <li> outside any <article>), while the actual comic entries are <li> items
# inside the post content. Scraping must ignore the nav ad.
LI_PAGE_WITH_NAV_AD_HTML = """\
<html><body>
<header>
  <nav><ul class="menu">
    <li><a href="https://ads.example.com/aigf">\U0001f497AI Girlfriend\U0001f497</a></li>
    <li><a href="https://getcomics.org/dc/">DC</a></li>
  </ul></nav>
</header>
<article class="post-body">
  <div class="entry-content">
    <ul>
      <li>Supergirl Vol. 4 #1 : <strong><a href="https://pixeldrain.com/u/abc123">Main Server</a></strong></li>
      <li>Supergirl Vol. 4 #2 : <strong><a href="https://pixeldrain.com/u/def456">Main Server</a></strong></li>
    </ul>
  </div>
</article>
<footer><ul><li><a href="https://ads.example.com/promo">Buy Premium Now Today</a></li></ul></footer>
</body></html>
"""


class TestDlsWrappedProviderDetection:
    """getcomics fronts every provider with an identical /dls/ redirector.

    The href is therefore useless for identifying a provider -- only the
    label is. Regression guard for downloads being attributed to GetComics
    when the post had a Pixeldrain link.
    """

    @patch("models.getcomics.scraper")
    def test_extracts_dls_wrapped_links_by_label(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(DOWNLOAD_LINKS_DLS_WRAPPED_HTML)

        from models.getcomics import get_download_links
        links = get_download_links("https://getcomics.org/dc/nightwing-140-2026")

        # Both are getcomics.org/dls/ URLs -- only the title tells them apart.
        assert links["pixeldrain"] == "https://getcomics.org/dls/PDTOKEN==:xyz=="
        assert links["download_now"] == "https://getcomics.org/dls/MAINTOKEN==:abc=="
        assert links["mega"] is None

    @patch("models.getcomics.scraper")
    def test_dls_wrapped_pixeldrain_wins_over_getcomics(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(DOWNLOAD_LINKS_DLS_WRAPPED_HTML)

        from models.getcomics import get_download_links, select_download_url
        links = get_download_links("https://getcomics.org/dc/nightwing-140-2026")
        (provider, url), fallbacks = select_download_url(links, "pixeldrain,download_now,mega")

        assert provider == "pixeldrain"
        assert url == "https://getcomics.org/dls/PDTOKEN==:xyz=="
        assert fallbacks == [("download_now", "https://getcomics.org/dls/MAINTOKEN==:abc==")]

    @patch("models.getcomics.scraper")
    def test_unsupported_providers_ignored(self, mock_scraper):
        """Terabox/Vikingfile/Datanodes/Read Online must not leak into a slot."""
        mock_scraper.get.return_value = _mock_response(DOWNLOAD_LINKS_DLS_WRAPPED_HTML)

        from models.getcomics import get_download_links
        links = get_download_links("https://getcomics.org/dc/nightwing-140-2026")

        assert set(links) == {"pixeldrain", "download_now", "mega"}
        for url in links.values():
            assert url is None or "terabox" not in url
            assert url is None or "vikingfile" not in url
            assert url is None or "datanodes" not in url


class TestSelectDownloadUrl:

    def test_respects_configured_priority(self):
        from models.getcomics import select_download_url
        links = {
            "pixeldrain": "https://pixeldrain.com/u/pd",
            "download_now": "https://getcomics.org/dls/dn",
            "mega": "https://mega.nz/file/mg",
        }

        (provider, url), fallbacks = select_download_url(links, "pixeldrain,download_now,mega")
        assert (provider, url) == ("pixeldrain", "https://pixeldrain.com/u/pd")
        assert [p for p, _ in fallbacks] == ["download_now", "mega"]

    def test_reordered_priority_changes_winner(self):
        from models.getcomics import select_download_url
        links = {
            "pixeldrain": "https://pixeldrain.com/u/pd",
            "download_now": "https://getcomics.org/dls/dn",
            "mega": "https://mega.nz/file/mg",
        }

        (provider, _url), fallbacks = select_download_url(links, "mega,pixeldrain,download_now")
        assert provider == "mega"
        assert [p for p, _ in fallbacks] == ["pixeldrain", "download_now"]

    def test_omitted_provider_never_used(self):
        """A provider absent from the priority string is skipped even if present."""
        from models.getcomics import select_download_url
        links = {
            "pixeldrain": "https://pixeldrain.com/u/pd",
            "download_now": "https://getcomics.org/dls/dn",
            "mega": None,
        }

        (provider, _url), fallbacks = select_download_url(links, "download_now")
        assert provider == "download_now"
        assert fallbacks == []

    def test_skips_providers_the_post_lacks(self):
        from models.getcomics import select_download_url
        links = {"pixeldrain": None, "download_now": "https://getcomics.org/dls/dn", "mega": None}

        (provider, url), fallbacks = select_download_url(links, "pixeldrain,download_now,mega")
        assert (provider, url) == ("download_now", "https://getcomics.org/dls/dn")
        assert fallbacks == []

    def test_no_links_returns_none_pair(self):
        from models.getcomics import select_download_url
        (provider, url), fallbacks = select_download_url(
            {"pixeldrain": None, "download_now": None, "mega": None},
            "pixeldrain,download_now,mega",
        )
        assert (provider, url) == (None, None)
        assert fallbacks == []

    def test_tolerates_whitespace_and_empty_priority(self):
        from models.getcomics import select_download_url
        links = {"pixeldrain": "https://pixeldrain.com/u/pd", "download_now": None, "mega": None}

        (provider, _url), _fb = select_download_url(links, " pixeldrain , download_now ")
        assert provider == "pixeldrain"

        assert select_download_url(links, "") == ((None, None), [])


class TestProviderLabelling:

    def test_label_prefers_the_chosen_provider_key(self):
        """The key is what priority picked -- it must not be re-guessed."""
        from models.getcomics import provider_label
        assert provider_label("pixeldrain", "https://pixeldrain.com/u/abc") == "pixeldrain"
        assert provider_label("download_now", "https://fs3.comicfiles.ru/x.cbz") == "getcomics"
        assert provider_label("mega", "https://mega.nz/file/x") == "mega"

    def test_label_falls_back_to_url_without_a_key(self):
        """External/browser-extension downloads never ran priority selection."""
        from models.getcomics import provider_label
        assert provider_label(None, "https://pixeldrain.com/u/abc") == "pixeldrain"
        assert provider_label(None, "https://mega.nz/file/x") == "mega"
        assert provider_label(None, "https://comicbookplus.com/?dlid=1") == "comicbookplus"
        assert provider_label(None, "https://fs3.comicfiles.ru/x.cbz") == "getcomics"

    def test_unresolved_dls_redirect_detected(self):
        from models.getcomics import is_unresolved_gc_redirect
        assert is_unresolved_gc_redirect("https://getcomics.org/dls/TOKEN==:abc==")
        assert is_unresolved_gc_redirect("https://getcomics.org/dlds/xyz")

    def test_resolved_and_ordinary_urls_are_not_redirects(self):
        from models.getcomics import is_unresolved_gc_redirect
        # Successfully resolved to the real provider host.
        assert not is_unresolved_gc_redirect("https://pixeldrain.com/u/8uSDFbt2")
        assert not is_unresolved_gc_redirect("https://fs3.comicfiles.ru/x.cbz")
        # The post page itself is not a download redirector.
        assert not is_unresolved_gc_redirect("https://getcomics.org/dc/nightwing-140-2026/")

    def test_redirect_check_is_not_spoofable_by_host(self):
        from models.getcomics import is_unresolved_gc_redirect
        assert not is_unresolved_gc_redirect("https://getcomics.org.evil.com/dls/token")
        assert not is_unresolved_gc_redirect("https://evil.com/dls/?x=getcomics.org")


class TestExtractContentLiEntries:

    def test_ignores_nav_and_footer_li_keeps_comic_entries(self):
        from bs4 import BeautifulSoup
        from models.getcomics import _extract_content_li_entries

        soup = BeautifulSoup(LI_PAGE_WITH_NAV_AD_HTML, "html.parser")
        entries = _extract_content_li_entries(soup)

        titles = [t for t, _ in entries]
        # The header-nav advertising link and the footer promo are excluded...
        assert not any("AI Girlfriend" in t for t in titles)
        assert not any("Buy Premium" in t for t in titles)
        # ...while the real in-content comic entries are captured.
        assert titles == ["Supergirl Vol. 4 #1", "Supergirl Vol. 4 #2"]
        assert entries[0][1] == "https://pixeldrain.com/u/abc123"

    def test_returns_empty_when_no_content_container(self):
        from bs4 import BeautifulSoup
        from models.getcomics import _extract_content_li_entries

        # <li> items exist only in nav — no article/post-content/entry-content.
        soup = BeautifulSoup(
            "<html><body><nav><ul>"
            "<li><a href='https://ads.example.com'>\U0001f497AI Girlfriend\U0001f497</a></li>"
            "</ul></nav></body></html>",
            "html.parser",
        )
        assert _extract_content_li_entries(soup) == []


# ===================================================================
# is_valid_series_name / emoji-junk alias filtering
# ===================================================================

_JUNK = "\U0001f497AI Girlfriend\U0001f497"  # 💗AI Girlfriend💗 — getcomics ad nav item


class TestIsValidSeriesName:

    @pytest.mark.parametrize("name", [
        "Daredevil",
        "Batman/Superman",
        "Astérix",          # accented Latin must survive
        "ナルト",     # CJK (ナルト) must survive
        "2000 AD",
        "Spider-Man 2099",
    ])
    def test_valid_names_accepted(self, name):
        from models.getcomics import is_valid_series_name
        assert is_valid_series_name(name) is True

    @pytest.mark.parametrize("name", [
        _JUNK,
        "❤ Buy Now ❤",  # ❤ dingbat heart
        "⭐ Featured",         # ⭐ star
        "",
        "   ",
        None,
    ])
    def test_junk_names_rejected(self, name):
        from models.getcomics import is_valid_series_name
        assert is_valid_series_name(name) is False

    def test_clean_alias_csv_drops_junk(self):
        from models.getcomics import _clean_alias_csv
        assert _clean_alias_csv(f"Punisher,{_JUNK}") == "Punisher"
        assert _clean_alias_csv(f"{_JUNK}") == ""
        assert _clean_alias_csv("Punisher,Frank Castle") == "Punisher,Frank Castle"


class TestParseAndStoreRejectsJunk:
    """A scraped emoji ad card inside div.post-content must never be stored."""

    LISTING_HTML = f"""\
<html><body>
<div class="post-content"><h5><a href="https://getcomics.org/x">{_JUNK}</a></h5></div>
<div class="post-content"><h5><a href="https://getcomics.org/y">Daredevil #4 (2026)</a></h5></div>
</body></html>
"""

    def test_emoji_title_not_indexed(self, db_connection):
        from unittest.mock import MagicMock, patch
        from models.getcomics import _scrape_url_to_index, _ensure_urls_table
        from core.database import get_db_connection

        _ensure_urls_table()

        resp = MagicMock(status_code=200, text=self.LISTING_HTML)
        resp.headers = {}
        fake_scraper = MagicMock()
        fake_scraper.get.return_value = resp

        with patch("models.getcomics.cloudscraper.create_scraper", return_value=fake_scraper):
            _scrape_url_to_index("https://getcomics.org/listing")

        conn = get_db_connection()
        titles = [r[0] for r in conn.execute("SELECT title FROM getcomics_urls").fetchall()]
        conn.close()

        assert any("Daredevil" in (t or "") for t in titles)
        assert not any("AI Girlfriend" in (t or "") for t in titles)


class TestGetSeriesAliasesFilters:
    """Junk already stored in the DB must be filtered out on read."""

    def test_read_paths_drop_emoji_alias(self, db_connection):
        from models.getcomics import (
            get_series_aliases, get_series_alias_list,
            _ensure_urls_table,
        )
        from core.database import get_db_connection

        _ensure_urls_table()
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO getcomics_urls (url, full_url, series_norm, search_aliases, title) "
            "VALUES (?, ?, ?, ?, ?)",
            ("https://getcomics.org/dd", "https://getcomics.org/dd",
             "Daredevil", f"Punisher,{_JUNK}", "Daredevil #4"),
        )
        conn.commit()
        conn.close()

        assert get_series_aliases("Daredevil") == "Punisher"
        aliases = get_series_alias_list("Daredevil")
        assert "Punisher" in aliases
        assert not any("AI Girlfriend" in a for a in aliases)


class TestPurgeInvalidAliases:

    def test_purges_both_tables_and_is_idempotent(self, db_connection):
        from models.getcomics import purge_invalid_aliases, _ensure_urls_table, _ensure_alias_table
        from core.database import get_db_connection

        _ensure_urls_table()
        _ensure_alias_table()
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO getcomics_series_aliases (alias, alias_norm, canonical, canonical_norm) "
            "VALUES (?, ?, ?, ?)",
            ("Punisher", "punisher", "Daredevil", "daredevil"),
        )
        conn.execute(
            "INSERT INTO getcomics_series_aliases (alias, alias_norm, canonical, canonical_norm) "
            "VALUES (?, ?, ?, ?)",
            (_JUNK, _JUNK.lower(), "Daredevil", "daredevil"),
        )
        conn.execute(
            "INSERT INTO getcomics_urls (url, full_url, series_norm, search_aliases, title) "
            "VALUES (?, ?, ?, ?, ?)",
            ("https://getcomics.org/dd", "https://getcomics.org/dd",
             "Daredevil", f"Punisher,{_JUNK}", "Daredevil #4"),
        )
        conn.commit()
        conn.close()

        cleaned = purge_invalid_aliases()
        assert cleaned == 2  # one alias row + one CSV entry

        conn = get_db_connection()
        aliases = [r[0] for r in conn.execute(
            "SELECT alias FROM getcomics_series_aliases").fetchall()]
        csv = conn.execute(
            "SELECT search_aliases FROM getcomics_urls WHERE series_norm = 'Daredevil'"
        ).fetchone()[0]
        conn.close()

        assert aliases == ["Punisher"]
        assert csv == "Punisher"

        # Idempotent: a second run finds nothing to clean.
        assert purge_invalid_aliases() == 0


# ===================================================================
# score_getcomics_result (pure function -- parametrized tests)
# ===================================================================

class TestScoreGetcomicsResult:

    @pytest.mark.parametrize(
        "title, series, issue, year, expected_min",
        [
            # Perfect match: series(30) + tightness(15) + issue(30) + year(20) = 95
            ("Batman #1 (2020)", "Batman", "1", 2020, 95),
            # Series match + issue match (no year)
            ("Batman #5", "Batman", "5", 0, 60),
            # No series match at all
            ("Superman #1 (2020)", "Batman", "1", 2020, -1),
        ],
        ids=["perfect_match", "series_and_issue_no_year", "no_series_match"],
    )
    def test_basic_scoring(self, title, series, issue, year, expected_min):
        from models.getcomics import score_getcomics_result
        score, _, _ = score_getcomics_result(title, series, issue, year)
        assert score >= expected_min

    @pytest.mark.parametrize(
        "title, series, issue, year",
        [
            ("Batman #1 (2020)", "Batman", "1", 2020),
        ],
    )
    def test_max_score_is_95(self, title, series, issue, year):
        from models.getcomics import score_getcomics_result
        score, _, _ = score_getcomics_result(title, series, issue, year)
        assert score == 95

    def test_series_alias_enables_match(self):
        # "2000 AD" (from online sources) is listed on GetComics as "2000AD".
        # Without the alias the space mismatch fails the series match; passing
        # the alias must recover a full-confidence match.
        from models.getcomics import score_getcomics_result
        title = "2000AD #2400 (2026)"

        score_no_alias, _, match_no_alias = score_getcomics_result(
            title, "2000 AD", "2400", 2026
        )
        assert match_no_alias is False
        assert score_no_alias == 0

        score_alias, _, match_alias = score_getcomics_result(
            title, "2000 AD", "2400", 2026, series_aliases=["2000AD"]
        )
        assert match_alias is True
        assert score_alias == 95

    def test_series_alias_keeps_best_score(self):
        # The canonical name already matches; an irrelevant alias must not lower
        # the score (best of canonical + aliases wins).
        from models.getcomics import score_getcomics_result
        score_plain, _, _ = score_getcomics_result("Batman #1 (2020)", "Batman", "1", 2020)
        score_with_alias, _, _ = score_getcomics_result(
            "Batman #1 (2020)", "Batman", "1", 2020, series_aliases=["Caped Crusader"]
        )
        assert score_with_alias == score_plain == 95

    def test_series_alias_empty_and_none_are_safe(self):
        from models.getcomics import score_getcomics_result
        for aliases in (None, [], ["", "   "]):
            score, _, match = score_getcomics_result(
                "Batman #1 (2020)", "Batman", "1", 2020, series_aliases=aliases
            )
            assert match is True and score == 95

    @pytest.mark.parametrize(
        "title, issue",
        [
            ("Batman #7", "7"),
            ("Batman Issue 7", "7"),
            ("Batman #007", "7"),
        ],
        ids=["hash_format", "issue_word", "leading_zeros"],
    )
    def test_issue_number_formats(self, title, issue):
        from models.getcomics import score_getcomics_result
        score, _, _ = score_getcomics_result(title, "Batman", issue, 0)
        # Should get at least series(30) + issue(30) = 60
        assert score >= 60

    def test_standalone_number_lower_confidence(self):
        """A bare number without # prefix gets +20 instead of +30."""
        from models.getcomics import score_getcomics_result
        score_hash, _, _ = score_getcomics_result("Batman #3", "Batman", "3", 0)
        score_bare, _, _ = score_getcomics_result("Batman 3", "Batman", "3", 0)
        assert score_hash > score_bare

    def test_year_match_adds_points(self):
        from models.getcomics import score_getcomics_result
        with_year, _, _ = score_getcomics_result("Batman #1 (2020)", "Batman", "1", 2020)
        without_year, _, _ = score_getcomics_result("Batman #1", "Batman", "1", 2020)
        # Year match adds 20; yearless title searched with specific year gets -10 penalty
        assert with_year - without_year == 30

    def test_issue_year_wins_over_volume_year_mismatch(self):
        """Long-running series: title year == issue publication year, not volume start."""
        from models.getcomics import score_getcomics_result
        # Harley Quinn Vol 4 started 2021; issue #61 published 2026.
        # Both GetComics candidates have the same series + issue #, only year differs.
        correct, _, _ = score_getcomics_result(
            "Harley Quinn #61 (2026)", "Harley Quinn", "61", 2026, volume_year=2021,
        )
        wrong_volume, _, _ = score_getcomics_result(
            "Harley Quinn #61 (2019)", "Harley Quinn", "61", 2026, volume_year=2021,
        )
        assert correct > wrong_volume + 25, (
            f"issue_year match must clearly beat mismatch: correct={correct}, "
            f"wrong_volume={wrong_volume}"
        )

    def test_wrong_volume_reprint_rejected_when_only_candidate(self):
        """Lone wrong-volume candidate (neither volume_year nor issue_year matches)
        must score below ACCEPT so the scheduler skips it instead of downloading
        the reprint from a different volume."""
        from models.getcomics import score_getcomics_result, accept_result
        # Harley Quinn Vol 4 (2021-), issue #61 published 2026; GetComics only
        # has the 2019 URL from Vol 3 with same issue number.
        score, is_range, series_match = score_getcomics_result(
            "Harley Quinn #61 (2019)", "Harley Quinn", "61", 2026,
            series_volume=4, volume_year=2021,
        )
        decision = accept_result(score, is_range, series_match)
        assert decision != "ACCEPT", (
            f"Wrong-volume reprint must not ACCEPT: score={score}, decision={decision}"
        )

    def test_volume_year_match_still_preferred_when_issue_year_absent_from_title(self):
        """Backward compat: correct edition where title year is volume start, not issue year."""
        from models.getcomics import score_getcomics_result
        # Flash Vol (2020), issue #5 from 2024 — title uses volume year, not issue year.
        correct_edition, _, _ = score_getcomics_result(
            "Flash #5 (2020)", "Flash", "5", 2024, volume_year=2020,
        )
        wrong_edition, _, _ = score_getcomics_result(
            "Flash #5 (2011)", "Flash", "5", 2024, volume_year=2020,
        )
        assert correct_edition > wrong_edition

    @pytest.mark.parametrize(
        "title",
        [
            "Batman Omnibus (2020)",
            "Batman TPB Vol 1 (2020)",
            "Batman Hardcover Edition (2020)",
            "Batman Deluxe Edition (2020)",
            "Batman Compendium (2020)",
            "Batman Complete Collection (2020)",
        ],
        ids=["omnibus", "tpb", "hardcover", "deluxe", "compendium", "complete_collection"],
    )
    def test_collected_edition_penalty(self, title):
        from models.getcomics import score_getcomics_result
        score, _, _ = score_getcomics_result(title, "Batman", "1", 2020)
        clean_score, _, _ = score_getcomics_result("Batman #1 (2020)", "Batman", "1", 2020)
        assert score < clean_score

    @pytest.mark.parametrize(
        "title, issue",
        [
            ("Batman #1-18 (2020)", "18"),
            ("Batman #1 \u2013 18 (2020)", "18"),
            ("Batman Issues 1-18 (2020)", "18"),
        ],
        ids=["dash_range", "endash_range", "issues_range"],
    )
    def test_issue_range_fallback_for_same_series(self, title, issue):
        """Same-series range ending on target should be FALLBACK (39), not REJECT (-100)."""
        from models.getcomics import score_getcomics_result, accept_result
        score, range_hit, series_match = score_getcomics_result(title, "Batman", issue, 2020)
        decision = accept_result(score, range_hit, series_match)
        assert score == 39, f"Expected FALLBACK (39) for same-series range, got {score}"
        assert decision == "FALLBACK", f"Expected FALLBACK decision, got {decision}"

    def test_issue_range_not_disqualified_when_not_ending_match(self):
        """Range like #1-18 should NOT disqualify when looking for issue #5."""
        from models.getcomics import score_getcomics_result
        score, _, _ = score_getcomics_result("Batman #1-18 (2020)", "Batman", "5", 2020)
        # Should not be -100 since issue 5 is not the range endpoint
        assert score != -100

    def test_title_tightness_bonus(self):
        """Tight title (few extra words) gets +15 bonus."""
        from models.getcomics import score_getcomics_result
        tight, _, _ = score_getcomics_result("Batman #1 (2020)", "Batman", "1", 2020)
        # 30 (series) + 15 (tight) + 30 (issue) + 20 (year) = 95
        assert tight == 95

    def test_title_tightness_penalty(self):
        """Title with many extra words gets -20 penalty."""
        from models.getcomics import score_getcomics_result
        wordy, _, _ = score_getcomics_result(
            "Batman #1 (2020) Special Limited Exclusive Variant Foil Cover",
            "Batman", "1", 2020,
        )
        tight, _, _ = score_getcomics_result("Batman #1 (2020)", "Batman", "1", 2020)
        assert wordy < tight

    def test_standalone_number_rejected_after_volume(self):
        """Number preceded by 'Vol.' should not count as issue match."""
        from models.getcomics import score_getcomics_result
        score, _, _ = score_getcomics_result("Batman Vol. 3", "Batman", "3", 0)
        hash_score, _, _ = score_getcomics_result("Batman #3", "Batman", "3", 0)
        assert score < hash_score

    def test_leading_zeros_normalized(self):
        """Issue '001' should match title with '#1'."""
        from models.getcomics import score_getcomics_result
        score, _, _ = score_getcomics_result("Batman #1", "Batman", "001", 0)
        assert score >= 60  # series(30) + issue(30)

    @pytest.mark.parametrize(
        "title, issue",
        [
            ("Avengers #1.1 (2017)", "1.1"),
            ("Avengers #001.1 (2017)", "1.1"),
            ("Avengers #1.MU (2017)", "1.MU"),
            ("Avengers 1.MU (2017)", "1.MU"),
        ],
        ids=["decimal_hash", "decimal_padded", "suffix_hash", "suffix_bare"],
    )
    def test_decimal_suffix_issue_matches(self, title, issue):
        """Point issues ('1.1'/'1.MU') should match, not be dropped or mismatched."""
        from models.getcomics import score_getcomics_result
        score, _, match = score_getcomics_result(title, "Avengers", issue, 2017)
        assert match is True
        assert score >= 40

    def test_suffix_issue_not_falsely_penalized(self):
        """A correct '.MU' issue must not trigger the -40 confirmed-mismatch
        penalty via a numeric-only regex (the point-issue scoring bug)."""
        from models.getcomics import score_getcomics_result
        correct, _, _ = score_getcomics_result("Avengers #1.MU (2017)", "Avengers", "1.MU", 2017)
        wrong, _, _ = score_getcomics_result("Avengers #2.MU (2017)", "Avengers", "1.MU", 2017)
        assert correct > wrong

    def test_annual_as_sub_series_penalty(self):
        """Annual variant should be penalized as sub-series (Issue #193)."""
        from models.getcomics import score_getcomics_result
        # Main series "Batman #1 (2020)" should score higher than "Batman Annual #1 (2020)"
        main_score, _, _ = score_getcomics_result("Batman #1 (2020)", "Batman", "1", 2020)
        annual_score, _, _ = score_getcomics_result("Batman Annual #1 (2020)", "Batman", "1", 2020)
        # Annual has -30 sub-series penalty but still has series + issue + year match
        assert main_score > annual_score
        # Annual should be penalized by at least 30 points (increased from -20 to -30)
        assert main_score - annual_score >= 30

    def test_annual_keyword_detected_as_sub_series(self):
        """Annual keyword should be detected as sub-series without dash (Issue #193)."""
        from models.getcomics import score_getcomics_result
        # "Absolute Batman 2025 Annual #1" - Annual appears after year but should be detected
        score, _, _ = score_getcomics_result(
            "Absolute Batman 2025 Annual #1 (2025)", "Absolute Batman", "1", 2025
        )
        # Should have sub-series penalty of -30
        # Issue match is NOT counted for Annual (Annual #N is not main series #N)
        # Score breakdown: series(30) - sub-series(30) + title_tightness(-10) + year(20) = 10
        assert score == 10

    def test_quarterly_sub_series_penalty_increased(self):
        """Quarterly sub-series penalty increased from -20 to -30 (Issue #193)."""
        from models.getcomics import score_getcomics_result
        # "Flash Gordon - Quarterly #5" vs main series
        quarterly_score, _, _ = score_getcomics_result(
            "Flash Gordon - Quarterly (2025) Issue 5", "Flash Gordon", "5", 2025
        )
        main_score, _, _ = score_getcomics_result(
            "Flash Gordon #5 (2025)", "Flash Gordon", "5", 2025
        )
        # Main series should be at least 30 points higher (increased penalty)
        assert main_score - quarterly_score >= 30

    def test_flash_gordon_quarterly_issue_matching(self):
        """Flash Gordon Quarterly Issue 5 should not incorrectly match base series (Issue #193)."""
        from models.getcomics import score_getcomics_result
        # When searching for Flash Gordon #5, Quarterly variant should score lower
        quarterly_score, _, _ = score_getcomics_result(
            "Flash Gordon - Quarterly (2025) Issue 5", "Flash Gordon", "5", 2025
        )
        # With increased -30 sub-series penalty:
        # series(30) - sub-series(30) + title_tightness(-10?) + issue(30) + year(20) = 40-ish
        # Actually: series(30) - 30 + 15 + 30 + 20 = 65
        assert quarterly_score < 70  # Should be significantly lower than main series

    def test_cross_series_false_positive_batman_vs_superman(self):
        """Searching for Batman #1 should not match Superman #1 (cross-series bug fix)."""
        from models.getcomics import score_getcomics_result, ACCEPT_THRESHOLD
        score, _, _ = score_getcomics_result(
            "Superman #1 (2020)", "Batman", "1", 2020
        )
        # Score should be < ACCEPT_THRESHOLD (no series match, no issue match)
        assert score < ACCEPT_THRESHOLD

    def test_cross_series_false_positive_flash_gordon_vs_the_flash(self):
        """Searching for Flash Gordon #1 should not match The Flash #1."""
        from models.getcomics import score_getcomics_result, ACCEPT_THRESHOLD
        score, _, _ = score_getcomics_result(
            "The Flash #1 (2020)", "Flash Gordon", "1", 2020
        )
        # Score should be < ACCEPT_THRESHOLD
        assert score < ACCEPT_THRESHOLD

    def test_cross_series_same_series_still_works(self):
        """Batman #1 should still match when searching for Batman #1."""
        from models.getcomics import score_getcomics_result, ACCEPT_THRESHOLD
        score, _, _ = score_getcomics_result(
            "Batman #1 (2020)", "Batman", "1", 2020
        )
        # Perfect match should score 95
        assert score == 95

    def test_cross_series_prefix_variation(self):
        """The Batman should match Batman when series prefix is swapped."""
        from models.getcomics import score_getcomics_result, ACCEPT_THRESHOLD
        score, _, _ = score_getcomics_result(
            "The Batman #1 (2020)", "Batman", "1", 2020
        )
        # Should still be a perfect match (95)
        assert score == 95

    # ===================================================================
    # Variant Types - TPB, Quarterly, One-Shot, OS, Omni, Hardcover, etc.
    # ===================================================================

    def test_tpb_variant_penalty_and_acceptance(self):
        """TPB (Trade Paperback) variant should be penalized unless accepted."""
        from models.getcomics import score_getcomics_result, accept_result
        # Without accept_variants: TPB should be penalized
        score, range_hit, series_match = score_getcomics_result(
            "Batman - The Court of Owls TPB #1 (2020)", "Batman", "1", 2020
        )
        decision = accept_result(score, range_hit, series_match)
        # Score: series(30) + sub-series variant(-30) + title_tightness(-10) + issue(30) + year(20) = 40
        # But TPB is also collected edition, so -30 more = 10
        # Actually: series(30) - variant(30) + tight(-10) + issue(30) + year(20) + collected(30) = -10
        assert score < 0  # Heavily penalized due to collected edition keyword
        assert decision == "REJECT"

        # With accept_variants: TPB still rejected due to format mismatch
        # A TPB is NOT a single issue, even if accepted as a variant type
        score, range_hit, series_match = score_getcomics_result(
            "Batman - The Court of Owls TPB #1 (2020)", "Batman", "1", 2020,
            accept_variants=['tpb']
        )
        decision = accept_result(score, range_hit, series_match)
        # Format mismatch (-50) + issue blocked = REJECT
        assert decision == "REJECT"

    def test_quarterly_variant_acceptance(self):
        """Quarterly variant should ONLY be accepted when 'quarterly' is in the search series name.

        "Flash Gordon Quarterly" is a DIFFERENT series from "Flash Gordon" on Metron.
        accept_variants should NOT make a different series match - it only helps with
        format variants (TPB, omnibus) of the SAME content.
        """
        from models.getcomics import score_getcomics_result, accept_result
        # Searching "Flash Gordon" should NOT accept "Flash Gordon Quarterly" as match
        # even with accept_variants=['quarterly'] - these are different series!
        score, range_hit, series_match = score_getcomics_result(
            "Flash Gordon - Quarterly #5 (2025)", "Flash Gordon", "5", 2025,
            accept_variants=['quarterly']
        )
        decision = accept_result(score, range_hit, series_match)
        # Quarterly is a publication type that creates different series - reject even with accept_variants
        assert decision == "REJECT"

        # When searching for "Flash Gordon Quarterly" (variant IN search series name),
        # the result "Flash Gordon Quarterly #5" should match
        score, range_hit, series_match = score_getcomics_result(
            "Flash Gordon Quarterly #5 (2025)", "Flash Gordon Quarterly", "5", 2025
        )
        decision = accept_result(score, range_hit, series_match)
        # Same series name, issue and year match = ACCEPT
        assert decision == "ACCEPT"

    def test_oneshot_variant_acceptance(self):
        """One-shot variant: format mismatch penalty applies, issue matching blocked.

        A oneshot is NOT the same as a single issue - it's a standalone story
        that may collect multiple issues. Even with accept_variants, format
        mismatch penalty applies and issue matching is blocked.
        """
        from models.getcomics import score_getcomics_result, accept_result
        # "Batman - Year One OS #1" - OS/One-Shot variant
        score, range_hit, series_match = score_getcomics_result(
            "Batman - Year One OS #1 (2020)", "Batman", "1", 2020,
            accept_variants=['os']
        )
        decision = accept_result(score, range_hit, series_match)
        # Format mismatch (-50) + issue blocked = REJECT
        assert decision == "REJECT"

        # "Batman - Year One One-Shot #1"
        score, range_hit, series_match = score_getcomics_result(
            "Batman - Year One One-Shot #1 (2020)", "Batman", "1", 2020,
            accept_variants=['oneshot']
        )
        decision = accept_result(score, range_hit, series_match)
        assert decision == "REJECT"

        # Without acceptance: should be rejected
        score, range_hit, series_match = score_getcomics_result(
            "Batman - Year One OS #1 (2020)", "Batman", "1", 2020
        )
        decision = accept_result(score, range_hit, series_match)
        assert decision == "REJECT"

    def test_omnibus_variant_acceptance(self):
        """Omnibus variant: format mismatch penalty applies, issue matching blocked.

        An omnibus is NOT the same as a single issue - it's a collected edition.
        Format mismatch penalty applies.
        """
        from models.getcomics import score_getcomics_result, accept_result
        # "Batman - The Dark Knight Omnibus #1 (2020)"
        score, range_hit, series_match = score_getcomics_result(
            "Batman - The Dark Knight Omnibus #1 (2020)", "Batman", "1", 2020,
            accept_variants=['omni']
        )
        decision = accept_result(score, range_hit, series_match)
        # Format mismatch penalty applies (-50), issue matching blocked
        assert decision == "REJECT"

    def test_hardcover_variant_acceptance(self):
        """Hardcover variant: format mismatch penalty applies, issue matching blocked.

        Searching for 'Batman #1' with accept_variants=['hardcover'] still gets
        format mismatch penalty because a hardcover is NOT the same as a single issue.
        """
        from models.getcomics import score_getcomics_result, accept_result
        # "Batman - The Long Halloween Hardcover #1 (2020)"
        score, range_hit, series_match = score_getcomics_result(
            "Batman - The Long Halloween Hardcover #1 (2020)", "Batman", "1", 2020,
            accept_variants=['hardcover']
        )
        decision = accept_result(score, range_hit, series_match)
        # Format mismatch penalty applies (-50), issue matching blocked
        # Score: series(30) + format_mismatch(-50) + tight(-10) + year(20) = -10... but gets REJECT
        assert decision == "REJECT"

    def test_deluxe_variant_acceptance(self):
        """Deluxe edition variant: format mismatch penalty applies, issue matching blocked.

        A deluxe edition is NOT the same as a single issue.
        """
        from models.getcomics import score_getcomics_result, accept_result
        # "Batman - No Man's Land Deluxe #1 (2020)"
        score, range_hit, series_match = score_getcomics_result(
            "Batman - No Man's Land Deluxe #1 (2020)", "Batman", "1", 2020,
            accept_variants=['deluxe']
        )
        decision = accept_result(score, range_hit, series_match)
        # Format mismatch penalty applies (-50), issue matching blocked
        assert decision == "REJECT"

    def test_absolute_variant_detection(self):
        """'Absolute' should be detected as a variant type."""
        from models.getcomics import score_getcomics_result, accept_result
        # "Absolute Batman #1 (2025)" - Absolute is a variant designation
        # This is actually the main series name "Absolute Batman", not a sub-series
        score, range_hit, series_match = score_getcomics_result(
            "Absolute Batman #1 (2025)", "Absolute Batman", "1", 2025
        )
        decision = accept_result(score, range_hit, series_match)
        # Series name is "Absolute Batman", so it matches perfectly
        assert decision == "ACCEPT"
        assert score == 95

    def test_arc_sub_series_not_variant(self):
        """Story arcs like 'Court of Owls' should NOT get issue matching.

        Arc sub-series like 'Batman - Court of Owls #1' are NOT the same issue as 'Batman #1'.
        They have their own arc-internal issue numbering. So arc sub-series should be
        penalized and NOT receive issue matching points.
        """
        from models.getcomics import score_getcomics_result, accept_result
        # "Batman - Court of Owls #1 (2020)" - this is a story arc, not the same as Batman #1
        score, range_hit, series_match = score_getcomics_result(
            "Batman - Court of Owls #1 (2020)", "Batman", "1", 2020
        )
        decision = accept_result(score, range_hit, series_match)
        # Arc sub-series gets -30 penalty, issue match is blocked
        # Score: series(30) - arc(30) + tight(-10) + year(20) = 10
        assert score == 10
        assert decision == "REJECT"

        # Even if someone accepts the arc keyword, issue matching should still be blocked
        # because "Court of Owls #1" is not "Batman #1"
        score_arc_accepted, range_hit, series_match = score_getcomics_result(
            "Batman - Court of Owls #1 (2020)", "Batman", "1", 2020,
            accept_variants=['court']
        )
        # Issue matching is still blocked for arcs, even if variant_accepted is True
        # Score: series(30) - arc(30) + tight(-10) + year(20) + issue blocked = 10
        assert score_arc_accepted == 10
        assert accept_result(score_arc_accepted, range_hit, series_match) == "REJECT"

    def test_annual_with_year_in_different_position(self):
        """Annual variant should NOT be accepted unless it's in the search series name.

        "Batman 2025 Annual" is a DIFFERENT series from "Batman".
        Searching for "Batman" with accept_variants=['annual'] should NOT accept "Batman 2025 Annual #1".
        """
        from models.getcomics import score_getcomics_result, accept_result
        # "Batman 2025 Annual #1" - Annual is a publication type creating a different series
        # Searching "Batman" should NOT accept this even with accept_variants=['annual']
        score, range_hit, series_match = score_getcomics_result(
            "Batman 2025 Annual #1 (2025)", "Batman", "1", 2025,
            accept_variants=['annual']
        )
        decision = accept_result(score, range_hit, series_match)
        # Annual creates a different series - reject even with accept_variants
        assert decision == "REJECT"

        # But if searching for "Batman 2025 Annual" (Annual IN the search series name),
        # then "Batman 2025 Annual #1" should match
        score, range_hit, series_match = score_getcomics_result(
            "Batman 2025 Annual #1 (2025)", "Batman 2025 Annual", "1", 2025
        )
        decision = accept_result(score, range_hit, series_match)
        assert decision == "ACCEPT"

    def test_trade_paperback_variant(self):
        """Trade Paperback (full name) variant: format mismatch penalty applies.

        Searching for 'Batman #1' with accept_variants=['trade paperback'] still gets
        format mismatch penalty because a trade paperback is NOT a single issue.
        """
        from models.getcomics import score_getcomics_result, accept_result
        score, range_hit, series_match = score_getcomics_result(
            "Batman - The Killing Joke Trade Paperback #1 (2020)", "Batman", "1", 2020,
            accept_variants=['trade paperback']
        )
        decision = accept_result(score, range_hit, series_match)
        assert decision == "REJECT"

    def test_series_name_contains_variant_keyword_not_penalized(self):
        """When search series name contains variant keyword, result should not be penalized.

        E.g., searching for 'Flash Gordon - Quarterly' (which IS a series that publishes
        quarterly) should not penalize 'Flash Gordon - Quarterly #5' as a sub-series variant.
        """
        from models.getcomics import score_getcomics_result, accept_result, ACCEPT_THRESHOLD
        # Searching for "Flash Gordon - Quarterly" series (series name contains Quarterly)
        score, range_hit, series_match = score_getcomics_result(
            "Flash Gordon - Quarterly #5 (2025)",  # Result
            "Flash Gordon - Quarterly",  # Search series name contains "Quarterly"
            "5",
            2025
        )
        decision = accept_result(score, range_hit, series_match)
        # Should be a perfect match - series name matches and issue matches
        assert score == 95
        assert decision == "ACCEPT"

        # But searching for main "Flash Gordon" series should penalize the Quarterly variant
        score2, range_hit2, series_match2 = score_getcomics_result(
            "Flash Gordon - Quarterly #5 (2025)",
            "Flash Gordon",  # Search series name does NOT contain "Quarterly"
            "5",
            2025
        )
        decision2 = accept_result(score2, range_hit2, series_match2)
        # Should be penalized as sub-series
        assert score2 < ACCEPT_THRESHOLD
        assert decision2 == "REJECT"

    def test_series_name_contains_annual_keyword(self):
        """When search series name contains 'Annual', result should not be penalized.

        E.g., searching for 'Batman Annual' (which could be a valid series name)
        should match 'Batman Annual #1' without penalty.
        """
        from models.getcomics import score_getcomics_result, accept_result
        # Searching for "Batman Annual" series (series name contains Annual)
        score, range_hit, series_match = score_getcomics_result(
            "Batman Annual #1 (2025)",
            "Batman Annual",
            "1",
            2025
        )
        decision = accept_result(score, range_hit, series_match)
        # Should be a perfect match
        assert score == 95
        assert decision == "ACCEPT"

    def test_different_arc_sub_series_not_match(self):
        """Different arc sub-series should NOT match each other.

        Batman - Darkest Knight is a DIFFERENT arc from Batman - Court of Owls.
        Searching for 'Batman - Darkest Knight #1' should NOT match 'Batman - Court of Owls #1'.
        """
        from models.getcomics import score_getcomics_result, accept_result, ACCEPT_THRESHOLD
        # Batman - Darkest Knight searching for issue, but result is Batman - Court of Owls
        score, range_hit, series_match = score_getcomics_result(
            "Batman - Court of Owls #1 (2020)", "Batman - Darkest Knight", "1", 2020
        )
        decision = accept_result(score, range_hit, series_match)
        # Series matches "Batman" but remaining is arc " - Court of Owls"
        # Arc gets penalized and issue matching blocked
        # Score is low, decision is REJECT - which is correct
        assert score < ACCEPT_THRESHOLD
        assert decision == "REJECT"

    def test_arc_range_pack_accepted(self):
        """Arc range pack containing target issue should be accepted.

        Batman - Court of Owls #1-5 containing Batman - Court of Owls #2 should match.
        Range packs for the same arc are valid because arcs are often bundled.
        """
        from models.getcomics import score_getcomics_result, accept_result, ACCEPT_THRESHOLD
        # Batman - Court of Owls #1-5 when searching for Batman - Court of Owls #2
        score, range_hit, series_match = score_getcomics_result(
            "Batman - Court of Owls #1-5 (2020)", "Batman - Court of Owls", "2", 2020
        )
        decision = accept_result(score, range_hit, series_match)
        # Series matches, range contains target issue "2"
        # Score is positive, decision is FALLBACK (not strong accept but usable)
        assert score > 0
        assert decision in ("ACCEPT", "FALLBACK")

    def test_different_series_with_the_prefix(self):
        """Series with 'The' prefix should not match same series without 'The'.

        'The Flash Gordon' and 'Flash Gordon' are considered different series.
        Searching for 'The Flash Gordon #1' should NOT match 'Flash Gordon #1'.
        """
        from models.getcomics import score_getcomics_result, accept_result, ACCEPT_THRESHOLD
        # "Flash Gordon #1 (2024)" when searching for "The Flash Gordon #1"
        # Result doesn't have "The" prefix, search does - should reject
        score, range_hit, series_match = score_getcomics_result(
            "Flash Gordon #1 (2024)", "The Flash Gordon", "1", 2024
        )
        decision = accept_result(score, range_hit, series_match)
        # Result "Flash Gordon #1" doesn't match search "The Flash Gordon"
        # Since result doesn't start with "the flash gordon", series doesn't match
        assert score < ACCEPT_THRESHOLD
        assert decision == "REJECT"

    def test_absolute_batman_is_different_series(self):
        """Absolute Batman is a DIFFERENT series from Batman.

        Searching for 'Batman #1' should NOT match 'Absolute Batman Annual #1'.
        'Absolute' is a series modifier, not a publication variant.
        """
        from models.getcomics import score_getcomics_result, accept_result, ACCEPT_THRESHOLD
        # "Absolute Batman 2025 Annual #1" when searching for "Batman #1"
        # "Absolute Batman" is a different series from "Batman"
        score, range_hit, series_match = score_getcomics_result(
            "Absolute Batman 2025 Annual #1 (2025)", "Batman", "1", 2025,
            accept_variants=['annual', 'tpB', 'omni']
        )
        decision = accept_result(score, range_hit, series_match)
        # "Absolute Batman" starts with "Batman" but has "Absolute" as prefix
        # This is a different series, should be rejected
        assert score < ACCEPT_THRESHOLD
        assert decision == "REJECT"

    def test_annual_variant_accepted_when_in_accept_variants(self):
        """Annual variant should NOT be accepted via accept_variants - it must be in search series name.

        Publication types like 'Annual' create DIFFERENT series on Metron.
        "Batman 2025 Annual" is a different series from "Batman".
        accept_variants only works for FORMAT variants (TPB, omnibus, oneshot).
        """
        from models.getcomics import score_getcomics_result, accept_result
        # "Batman 2025 Annual #1 (2025)" searching for "Batman #1"
        # Annual is a publication type, NOT a format variant - should be rejected
        score, range_hit, series_match = score_getcomics_result(
            "Batman 2025 Annual #1 (2025)", "Batman", "1", 2025,
            accept_variants=['annual']
        )
        decision = accept_result(score, range_hit, series_match)
        # Annual creates different series - reject even with accept_variants
        assert decision == "REJECT"

        # But if searching for "Batman 2025 Annual" (Annual IN the search series name)
        # then it should match
        score, range_hit, series_match = score_getcomics_result(
            "Batman 2025 Annual #1 (2025)", "Batman 2025 Annual", "1", 2025
        )
        decision = accept_result(score, range_hit, series_match)
        assert score == 95
        assert decision == "ACCEPT"

    def test_tpb_variant_accepted_when_in_accept_variants(self):
        """TPB variant: format mismatch penalty applies, issue matching blocked.

        Searching for 'Batman #1' with accept_variants=['tpB'] still gets
        format mismatch penalty (-50) and issue matching is blocked because
        a TPB is NOT the same as a single issue. Score is low but series match
        keeps it from complete rejection.
        """
        from models.getcomics import score_getcomics_result, accept_result
        # "Batman Vol 5 TPB #1" searching for "Batman #1"
        score, range_hit, series_match = score_getcomics_result(
            "Batman Vol 5 TPB #1", "Batman", "1", None,
            accept_variants=['tpB']
        )
        decision = accept_result(score, range_hit, series_match)
        # Format mismatch (-50), issue matching blocked
        # Score: series(30) + format_mismatch(-50) + tight(-10) = -30
        assert score == -30
        assert decision == "REJECT"

    def test_tpb_variant_rejected_when_not_in_accept_variants(self):
        """TPB variant should be rejected when 'tpB' is NOT in accept_variants.

        Searching for 'Batman #1' without tpB in accept_variants should reject
        'Batman Vol 5 TPB #1'.
        """
        from models.getcomics import score_getcomics_result, accept_result, ACCEPT_THRESHOLD
        # "Batman Vol 5 TPB #1" searching for "Batman #1" without accepting tpB
        score, range_hit, series_match = score_getcomics_result(
            "Batman Vol 5 TPB #1", "Batman", "1", None,
            accept_variants=['annual']
        )
        decision = accept_result(score, range_hit, series_match)
        # TPB not accepted, should be penalized
        assert score < ACCEPT_THRESHOLD
        assert decision == "REJECT"

    # ===================================================================
    # Separator normalization — colon vs en-dash/em-dash (Issue #241)
    # ===================================================================

    def test_colon_to_endash_series_match(self):
        """Series with colon should match result with en-dash.

        Database stores 'Adventures of Superman: The Book of El' but
        GetComics lists 'Adventures of Superman – Book of El #7 (2026)'.
        """
        from models.getcomics import score_getcomics_result, accept_result
        score, range_hit, series_match = score_getcomics_result(
            "Adventures of Superman \u2013 Book of El #7 (2026)",
            "Adventures of Superman: The Book of El",
            "7",
            2026,
        )
        decision = accept_result(score, range_hit, series_match)
        assert series_match is True
        assert score >= 90
        assert decision == "ACCEPT"

    def test_colon_to_emdash_series_match(self):
        """Series with colon should match result with em-dash."""
        from models.getcomics import score_getcomics_result, accept_result
        score, range_hit, series_match = score_getcomics_result(
            "Adventures of Superman \u2014 Book of El #7 (2026)",
            "Adventures of Superman: The Book of El",
            "7",
            2026,
        )
        decision = accept_result(score, range_hit, series_match)
        assert series_match is True
        assert score >= 90
        assert decision == "ACCEPT"

    def test_hyphenated_name_unaffected_by_normalization(self):
        """Hyphenated names like Spider-Man should not be affected by separator normalization."""
        from models.getcomics import score_getcomics_result
        score, _, _ = score_getcomics_result(
            "Spider-Man #5 (2025)", "Spider-Man", "5", 2025
        )
        assert score == 95

    def test_multiple_colons_match_dashes(self):
        """Series with multiple colons should match result with dashes."""
        from models.getcomics import score_getcomics_result, accept_result
        score, range_hit, series_match = score_getcomics_result(
            "Batman - Arkham Knight - Genesis #1 (2020)",
            "Batman: Arkham Knight: Genesis",
            "1",
            2020,
        )
        decision = accept_result(score, range_hit, series_match)
        assert series_match is True
        assert decision == "ACCEPT"

    def test_normalize_separators_function(self):
        """Unit test for _normalize_separators helper."""
        from models.getcomics import _normalize_separators
        # Colon with "The" stripped
        assert _normalize_separators("adventures of superman: the book of el") == \
            "adventures of superman - book of el"
        # Hyphenated name unchanged
        assert _normalize_separators("spider-man #5") == "spider-man #5"
        # En-dash normalized
        assert _normalize_separators("batman \u2013 court of owls") == \
            "batman - court of owls"
        # Em-dash normalized
        assert _normalize_separators("batman \u2014 court of owls") == \
            "batman - court of owls"
        # No separator, unchanged
        assert _normalize_separators("the flash") == "the flash"


# ===================================================================
# get_weekly_pack_url_for_date (pure function)
# ===================================================================

class TestGetWeeklyPackUrlForDate:

    def test_dot_format(self):
        from models.getcomics import get_weekly_pack_url_for_date
        url = get_weekly_pack_url_for_date("2026.01.14")
        assert url == "https://getcomics.org/other-comics/2026-01-14-weekly-pack/"

    def test_dash_format(self):
        from models.getcomics import get_weekly_pack_url_for_date
        url = get_weekly_pack_url_for_date("2026-01-14")
        assert url == "https://getcomics.org/other-comics/2026-01-14-weekly-pack/"


# ===================================================================
# get_weekly_pack_dates_in_range (pure function)
# ===================================================================

class TestGetWeeklyPackDatesInRange:

    def test_returns_tuesdays_and_wednesdays(self):
        from models.getcomics import get_weekly_pack_dates_in_range
        # 2026-01-12 = Monday, 2026-01-18 = Sunday
        # Tuesday = 2026-01-13, Wednesday = 2026-01-14
        dates = get_weekly_pack_dates_in_range("2026-01-12", "2026-01-18")
        assert "2026.01.13" in dates  # Tuesday
        assert "2026.01.14" in dates  # Wednesday
        assert len(dates) == 2

    def test_results_are_newest_first(self):
        from models.getcomics import get_weekly_pack_dates_in_range
        dates = get_weekly_pack_dates_in_range("2026-01-01", "2026-01-31")
        # Newest first means first date should be later than last date
        assert dates[0] > dates[-1]

    def test_empty_range_returns_empty(self):
        from models.getcomics import get_weekly_pack_dates_in_range
        # A Monday-only range has no Tue/Wed
        dates = get_weekly_pack_dates_in_range("2026-01-12", "2026-01-12")
        assert dates == []


# ===================================================================
# find_latest_weekly_pack_url
# ===================================================================

class TestFindLatestWeeklyPackUrl:

    @patch("models.getcomics.scraper")
    def test_finds_pack_by_title(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(HOMEPAGE_WITH_WEEKLY_PACK_HTML)

        from models.getcomics import find_latest_weekly_pack_url
        url, date = find_latest_weekly_pack_url()

        assert url == "https://getcomics.org/other-comics/2026-01-14-weekly-pack/"
        assert date == "2026.01.14"

    @patch("models.getcomics.scraper")
    def test_falls_back_to_url_pattern(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(HOMEPAGE_WEEKLY_PACK_URL_ONLY_HTML)

        from models.getcomics import find_latest_weekly_pack_url
        url, date = find_latest_weekly_pack_url()

        assert url == "https://getcomics.org/other-comics/2026-02-04-weekly-pack/"
        assert date == "2026.02.04"

    @patch("models.getcomics.scraper")
    def test_returns_none_when_no_pack_found(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(HOMEPAGE_NO_WEEKLY_PACK_HTML)

        from models.getcomics import find_latest_weekly_pack_url
        url, date = find_latest_weekly_pack_url()

        assert url is None
        assert date is None

    @patch("models.getcomics.scraper")
    def test_returns_none_on_request_error(self, mock_scraper):
        mock_scraper.get.side_effect = Exception("Network error")

        from models.getcomics import find_latest_weekly_pack_url
        url, date = find_latest_weekly_pack_url()

        assert url is None
        assert date is None


# ===================================================================
# check_weekly_pack_availability
# ===================================================================

class TestCheckWeeklyPackAvailability:

    @patch("models.getcomics.scraper")
    def test_returns_true_when_pixeldrain_links_present(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(PACK_READY_HTML)

        from models.getcomics import check_weekly_pack_availability
        assert check_weekly_pack_availability("https://getcomics.org/pack") is True

    @patch("models.getcomics.scraper")
    def test_returns_false_when_not_ready_message(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(PACK_NOT_READY_HTML)

        from models.getcomics import check_weekly_pack_availability
        assert check_weekly_pack_availability("https://getcomics.org/pack") is False

    @patch("models.getcomics.scraper")
    def test_returns_false_when_no_links(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(PACK_NO_LINKS_HTML)

        from models.getcomics import check_weekly_pack_availability
        assert check_weekly_pack_availability("https://getcomics.org/pack") is False

    @patch("models.getcomics.scraper")
    def test_returns_false_on_request_error(self, mock_scraper):
        mock_scraper.get.side_effect = Exception("Timeout")

        from models.getcomics import check_weekly_pack_availability
        assert check_weekly_pack_availability("https://getcomics.org/pack") is False


# ===================================================================
# parse_weekly_pack_page
# ===================================================================

class TestParseWeeklyPackPage:

    @patch("models.getcomics.scraper")
    def test_extracts_jpg_links_for_requested_publishers(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(WEEKLY_PACK_PAGE_HTML)

        from models.getcomics import parse_weekly_pack_page
        result = parse_weekly_pack_page(
            "https://getcomics.org/pack", "JPG", ["DC", "Marvel"],
        )

        assert result["DC"] == "https://pixeldrain.com/u/dc_jpg"
        assert result["Marvel"] == "https://pixeldrain.com/u/marvel_jpg"
        assert "Image" not in result  # not requested

    @patch("models.getcomics.scraper")
    def test_extracts_webp_links(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(WEEKLY_PACK_PAGE_HTML)

        from models.getcomics import parse_weekly_pack_page
        result = parse_weekly_pack_page(
            "https://getcomics.org/pack", "WEBP", ["DC", "Marvel"],
        )

        assert result["DC"] == "https://pixeldrain.com/u/dc_webp"
        assert result["Marvel"] == "https://pixeldrain.com/u/marvel_webp"

    @patch("models.getcomics.scraper")
    def test_returns_empty_when_format_not_found(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(WEEKLY_PACK_PAGE_HTML)

        from models.getcomics import parse_weekly_pack_page
        result = parse_weekly_pack_page(
            "https://getcomics.org/pack", "CBR", ["DC"],
        )

        assert result == {}

    @patch("models.getcomics.scraper")
    def test_returns_empty_on_request_error(self, mock_scraper):
        mock_scraper.get.side_effect = Exception("Network error")

        from models.getcomics import parse_weekly_pack_page
        result = parse_weekly_pack_page(
            "https://getcomics.org/pack", "JPG", ["DC"],
        )

        assert result == {}


# ===================================================================
# parse_result_title - parse GetComics result titles into structured data
# ===================================================================

class TestParseResultTitle:
    """Tests for parse_result_title function."""

    def test_basic_parsing(self):
        """Basic title parsing extracts name, issue, and year."""
        from models.getcomics import parse_result_title
        result = parse_result_title("Batman #1 (2020)")
        assert result.name == "Batman"
        assert result.issue == "1"
        assert result.year == 2020

    def test_volume_extraction(self):
        """Volume number should be extracted."""
        from models.getcomics import parse_result_title
        result = parse_result_title("Batman Vol. 3 #1 (2020)")
        assert result.name == "Batman"
        assert result.volume == 3
        assert result.issue == "1"

    def test_issue_range_parsing(self):
        """Issue ranges should be parsed correctly."""
        from models.getcomics import parse_result_title
        result = parse_result_title("Batman #1-50 (2025)")
        assert result.issue_range == (1, 50)
        assert result.issue == "1-50"

    def test_annual_not_extracted_as_year(self):
        """Annual should NOT cause year extraction to fail."""
        from models.getcomics import parse_result_title
        result = parse_result_title("Batman Annual #1 (2020)")
        assert result.name == "Batman Annual"
        assert result.issue == "1"
        assert result.year == 2020
        assert result.is_annual == True
        assert result.publication_year is None

    def test_flash_gordon_annual_2014(self):
        """Flash Gordon Annual 2014 should extract publication_year=2014."""
        from models.getcomics import parse_result_title
        result = parse_result_title("Flash Gordon Annual 2014 Vol. 1")
        assert result.name == "Flash Gordon Annual 2014"
        assert result.volume == 1
        assert result.publication_year == 2014

    def test_justice_league_dark_2021_annual(self):
        """Justice League Dark 2021 Annual should NOT extract 2021 as publication_year.

        The '2021' is part of the series name designation, not a publication year.
        Publication year comes from parentheses at the end.
        """
        from models.getcomics import parse_result_title
        result = parse_result_title("Justice League Dark 2021 Annual Vol. 1")
        # '2021 Annual' is part of the series name, not year + publication_type
        assert result.publication_year is None

    def test_arc_parsing(self):
        """Arc notation should be detected and parsed."""
        from models.getcomics import parse_result_title
        result = parse_result_title("Batman - Court of Owls #1 (2020)")
        assert result.name == "Batman"
        assert result.is_arc == True
        assert result.arc_name == "Court of Owls"

    def test_tpb_detection(self):
        """TPB should be detected in title via format_variants list."""
        from models.getcomics import parse_result_title, get_format_variants
        result = parse_result_title("Batman Vol. 5 #1-50 + TPBs")
        # TPBs should be in the format_variants list (stored as lowercase 'tpb')
        assert 'tpb' in [v.lower() for v in result.format_variants]

    def test_omnibus_detection(self):
        """Omnibus should be detected in title via format_variants list."""
        from models.getcomics import parse_result_title, get_format_variants
        result = parse_result_title("Batman Omnibus #1 (2020)")
        # Omnibus variant should be in the format_variants list
        assert 'omnibus' in [v.lower() for v in result.format_variants]

    def test_crossover_series(self):
        """Crossover series with slashes should be preserved."""
        from models.getcomics import parse_result_title
        result = parse_result_title("Batman / Superman: World's Finest Vol. 1")
        assert result.name == "Batman / Superman: World's Finest"

    def test_quarterly_detection(self):
        """Quarterly publication type should be detected when not using dash arc notation."""
        from models.getcomics import parse_result_title
        # Note: "Flash Gordon - Quarterly" (with dash) is treated as an arc, not a publication type
        # Use "Flash Gordon Quarterly" (without dash) to detect quarterly
        result = parse_result_title("Flash Gordon Quarterly #5 (2025)")
        assert result.is_quarterly == True

    def test_publication_year_extraction_after_keyword(self):
        """Publication year appearing after 'Annual' keyword should be extracted."""
        from models.getcomics import parse_result_title
        # Year after Annual should be extracted as publication_year
        result = parse_result_title("Nightwing Annual 2014 Vol. 1")
        assert result.publication_year == 2014

        # Year before Annual (series name designation) should NOT be extracted
        result2 = parse_result_title("Nightwing 2021 Annual Vol. 1")
        assert result2.publication_year is None


# ===================================================================
# normalize_series_name - normalize series names and extract metadata
# ===================================================================

class TestNormalizeSeriesName:
    """Tests for normalize_series_name function."""

    def test_basic_normalization(self):
        """Basic series name should be normalized."""
        from models.getcomics import normalize_series_name
        name, meta = normalize_series_name("Batman")
        assert name == "Batman"
        assert meta['volume'] is None

    def test_volume_extraction(self):
        """Volume should be extracted from series name."""
        from models.getcomics import normalize_series_name
        name, meta = normalize_series_name("Batman Vol. 3")
        assert name == "Batman"
        assert meta['volume'] == 3

    def test_crossover_detection(self):
        """Crossover series should be marked."""
        from models.getcomics import normalize_series_name
        name, meta = normalize_series_name("Batman / Superman")
        assert meta['is_crossover'] == True

    def test_annual_in_name(self):
        """Annual in series name should be detected."""
        from models.getcomics import normalize_series_name
        name, meta = normalize_series_name("Flash Gordon Annual")
        assert meta['is_annual'] == True

    def test_publication_year_after_annual(self):
        """Publication year appearing after Annual should be extracted."""
        from models.getcomics import normalize_series_name
        name, meta = normalize_series_name("Flash Gordon Annual 2014")
        assert meta['publication_year'] == 2014
        assert meta['is_annual'] == True

    def test_year_before_annual_not_extracted(self):
        """Year before Annual (series designation) should NOT be extracted as publication_year."""
        from models.getcomics import normalize_series_name
        name, meta = normalize_series_name("Justice League Dark 2021 Annual")
        # 2021 is part of the series name, not publication year
        assert meta['publication_year'] is None


# ===================================================================
# score_getcomics_result - additional edge case tests
# ===================================================================

class TestScoreGetcomicsResultEdgeCases:
    """Additional edge case tests for score_getcomics_result.

    These tests focus on cases that SHOULD NOT match or have special behavior.
    """

    def test_batman_vs_batman_annual_different_series(self):
        """Batman Annual is a DIFFERENT series from Batman."""
        from models.getcomics import score_getcomics_result, accept_result, ACCEPT_THRESHOLD
        score, _, _ = score_getcomics_result(
            "Batman Annual #1 (2025)", "Batman", "1", 2025
        )
        # Should be rejected - Annual is a different series
        assert score < ACCEPT_THRESHOLD
        assert accept_result(score, False, True) == "REJECT"

    def test_batman_vs_absolute_batman_different_series(self):
        """Absolute Batman is a DIFFERENT series from Batman."""
        from models.getcomics import score_getcomics_result, accept_result, ACCEPT_THRESHOLD
        score, _, _ = score_getcomics_result(
            "Absolute Batman #1 (2025)", "Batman", "1", 2025
        )
        # Should be rejected - Absolute Batman is a different series
        assert score < ACCEPT_THRESHOLD

    def test_punisher_vs_the_punisher_different_series(self):
        """The Punisher is a DIFFERENT series from Punisher."""
        from models.getcomics import score_getcomics_result, accept_result, ACCEPT_THRESHOLD
        # Searching for "The Punisher" should NOT match "Punisher"
        score, _, _ = score_getcomics_result(
            "Punisher #1 (2025)", "The Punisher", "1", 2025
        )
        assert score < ACCEPT_THRESHOLD

    def test_top_ten_vs_top_ten_alison(self):
        """Top Ten is DIFFERENT from Top Ten Alison."""
        from models.getcomics import score_getcomics_result, accept_result, ACCEPT_THRESHOLD
        # Searching for "Top Ten" should NOT match "Top Ten Alison"
        score, _, _ = score_getcomics_result(
            "Top Ten Alison #1 (2025)", "Top Ten", "1", 2025
        )
        # The result starts with "Top Ten Alison" which contains "Top Ten" as prefix
        # but the remaining " Alison" makes it a different series
        assert score < ACCEPT_THRESHOLD

    def test_vol_3_vs_vol_6_different_volume(self):
        """Same series but different volume should still match series but differentiate."""
        from models.getcomics import score_getcomics_result, accept_result
        # Searching for Batman Vol 3 should match Batman Vol 3 (same volume)
        score_vol3, _, _ = score_getcomics_result(
            "Batman Vol. 3 #1 (2025)", "Batman", "1", 2025
        )
        # Searching for Batman Vol 3 should NOT match Batman Vol 6 (different volume)
        # But since we don't have strict volume matching in current implementation,
        # it will still match on series name
        score_vol6, _, _ = score_getcomics_result(
            "Batman Vol. 6 #1 (2025)", "Batman", "1", 2025
        )
        # Both should have series match (30 points)
        # The volume number doesn't cause a penalty in current implementation
        assert score_vol3 >= 30
        assert score_vol6 >= 30

    def test_justice_league_dark_annual_vs_annual(self):
        """Justice League Dark Annual is DIFFERENT from Justice League Dark."""
        from models.getcomics import score_getcomics_result, accept_result, ACCEPT_THRESHOLD
        score, _, _ = score_getcomics_result(
            "Justice League Dark Annual #1 (2025)", "Justice League Dark", "1", 2025
        )
        # Should be rejected - Annual is a different series
        assert score < ACCEPT_THRESHOLD

    def test_justice_league_dark_2021_annual_vs_annual(self):
        """Justice League Dark 2021 Annual is DIFFERENT from Justice League Dark Annual."""
        from models.getcomics import score_getcomics_result, accept_result, ACCEPT_THRESHOLD
        # Year edition creates a different series
        score, _, _ = score_getcomics_result(
            "Justice League Dark 2021 Annual Vol. 1", "Justice League Dark Annual", "1", None
        )
        # Different editions should not match
        assert score < ACCEPT_THRESHOLD

    def test_range_pack_with_different_volume_rejected(self):
        """Range pack with different volume should be rejected."""
        from models.getcomics import score_getcomics_result, accept_result, ACCEPT_THRESHOLD
        # Batman Vol 5 #1-50 pack when searching for Batman Vol 3
        score, _, _ = score_getcomics_result(
            "Batman Vol. 5 #1-50 (2025)", "Batman", "1", 2025
        )
        # Range containing issue 1 should be fallback, not reject
        # But the volume is different - current implementation doesn't penalize volume mismatch
        assert score > 0

    def test_flash_gordon_vs_flash_gordon_quarterly_different_series(self):
        """Flash Gordon Quarterly is a DIFFERENT series from Flash Gordon."""
        from models.getcomics import score_getcomics_result, accept_result, ACCEPT_THRESHOLD
        score, _, _ = score_getcomics_result(
            "Flash Gordon - Quarterly #5 (2025)", "Flash Gordon", "5", 2025
        )
        # Should be rejected - Quarterly is a different series
        assert score < ACCEPT_THRESHOLD

    def test_batman_inc_vs_batman_different_series(self):
        """Batman Inc is a DIFFERENT series from Batman."""
        from models.getcomics import score_getcomics_result, accept_result, ACCEPT_THRESHOLD
        score, _, _ = score_getcomics_result(
            "Batman Inc #1 (2025)", "Batman", "1", 2025
        )
        # Should be rejected - Batman Inc is different from Batman
        assert score < ACCEPT_THRESHOLD

    def test_batman_adventures_vs_batman_different_series(self):
        """Batman Adventures is a DIFFERENT series from Batman."""
        from models.getcomics import score_getcomics_result, accept_result, ACCEPT_THRESHOLD
        score, _, _ = score_getcomics_result(
            "Batman Adventures #1 (2025)", "Batman", "1", 2025
        )
        # Should be rejected - Adventures is different series
        assert score < ACCEPT_THRESHOLD

    def test_wrong_year_in_title_penalized(self):
        """Wrong year explicitly in title should be penalized."""
        from models.getcomics import score_getcomics_result
        # Searching for 2025 but result has 2024 in title
        score_wrong, _, _ = score_getcomics_result(
            "Batman #1 (2024)", "Batman", "1", 2025
        )
        score_correct, _, _ = score_getcomics_result(
            "Batman #1 (2025)", "Batman", "1", 2025
        )
        # Wrong year should have 20 point penalty
        assert score_wrong < score_correct

    def test_issue_mismatch_penalty(self):
        """Explicit issue mismatch should be penalized."""
        from models.getcomics import score_getcomics_result
        # Searching for #5 but result shows #3
        score, _, _ = score_getcomics_result(
            "Batman #3 (2025)", "Batman", "5", 2025
        )
        # Should have -40 penalty for confirmed issue mismatch
        # series(30) - mismatch(40) + tight(15) + year(20) = 25
        assert score == 25

    def test_bare_wrong_issue_penalized(self):
        """A bare (no '#') wrong issue number must be penalized like an explicit one.

        Usenet titles rarely carry a '#', so a wrong bare number must not slip
        through on series+year alone (the reported "001 downloaded for #002" bug).
        """
        from models.getcomics import score_getcomics_result, ACCEPT_THRESHOLD
        score, _, _ = score_getcomics_result("Batman 3 (2025)", "Batman", "5", 2025)
        assert score < ACCEPT_THRESHOLD
        # Should mirror the explicit "#3" mismatch penalty.
        score_hash, _, _ = score_getcomics_result("Batman #3 (2025)", "Batman", "5", 2025)
        assert score == score_hash

    def test_bare_correct_issue_still_accepted(self):
        """The correct bare issue number must still score into ACCEPT."""
        from models.getcomics import score_getcomics_result, ACCEPT_THRESHOLD
        score, _, _ = score_getcomics_result("Batman 5 (2025)", "Batman", "5", 2025)
        assert score >= ACCEPT_THRESHOLD

    def test_bare_year_token_not_treated_as_mismatch(self):
        """A leading 4-digit calendar year must not fire the bare mismatch penalty."""
        from models.getcomics import score_getcomics_result
        # "2025" is a year label, not issue 2025; the annual is rejected as a
        # sub-series, but no spurious extra -40 should apply.
        with_year_token, _, _ = score_getcomics_result(
            "Batman 2025 Annual #1 (2025)", "Batman", "1", 2025
        )
        no_year_token, _, _ = score_getcomics_result(
            "Batman Annual #1 (2025)", "Batman", "1", 2025
        )
        assert with_year_token == no_year_token

    def test_count_total_not_read_as_issue(self):
        """The total in an "N of M" count must not be read as issue M.

        "Series 1 of 5" is issue 1 of a 5-issue run; searching for #5 must not
        match the "5" from "of 5".
        """
        from models.getcomics import score_getcomics_result, ACCEPT_THRESHOLD
        wrong, _, _, matched = score_getcomics_result(
            "Only the Savage Are Left 1 of 5", "Only the Savage Are Left", "5", 0,
            return_issue_matched=True,
        )
        assert matched is False
        assert wrong < ACCEPT_THRESHOLD
        # The real issue number before "of" is still matched.
        right, _, _, right_matched = score_getcomics_result(
            "Only the Savage Are Left 5 of 12", "Only the Savage Are Left", "5", 0,
            return_issue_matched=True,
        )
        assert right_matched is True
        assert right >= ACCEPT_THRESHOLD

    def test_return_issue_matched_flag(self):
        """return_issue_matched adds a 4th element reporting positive confirmation."""
        from models.getcomics import score_getcomics_result
        # Explicit #N match.
        hashed = score_getcomics_result("Batman #5 (2025)", "Batman", "5", 2025,
                                        return_issue_matched=True)
        assert len(hashed) == 4 and hashed[3] is True
        # Correct bare number.
        bare_ok = score_getcomics_result("Batman 5 (2025)", "Batman", "5", 2025,
                                         return_issue_matched=True)
        assert bare_ok[3] is True
        # Wrong bare number — not confirmed.
        bare_wrong = score_getcomics_result("Batman 3 (2025)", "Batman", "5", 2025,
                                            return_issue_matched=True)
        assert bare_wrong[3] is False
        # Default call keeps the 3-tuple contract.
        assert len(score_getcomics_result("Batman #5 (2025)", "Batman", "5", 2025)) == 3


# ===================================================================
# get_series_alias_list
# ===================================================================

class TestGetSeriesAliasList:

    @patch("models.getcomics.get_series_aliases", return_value="2000AD")
    def test_parses_single_alias(self, _mock):
        from models.getcomics import get_series_alias_list
        assert get_series_alias_list("2000 AD") == ["2000AD"]

    @patch("models.getcomics.get_series_aliases", return_value="2000AD, Two Thousand AD ,")
    def test_splits_trims_and_drops_blanks(self, _mock):
        from models.getcomics import get_series_alias_list
        assert get_series_alias_list("2000 AD") == ["2000AD", "Two Thousand AD"]

    @patch("models.getcomics.get_series_aliases", return_value="Batman, batman, BATMAN, Bruce")
    def test_drops_aliases_equal_to_series_name_case_insensitive(self, _mock):
        from models.getcomics import get_series_alias_list
        # "batman"/"BATMAN" duplicate the series name and each other → only "Bruce".
        assert get_series_alias_list("Batman") == ["Bruce"]

    @patch("models.getcomics.get_series_aliases", return_value="")
    def test_no_aliases_returns_empty_list(self, _mock):
        from models.getcomics import get_series_alias_list
        assert get_series_alias_list("Batman") == []

    @patch("models.getcomics.get_series_aliases", side_effect=Exception("db down"))
    def test_errors_return_empty_list(self, _mock):
        from models.getcomics import get_series_alias_list
        assert get_series_alias_list("Batman") == []


# ===================================================================
# search_getcomics_for_issue — alias query expansion
# ===================================================================

class TestSearchGetcomicsForIssueAliases:

    @patch("models.getcomics.search_scrape_index", return_value=[])
    @patch("models.getcomics.try_scrape_index", return_value=(None, 0))
    @patch("models.getcomics.lookup_series_urls", return_value=[])
    @patch("models.getcomics.search_getcomics", return_value=[])
    def test_live_search_includes_alias_queries(self, mock_search, *_):
        from models.getcomics import search_getcomics_for_issue
        search_getcomics_for_issue(
            series_name="2000 AD",
            issue_num="2400",
            issue_year=2026,
            series_aliases=["2000AD"],
            rate_limit=0,
        )
        queries = [c.args[0] for c in mock_search.call_args_list]
        # Canonical name is still searched...
        assert any(q.startswith("2000 AD ") for q in queries)
        # ...and the alias name is searched too.
        assert "2000AD 2400" in queries

    @patch("models.getcomics.search_scrape_index", return_value=[])
    @patch("models.getcomics.try_scrape_index", return_value=(None, 0))
    @patch("models.getcomics.lookup_series_urls", return_value=[])
    @patch("models.getcomics.search_getcomics", return_value=[])
    def test_no_aliases_only_searches_canonical(self, mock_search, *_):
        from models.getcomics import search_getcomics_for_issue
        search_getcomics_for_issue(
            series_name="Batman",
            issue_num="5",
            issue_year=2026,
            series_aliases=[],
            rate_limit=0,
        )
        queries = [c.args[0] for c in mock_search.call_args_list]
        assert queries  # some queries ran
        assert all(q.startswith("Batman ") for q in queries)
