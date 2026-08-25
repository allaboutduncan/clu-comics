"""Tests for the GCD search-variation builders in models/gcd.py.

These are pure functions -- no database needed -- so they live in unit tests.
"""
import re

import pytest

from models.gcd import (
    generate_search_variations,
    lookahead_regex,
    normalize_title,
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
