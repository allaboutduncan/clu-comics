"""A GetComics result is accepted only with evidence for the wanted issue (#729).

A bare series title ("Empyre") scored 45 on name + tightness alone, above
ACCEPT_THRESHOLD, so it was a "direct match" for every missing issue of the
series. The bare titles were CLU's own doing: the sitemap candidate reader cut
each page title at its first dash, discarding the tie-in name and the issue
number before anything was scored.
"""
from unittest.mock import MagicMock, patch

import pytest

from models.getcomics import (
    ACCEPT_THRESHOLD,
    _strip_site_suffix,
    accept_result,
    score_comic,
    score_getcomics_result,
    scrape_and_score_candidate,
    search_criteria,
)


def _decide(title, series, issue, year=None, volume=None, volume_year=None):
    result = score_getcomics_result(
        title, series, issue, year, series_volume=volume, volume_year=volume_year)
    return accept_result(*result)


class TestBareTitleIsNotAnIssue:

    @pytest.mark.parametrize("title,series,issue,year,volume,volume_year", [
        ("Empyre", "Empyre", "3", None, 1, 2020),
        ("Astonishing X-Men", "Astonishing X-Men", "7", None, 3, 2004),
        ("Eternals", "Eternals", "6", 2021, 1, 2021),
        ("Ghost Rider", "Ghost Rider", "4", None, 1, 2016),
        ("Green Lantern (2011)", "Green Lantern", "41", 2015, 5, 2011),
        # A matching year does not make a bare page issue 3.
        ("Empyre (2020)", "Empyre", "3", 2020, 1, 2020),
        ("Batman Vol. 3", "Batman", "3", None, 3, None),
    ])
    def test_rejected(self, title, series, issue, year, volume, volume_year):
        assert _decide(title, series, issue, year, volume, volume_year) == "REJECT"

    def test_score_stays_below_the_threshold(self):
        score, is_range, series_match, issue_matched = score_getcomics_result(
            "Empyre", "Empyre", "3", None, series_volume=1, volume_year=2020,
            return_issue_matched=True)
        assert series_match and not issue_matched and not is_range
        assert score < ACCEPT_THRESHOLD

    @pytest.mark.parametrize("title,series,issue,year,volume,volume_year", [
        ("Empyre #3 (2020)", "Empyre", "3", 2020, 1, 2020),
        ("Astonishing X-Men #7 (2004)", "Astonishing X-Men", "7", 2004, 3, 2004),
        # "Vol. N" before the number is a volume marker, not another series.
        ("Catwoman Vol. 2 #5 (1994)", "Catwoman", "5", 1994, 2, 1993),
    ])
    def test_a_title_naming_the_issue_is_still_accepted(
            self, title, series, issue, year, volume, volume_year):
        assert _decide(title, series, issue, year, volume, volume_year) == "ACCEPT"

    def test_the_wrong_volume_is_not_accepted(self):
        assert _decide("Catwoman Vol. 3 #5 (2002)", "Catwoman", "5", 1994, 2, 1993) == "REJECT"


class TestTieInsAndOtherSeries:

    @pytest.mark.parametrize("title,series,issue,year,volume,volume_year", [
        ("Astonishing X-Men - Infinity Comic #5 - 7 (2022)", "Astonishing X-Men", "7", None, 3, 2004),
        ("Empyre - Avengers #1 - 3 (2020)", "Empyre", "3", 2020, 1, 2020),
        ("Eternals - Thanos Rises #1 (2021)", "Eternals", "6", 2021, 1, 2021),
        ("Ghost Rider - Danny Ketch Omnibus (2016)", "Ghost Rider", "4", 2016, 1, 2016),
        # The trailing "+ Annual" must not excuse the leading "Corps".
        ("Green Lantern Corps #0 - 37 + Annual (2012-2015)", "Green Lantern", "0", 2012, 5, 2011),
        ("Green Lantern Corps #0 – 37 + Annual (2012-2015)", "Green Lantern", "12", 2012, 5, 2011),
    ])
    def test_rejected(self, title, series, issue, year, volume, volume_year):
        assert _decide(title, series, issue, year, volume, volume_year) == "REJECT"

    @pytest.mark.parametrize("title,series,issue,volume,volume_year", [
        ("Catwoman Vol. 2 #0 – 94 + Annuals", "Catwoman", "12", 2, 1993),
        ("Detective Comics Vol. 2 #13 – 22", "Detective Comics", "15", 2, 2011),
        ("Batman #1-50 + Annuals", "Batman", "50", None, None),
    ])
    def test_same_series_range_packs_stay_fallbacks(
            self, title, series, issue, volume, volume_year):
        assert _decide(title, series, issue, None, volume, volume_year) == "FALLBACK"


