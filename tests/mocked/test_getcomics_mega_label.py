"""Only a real Mega link fills the Mega slot (#556).

The label tiers matched "MEGA" as a substring, and GetComics' sidebar lists
recent posts on every page: on the day it showed "The Omega Book #2", 82 of
125 sampled posts got that post's page as their Mega download. Two guards,
each tested on its own: "MEGA" must be a whole word, and a link to an ordinary
getcomics page is never a provider, whatever its text says.
"""

import pytest
from bs4 import BeautifulSoup


def _links(html):
    from models.getcomics import _extract_download_links
    return _extract_download_links(BeautifulSoup(html, "html.parser"))


# Shaped like a real post (trimmed): a Pixeldrain button and no Mega one in
# the content; the sidebar and the footer both link to "The Omega Book #2".
POST_WITHOUT_MEGA = """\
<html><body>
<aside class="page-sidebar"><section class="widget widget_recent_entries"><ul>
<li><a href="https://getcomics.org/other-comics/the-omega-book-2-2026/">The Omega Book #2 (2026)</a></li>
</ul></section></aside>
<article class="post-body"><section class="post-contents">
<p><a class="aio-purple" title="PIXELDRAIN" href="https://getcomics.org/dls/pd-token">PIXELDRAIN</a></p>
<p>Tags: <a href="https://getcomics.org/tag/the-omega-book/">The Omega Book</a></p>
</section></article>
<footer><a href="https://getcomics.org/other-comics/the-omega-book-2-2026/">Next Post The Omega Book #2 (2026)</a></footer>
</body></html>
"""


class TestNoBogusMega:

    def test_sidebar_tag_and_footer_omega_links_are_not_mega(self):
        links = _links(POST_WITHOUT_MEGA)
        assert links["mega"] is None
        assert links["pixeldrain"] == "https://getcomics.org/dls/pd-token"

    def test_real_mega_button_is_kept_even_after_the_sidebar(self):
        html = POST_WITHOUT_MEGA.replace(
            '<p><a class="aio-purple"',
            '<p><a class="aio-orange" title="MEGA" href="https://getcomics.org/dls/mega-token">MEGA</a></p>\n'
            '<p><a class="aio-purple"')
        assert _links(html)["mega"] == "https://getcomics.org/dls/mega-token"

    @pytest.mark.parametrize("anchor", [
        '<a class="aio-orange" title="MEGA" href="https://getcomics.org/dls/m">MEGA</a>',   # tier 1
        '<a title="Mega Link" href="https://getcomics.org/dls/m">Mega Link</a>',            # tier 1
        '<a class="aio-red" href="https://getcomics.org/dls/m">MEGA</a>',                   # tier 2
        '<a href="https://getcomics.org/dls/m"><span>Mega</span></a>',                      # tier 3
        '<a href="https://mega.nz/file/m">Click here</a>',                                  # tier 4
    ])
    def test_every_real_label_seen_still_matches(self, anchor):
        assert _links(f"<html><body><article>{anchor}</article></body></html>")["mega"] is not None

    @pytest.mark.parametrize("label", ["MEGA", "Mega", "mega", "mEgA", "mega link", "MEGA.nz",
                                       "Download from Mega"])
    @pytest.mark.parametrize("where", ["title", "text"])
    def test_any_letter_case(self, label, where):
        attr = f' title="{label}"' if where == "title" else ""
        body = "x" if where == "title" else f"<span>{label}</span>"
        html = f'<html><body><article><a{attr} href="https://getcomics.org/dls/m">{body}</a></article></body></html>'
        assert _links(html)["mega"] == "https://getcomics.org/dls/m"

    @pytest.mark.parametrize("label", ["Omega", "OMEGA", "The Omega Book #2 (2026)", "Megaupload"])
    def test_mega_inside_another_word_is_not_mega(self, label):
        html = f'<html><body><article><a href="https://ads.example.com/x">{label}</a></article></body></html>'
        assert _links(html)["mega"] is None


class TestEachGuardOnItsOwn:

    def test_whole_word_guard_rejects_omega_on_any_host(self):
        # Not a getcomics page, so only the whole-word rule can stop it.
        html = ('<html><body><article>'
                '<a title="Omega Watches" href="https://ads.example.com/omega">Omega Watches</a>'
                '<a href="https://ads.example.com/omega2">OMEGA SALE</a>'
                '</article></body></html>')
        assert _links(html)["mega"] is None

    def test_page_guard_rejects_navigation_even_with_mega_as_a_word(self):
        # "Mega" is a whole word here, so only the page rule can stop it.
        html = ('<html><body><aside><ul>'
                '<li><a href="https://getcomics.org/other-comics/mega-man-1-2026/">Mega Man #1 (2026)</a></li>'
                '</ul></aside></body></html>')
        assert _links(html)["mega"] is None

    def test_page_guard_keeps_other_providers_real_hosts(self):
        html = ('<html><body><article>'
                '<a class="aio-red" title="Download Now" href="https://light.getcomics.info/Comics/x.zip">Download Now</a>'
                '<a title="PIXELDRAIN" href="https://getcomics.org/dlds/pd">PIXELDRAIN</a>'
                '</article></body></html>')
        links = _links(html)
        assert links["download_now"] == "https://light.getcomics.info/Comics/x.zip"
        assert links["pixeldrain"] == "https://getcomics.org/dlds/pd"


class TestIsGetcomicsPage:

    @pytest.mark.parametrize("href", [
        "https://getcomics.org/other-comics/the-omega-book-2-2026/",
        "https://getcomics.org/tag/the-omega-book/",
        "https://getcomics.org/cat/dc/",
        "https://getcomics.org/",
        "https://www.getcomics.org/dc/batman-1/",
    ])
    def test_site_pages(self, href):
        from models.getcomics import _is_getcomics_page
        assert _is_getcomics_page(href)

    @pytest.mark.parametrize("href", [
        "https://getcomics.org/dls/token==:x==",
        "https://getcomics.org/dlds/token",
        "https://pixeldrain.com/u/abc",
        "https://mega.nz/file/abc",
        "https://light.getcomics.info/Comics/x.zip",
        "https://getcomics.org.evil.com/dc/batman-1/",   # look-alike host is not getcomics
        "/dls/relative",
    ])
    def test_not_site_pages(self, href):
        from models.getcomics import _is_getcomics_page
        assert not _is_getcomics_page(href)
