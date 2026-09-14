"""GetComics posts split into several downloads (#542).

Big packs are published as one post with a ``<li>`` per part, each carrying
its own provider buttons -- Supergirl Vol. 4 #1-80 is six ranges plus the
annuals. Reading such a page as a whole kept only the first part's buttons,
so only #1-15 was ever downloaded, and the nightly sweep then marked #1-80 as
covered.
"""

from unittest.mock import MagicMock, patch

from bs4 import BeautifulSoup


def _mock_response(html, status_code=200):
    resp = MagicMock()
    resp.text = html
    resp.status_code = status_code
    resp.headers = {}
    resp.raise_for_status = MagicMock()
    return resp


# Trimmed from https://getcomics.org/dc/supergirl-vol-4-1-80-extras/ : every
# provider sits behind an identical getcomics /dls/ redirector, so only the
# label says which is which. The notes <li> and the header nav must not
# become parts.
SPLIT_POST_HTML = """\
<html><head><title>Supergirl Vol. 4 #1 – 80 + Annuals (1996-2003) – GetComics</title></head>
<body>
<header><nav><ul><li><a href="https://ads.example.com/x">Buy Premium Now</a></li></ul></nav></header>
<article class="post-body"><section class="post-contents">
<p style="text-align: center;"><strong>Supergirl Vol. 4 #1 – 80 + Annuals (1996-2003)</strong></p>
<ul>
<li>Supergirl Vol. 4 #1 – 15 (1996-1997) (470 MB) <strong>:</strong><br/><strong>
<a href="https://getcomics.org/dls/tb1"><span style="color: #008000;">TERABOX</span></a> |
<a href="https://getcomics.org/dls/mg1"><span style="color: #800077;">Mega</span></a> |
<a href="https://getcomics.org/dls/mf1"><span style="color: #808080;">Mediafire</span></a> |
<a href="https://getcomics.org/dls/pd1"><span style="color: #ff9900;">PIXELDRAIN</span></a>
</strong></li>
<li>Supergirl Vol. 4 #16 – 28 (1997-1998) (465 MB) <strong>:</strong><br/><strong>
<a href="https://getcomics.org/dls/tb2"><span style="color: #008000;">TERABOX</span></a> |
<a href="https://getcomics.org/dls/mg2"><span style="color: #800077;">Mega</span></a> |
<a href="https://getcomics.org/dls/pd2"><span style="color: #ff9900;">PIXELDRAIN</span></a>
</strong></li>
<li>Supergirl Vol. 4 Annual #1 – 02 (1996-1997) (132 MB) <strong>:</strong><br/><strong>
<a href="https://getcomics.org/dls/mg3"><span style="color: #800077;">Mega</span></a>
</strong></li>
</ul>
<ul>
<li>If you have any difficulties to download the files, please <a href="https://getcomics.org/contact/">contact us</a>.</li>
</ul>
</section></article>
</body></html>
"""

# Trimmed from https://getcomics.org/other-comics/ginseng-roots-1-10-2019-2022/ :
# the post's own buttons are the #1-10 pack, and an "UPDATE:" list adds #11
# and #12. The sidebar's "The Omega Book" link reads as MEGA to the text
# tiers, and the collapsed "OLD LINKS" spoiler holds superseded buttons --
# neither is a download.
MIXED_POST_HTML = """\
<html><head><title>Ginseng Roots #1 - 12 (2019-2023) – GetComics</title></head>
<body>
<article class="post-body"><section class="post-contents">
<p>UPDATE:</p>
<ul>
<li>Ginseng Roots #11 (2022) (24 MB) : <a href="https://getcomics.org/dls/main11"><span>Main Server</span></a> |
<a href="https://getcomics.org/dls/pd11"><span>PIXELDRAIN</span></a></li>
<li>Ginseng Roots #12 (2023) (46 MB) : <a href="https://getcomics.org/dls/pd12"><span>PIXELDRAIN</span></a></li>
</ul>
<h2>Free Comics Download</h2>
<p style="text-align: center;"><strong>Ginseng Roots #1 – 10</strong><br/>
<strong>Language :</strong> English | <strong>Size :</strong> 1.2 GB</p>
<p><a class="aio-red" title="DOWNLOAD NOW" href="https://getcomics.org/dls/main1-10">DOWNLOAD NOW</a></p>
<p><a class="aio-purple" title="PIXELDRAIN" href="https://getcomics.org/dls/pd1-10">PIXELDRAIN</a></p>
<div class="su-spoiler su-spoiler-closed"><div class="su-spoiler-title">OLD LINKS</div>
<div class="su-spoiler-content"><p><a class="aio-orange" title="MEGA" href="https://mega.nz/file/old">MEGA</a></p></div></div>
<ul><li>If you have any difficulties to download the files, please <a href="https://getcomics.org/contact/">contact us</a>.</li></ul>
</section></article>
<aside class="page-sidebar"><ul><li><a href="https://getcomics.org/other-comics/the-omega-book-2/">The Omega Book #2 (2026)</a></li></ul></aside>
</body></html>
"""

