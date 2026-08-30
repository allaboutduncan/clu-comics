"""Tests for the GCD search-variation builders in models/gcd.py.

These are pure functions -- no database needed -- so they live in unit tests.
"""
import re

import pytest

from models.gcd import (
    generate_search_variations,
    lookahead_regex,
    normalize_title,
    rank_key,
    tokens_for_all_match,
)


class TestNormalizeTitle:
    def test_lowercases_and_strips_punctuation(self):
        assert normalize_title("Superman: The Secret Years!") == "superman the secret years"

    def test_collapses_whitespace(self):
        assert normalize_title("Batman   -   Detective") == "batman detective"


class TestTokensForAllMatch:
    def test_drops_english_stopwords(self):
        _, toks = tokens_for_all_match("Superman and the Secret Years")
        assert toks == ["superman", "secret", "years"]

    def test_keeps_non_stopwords_in_order(self):
        _, toks = tokens_for_all_match("Le Grandi Storie Walt Disney")
        assert toks[0] == "le"


class TestLookaheadRegex:
    """The escaping here decides whether the `tokenized` variation works at all."""

    def test_emits_a_real_word_boundary_not_a_literal_backslash(self):
        pattern = lookahead_regex(["superman", "secret"])
        assert r"\\b" not in pattern
        assert r"\b" in pattern

    def test_matches_a_series_containing_every_token(self):
        pattern = lookahead_regex(["superman", "secret", "years"])
        assert re.search(pattern, "superman: the secret years")

    def test_matches_regardless_of_token_order(self):
        pattern = lookahead_regex(["years", "superman"])
        assert re.search(pattern, "superman: the secret years")

    def test_does_not_match_when_a_token_is_missing(self):
        pattern = lookahead_regex(["superman", "secret", "decades"])
        assert not re.search(pattern, "superman: the secret years")

    def test_word_boundary_rejects_a_substring_hit(self):
        """`bat` must not match `batman` -- that is the point of the boundary."""
        pattern = lookahead_regex(["bat"])
        assert not re.search(pattern, "batman")
        assert re.search(pattern, "bat out of hell")

    def test_empty_tokens_fall_back_to_match_all(self):
        assert lookahead_regex([]) == r".*"

    def test_regex_metacharacters_in_a_token_are_escaped(self):
        """`tokens_for_all_match` normalises these away, but the escaping must
        still hold so a stray token cannot blow up the query."""
        pattern = lookahead_regex(["c++"])
        re.compile(pattern)          # must not raise
        assert r"c\+\+" in pattern


class TestGenerateSearchVariations:
    def test_exact_is_always_first(self):
        variations = generate_search_variations("Batman")
        assert variations[0] == ("exact", "%Batman%")

    def test_tokenized_offered_for_multi_token_titles(self):
        names = [t for t, _ in generate_search_variations("Superman The Secret Years")]
        assert "tokenized" in names

    def test_tokenized_not_offered_for_a_single_token_title(self):
        names = [t for t, _ in generate_search_variations("Topomistery")]
        assert "tokenized" not in names

    def test_tokenized_pattern_matches_the_real_series_name(self):
        """The regression that mattered: this returned nothing for every title."""
        pattern = dict(generate_search_variations("Superman The Secret Years"))["tokenized"]
        assert re.search(pattern, "superman: the secret years")

    def test_year_labels_the_main_word_variation(self):
        with_year = [t for t, _ in generate_search_variations("Batman Detective", "2016")]
        without = [t for t, _ in generate_search_variations("Batman Detective")]
        assert "main_with_year" in with_year
        assert "main_only" in without


class TestMainWordGuardConstants:
    """The fallback guard is a policy the rest of the module depends on."""

    def test_only_the_main_word_variations_are_guarded(self):
        from models.gcd import MAIN_WORD_VARIATIONS
        assert MAIN_WORD_VARIATIONS == {"main_only", "main_with_year"}

    def test_every_guarded_name_is_a_variation_the_builder_can_emit(self):
        from models.gcd import MAIN_WORD_VARIATIONS
        emitted = set()
        for title, year in [("Batman Detective", "2016"), ("Batman Detective", None)]:
            emitted.update(t for t, _ in generate_search_variations(title, year))
        assert MAIN_WORD_VARIATIONS <= emitted

    def test_candidate_cap_is_a_positive_int(self):
        from models.gcd import MAIN_WORD_MAX_CANDIDATES
        assert isinstance(MAIN_WORD_MAX_CANDIDATES, int)
        assert MAIN_WORD_MAX_CANDIDATES > 0


