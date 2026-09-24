"""Series identity in the reading-list matcher.

A year cannot separate two series that began the same year. "Batman (2025) #14"
matched "Absolute Batman 014 (2025)": same issue number, same volume year, and
"Batman" is a substring of "Absolute Batman". Extra words in FRONT of the
wanted name mean a different series.

The guard has to be structural. Blacklisting "absolute" would be wrong twice
over: it is an ordinary word in real issue titles ("Nightwing 117 - Absolute
Power"), which is the exact trap CLAUDE.md records against reusing
VARIANT_TYPES for filename matching, and it would do nothing for the next
prefixed series ("Ultimate Spider-Man", "All-Star Superman").
"""
from unittest.mock import patch

import pytest

from models.cbl import filename_series_matches
from helpers.collection import series_names_compatible


SAMPLE_CBL = """\
<?xml version="1.0" encoding="utf-8"?>
<ReadingList><Name>Test</Name><Books/></ReadingList>
"""

ABSOLUTE_PATH = "/data/DC Comics/Absolute Batman/Absolute Batman 014 (2025).cbz"
ABSOLUTE_NAME = "Absolute Batman 014 (2025).cbz"


def _meta(path, name, series, number, ci_volume="", ci_year=""):
    return {
        "path": path, "name": name, "type": "file", "parent": "/data",
        "size": 1000, "ci_series": series, "ci_number": number,
        "ci_volume": ci_volume, "ci_year": ci_year, "ci_publisher": "DC Comics",
    }


class TestFilenameSeriesMatches:
    """The filename tier has no ComicInfo, so position is the only signal."""

    @pytest.mark.parametrize("filename", [
        "Batman 014 (2026).cbz",
        "Batman 014.cbz",
        "Batman Vol 3 014.cbz",
        "batman 014.cbz",
    ])
    def test_series_at_the_start_matches(self, filename):
        assert filename_series_matches(filename, "Batman") is True

    @pytest.mark.parametrize("filename", [
        "Absolute Batman 014 (2025).cbz",
        "Ultimate Batman 014.cbz",
        "All-Star Batman 014.cbz",
    ])
    def test_prefixed_series_is_rejected(self, filename):
        assert filename_series_matches(filename, "Batman") is False

    def test_full_path_is_reduced_to_the_basename(self):
        # A parent folder named for the wanted series must not rescue a file
        # that is plainly something else.
        assert filename_series_matches(
            "/data/DC Comics/Batman/Absolute Batman 014.cbz", "Batman") is False

    def test_leading_article_may_be_dropped_from_the_filename(self):
        assert filename_series_matches("The Flash 094.cbz", "Flash") is True

    def test_leading_article_may_be_dropped_from_the_wanted_name(self):
        assert filename_series_matches("Flash 094.cbz", "The Flash") is True

    def test_partial_word_is_not_a_match(self):
        # "Batman" must not satisfy a list asking for "Batgirl", nor may a
        # prefix of a longer word count as the word.
        assert filename_series_matches("Batgirl 014.cbz", "Batman") is False
        assert filename_series_matches("Batmanx 014.cbz", "Batman") is False

    def test_punctuation_is_normalised_on_both_sides(self):
        assert filename_series_matches(
            "Batman - The Dark Knight 001.cbz", "Batman: The Dark Knight") is True

    def test_subtitle_after_the_issue_number_is_fine(self):
        # The subtitle comes AFTER the number, so this is a real issue title,
        # not a spin-off -- the same distinction helpers/collection draws.
        assert filename_series_matches(
            "Nightwing 117 - Absolute Power.cbz", "Nightwing") is True

    @pytest.mark.parametrize("filename,series", [
        ("", "Batman"), (None, "Batman"), ("Batman 014.cbz", ""),
        ("Batman 014.cbz", None),
    ])
    def test_missing_input_is_not_a_match(self, filename, series):
        assert filename_series_matches(filename, series) is False


class TestSeriesNamesCompatible:
    """The ComicInfo guard, reused from helpers/collection."""

    def test_rejects_a_prefixed_series(self):
        assert series_names_compatible("Absolute Batman", "Batman") is False

    def test_accepts_an_exact_name(self):
        assert series_names_compatible("Batman", "Batman") is True

    def test_accepts_a_volume_or_year_tag(self):
        assert series_names_compatible("Batman (2025)", "Batman") is True
        assert series_names_compatible("Batman Vol 3", "Batman") is True

    def test_accepts_a_shorter_library_spelling(self):
        assert series_names_compatible("Ultimates", "The Ultimates") is True

    def test_rejects_a_subtitled_spin_off(self):
        assert series_names_compatible("Batman: White Knight", "Batman") is False


