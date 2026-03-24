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
    def test_issue_range_disqualification(self, title, issue):
        from models.getcomics import score_getcomics_result
        score, _, _ = score_getcomics_result(title, "Batman", issue, 2020)
        assert score == -100

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

        # With accept_variants: TPB should be accepted
        score, range_hit, series_match = score_getcomics_result(
            "Batman - The Court of Owls TPB #1 (2020)", "Batman", "1", 2020,
            accept_variants=['tpb']
        )
        decision = accept_result(score, range_hit, series_match)
        # Score: series(30) + tight(-10) + issue(30) + year(20) + collected(-30) = 40
        assert decision == "ACCEPT"

    def test_quarterly_variant_acceptance(self):
        """Quarterly variant should be accepted when 'quarterly' is in accept_variants."""
        from models.getcomics import score_getcomics_result, accept_result
        # Without accept_variants: Quarterly should be rejected
        score, range_hit, series_match = score_getcomics_result(
            "Flash Gordon - Quarterly #5 (2025)", "Flash Gordon", "5", 2025
        )
        decision = accept_result(score, range_hit, series_match)
        # Quarterly is detected as variant, issue match blocked, -30 sub-series penalty
        # Score: series(30) - variant(30) + tight(-10) + year(20) = 10
        assert decision == "REJECT"

        # With accept_variants: Quarterly should be accepted
        score, range_hit, series_match = score_getcomics_result(
            "Flash Gordon - Quarterly #5 (2025)", "Flash Gordon", "5", 2025,
            accept_variants=['quarterly']
        )
        decision = accept_result(score, range_hit, series_match)
        # Score: series(30) + tight(-10) + issue(30) + year(20) = 70
        assert decision == "ACCEPT"

    def test_oneshot_variant_acceptance(self):
        """One-shot variant (o.s., os, oneshot) should be accepted when accepted."""
        from models.getcomics import score_getcomics_result, accept_result
        # "Batman - Year One OS #1" - OS/One-Shot variant
        score, range_hit, series_match = score_getcomics_result(
            "Batman - Year One OS #1 (2020)", "Batman", "1", 2020,
            accept_variants=['os']
        )
        decision = accept_result(score, range_hit, series_match)
        # Score: series(30) + tight(-10) + issue(30) + year(20) = 70
        assert decision == "ACCEPT"

        # "Batman - Year One One-Shot #1"
        score, range_hit, series_match = score_getcomics_result(
            "Batman - Year One One-Shot #1 (2020)", "Batman", "1", 2020,
            accept_variants=['oneshot']
        )
        decision = accept_result(score, range_hit, series_match)
        assert decision == "ACCEPT"

        # Without acceptance: should be rejected
        score, range_hit, series_match = score_getcomics_result(
            "Batman - Year One OS #1 (2020)", "Batman", "1", 2020
        )
        decision = accept_result(score, range_hit, series_match)
        assert decision == "REJECT"

    def test_omnibus_variant_acceptance(self):
        """Omnibus variant should be accepted when 'omni' or 'omnibus' is in accept_variants."""
        from models.getcomics import score_getcomics_result, accept_result
        # "Batman - The Dark Knight Omnibus #1 (2020)"
        score, range_hit, series_match = score_getcomics_result(
            "Batman - The Dark Knight Omnibus #1 (2020)", "Batman", "1", 2020,
            accept_variants=['omni']
        )
        decision = accept_result(score, range_hit, series_match)
        # Score: series(30) + tight(-10) + issue(30) + year(20) = 70
        assert decision == "ACCEPT"

    def test_hardcover_variant_acceptance(self):
        """Hardcover variant should be accepted when 'hardcover' is in accept_variants."""
        from models.getcomics import score_getcomics_result, accept_result
        # "Batman - The Long Halloween Hardcover #1 (2020)"
        score, range_hit, series_match = score_getcomics_result(
            "Batman - The Long Halloween Hardcover #1 (2020)", "Batman", "1", 2020,
            accept_variants=['hardcover']
        )
        decision = accept_result(score, range_hit, series_match)
        # Score: series(30) + tight(-10) + issue(30) + year(20) = 70
        assert decision == "ACCEPT"

    def test_deluxe_variant_acceptance(self):
        """Deluxe edition variant should be accepted when 'deluxe' is in accept_variants."""
        from models.getcomics import score_getcomics_result, accept_result
        # "Batman - No Man's Land Deluxe #1 (2020)"
        score, range_hit, series_match = score_getcomics_result(
            "Batman - No Man's Land Deluxe #1 (2020)", "Batman", "1", 2020,
            accept_variants=['deluxe']
        )
        decision = accept_result(score, range_hit, series_match)
        assert decision == "ACCEPT"

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
        """Annual variant with year in different position should still be detected."""
        from models.getcomics import score_getcomics_result, accept_result
        # "Batman 2025 Annual #1" - Annual after year
        score, range_hit, series_match = score_getcomics_result(
            "Batman 2025 Annual #1 (2025)", "Batman", "1", 2025,
            accept_variants=['annual']
        )
        decision = accept_result(score, range_hit, series_match)
        assert decision == "ACCEPT"

    def test_trade_paperback_variant(self):
        """Trade Paperback (full name) variant should be detected and accepted."""
        from models.getcomics import score_getcomics_result, accept_result
        score, range_hit, series_match = score_getcomics_result(
            "Batman - The Killing Joke Trade Paperback #1 (2020)", "Batman", "1", 2020,
            accept_variants=['trade paperback']
        )
        decision = accept_result(score, range_hit, series_match)
        assert decision == "ACCEPT"

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