class TestYearConstrainedVariations:
    """`main_with_year` was named for a year it never applied."""

    def test_main_with_year_is_year_constrained(self):
        from models.gcd import YEAR_CONSTRAINED_VARIATIONS
        assert "main_with_year" in YEAR_CONSTRAINED_VARIATIONS

    def test_main_only_is_not(self):
        """There is no year to constrain by when the filename carried none."""
        from models.gcd import YEAR_CONSTRAINED_VARIATIONS
        assert "main_only" not in YEAR_CONSTRAINED_VARIATIONS

    def test_tokenized_is_not(self):
        """It runs through the REGEXP branch, which has no year clause."""
        from models.gcd import YEAR_CONSTRAINED_VARIATIONS
        assert "tokenized" not in YEAR_CONSTRAINED_VARIATIONS

    def test_every_constrained_name_is_a_variation_the_builder_can_emit(self):
        from models.gcd import YEAR_CONSTRAINED_VARIATIONS
        emitted = set()
        for title in ["Batman Detective 001", "Batman - Detective (2016)"]:
            emitted.update(t for t, _ in generate_search_variations(title, "2016"))
        assert YEAR_CONSTRAINED_VARIATIONS <= emitted


def series(id, name, year_began=None):
    """A candidate row as the dict row factory returns it."""
    return {"id": id, "name": name, "year_began": year_began}


def best(query, year, candidates):
    return min(candidates, key=lambda c: rank_key(query, year, c))


class TestRankKey:
    """The selection rule: best candidate, not newest survivor."""

    def test_exact_name_beats_a_longer_name_containing_it(self):
        """The defect in #519: `exact` is an unanchored LIKE, so a longer
        series name qualifies and, being newer, used to win."""
        right = series(1, "Superman", 2000)
        wrong = series(2, "Mann and Superman", 2000)
        assert best("Superman", 2000, [wrong, right]) is right

    def test_newer_containing_name_does_not_win(self):
        """`Saga 012 (2013)` used to match a longer 2013 series."""
        right = series(1, "Saga", 2012)
        wrong = series(2, "Michael Turner's Fathom: The Elite Saga", 2013)
        assert best("Saga", 2013, [wrong, right]) is right

    def test_deluxe_edition_does_not_swallow_the_base_series(self):
        right = series(1, "Action Comics", 1938)
        wrong = series(2, "Superman: Action Comics: The Oz Effect - Deluxe Edition", 2018)
        assert best("Action Comics", 2018, [wrong, right]) is right

    def test_fewer_extra_words_wins_when_neither_is_exact(self):
        closer = series(1, "The Green Hornet", 1989)
        further = series(2, "The Green Hornet: Year One Special", 1989)
        assert best("Green Hornet", 1989, [further, closer]) is closer

    def test_year_only_breaks_ties_between_equally_good_names(self):
        """Two records with the same name: the parsed year decides."""
        right = series(1, "Nathan Never", 1991)
        wrong = series(2, "Nathan Never", 2020)
        assert best("Nathan Never", 1993, [wrong, right]) is right

    def test_year_does_not_override_name_agreement(self):
        """A perfect-year wrong name still loses to an exact name."""
        right = series(1, "Diabolik", 1962)
        wrong = series(2, "Il grande Diabolik", 2014)
        assert best("Diabolik", 2014, [wrong, right]) is right

    def test_missing_year_is_not_treated_as_year_zero(self):
        """An undated series must not outrank a real candidate."""
        right = series(1, "Topolino", 1949)
        undated = series(2, "Topolino", None)
        assert best("Topolino", 1950, [undated, right]) is right

    def test_no_parsed_year_still_ranks_by_name(self):
        right = series(1, "Dylan Dog", 1986)
        wrong = series(2, "Dylan Dog Color Fest", 2007)
        assert best("Dylan Dog", None, [wrong, right]) is right

    def test_string_year_is_accepted(self):
        """The interactive loop carries the year as the string it parsed."""
        right = series(1, "Zagor", 1961)
        wrong = series(2, "Zagor", 2015)
        assert best("Zagor", "1965", [wrong, right]) is right

    def test_unparseable_year_ties_instead_of_raising(self):
        a, b = series(1, "Tex", 1948), series(2, "Tex", 1990)
        assert best("Tex", "n/a", [b, a]) is a

    def test_ranking_ignores_punctuation_and_case(self):
        right = series(1, "Batman: Detective Comics - Rebirth", 2016)
        wrong = series(2, "Batman: Detective Comics - Rebirth Deluxe", 2016)
        assert best("batman detective comics rebirth", 2016, [wrong, right]) is right

    def test_order_is_total_so_input_order_cannot_change_the_winner(self):
        import itertools
        cands = [series(1, "Superman", 2000),
                 series(2, "Mann and Superman", 2000),
                 series(3, "Superman Family", 2000)]
        winners = {best("Superman", 2000, list(p))["id"]
                   for p in itertools.permutations(cands)}
        assert winners == {1}

    def test_id_is_the_final_tiebreak(self):
        """Two identical records differ only by id; the lower one wins."""
        a, b = series(7, "Alan Ford", 1969), series(9, "Alan Ford", 1969)
        assert best("Alan Ford", 1969, [b, a]) is a

