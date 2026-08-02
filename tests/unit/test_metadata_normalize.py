"""Unit tests for core.metadata_normalize — provider-ID stripping."""

import pytest

from core.metadata_normalize import (
    normalize_credit_list,
    split_credit_list,
    strip_provider_ids,
)


class TestStripProviderIds:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            # The target cases from real ComicInfo.xml files
            ("Ron Lim [3258]", "Ron Lim"),
            ("Roger Stern [41502]", "Roger Stern"),
            ("Aquaman [2357]", "Aquaman"),
            # Spacing variants
            ("Ron Lim[3258]", "Ron Lim"),
            ("Ron Lim [ 3258 ]", "Ron Lim"),
            ("Ron Lim  [3258]", "Ron Lim"),
            ("  Ron Lim [3258]  ", "Ron Lim"),
            # Mid-string and repeated IDs
            ("Ron [3258] Lim", "Ron Lim"),
            ("Ron Lim [3258] [41]", "Ron Lim"),
            # A bare ID is not a name
            ("[3258]", ""),
            ("[ 3258 ]", ""),
        ],
    )
    def test_strips_numeric_ids(self, raw, expected):
        assert strip_provider_ids(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "Ron Lim [uncredited]",
            "Batman [Bruce Wayne]",
            "Jack Kirby [1st printing]",
            "Alan Smithee [as A. Smithee]",
            # Unicode digits are not ASCII digits — far more likely part of a name
            "Ron Lim [٣٢٥٨]",
        ],
    )
    def test_preserves_non_numeric_brackets(self, raw):
        assert strip_provider_ids(raw) == raw

    @pytest.mark.parametrize("raw", [None, "", 0, [], {}])
    def test_empty_input(self, raw):
        assert strip_provider_ids(raw) == ""

    def test_unicode_names_survive(self):
        assert strip_provider_ids("Ronald Müller [12]") == "Ronald Müller"
        assert strip_provider_ids("张三 [77]") == "张三"

    def test_clean_values_keep_internal_whitespace(self):
        # No substitution happened, so no whitespace collapse — only .strip().
        assert strip_provider_ids("  Ron   Lim  ") == "Ron   Lim"

    def test_collapses_whitespace_only_on_a_hit(self):
        assert strip_provider_ids("  Ron   Lim [1]  ") == "Ron Lim"

    def test_coerces_non_string(self):
        assert strip_provider_ids(1234) == "1234"


class TestSplitCreditList:
    def test_splits_and_strips(self):
        assert split_credit_list("Ron Lim [3258], Joe Sinnott [99]") == (
            "Ron Lim",
            "Joe Sinnott",
        )

    def test_dedupes_dirty_against_clean(self):
        assert split_credit_list("Ron Lim [3258], Ron Lim") == ("Ron Lim",)

    def test_dedupe_is_case_insensitive_first_spelling_wins(self):
        assert split_credit_list("Batman, BATMAN") == ("Batman",)

    def test_drops_empty_and_bare_id_tokens(self):
        assert split_credit_list("A [1],,B") == ("A", "B")
        assert split_credit_list("[3258]") == ()
        assert split_credit_list(",") == ()
        assert split_credit_list(", ,") == ()

    @pytest.mark.parametrize("raw", [None, "", 0])
    def test_empty_input(self, raw):
        assert split_credit_list(raw) == ()

    def test_preserves_order(self):
        assert split_credit_list("C, A, B") == ("C", "A", "B")

    def test_custom_separator(self):
        assert split_credit_list("A [1]; B [2]", sep=";") == ("A", "B")


class TestNormalizeCreditList:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Ron Lim [3258], Ron Lim", "Ron Lim"),
            ("A [1],B [2]", "A, B"),
            ("[3258]", ""),
            # Re-joined with ", " so a stripped trailing token leaves no gap
            ("Ron Lim [3258], Joe", "Ron Lim, Joe"),
            ("Ron Lim,Joe", "Ron Lim, Joe"),
        ],
    )
    def test_normalizes(self, raw, expected):
        assert normalize_credit_list(raw) == expected

    def test_is_idempotent(self):
        once = normalize_credit_list("Ron Lim [3258], Joe Sinnott [99]")
        assert normalize_credit_list(once) == once

    def test_clean_list_is_unchanged(self):
        assert normalize_credit_list("Ron Lim, Joe Sinnott") == "Ron Lim, Joe Sinnott"

    @pytest.mark.parametrize("raw", [None, "", 0])
    def test_empty_input(self, raw):
        assert normalize_credit_list(raw) == ""