SINGLE_SET_HTML = """\
<html><body><article class="post-body">
<a class="aio-red" title="PIXELDRAIN" href="https://getcomics.org/dls/only">PIXELDRAIN</a>
<a class="aio-red" title="DOWNLOAD NOW" href="https://getcomics.org/dls/main">DOWNLOAD NOW</a>
</article></body></html>
"""

# The mirrors of ONE comic listed as a <ul>, a <li> per provider. Every item
# holds a supported link, but none is a download of its own.
MIRROR_LIST_HTML = """\
<html><head><title>Batman #20 (2024) – GetComics</title></head>
<body><article class="post-body"><section class="post-contents">
<p style="text-align: center;"><strong>Batman #20 (2024)</strong><br/>
<strong>Language :</strong> English | <strong>Size :</strong> 48 MB</p>
<ul>
<li><a class="aio-red" title="DOWNLOAD NOW" href="https://getcomics.org/dls/main20">DOWNLOAD NOW</a></li>
<li><a class="aio-orange" title="MEGA" href="https://getcomics.org/dls/mg20">MEGA</a></li>
<li><a class="aio-purple" title="PIXELDRAIN" href="https://getcomics.org/dls/pd20">PIXELDRAIN</a></li>
</ul>
</section></article></body></html>
"""


def _part(label):
    return {"label": label, "links": {"pixeldrain": f"https://getcomics.org/dls/{label}"}}


# ===================================================================
# _extract_download_parts / get_download_parts
# ===================================================================

