"""Tests for helpers.collection.generate_filename_pattern.

Regression coverage for the wanted-issue matching bug: custom rename patterns
that use a year token other than the legacy {volume_year} (e.g. {issue_year})
left the literal placeholder in the compiled regex, so no file ever matched.
See helpers/collection.py.
"""
import re
import pytest

from helpers.collection import (
    generate_filename_pattern,
    strip_year_token,
    build_series_match_names,
)


# ---- helper -------------------------------------------------------------

def _no_placeholder_leak(regex):
    """The compiled pattern must not contain a literal {token} placeholder.

    Regex quantifiers like \\d{4} or \\d{1,4} are allowed; only alphabetic
    placeholder tokens ({cover_year}, ...) are forbidden.
    """
    stripped = re.sub(r'\{\d', '', regex.pattern)  # drop {4 / {1,4 quantifier starts
    return not re.search(r'\{[A-Za-z_]', stripped)


# ---- year token variants ------------------------------------------------

class TestYearTokenVariants:

    @pytest.mark.parametrize("token", [
        "volume_year", "issue_year", "year",
    ])
    def test_year_token_matches_file_with_year(self, token):
        regex = generate_filename_pattern(
            "{series_name} {issue_number} ({%s})" % token,
            "Absolute Catwoman", "1",
        )
        assert regex is not None
        assert regex.search("Absolute Catwoman 001 (2025).cbz")
        assert _no_placeholder_leak(regex)

    @pytest.mark.parametrize("token", [
        "volume_year", "issue_year",
    ])
    def test_no_literal_placeholder_in_pattern(self, token):
        regex = generate_filename_pattern(
            "{series_name} {issue_number} ({%s})" % token,
            "Daredevil", "3",
        )
        assert "{%s}" % token not in regex.pattern
        assert _no_placeholder_leak(regex)

    def test_year_is_four_digits(self):
        # The year placeholder must compile to a real 4-digit matcher, not be
        # collapsed by the unknown-token safety net.
        regex = generate_filename_pattern(
            "{series_name} {issue_number} ({issue_year})", "Daredevil", "3",
        )
        assert r"\d{4}" in regex.pattern
        assert not regex.search("Daredevil 003 (12).cbz")  # 2-digit not a year


# ---- month token variants ----------------------------------------------

class TestMonthTokens:

    def test_numeric_month(self):
        regex = generate_filename_pattern(
            "{series_name} {issue_number} {issue_month_m} ({issue_year})",
            "Sentry", "4",
        )
        assert regex.search("Sentry 004 03 (2024).cbz")
        assert _no_placeholder_leak(regex)

    def test_name_month(self):
        regex = generate_filename_pattern(
            "{series_name} {issue_number} {issue_month_M} ({issue_year})",
            "Sentry", "4",
        )
        assert regex.search("Sentry 004 March (2024).cbz")
        assert _no_placeholder_leak(regex)


# ---- defensive safety net ----------------------------------------------

class TestUnknownTokenSafetyNet:

    def test_unknown_token_stripped_not_required(self):
        regex = generate_filename_pattern(
            "{series_name} {issue_number} ({totally_made_up})", "Sentry", "4",
        )
        assert _no_placeholder_leak(regex)
        # Stripping the unknown ({token}) must not leave a dangling space that
        # blocks a plain filename.
        assert regex.search("Sentry 004.cbz")


# ---- regressions / existing behaviour -----------------------------------

class TestExistingBehaviour:

    def test_volume_year_still_matches(self):
        regex = generate_filename_pattern(
            "{series_name} {issue_number} ({volume_year})",
            "Spider-Man 2099", "44",
        )
        assert regex.search("Spider-Man 2099 044 (1992).cbz")

    def test_wrong_issue_number_rejected(self):
        regex = generate_filename_pattern(
            "{series_name} {issue_number} ({volume_year})",
            "Spider-Man 2099", "44",
        )
        assert not regex.search("Spider-Man 2099 045 (1992).cbz")

    def test_leading_zeros_flexible(self):
        regex = generate_filename_pattern(
            "{series_name} {issue_number} ({cover_year})", "Black Cat", "11",
        )
        assert regex.search("Black Cat 011 (2024).cbz")

    def test_empty_pattern_returns_none(self):
        assert generate_filename_pattern("", "Foo", "1") is None
        assert generate_filename_pattern("{series_name}", "", "1") is None


# ---- strip_year_token (app.py no-year matching path) --------------------

class TestStripYearToken:

    @pytest.mark.parametrize("token", [
        "volume_year", "cover_year", "issue_year", "store_year", "year",
    ])
    def test_strips_parenthesized_year(self, token):
        out = strip_year_token("{series_name} {issue_number} ({%s})" % token)
        assert out == "{series_name} {issue_number}"

    def test_strips_bracketed_year(self):
        out = strip_year_token("{series_name} [{volume_year}] {issue_number}")
        assert out == "{series_name} {issue_number}"

    def test_strips_bare_year(self):
        out = strip_year_token("{series_name} - {cover_year}")
        assert out == "{series_name} -"

    def test_no_year_token_unchanged(self):
        out = strip_year_token("{series_name} {issue_number}")
        assert out == "{series_name} {issue_number}"

    def test_empty(self):
        assert strip_year_token("") == ""


# ---- search-alias matching (Thor -> Mortal Thor) ------------------------

class TestBuildSeriesMatchNames:

    def test_primary_name_first(self):
        assert build_series_match_names("Thor", "")[0] == "Thor"

    def test_comma_string_aliases(self):
        names = build_series_match_names("Thor", "mortal thor, ultimate thor")
        assert names == ["Thor", "mortal thor", "ultimate thor"]

    def test_iterable_aliases(self):
        names = build_series_match_names("Thor", ["mortal thor"])
        assert names == ["Thor", "mortal thor"]

    def test_dedups_case_insensitively(self):
        names = build_series_match_names("Thor", "thor, THOR, Mortal Thor")
        assert names == ["Thor", "Mortal Thor"]

    def test_skips_blank_entries(self):
        names = build_series_match_names("Thor", " , mortal thor , ")
        assert names == ["Thor", "mortal thor"]

    def test_no_aliases(self):
        assert build_series_match_names("Thor", "") == ["Thor"]


class TestAliasMatching:
    """An aliased file should match via the alias name, not the series name."""

    def test_alias_matches_aliased_file(self):
        # Aliases are stored normalized (lowercase). Series "Thor" with alias
        # "mortal thor" must match 'Mortal Thor 011.cbz'.
        match_pattern = strip_year_token("{series_name} {issue_number} ({cover_year})")
        names = build_series_match_names("Thor", "mortal thor")
        regexes = [
            generate_filename_pattern(match_pattern, n, "11") for n in names
        ]
        assert any(r.match("Mortal Thor 011.cbz") for r in regexes)

    def test_series_name_alone_does_not_match_aliased_file(self):
        match_pattern = strip_year_token("{series_name} {issue_number} ({cover_year})")
        regex = generate_filename_pattern(match_pattern, "Thor", "11")
        assert not regex.match("Mortal Thor 011.cbz")

    def test_alias_does_not_steal_plain_match(self):
        match_pattern = strip_year_token("{series_name} {issue_number} ({cover_year})")
        names = build_series_match_names("Thor", "mortal thor")
        regexes = [
            generate_filename_pattern(match_pattern, n, "11") for n in names
        ]
        assert any(r.match("Thor 011.cbz") for r in regexes)
