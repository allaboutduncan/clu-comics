"""Year-aware matching in models/cbl.py -- the reading-list file matcher.

A reading list has no mapped folder to anchor it the way
``helpers.collection.match_issues_to_collection`` does: it searches the whole
library for "series + issue number". For a rebooted character that question
has several correct-looking answers, and before this change the first one
alphabetically won.

Reported failures these lock down:
  "Batwoman (2026) #7"  (in-store 2026-09-17) -> "Batwoman 007 (2012)"
  "Batman (2025) #14"   (in-store 2026-10-07) -> "Batman 014 (1942)"
"""
from unittest.mock import patch

import pytest


SAMPLE_CBL = """\
<?xml version="1.0" encoding="utf-8"?>
<ReadingList>
  <Name>Test Reading List</Name>
  <Books/>
</ReadingList>
"""


def _meta(path, name, series, number, ci_volume="", ci_year=""):
    """A file_index row as search_by_comic_metadata returns it."""
    return {
        "path": path, "name": name, "type": "file", "parent": "/data",
        "size": 1000, "ci_series": series, "ci_number": number,
        "ci_volume": ci_volume, "ci_year": ci_year,
        "ci_publisher": "DC Comics",
    }


def _run(candidates, series, number, **hints):
    """Drive match_file with the same rows visible to both tiers."""
    from models.cbl import CBLLoader
    rows = [{"path": c["path"], "name": c["name"]} for c in candidates]
    with patch("models.cbl.search_by_comic_metadata", return_value=candidates), \
         patch("models.cbl.search_file_index", return_value=rows):
        return CBLLoader(SAMPLE_CBL).match_file(series, number, **hints)


class TestReportedWrongVolumeMatches:

    def test_batwoman_2026_does_not_match_the_2012_volume(self):
        candidates = [_meta(
            "/data/DC Comics/Batwoman/Batwoman 007 (2012).cbz",
            "Batwoman 007 (2012).cbz", "Batwoman", "7",
            ci_volume="2011", ci_year="2012",
        )]
        assert _run(candidates, "Batwoman", "7",
                    volume_year=2026, issue_years={2026}) is None

    def test_batman_2025_does_not_match_the_1942_volume(self):
        candidates = [_meta(
            "/data/DC Comics/Batman/Batman 014 (1942).cbz",
            "Batman 014 (1942).cbz", "Batman", "14",
            ci_volume="1940", ci_year="1942",
        )]
        assert _run(candidates, "Batman", "14",
                    volume_year=2025, issue_years={2026}) is None

    def test_right_volume_wins_over_the_wrong_one(self):
        candidates = [
            # Sorts first by name, so it used to win the tie outright.
            _meta("/data/DC Comics/Batman/Batman 014 (1942).cbz",
                  "Batman 014 (1942).cbz", "Batman", "14",
                  ci_volume="1940", ci_year="1942"),
            _meta("/data/DC Comics/Batman (2025)/Batman 014 (2026).cbz",
                  "Batman 014 (2026).cbz", "Batman", "14",
                  ci_volume="2025", ci_year="2026"),
        ]
        assert _run(candidates, "Batman", "14",
                    volume_year=2025, issue_years={2026}) == \
            "/data/DC Comics/Batman (2025)/Batman 014 (2026).cbz"

    def test_filename_tier_also_rejects_the_wrong_volume(self):
        """Tier 2 ignored the year entirely before; with no ComicInfo anywhere
        it is the tier that has to make the call."""
        from models.cbl import CBLLoader
        rows = [{"path": "/data/DC Comics/Batwoman/Batwoman 007 (2012).cbz",
                 "name": "Batwoman 007 (2012).cbz"}]
        with patch("models.cbl.search_by_comic_metadata", return_value=[]), \
             patch("models.cbl.search_file_index", return_value=rows):
            result = CBLLoader(SAMPLE_CBL).match_file(
                "Batwoman", "7", volume_year=2026, issue_years={2026})
        assert result is None


class TestLayoutsThatMustKeepMatching:
    """Over-rejection is the risk this change carries; these are the shapes a
    real library actually takes."""

    def test_folder_carries_the_year_and_the_file_does_not(self):
        candidates = [_meta("/data/DC Comics/Batman (2025)/Batman 014.cbz",
                            "Batman 014.cbz", "Batman", "14")]
        assert _run(candidates, "Batman", "14",
                    volume_year=2025, issue_years={2026}) == \
            "/data/DC Comics/Batman (2025)/Batman 014.cbz"

    def test_mylar_volume_folder(self):
        candidates = [_meta("/data/DC Comics/Batman/v2025/Batman 014.cbz",
                            "Batman 014.cbz", "Batman", "14")]
        assert _run(candidates, "Batman", "14",
                    volume_year=2025, issue_years={2026}) == \
            "/data/DC Comics/Batman/v2025/Batman 014.cbz"

    def test_yearless_library_is_untouched(self):
        """No year in the path and no ComicInfo -- nothing contradicts, so this
        must behave exactly as it did before. Nothing tags CBR at all."""
        candidates = [_meta("/data/DC Comics/Batman/Batman 014.cbz",
                            "Batman 014.cbz", "Batman", "14")]
        assert _run(candidates, "Batman", "14",
                    volume_year=2025, issue_years={2026}) == \
            "/data/DC Comics/Batman/Batman 014.cbz"

    def test_long_running_series_is_not_rejected(self):
        """Issue #40 of a 2015 volume is honestly named "(2024)". Only the
        volume year is known, and it is a floor -- not a window."""
        candidates = [_meta("/data/Image/Saga/Saga 040 (2024).cbz",
                            "Saga 040 (2024).cbz", "Saga", "40")]
        assert _run(candidates, "Saga", "40", volume_year=2015) == \
            "/data/Image/Saga/Saga 040 (2024).cbz"

    def test_file_predating_the_volume_is_rejected(self):
        candidates = [_meta("/data/Image/Saga/Saga 040 (2011).cbz",
                            "Saga 040 (2011).cbz", "Saga", "40")]
        assert _run(candidates, "Saga", "40", volume_year=2015) is None

    def test_unrelated_path_year_cannot_force_a_rejection(self):
        candidates = [_meta(
            "/data/2024 Backups/Batman (2025)/Batman 014 (2026).cbz",
            "Batman 014 (2026).cbz", "Batman", "14")]
        assert _run(candidates, "Batman", "14",
                    volume_year=2025, issue_years={2026}) is not None


