"""Plural variant keywords and annual add-ons in GetComics scoring (#554).

"Supergirl Vol. 4 #1 – 80 + Annuals" was rejected for every issue it holds,
while the same title with "+ Annual" was a range-pack fallback: the keyword
checks only knew the singular. Two neighbouring singular bugs are pinned here
too, because making the plural behave like the singular would have copied
them:

* "#1 – 50 + Annual" asked for #50 scored -100 -- the add-on was read as a
  different sub-series ending on the target;
* "Batman Annual #1 – 5" was a fallback for *regular* Batman #2, although
  annuals number their own issues.
"""

import pytest


def _decide(title, series, issue, issue_year=None, volume=None, volume_year=None):
    from models.getcomics import score_getcomics_result, accept_result
    result = score_getcomics_result(title, series, issue, issue_year,
                                    series_volume=volume, volume_year=volume_year)
    return accept_result(*result)


# ===================================================================
# Keyword forms
# ===================================================================

class TestKeywordForms:

    @pytest.mark.parametrize("keyword, plurals", [
        ("annual", ["annuals"]),
        ("quarterly", ["quarterlies"]),
        ("omnibus", ["omnibuses"]),
        ("tpb", ["tpbs"]),
        ("one-shot", ["one-shots"]),
        ("trade paperback", ["trade paperbacks"]),
        ("o.s.", []),
    ])
    def test_plurals(self, keyword, plurals):
        from models.getcomics import _keyword_plurals
        assert _keyword_plurals(keyword) == plurals

    @pytest.mark.parametrize("text, matches", [
        ("batman annual #1", True),
        ("supergirl #1 – 80 + annuals", True),
        ("the annualized report", False),
    ])
    def test_pattern_keeps_the_callers_word_boundaries(self, text, matches):
        import re
        from models.getcomics import _keyword_pattern
        pattern = rf'(?<![a-z]){_keyword_pattern("annual")}(?![a-z])'
        assert bool(re.search(pattern, text)) is matches

    def test_short_keyword_still_not_matched_mid_word(self):
        import re
        from models.getcomics import _keyword_pattern
        pattern = rf'(?<![a-z]){_keyword_pattern("os")}(?![a-z])'
        assert not re.search(pattern, "chaos #1")
        assert not re.search(pattern, "roses #1")

    @pytest.mark.parametrize("text, addon", [
        ("#1 – 80 + annuals (1996-2003)", True),
        ("#1-50 & annual", True),
        ("issues 1-12 + annuals", True),
        ("annual #1 – 5", False),        # the annual series itself
        ("+ annuals", False),            # nothing it is added to
        ("2021 annual #1", False),
    ])
    def test_publication_addon(self, text, addon):
        import re
        from models.getcomics import _is_publication_addon
        m = re.search(r'annuals?', text)
        assert _is_publication_addon(text, m) is addon


# ===================================================================
# Scoring: add-ons, annual series, and words that must not change
# ===================================================================

SUPERGIRL = "Supergirl Vol. 4 #1 – 80 + Annuals (1996-2003)"


class TestAnnualsAddOn:
    """A run of regular issues with the annuals thrown in is a range pack."""

    @pytest.mark.parametrize("issue, issue_year", [("1", 1996), ("20", 1997), ("80", 2003)])
    def test_supergirl_post_is_a_fallback_for_every_issue_it_holds(self, issue, issue_year):
        assert _decide(SUPERGIRL, "Supergirl", issue, issue_year, 4, 1996) == "FALLBACK"

    def test_issue_outside_the_range_is_still_rejected(self):
        assert _decide(SUPERGIRL, "Supergirl", "81", 2003, 4, 1996) == "REJECT"

    @pytest.mark.parametrize("title", [
        "Batman #1 – 50 + Annual (1940-1950)",
        "Batman #1 – 50 + Annuals (1940-1950)",
        "Batman #1 – 50 & Annuals (1940-1950)",
        "Batman #1 – 50 + Annuals #1-3 (1940-1950)",
    ])
    @pytest.mark.parametrize("issue", ["1", "20", "50"])
    def test_singular_and_plural_agree_across_the_range(self, title, issue):
        # "50" is the range end: the add-on used to trip the -100
        # "different sub-series ending on target" rule.
        assert _decide(title, "Batman", issue, None, None, 1940) == "FALLBACK"

    @pytest.mark.parametrize("volume_year", [1940, None])
    def test_plural_matches_singular_score(self, volume_year):
        from models.getcomics import score_getcomics_result
        singular = score_getcomics_result("Batman #1 – 50 + Annual (1940-1950)", "Batman", "20",
                                          1943, volume_year=volume_year)
        plural = score_getcomics_result("Batman #1 – 50 + Annuals (1940-1950)", "Batman", "20",
                                        1943, volume_year=volume_year)
        assert plural == singular

    def test_irregular_plural(self):
        assert _decide("Flash Gordon #1 – 12 + Quarterlies (2014)", "Flash Gordon",
                       "5", 2014, None, 2014) == "FALLBACK"

    def test_addon_range_does_not_serve_the_annual_series(self):
        # The range numbers are regular issues; nothing says which annuals.
        assert _decide("Batman #1 – 50 + Annuals (1940-1950)", "Batman Annual",
                       "2", 1962, None, 1961) == "REJECT"