class TestUnnumberedOneShot:
    """The one exception: a one-shot posted under its name and year alone."""

    SERIES = "Batman: The Killing Joke"

    def _score(self, title, issue, year=2008, volume_year=2008):
        return score_comic(title, search_criteria(
            series_name=self.SERIES, issue_number=issue, year=year, volume_year=volume_year))

    def test_accepted_for_issue_one(self):
        cs = self._score("Batman - The Killing Joke (2008)", "1")
        assert cs.unnumbered_oneshot and not cs.issue_matched
        assert _decide("Batman - The Killing Joke (2008)", self.SERIES, "1", 2008, None, 2008) == "ACCEPT"

    def test_never_for_another_issue(self):
        assert not self._score("Batman - The Killing Joke (2008)", "2").unnumbered_oneshot
        assert _decide("Batman - The Killing Joke (2008)", self.SERIES, "2", 2008, None, 2008) == "REJECT"

    def test_needs_the_year_in_the_title(self):
        assert not self._score("Batman - The Killing Joke", "1").unnumbered_oneshot
        assert _decide("Batman - The Killing Joke", self.SERIES, "1", 2008, None, 2008) == "REJECT"

    def test_needs_a_known_year_to_compare(self):
        cs = self._score("Batman - The Killing Joke (2008)", "1", year=None, volume_year=None)
        assert not cs.unnumbered_oneshot

    def test_a_different_year_is_not_this_edition(self):
        assert not self._score("Batman - The Killing Joke (2019)", "1").unnumbered_oneshot

    def test_one_extra_word_is_another_book(self):
        assert not self._score("Batman - The Killing Joke Sketchbook (2008)", "1").unnumbered_oneshot
        assert _decide("Batman - The Killing Joke Deluxe (2008)", self.SERIES, "1", 2008, None, 2008) == "REJECT"


class TestStripSiteSuffix:

    @pytest.mark.parametrize("raw,expected", [
        ("Astonishing X-Men – Infinity Comic #5 – 7 (2022) – GetComics",
         "Astonishing X-Men - Infinity Comic #5 - 7 (2022)"),
        ("Batman #20 (2024) – GetComics", "Batman #20 (2024)"),
        ("Supergirl Vol. 4 #1 – 80 + Annuals (1996-2003) - GetComics",
         "Supergirl Vol. 4 #1 - 80 + Annuals (1996-2003)"),
        ("Ginseng Roots #1 - 12 (2019-2023)", "Ginseng Roots #1 - 12 (2019-2023)"),
        ("Empyre — Avengers #1 | GetComics", "Empyre - Avengers #1"),
        ("", ""),
    ])
    def test_only_the_site_suffix_goes(self, raw, expected):
        assert _strip_site_suffix(raw) == expected


def _page(title):
    resp = MagicMock()
    resp.status_code = 200
    resp.text = f"""
<html><head><title>{title} – GetComics</title></head><body>
<section class="post-contents">
  <div class="aio-pulse"><a href="https://getcomics.org/dls/pd1" title="PIXELDRAIN">PIXELDRAIN</a></div>
</section></body></html>"""
    return resp


class TestSitemapCandidateTitle:
    """The candidate reader must score the whole title, not its first segment."""

    @patch("models.getcomics.scraper")
    def test_a_tie_in_page_is_not_the_series(self, mock_scraper):
        mock_scraper.get.return_value = _page("Astonishing X-Men – Infinity Comic #5 – 7 (2022)")
        assert scrape_and_score_candidate(
            "https://getcomics.org/marvel/astonishing-x-men-infinity-comic-5-7/",
            "Astonishing X-Men", "7", None, series_volume=3, volume_year=2004) is None

    @patch("models.getcomics.scraper")
    def test_a_range_pack_is_not_its_first_issue(self, mock_scraper):
        mock_scraper.get.return_value = _page("Supergirl Vol. 4 #1 – 80 + Annuals (1996-2003)")
        assert scrape_and_score_candidate(
            "https://getcomics.org/dc/supergirl-vol-4-1-80/",
            "Supergirl", "1", 1996, series_volume=4, volume_year=1996) is None

    @patch("models.getcomics.scraper")
    def test_the_issue_page_is_still_found(self, mock_scraper):
        mock_scraper.get.return_value = _page("Astonishing X-Men #7 (2004)")
        found = scrape_and_score_candidate(
            "https://getcomics.org/marvel/astonishing-x-men-7-2004/",
            "Astonishing X-Men", "7", 2004, series_volume=3, volume_year=2004)
        assert found is not None
        result, score = found
        assert result["title"] == "Astonishing X-Men #7 (2004)"
        assert score >= ACCEPT_THRESHOLD