class TestExtractDownloadParts:

    def test_splits_post_into_labelled_parts(self):
        from models.getcomics import _extract_download_parts

        parts = _extract_download_parts(BeautifulSoup(SPLIT_POST_HTML, "html.parser"))

        assert [p["label"] for p in parts] == [
            "Supergirl Vol. 4 #1 – 15 (1996-1997)",
            "Supergirl Vol. 4 #16 – 28 (1997-1998)",
            "Supergirl Vol. 4 Annual #1 – 02 (1996-1997)",
        ]
        # Each part keeps its own buttons -- not the first part's.
        assert parts[0]["links"] == {"pixeldrain": "https://getcomics.org/dls/pd1",
                                     "download_now": None,
                                     "mega": "https://getcomics.org/dls/mg1"}
        assert parts[1]["links"]["pixeldrain"] == "https://getcomics.org/dls/pd2"
        assert parts[2]["links"] == {"pixeldrain": None, "download_now": None,
                                     "mega": "https://getcomics.org/dls/mg3"}

    def test_main_download_of_a_mixed_post_is_the_first_part(self):
        """The #1-10 pack must not be dropped in favour of the #11/#12 updates."""
        from models.getcomics import _extract_download_parts

        parts = _extract_download_parts(BeautifulSoup(MIXED_POST_HTML, "html.parser"))

        assert [p["label"] for p in parts] == [
            "Ginseng Roots #1 – 10",
            "Ginseng Roots #11 (2022)",
            "Ginseng Roots #12 (2023)",
        ]
        # Only the post's own buttons: not the OLD LINKS spoiler's Mega, and
        # not the sidebar's "The Omega Book".
        assert parts[0]["links"] == {"pixeldrain": "https://getcomics.org/dls/pd1-10",
                                     "download_now": "https://getcomics.org/dls/main1-10",
                                     "mega": None}
        assert parts[1]["links"]["pixeldrain"] == "https://getcomics.org/dls/pd11"

    def test_a_split_post_without_its_own_buttons_has_no_main_part(self):
        from models.getcomics import _extract_download_parts
        parts = _extract_download_parts(BeautifulSoup(SPLIT_POST_HTML, "html.parser"))
        assert parts[0]["label"] == "Supergirl Vol. 4 #1 – 15 (1996-1997)"

    def test_buttons_plus_one_update_item_is_split(self):
        """Main pack + a single "UPDATE:" issue is two downloads, not one."""
        from models.getcomics import _extract_download_parts
        html = MIXED_POST_HTML.replace(
            '<li>Ginseng Roots #12 (2023) (46 MB) : <a href="https://getcomics.org/dls/pd12">'
            '<span>PIXELDRAIN</span></a></li>', '')

        parts = _extract_download_parts(BeautifulSoup(html, "html.parser"))

        assert [p["label"] for p in parts] == ["Ginseng Roots #1 – 10", "Ginseng Roots #11 (2022)"]

    def test_single_set_of_buttons_is_not_split(self):
        from models.getcomics import _extract_download_parts
        assert _extract_download_parts(BeautifulSoup(SINGLE_SET_HTML, "html.parser")) == []

    def test_one_list_item_with_links_is_not_split(self):
        from models.getcomics import _extract_download_parts
        html = ('<html><body><article><ul><li>Batman #1 (2020) : '
                '<a href="https://pixeldrain.com/u/a">Main Server</a></li>'
                '</ul></article></body></html>')
        assert _extract_download_parts(BeautifulSoup(html, "html.parser")) == []

    def test_a_mirror_list_is_not_split(self):
        """Three mirrors of one comic are one download, not three parts."""
        from models.getcomics import _extract_download_parts
        assert _extract_download_parts(BeautifulSoup(MIRROR_LIST_HTML, "html.parser")) == []

    def test_a_list_item_with_no_title_of_its_own_is_not_a_part(self):
        """A text-less <li> is indistinguishable from a bare mirror button."""
        from models.getcomics import _extract_download_parts
        html = ('<html><body><article><ul>'
                '<li><a href="https://pixeldrain.com/u/a"><img src="x.png"/></a></li>'
                '<li><a href="https://pixeldrain.com/u/b"><img src="y.png"/></a></li>'
                '</ul></article></body></html>')
        assert _extract_download_parts(BeautifulSoup(html, "html.parser")) == []

    def test_numbered_mirror_buttons_are_not_parts(self):
        from models.getcomics import _extract_download_parts
        html = ('<html><body><article><ul>'
                '<li><a href="https://pixeldrain.com/u/a">Main Server 1</a></li>'
                '<li><a href="https://pixeldrain.com/u/b">Main Server 2</a></li>'
                '</ul></article></body></html>')
        assert _extract_download_parts(BeautifulSoup(html, "html.parser")) == []

    def test_a_comic_named_after_a_mirror_is_still_a_part(self):
        """A label with an #issue is a title, whatever words it is made of."""
        from models.getcomics import _extract_download_parts
        html = ('<html><body><article><ul>'
                '<li>Mirror #1 – 5 : <a href="https://pixeldrain.com/u/a">PIXELDRAIN</a></li>'
                '<li>Mirror #6 – 10 : <a href="https://pixeldrain.com/u/b">PIXELDRAIN</a></li>'
                '</ul></article></body></html>')
        parts = _extract_download_parts(BeautifulSoup(html, "html.parser"))
        assert [p["label"] for p in parts] == ["Mirror #1 – 5", "Mirror #6 – 10"]

    def test_a_title_that_contains_a_mirror_word_is_still_a_part(self):
        """Mirror words are subtracted, not matched: "Man" survives."""
        from models.getcomics import _extract_download_parts
        html = ('<html><body><article><ul>'
                '<li>Mega Man #1 – 10 : <a href="https://pixeldrain.com/u/a">PIXELDRAIN</a></li>'
                '<li>Mega Man #11 – 20 : <a href="https://pixeldrain.com/u/b">PIXELDRAIN</a></li>'
                '</ul></article></body></html>')
        parts = _extract_download_parts(BeautifulSoup(html, "html.parser"))
        assert [p["label"] for p in parts] == ["Mega Man #1 – 10", "Mega Man #11 – 20"]