class TestAnnualSeries:
    """The annual series numbers its own issues."""

    @pytest.mark.parametrize("title", [
        "Batman Annual #1 – 5 (1961-1965)",
        "Batman Annuals #1 – 5 (1961-1965)",
        "Batman Annual #1 – 5",
        "Batman Annuals #1 – 5",
        "Batman – Annuals #1 – 5",
    ])
    @pytest.mark.parametrize("issue_year, volume_year", [(1962, 1940), (1962, None), (None, None)])
    def test_annual_range_never_serves_a_regular_issue(self, title, issue_year, volume_year):
        assert _decide(title, "Batman", "2", issue_year, None, volume_year) == "REJECT"

    @pytest.mark.parametrize("title", [
        "Batman Annual #2 (2020)",
        "Batman Annual 2021 #1",
        "Batman 2021 Annual #1",
    ])
    def test_single_annual_is_not_a_regular_issue(self, title):
        issue = "2" if "#2" in title else "1"
        assert _decide(title, "Batman", issue, 2020 if issue == "2" else 2021, 3, 2016) == "REJECT"

    @pytest.mark.parametrize("title", [
        "Batman Annual #1 – 5 (1961-1965)",
        "Batman Annuals #1 – 5 (1961-1965)",
    ])
    def test_annual_series_search_takes_either_spelling(self, title):
        from models.getcomics import score_getcomics_result
        assert _decide(title, "Batman Annual", "2", 1962, None, 1961) == "FALLBACK"
        # No stray "s" left over to read as a different series.
        score, _, _ = score_getcomics_result(title, "Batman Annual", "2", 1962, volume_year=1961)
        assert score == 30

    def test_single_annual_for_the_annual_series(self):
        assert _decide("Batman Annual #2 (2020)", "Batman Annual", "2", 2020, None, 2017) == "ACCEPT"


class TestUnaffected:

    @pytest.mark.parametrize("title, series, issue, year, volume_year", [
        ("Absolute Batman #5 (2025)", "Absolute Batman", "5", 2025, 2024),
        ("Nightwing #117 - Absolute Power (2024)", "Nightwing", "117", 2024, 2016),
        ("Chaos #1 (2014)", "Chaos", "1", 2014, 2014),
        ("Batman #5 (2020)", "Batman", "5", 2020, 2016),
    ])
    def test_single_issues_still_accepted(self, title, series, issue, year, volume_year):
        assert _decide(title, series, issue, year, None, volume_year) == "ACCEPT"

    @pytest.mark.parametrize("title", [
        "Saga TPB Vol. 1 – 10 (2012-2022)",
        "Saga TPBs Vol. 1 – 10 (2012-2022)",
    ])
    def test_collections_still_rejected_for_an_issue(self, title):
        assert _decide(title, "Saga", "5", 2013, None, 2012) == "REJECT"


# ===================================================================
# Parsing and the collected-edition penalty
# ===================================================================

class TestParsingAndPenalty:

    @pytest.mark.parametrize("title, annual, quarterly", [
        ("Batman Annuals #1 – 5", True, False),
        ("Supergirl #1 – 80 + Annuals (1996-2003)", True, False),
        ("Flash Gordon Quarterlies #1 – 4", False, True),
        ("Batman #5 (2020)", False, False),
    ])
    def test_parse_flags_plural_publication_types(self, title, annual, quarterly):
        from models.getcomics import parse_result_title
        parsed = parse_result_title(title)
        assert (parsed.is_annual, parsed.is_quarterly) == (annual, quarterly)

    def test_normalize_series_name_flags_plural_annuals(self):
        from models.getcomics import normalize_series_name
        _, meta = normalize_series_name("Batman Annuals")
        assert meta.get("is_annual") is True

    def test_addon_is_not_a_collected_edition(self):
        from models.getcomics import _score_collected_edition
        assert _score_collected_edition(
            "supergirl #1 – 80 + annuals (1996-2003)", "supergirl", None, False) == 0

    def test_annuals_collection_is_still_a_collected_edition(self):
        from models.getcomics import _score_collected_edition
        assert _score_collected_edition("supergirl annuals (1996-2003)", "supergirl", None, False) == -30