class TestTheShorterNameMustEarnItToo:
    """The mirror of Absolute Batman, and the one that moved 26 files.

    The shorter-name direction used to be a raw substring test, accepted
    outright. A file tagged ``<Series>Batman</Series><Number>53</Number>``
    therefore satisfied wanted issue #53 of the Superman/Batman volume below,
    and the wanted scan moved it -- out of the Batman folder, into
    Superman/Batman's, renamed to fit. "Batman" is a substring of every one of
    these, and none of them is Batman.
    """

    SPECIAL = (
        "Superman-Batman ''Batman V Superman - Dawn of Justice Day'' "
        "Special Edition"
    )

    def test_the_reported_pair(self):
        assert series_names_compatible("Batman", self.SPECIAL) is False

    @pytest.mark.parametrize("wanted", [
        "Superman - Batman",
        "Superman/Batman",
        "Batman Beyond",
        "The Batman Adventures",
    ])
    def test_a_longer_wanted_name_is_a_different_series(self, wanted):
        assert series_names_compatible("Batman", wanted) is False

    def test_a_qualifier_in_front_is_rejected_from_either_side(self):
        # Both directions now give the same answer; before, only one did.
        assert series_names_compatible("Absolute Batman", "Batman") is False
        assert series_names_compatible("Batman", "Absolute Batman") is False

    def test_a_prefixed_series_still_matches_itself(self):
        assert series_names_compatible("Absolute Batman", "Absolute Batman") is True

    def test_partial_words_are_not_names(self):
        assert series_names_compatible("Batgirl", "Batman") is False
        assert series_names_compatible("Batmanx", "Batman") is False

    @pytest.mark.parametrize("meta,wanted", [
        ("Ultimates", "The Ultimates"),
        ("Amazing Spider-Man", "The Amazing Spider-Man"),
        ("The Flash", "Flash"),
    ])
    def test_a_leading_article_is_still_droppable(self, meta, wanted):
        """The one relaxation, and the only one the shorter side gets."""
        assert series_names_compatible(meta, wanted) is True

    def test_an_article_in_the_middle_is_not_droppable(self):
        # "the" is only ignorable at the front of a whole name; treating it as
        # a generally-droppable word would merge these two.
        assert series_names_compatible("Batman", "Batman the Detective") is False

    @pytest.mark.parametrize("meta,wanted", [
        ("Batman - The Dark Knight", "Batman: The Dark Knight"),
        ("Superman/Batman", "Superman - Batman"),
        ("Batman [2016]", "Batman"),
        ("Batman v3", "Batman"),
    ])
    def test_separator_spellings_are_the_same_name(self, meta, wanted):
        """Newly accepted: both sides are separator-normalised before comparison."""
        assert series_names_compatible(meta, wanted) is True


class TestAbsoluteBatmanEndToEnd:
    """The reported failure, through match_file, in both tiers."""

    def test_metadata_tier_rejects_absolute_batman(self):
        from models.cbl import CBLLoader
        candidates = [_meta(ABSOLUTE_PATH, ABSOLUTE_NAME, "Absolute Batman",
                            "14", ci_volume="2024", ci_year="2025")]
        with patch("models.cbl.search_by_comic_metadata", return_value=candidates), \
             patch("models.cbl.search_file_index", return_value=[]):
            result = CBLLoader(SAMPLE_CBL).match_file(
                "Batman", "14", volume_year=2025, issue_years={2026})
        assert result is None

    def test_filename_tier_rejects_absolute_batman(self):
        from models.cbl import CBLLoader
        rows = [{"path": ABSOLUTE_PATH, "name": ABSOLUTE_NAME}]
        with patch("models.cbl.search_by_comic_metadata", return_value=[]), \
             patch("models.cbl.search_file_index", return_value=rows):
            result = CBLLoader(SAMPLE_CBL).match_file(
                "Batman", "14", volume_year=2025, issue_years={2026})
        assert result is None

    def test_a_list_asking_for_absolute_batman_still_gets_it(self):
        from models.cbl import CBLLoader
        candidates = [_meta(ABSOLUTE_PATH, ABSOLUTE_NAME, "Absolute Batman",
                            "14", ci_volume="2024", ci_year="2025")]
        with patch("models.cbl.search_by_comic_metadata", return_value=candidates):
            result = CBLLoader(SAMPLE_CBL).match_file(
                "Absolute Batman", "14", volume_year=2024, issue_years={2025})
        assert result == ABSOLUTE_PATH

    def test_the_real_batman_is_unaffected(self):
        from models.cbl import CBLLoader
        good = "/data/DC Comics/Batman (2025)/Batman 014 (2026).cbz"
        candidates = [
            _meta(ABSOLUTE_PATH, ABSOLUTE_NAME, "Absolute Batman", "14",
                  ci_volume="2024", ci_year="2025"),
            _meta(good, "Batman 014 (2026).cbz", "Batman", "14",
                  ci_volume="2025", ci_year="2026"),
        ]
        with patch("models.cbl.search_by_comic_metadata", return_value=candidates):
            result = CBLLoader(SAMPLE_CBL).match_file(
                "Batman", "14", volume_year=2025, issue_years={2026})
        assert result == good

    def test_the_word_absolute_is_not_blacklisted(self):
        """Nightwing #117 really is titled "Absolute Power". Excluding files on
        that word would report owned issues as missing and re-download them."""
        from models.cbl import CBLLoader
        path = "/data/DC Comics/Nightwing/Nightwing 117 - Absolute Power.cbz"
        rows = [{"path": path, "name": "Nightwing 117 - Absolute Power.cbz"}]
        with patch("models.cbl.search_by_comic_metadata", return_value=[]), \
             patch("models.cbl.search_file_index", return_value=rows):
            result = CBLLoader(SAMPLE_CBL).match_file("Nightwing", "117")
        assert result == path