class TestYearHintNormalisation:

    def test_ordinal_volume_is_not_treated_as_a_year(self):
        """Metron hands us series.volume, an ordinal (1, 2, 3). Testing "3"
        against the path used to award a bonus to any path containing that
        digit -- which is nearly all of them."""
        from models.cbl import CBLLoader
        volume_year, issue_years = CBLLoader(SAMPLE_CBL)._year_hints("3", None)
        assert volume_year is None
        assert issue_years == set()

    def test_comicvine_volume_id_is_not_treated_as_a_year(self):
        from models.cbl import CBLLoader
        volume_year, _ = CBLLoader(SAMPLE_CBL)._year_hints("42721", None)
        assert volume_year is None

    def test_positional_args_keep_the_cbl_meaning(self):
        """A CBL Book carries Volume (year the run began) and Year (this
        issue). Every pre-existing caller relies on that reading."""
        from models.cbl import CBLLoader
        volume_year, issue_years = CBLLoader(SAMPLE_CBL)._year_hints("2011", "2012")
        assert volume_year == 2011
        assert issue_years == {2012}


class TestMetronIdMatching:
    """Tier 0: an exact join on the Metron issue id needs no guessing at all."""

    def test_metron_id_short_circuits_matching(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(SAMPLE_CBL)
        loader.metron_id_map = {12345: ["/data/DC Comics/Batman/Batman 014.cbz"]}
        with patch("models.cbl.search_by_comic_metadata") as meta, \
             patch("models.cbl.search_file_index") as fname:
            result = loader.match_file("Batman", "14", metron_id=12345)
        assert result == "/data/DC Comics/Batman/Batman 014.cbz"
        # An identity match must not fall through to the heuristics.
        meta.assert_not_called()
        fname.assert_not_called()

    def test_unknown_metron_id_falls_through_to_scoring(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(SAMPLE_CBL)
        loader.metron_id_map = {999: ["/data/other.cbz"]}
        rows = [{"path": "/data/DC Comics/Batman/Batman 014.cbz",
                 "name": "Batman 014.cbz"}]
        with patch("models.cbl.search_by_comic_metadata", return_value=[]), \
             patch("models.cbl.search_file_index", return_value=rows):
            result = loader.match_file("Batman", "14", metron_id=12345)
        assert result == "/data/DC Comics/Batman/Batman 014.cbz"

    def test_prefetch_batches_the_whole_list(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(SAMPLE_CBL)
        with patch("core.database.find_files_by_metron_ids",
                   return_value={1: ["/data/a.cbz"]}) as lookup:
            loader.prefetch_metron_ids([1, 2, None])
        # One call for the list, not one per issue.
        lookup.assert_called_once_with([1, 2])
        assert loader.metron_id_map == {1: ["/data/a.cbz"]}

    def test_prefetch_survives_a_database_failure(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(SAMPLE_CBL)
        with patch("core.database.find_files_by_metron_ids",
                   side_effect=Exception("db down")):
            loader.prefetch_metron_ids([1])
        assert loader.metron_id_map == {}


class TestEntryYearHints:
    """What a stored entry implies -- used by the re-match worker, which has
    only the database row to go on."""

    def test_prefers_the_issue_year_column(self):
        from models.cbl import CBLLoader
        vol, issues = CBLLoader.entry_year_hints(
            {"volume": "2025", "year": "2025", "issue_year": 2026})
        assert vol == 2025
        assert issues == {2025, 2026}

    def test_metron_entry_imported_before_issue_year_existed(self):
        """Metron writes the volume start year into `year` and leaves `volume`
        as an ordinal, so `year` is the only usable hint on an old row."""
        from models.cbl import CBLLoader
        vol, _ = CBLLoader.entry_year_hints({"volume": "3", "year": "2026"})
        assert vol == 2026

    def test_cbl_entry_reads_volume_as_the_series_year(self):
        from models.cbl import CBLLoader
        vol, issues = CBLLoader.entry_year_hints({"volume": "2011", "year": "2012"})
        assert vol == 2011
        assert issues == {2012}

    def test_entry_with_no_years_at_all(self):
        from models.cbl import CBLLoader
        vol, issues = CBLLoader.entry_year_hints({"series": "Batman"})
        assert vol is None
        assert issues == set()