class TestGetDownloadParts:

    @patch("models.getcomics.scraper")
    def test_returns_every_part_of_a_split_post(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(SPLIT_POST_HTML)
        from models.getcomics import get_download_parts

        parts = get_download_parts("https://getcomics.org/dc/supergirl-vol-4-1-80-extras/")

        assert len(parts) == 3
        assert mock_scraper.get.call_count == 1

    @patch("models.getcomics.scraper")
    def test_unsplit_post_is_one_unlabelled_part(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(SINGLE_SET_HTML)
        from models.getcomics import get_download_parts

        parts = get_download_parts("https://getcomics.org/batman-1")

        assert parts == [{"label": None, "links": {
            "pixeldrain": "https://getcomics.org/dls/only",
            "download_now": "https://getcomics.org/dls/main",
            "mega": None,
        }}]

    @patch("models.getcomics.scraper")
    def test_a_mirror_list_post_keeps_every_provider_on_one_part(self, mock_scraper):
        """The sweep matches #20 and downloads it, as on main -- not nothing."""
        mock_scraper.get.return_value = _mock_response(MIRROR_LIST_HTML)
        from models.getcomics import get_download_parts, select_parts_for_issue

        parts = get_download_parts("https://getcomics.org/dc/batman-20-2024/")

        assert parts == [{"label": None, "links": {
            "pixeldrain": "https://getcomics.org/dls/pd20",
            "download_now": "https://getcomics.org/dls/main20",
            "mega": "https://getcomics.org/dls/mg20",
        }}]
        chosen = select_parts_for_issue(parts, "20", "Batman")
        assert [p["links"] for p in chosen] == [parts[0]["links"]]

    @patch("models.getcomics.scraper")
    def test_failure_still_returns_one_empty_part(self, mock_scraper):
        mock_scraper.get.side_effect = Exception("Timeout")
        from models.getcomics import get_download_parts

        parts = get_download_parts("https://getcomics.org/fail", max_attempts=2)

        assert parts == [{"label": None, "links": {
            "pixeldrain": None, "download_now": None, "mega": None}}]

    @patch("models.getcomics.scraper")
    def test_get_download_links_is_the_first_part_only(self, mock_scraper):
        mock_scraper.get.return_value = _mock_response(SPLIT_POST_HTML)
        from models.getcomics import get_download_links

        links = get_download_links("https://getcomics.org/dc/supergirl-vol-4-1-80-extras/")

        # All from part one -- never a mix of one part's Pixeldrain and
        # another part's Mega.
        assert links == {"pixeldrain": "https://getcomics.org/dls/pd1",
                         "download_now": None,
                         "mega": "https://getcomics.org/dls/mg1"}


# ===================================================================
# select_parts_for_issue
# ===================================================================

SUPERGIRL_PARTS = [
    _part("Supergirl Vol. 4 #1 – 15 (1996-1997)"),
    _part("Supergirl Vol. 4 #16 – 28 (1997-1998)"),
    _part("Supergirl Vol. 4 #29 – 42 (1998-2000)"),
    _part("Supergirl Vol. 4 Annual #1 – 02 (1996-1997)"),
]


class TestSelectPartsForIssue:

    def _labels(self, parts):
        return [p["label"] for p in parts]

    def test_picks_the_part_whose_range_holds_the_issue(self):
        from models.getcomics import select_parts_for_issue

        chosen = select_parts_for_issue(SUPERGIRL_PARTS, "20", "Supergirl")

        assert self._labels(chosen) == ["Supergirl Vol. 4 #16 – 28 (1997-1998)"]
        assert chosen[0]["issue_range"] == (16, 28)
        assert chosen[0]["links"] == SUPERGIRL_PARTS[1]["links"]

    def test_range_bounds_are_inclusive(self):
        from models.getcomics import select_parts_for_issue
        assert self._labels(select_parts_for_issue(SUPERGIRL_PARTS, "15", "Supergirl")) == [
            "Supergirl Vol. 4 #1 – 15 (1996-1997)"]
        assert self._labels(select_parts_for_issue(SUPERGIRL_PARTS, "16", "Supergirl")) == [
            "Supergirl Vol. 4 #16 – 28 (1997-1998)"]

    def test_regular_issue_never_resolves_to_the_annuals(self):
        from models.getcomics import select_parts_for_issue
        assert self._labels(select_parts_for_issue(SUPERGIRL_PARTS, "1", "Supergirl")) == [
            "Supergirl Vol. 4 #1 – 15 (1996-1997)"]

    def test_annual_series_resolves_to_the_annuals(self):
        from models.getcomics import select_parts_for_issue
        assert self._labels(select_parts_for_issue(SUPERGIRL_PARTS, "2", "Supergirl Annual")) == [
            "Supergirl Vol. 4 Annual #1 – 02 (1996-1997)"]

    def test_no_part_holds_the_issue(self):
        """A neighbouring part is the wrong comics: download nothing."""
        from models.getcomics import select_parts_for_issue
        assert select_parts_for_issue(SUPERGIRL_PARTS, "50", "Supergirl") == []
        assert select_parts_for_issue(SUPERGIRL_PARTS, "1.MU", "Supergirl") == []

    def test_exact_issue_beats_a_range_and_narrow_beats_wide(self):
        from models.getcomics import select_parts_for_issue
        parts = [_part("Batman #1 – 50"), _part("Batman #1 – 12"), _part("Batman #007")]

        assert self._labels(select_parts_for_issue(parts, "7", "Batman")) == ["Batman #007"]
        assert self._labels(select_parts_for_issue(parts, "9", "Batman")) == ["Batman #1 – 12"]
        assert self._labels(select_parts_for_issue(parts, "40", "Batman")) == ["Batman #1 – 50"]

    def test_unnumbered_parts_are_never_picked(self):
        """A collection's volumes say nothing about which issues they hold, and
        taking all of them would download the whole collection for one issue."""
        from models.getcomics import select_parts_for_issue
        parts = [_part("Supergirl Vol. 1 – Power (2006)"), _part("Supergirl Vol. 2 – Candor (2007)")]

        assert select_parts_for_issue(parts, "1", "Supergirl") == []
        assert select_parts_for_issue(parts, "2", "Supergirl") == []

    def test_plus_annuals_range_still_serves_regular_issues(self):
        """"#104-150 + Annuals" is regular issues with the annuals added on."""
        from models.getcomics import select_parts_for_issue
        parts = [
            _part("Teenage Mutant Ninja Turtles #96 – 103 (2019-2020)"),
            _part("Teenage Mutant Ninja Turtles #104-150 + Annuals (2020-2024)"),
            _part("Teenage Mutant Ninja Turtles – Annual 2012 Deluxe Edition"),
        ]

        assert self._labels(select_parts_for_issue(parts, "120", "Teenage Mutant Ninja Turtles")) == [
            "Teenage Mutant Ninja Turtles #104-150 + Annuals (2020-2024)"]
        # Nor does an annual series take a regular run just because it has
        # annuals added on.
        assert select_parts_for_issue(parts, "120", "Teenage Mutant Ninja Turtles Annual") == []

    def test_mixed_post_main_pack_and_updates(self):
        from models.getcomics import _extract_download_parts, select_parts_for_issue
        parts = _extract_download_parts(BeautifulSoup(MIXED_POST_HTML, "html.parser"))

        assert self._labels(select_parts_for_issue(parts, "5", "Ginseng Roots")) == ["Ginseng Roots #1 – 10"]
        assert self._labels(select_parts_for_issue(parts, "11", "Ginseng Roots")) == ["Ginseng Roots #11 (2022)"]
        assert select_parts_for_issue(parts, "13", "Ginseng Roots") == []

    def test_unsplit_post_is_returned_unchanged(self):
        from models.getcomics import select_parts_for_issue
        parts = [{"label": None, "links": {"pixeldrain": "https://pixeldrain.com/u/a"}}]

        assert select_parts_for_issue(parts, "3", "Batman") == [
            {"label": None, "links": {"pixeldrain": "https://pixeldrain.com/u/a"},
             "issue_range": None}]

    def test_does_not_mutate_the_input(self):
        from models.getcomics import select_parts_for_issue
        select_parts_for_issue(SUPERGIRL_PARTS, "20", "Supergirl")
        assert all("issue_range" not in p for p in SUPERGIRL_PARTS)


# ===================================================================
# get_result_parts
# ===================================================================

class TestGetResultParts:

    @patch("models.getcomics.get_download_parts")
    def test_prefers_parts_already_scraped(self, mock_fetch):
        from models.getcomics import get_result_parts
        parts = [_part("A #1 – 5"), _part("A #6 – 10")]

        assert get_result_parts({"link": "u", "links": {"pixeldrain": "x"}, "parts": parts}) == parts
        mock_fetch.assert_not_called()

    @patch("models.getcomics.get_download_parts")
    def test_reuses_the_matched_entry_links(self, mock_fetch):
        """A listing-page entry must not be re-read page-wide (first entry wins)."""
        from models.getcomics import get_result_parts

        assert get_result_parts({"link": "u#entry", "links": {"pixeldrain": "x"}, "parts": []}) == [
            {"label": None, "links": {"pixeldrain": "x"}}]
        mock_fetch.assert_not_called()

    @patch("models.getcomics.get_download_parts", return_value=["fetched"])
    def test_fetches_when_nothing_was_scraped(self, mock_fetch):
        from models.getcomics import get_result_parts

        assert get_result_parts({"link": "https://getcomics.org/x", "download_url": ""}) == ["fetched"]
        mock_fetch.assert_called_once_with("https://getcomics.org/x")


# ===================================================================
# scrape_and_score_candidate / _extract_content_li_entries
# ===================================================================

class TestSplitPostInOtherReaders:

    @patch("models.getcomics.score_getcomics_result", return_value=(60, False, True))
    @patch("models.getcomics.scraper")
    def test_page_level_candidate_carries_its_parts(self, mock_scraper, _score):
        mock_scraper.get.return_value = _mock_response(SPLIT_POST_HTML)
        from models.getcomics import scrape_and_score_candidate

        result, _ = scrape_and_score_candidate(
            "https://getcomics.org/dc/supergirl-vol-4-1-80-extras/", "Supergirl", "20", 1997)

        assert len(result["parts"]) == 3
        assert result["parts"][1]["links"]["pixeldrain"] == "https://getcomics.org/dls/pd2"

    def test_index_entries_recognise_dls_wrapped_providers(self):
        """Every link here is a getcomics /dls/ redirector; they used to be
        skipped as 'getcomics' links, storing each part without a URL."""
        from models.getcomics import _extract_content_li_entries

        entries = dict(_extract_content_li_entries(BeautifulSoup(SPLIT_POST_HTML, "html.parser")))

        assert entries["Supergirl Vol. 4 #1 – 15 (1996-1997) (470 MB)"] == "https://getcomics.org/dls/pd1"
        assert entries["Supergirl Vol. 4 #16 – 28 (1997-1998) (465 MB)"] == "https://getcomics.org/dls/pd2"
        assert entries["Supergirl Vol. 4 Annual #1 – 02 (1996-1997) (132 MB)"] == "https://getcomics.org/dls/mg3"
        assert not any("Buy Premium" in t for t in entries)

    def test_download_filename(self):
        from models.getcomics import download_filename
        assert download_filename("Supergirl Vol. 4 #16 – 28 (1997-1998)") == \
            "Supergirl Vol. 4 16 – 28 (1997-1998).cbz"
        assert download_filename("Batman/Superman #1") == "Batman-Superman 1.cbz"
