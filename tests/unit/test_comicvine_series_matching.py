"""Series-name matching for the ComicVine paths.

ComicVine's search endpoint is a keyword match over the volume name: a word in
the query that the volume's name does not carry removes that volume from the
results entirely. Filenames routinely carry an article the catalogue does not --
``Red Range Pirates of the Fireworld 001 (2025).cbz`` against ComicVine's
``Red Range: Pirates of Fireworld`` -- so the verbatim query came back empty and
the provider cascade moved on as though ComicVine had never heard of the series.

Both halves of the fix are pure functions, so they live in unit tests:
``volume_search_variants`` decides what to ask ComicVine, and
``volume_name_matches`` decides whether an answer is confident enough to tag
without prompting the user.
"""

import pytest

from models.comicvine import volume_name_matches, volume_search_variants


class TestVolumeSearchVariants:

    def test_verbatim_query_comes_first(self):
        assert volume_search_variants("Batman")[0] == "Batman"

    def test_relaxed_variant_drops_the_stray_article(self):
        variants = volume_search_variants("Red Range Pirates of the Fireworld")
        assert variants == [
            "Red Range Pirates of the Fireworld",
            "red range pirates fireworld",
        ]

    def test_no_relaxed_variant_when_nothing_to_drop(self):
        """One query per search unless relaxing it would actually change it --
        the fallback costs a second ComicVine call against an hourly budget."""
        assert volume_search_variants("Batman") == ["Batman"]

    def test_every_content_word_survives_relaxation(self):
        """The relaxed query must never be truncated to a franchise name: a
        one-word query matches dozens of unrelated volumes, and the cascade
        auto-selects a lone result without asking."""
        _, relaxed = volume_search_variants("The Amazing Spider-Man")
        assert relaxed == "amazing spider man"

    def test_empty_name_asks_nothing(self):
        assert volume_search_variants("") == []
        assert volume_search_variants(None) == []


class TestVolumeNameMatches:

    def test_matches_across_punctuation_and_a_stray_article(self):
        assert volume_name_matches(
            "Red Range Pirates of the Fireworld", "Red Range: Pirates of Fireworld"
        )

    def test_matches_when_the_article_is_on_the_other_side(self):
        assert volume_name_matches("Flash", "The Flash")

    def test_search_name_may_be_a_prefix_of_the_catalogue_title(self):
        assert volume_name_matches("Batman", "Batman: Year One")

    def test_rejects_a_volume_missing_a_content_word(self):
        assert not volume_name_matches("Red Range Pirates of Fireworld", "Red Range")

    def test_a_word_inside_a_longer_word_is_not_a_match(self):
        """Substring matching made 'Wolverine' confidently match 'Wolverines'
        and auto-tag the wrong series without ever prompting."""
        assert not volume_name_matches("Wolverine", "Wolverines")

    def test_hyphenation_differences_do_not_matter(self):
        assert volume_name_matches("Spider Man 2099", "Spider-Man 2099")

    @pytest.mark.parametrize("blank", ["", None, "   ", "the"])
    def test_no_content_words_is_never_confident(self, blank):
        """With nothing to compare, "all words present" is vacuously true --
        which would auto-select the first volume of an arbitrary search."""
        assert not volume_name_matches(blank, "Batman")

    def test_missing_volume_name_is_not_a_match(self):
        assert not volume_name_matches("Batman", None)
