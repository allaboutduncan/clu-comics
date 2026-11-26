"""Three behaviour bugs found while consolidating the series-name normalisers.

Each was reproduced before it was fixed; the "used to" in a comment is what
the code returned on main.

1. Filing refused a download the GetComics scorer had accepted: "&" and "and",
   and a "+" crossover separator.
2. Metron's "confident match" compared word SETS, so a reordered name was
   tagged without asking.
3. The reading-list filename tier split words on an apostrophe.
"""
from unittest.mock import patch

import pytest

from helpers.collection import (
    _comicinfo_series_matches,
    generate_filename_pattern,
)

PATTERN = "{series_name} {issue_number}"


def _files(series, filename, issue="5"):
    regex = generate_filename_pattern(PATTERN, series, issue, strict_gap=True)
    return bool(regex and regex.match(filename))


class TestFilingAcceptsWhatTheScorerDownloads:
    """The invariant in .claude/rules/series-name-matching.md: filing has to
    accept at least what the scorer downloads, or the file is stranded in
    TARGET, the issue stays wanted and it is downloaded again."""

    @pytest.mark.parametrize("series,title", [
        ("Batman & Robin", "Batman and Robin #5 (2009)"),
        ("Batman and Robin", "Batman & Robin #5 (2009)"),
        ("Batman/Superman", "Batman + Superman #5 (2019)"),
    ])
    def test_scorer_accepts_these(self, series, title):
        from models.getcomics import accept_result, score_getcomics_result
        year = 2009 if "Robin" in series else 2019
        result = score_getcomics_result(title, series, "5", year)
        assert accept_result(*result[:3]) == "ACCEPT"

    @pytest.mark.parametrize("series,filename", [
        # Used to be False: "&" was deleted from the name, and "and" in the
        # file is letters the gap between two words may not hold.
        ("Batman & Robin", "Batman and Robin 005 (2009).cbz"),
        ("Batman and Robin", "Batman & Robin 005 (2009).cbz"),
        ("Batman & Robin", "Batman & Robin 005 (2009).cbz"),
        ("Batman & Robin", "Batman Robin 005 (2009).cbz"),
        # Used to be False: "+" was neither a separator nor allowed in the gap.
        ("Batman/Superman", "Batman + Superman 005 (2019).cbz"),
        ("Batman + Superman", "Batman Superman 005 (2019).cbz"),
        ("AT&T", "ATT 005.cbz"),
    ])
    def test_filename_tier_files_them(self, series, filename):
        assert _files(series, filename)

    @pytest.mark.parametrize("series,filename", [
        # "and" is optional, not a wildcard: other words still break the match.
        ("Batman & Robin", "Batman and Robin Eternal 005.cbz"),
        ("Batman", "Batman and Robin 005.cbz"),
        # The scorer rejects this one too, so filing need not take it.
        ("Batman/Superman", "Batman and Superman 005.cbz"),
    ])
    def test_filename_tier_still_refuses(self, series, filename):
        assert not _files(series, filename)

    @pytest.mark.parametrize("series", ["&", "And", "the", "!!!"])
    def test_a_name_of_only_optional_words_claims_nothing_else(self, series):
        # Every word optional would compile to a pattern that matches any
        # filename with the right issue number. "And" already did, before "&"
        # became a word.
        assert not _files(series, "Batman 005.cbz")

    @pytest.mark.parametrize("meta,series,expected", [
        ("batman and robin", "Batman & Robin", True),
        ("batman & robin", "Batman and Robin", True),
        ("batman + superman", "Batman/Superman", True),
        # The comparison never DROPS the word; only the anchored regex may.
        ("black white", "Black & White", False),
        ("batman", "Batman & Robin", False),
    ])
    def test_comicinfo_tier_agrees(self, meta, series, expected):
        assert _comicinfo_series_matches(meta, series) is expected


class TestMetronConfidentMatchKeepsWordOrder:
    def _run(self, series_name, candidates):
        from routes import metadata as routes_metadata
        with patch.object(routes_metadata.metron, "get_flask_api", return_value=object()), \
             patch.object(routes_metadata.metron, "search_series_list", return_value=candidates), \
             patch.object(routes_metadata.metron, "get_issue_metadata",
                          return_value={"id": 99}) as get_issue, \
             patch.object(routes_metadata.metron, "map_to_comicinfo",
                          return_value={"Series": "x"}), \
             patch.object(routes_metadata, "_metron_image_url", return_value=None):
            result = routes_metadata._try_metron_single(None, series_name, "1", None)
        return result, get_issue

    def test_reordered_name_is_not_confident(self):
        candidates = [
            {"id": 1, "name": "Superman/Batman"},
            {"id": 2, "name": "Batman/Superman: World's Finest"},
        ]
        (metadata, _image, selection), get_issue = self._run("Batman/Superman", candidates)
        # Used to tag from series 1 without asking: the word sets are equal.
        assert metadata is None
        assert selection["requires_selection"] is True
        assert selection["possible_matches"] == candidates
        get_issue.assert_not_called()

    def test_same_words_in_order_is_still_confident(self):
        candidates = [
            {"id": 1, "name": "Superman/Batman"},
            {"id": 2, "name": "Batman / Superman"},
        ]
        (metadata, _image, selection), get_issue = self._run("Batman: Superman", candidates)
        assert selection is None
        assert metadata == {"Series": "x"}
        assert get_issue.call_args.args[1] == 2


class TestReadingListFilenameApostrophes:
    @pytest.mark.parametrize("series,filename", [
        # Used to be False both ways: "world's" was the words "world", "s".
        ("World's Finest", "Worlds Finest 001 (2023).cbz"),
        ("Worlds Finest", "World's Finest 001 (2023).cbz"),
        ("It's Jeff!", "Its Jeff 001.cbz"),
        ("Bill & Ted’s Excellent Comic", "Bill & Teds Excellent Comic 001.cbz"),
        # Unchanged.
        ("World's Finest", "World's Finest 001 (2023).cbz"),
        ("X-Men '92", "X-Men 92 001 (2015).cbz"),
        # GetComics writes a possessive with a space in front of it. The
        # apostrophe-as-word-break reading is what matches this, so it stays.
        ("Marvel's Voices", "Marvel 's Voices 001.cbz"),
        ("The Flash", "Flash 001.cbz"),
    ])
    def test_matches(self, series, filename):
        from models.cbl import filename_issue_matches
        assert filename_issue_matches(filename, series, "1")

    @pytest.mark.parametrize("series,filename", [
        ("Batman", "Absolute Batman 001.cbz"),
        ("World's Finest", "Worlds Collide 001.cbz"),
        ("World's Finest", "Worlds Finest 002.cbz"),
    ])
    def test_still_refuses(self, series, filename):
        from models.cbl import filename_issue_matches
        assert not filename_issue_matches(filename, series, "1")

    def test_remainder_starts_after_the_series(self):
        from models.cbl import _series_remainder
        assert _series_remainder("World's Finest 001 (2023).cbz", "Worlds Finest") == " 001 (2023)"
        assert _series_remainder("Worlds Finest 001 (2023).cbz", "World's Finest") == " 001 (2023)"
