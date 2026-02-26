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
        score = score_getcomics_result(title, series, issue, year)
        assert score >= expected_min

    @pytest.mark.parametrize(
        "title, series, issue, year",
        [
            ("Batman #1 (2020)", "Batman", "1", 2020),
        ],
    )
    def test_max_score_is_95(self, title, series, issue, year):
        from models.getcomics import score_getcomics_result
        score = score_getcomics_result(title, series, issue, year)
        assert score == 95

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
        score = score_getcomics_result(title, "Batman", issue, 0)
        # Should get at least series(30) + issue(30) = 60
        assert score >= 60

    def test_standalone_number_lower_confidence(self):
        """A bare number without # prefix gets +20 instead of +30."""
        from models.getcomics import score_getcomics_result
        score_hash = score_getcomics_result("Batman #3", "Batman", "3", 0)
        score_bare = score_getcomics_result("Batman 3", "Batman", "3", 0)
        assert score_hash > score_bare

    def test_year_match_adds_points(self):
        from models.getcomics import score_getcomics_result
        with_year = score_getcomics_result("Batman #1 (2020)", "Batman", "1", 2020)
        without_year = score_getcomics_result("Batman #1", "Batman", "1", 2020)
        assert with_year - without_year == 20

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
        score = score_getcomics_result(title, "Batman", "1", 2020)
        clean_score = score_getcomics_result("Batman #1 (2020)", "Batman", "1", 2020)
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
    def test_issue_range_disqualification(self, title, issue):
        from models.getcomics import score_getcomics_result
        score = score_getcomics_result(title, "Batman", issue, 2020)
        assert score == -100

    def test_issue_range_not_disqualified_when_not_ending_match(self):
        """Range like #1-18 should NOT disqualify when looking for issue #5."""
        from models.getcomics import score_getcomics_result
        score = score_getcomics_result("Batman #1-18 (2020)", "Batman", "5", 2020)
        # Should not be -100 since issue 5 is not the range endpoint
        assert score != -100

    def test_title_tightness_bonus(self):
        """Tight title (few extra words) gets +15 bonus."""
        from models.getcomics import score_getcomics_result
        tight = score_getcomics_result("Batman #1 (2020)", "Batman", "1", 2020)
        # 30 (series) + 15 (tight) + 30 (issue) + 20 (year) = 95
        assert tight == 95

    def test_title_tightness_penalty(self):
        """Title with many extra words gets -20 penalty."""
        from models.getcomics import score_getcomics_result
        wordy = score_getcomics_result(
            "Batman #1 (2020) Special Limited Exclusive Variant Foil Cover",
            "Batman", "1", 2020,
        )
        tight = score_getcomics_result("Batman #1 (2020)", "Batman", "1", 2020)
        assert wordy < tight

    def test_standalone_number_rejected_after_volume(self):
        """Number preceded by 'Vol.' should not count as issue match."""
        from models.getcomics import score_getcomics_result
        score = score_getcomics_result("Batman Vol. 3", "Batman", "3", 0)
        hash_score = score_getcomics_result("Batman #3", "Batman", "3", 0)
        assert score < hash_score

    def test_leading_zeros_normalized(self):
        """Issue '001' should match title with '#1'."""
        from models.getcomics import score_getcomics_result
        score = score_getcomics_result("Batman #1", "Batman", "001", 0)
        assert score >= 60  # series(30) + issue(30)


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
