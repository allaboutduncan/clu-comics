"""Reading-list matching: ComicVine ids, exact issue numbers, punctuation.

Each case here is a real miss or false match from one library:

* "Amazing Spider-Man: Sins Rising Prelude #1" never matched although the
  file's ComicInfo said exactly that -- every series with a ':' failed.
* "The Amazing Spider-Man #50.LR" matched "#050" -- SQLite's CAST reads the
  numeric prefix, and the filename regex accepted "50" followed by ".".
* "Morbius: The Living Vampire #001 (2013).cbz" was unreachable from the
  filename tier, which only ever searched "Morbius - The Living Vampire 001".
* The CBL's <Database Name="cv"> ids were dropped at parse time.
"""

from unittest.mock import patch

import pytest

from core.comicinfo import comicvine_issue_id
from core.database import normalize_issue_number, _series_like_pattern
from models import cbl
from models.cbl import CBLLoader, filename_issue_matches, filename_series_matches

EMPTY = "<ReadingList><Name>x</Name><Books/></ReadingList>"


class TestComicVineIssueId:

    @pytest.mark.parametrize("web, notes, expected", [
        ("https://comicvine.gamespot.com/amazing-spider-man-sins-rising-prelude-1/4000-781429/",
         None, 781429),
        (None, "Tagged with ComicTagger 1.6.0 using info from Comic Vine on "
               "2026-10-12. [Issue ID 781429]", 781429),
        (None, "Metadata from ComicVine. Issue ID: 42", 42),
        (None, "Metadata from ComicVine (Local DB). Issue ID: 43", 43),
        (None, "Tagged by CLU [Issue ID 4000-44] urn:comicvine:4000-44", 44),
        (None, "Tagged with ComicTagger [CVDB45]", 45),
    ])
    def test_reads_every_tagger_spelling(self, web, notes, expected):
        assert comicvine_issue_id(web, notes) == expected

    @pytest.mark.parametrize("notes", [
        "Metadata from Metron. Issue ID: 12345",
        "Metadata from Grand Comics Database. Issue ID: 9",
        "Tagged with ComicTagger using info from Metron [Issue ID 777]",
        "",
        None,
    ])
    def test_another_providers_issue_id_is_not_a_comicvine_id(self, notes):
        assert comicvine_issue_id(None, notes) is None

    def test_a_non_comicvine_web_url_is_ignored(self):
        assert comicvine_issue_id("https://metron.cloud/issue/4000-1/", None) is None


class TestNormalizeIssueNumber:

    @pytest.mark.parametrize("raw, expected", [
        ("001", "1"), ("1", "1"), ("0", "0"), ("000", "0"),
        ("050.LR", "50.lr"), ("50.LR", "50.lr"), ("#7", "7"),
        ("1.MU", "1.mu"), (" 12 ", "12"), ("Annual", "annual"), (None, ""),
    ])
    def test_forms(self, raw, expected):
        assert normalize_issue_number(raw) == expected

    def test_a_variant_suffix_is_a_different_issue(self):
        assert normalize_issue_number("50.LR") != normalize_issue_number("50")


class TestSeriesLikePattern:

    def test_punctuation_and_spacing_become_wildcards(self):
        assert _series_like_pattern("Amazing Spider-Man: Sins Rising Prelude") == \
            "%amazing%spider%man%sins%rising%prelude%"

    def test_empty(self):
        assert _series_like_pattern(":: -") is None


class TestFilenameIssueMatches:

    @pytest.mark.parametrize("name, series, number", [
        ("Morbius: The Living Vampire #001 (2013).cbz", "Morbius: The Living Vampire", "1"),
        ("Morbius - The Living Vampire 001 (2013).cbz", "Morbius: The Living Vampire", "1"),
        ("Alpha: Big Time #001 (2013).cbz", "Alpha: Big Time", "1"),
        ("The Amazing Spider-Man #050.LR (2020).cbz", "The Amazing Spider-Man", "50.LR"),
        ("The Flash 094.cbz", "Flash", "94"),
        ("Flash 094.cbz", "The Flash", "94"),
        ("Batman 1.cbz", "Batman", "1"),
    ])
    def test_matches(self, name, series, number):
        assert filename_issue_matches(name, series, number)

    @pytest.mark.parametrize("name, series, number", [
        # The variant is not the regular issue, and vice versa
        ("The Amazing Spider-Man #050.LR (2020).cbz", "The Amazing Spider-Man", "50"),
        ("The Amazing Spider-Man #050 (2020).cbz", "The Amazing Spider-Man", "50.LR"),
        # A duplicate counter or a year is not an issue number
        ("Morbius: The Living Vampire #002 (2013) (1).cbz", "Morbius: The Living Vampire", "1"),
        ("Batman 014 (2001).cbz", "Batman", "2001"),
        # #1 is not #10
        ("Batman #010.cbz", "Batman", "1"),
        # A different series that contains ours
        ("Absolute Batman 014 (2025).cbz", "Batman", "14"),
        ("Morbius #001 (2020).cbz", "Morbius: The Living Vampire", "1"),
    ])
    def test_rejects(self, name, series, number):
        assert not filename_issue_matches(name, series, number)

    def test_series_check_is_punctuation_blind(self):
        assert filename_series_matches("Morbius: The Living Vampire #001.cbz",
                                        "Morbius - The Living Vampire")


class TestCblDatabaseIds:

    CBL = """<?xml version='1.0' encoding='utf-8'?>
<ReadingList>
  <Name>Sins Rising</Name>
  <Books>
    <Book Series="Amazing Spider-Man: Sins Rising Prelude" Number="1" Volume="2020" Year="2020">
      <Database Name="cv" Series="128879" Issue="781429" />
    </Book>
    <Book Series="Batman" Number="1" Volume="2016" Year="2016">
      <Database Name="Metron" Series="5" Issue="99" />
    </Book>
    <Book Series="Plain" Number="1" Volume="2016" Year="2016" />
  </Books>
</ReadingList>"""

    def test_parse_entries_keeps_the_ids(self):
        entries = CBLLoader(self.CBL).parse_entries()
        assert (entries[0]["cv_volume_id"], entries[0]["cv_issue_id"]) == (128879, 781429)
        assert entries[0]["metron_id"] is None
        assert entries[1]["metron_id"] == 99
        assert entries[1]["cv_issue_id"] is None
        assert entries[2]["cv_issue_id"] is None and entries[2]["metron_id"] is None


class TestIdTiers:

    def test_cv_issue_id_wins_before_any_name_guessing(self):
        loader = CBLLoader(EMPTY)
        with patch("core.database.find_files_by_cv_ids",
                   return_value={781429: ["/data/x/Sins #001.cbz"]}), \
             patch.object(loader, "_match_by_metadata") as meta, \
             patch.object(loader, "_match_by_filename") as fname:
            result = loader.match_file("Whatever", "1", cv_issue_id=781429)
        assert result == "/data/x/Sins #001.cbz"
        meta.assert_not_called()
        fname.assert_not_called()

    def test_prefetched_map_is_used(self):
        loader = CBLLoader(EMPTY)
        with patch("core.database.find_files_by_cv_ids",
                   return_value={7: ["/b.cbz", "/a.cbz"]}) as lookup:
            loader.prefetch_ids([{"cv_issue_id": 7}, {"cv_issue_id": None}])
            assert loader.match_file("S", "1", cv_issue_id=7) == "/a.cbz"
        lookup.assert_called_once_with([7])

    def test_cv_volume_folder_picks_the_issue_exactly(self):
        loader = CBLLoader(EMPTY)
        rows = [
            {"name": "ASM #050.LR (2020).cbz", "path": "/f/ASM #050.LR (2020).cbz",
             "ci_number": "50.LR"},
            {"name": "ASM #050 (2020).cbz", "path": "/f/ASM #050 (2020).cbz",
             "ci_number": "50"},
            {"name": "ASM #050 (2020) (1).cbz", "path": "/f/ASM #050 (2020) (1).cbz",
             "ci_number": ""},
        ]
        with patch.object(cbl, "cv_volume_folders", return_value={128879: ["/f"]}), \
             patch("core.database.files_in_dirs", return_value=rows), \
             patch.object(loader, "_match_by_metadata", return_value=None), \
             patch.object(loader, "_match_by_filename", return_value=None):
            assert loader.match_file("ASM", "50", cv_volume_id=128879) == "/f/ASM #050 (2020).cbz"
            assert loader.match_file("ASM", "50.LR", cv_volume_id=128879) == \
                "/f/ASM #050.LR (2020).cbz"
            assert loader.match_file("ASM", "51", cv_volume_id=128879) is None

    def test_cv_volume_folders_reads_cvinfo(self, tmp_path):
        good = tmp_path / "Sins (2020)"
        good.mkdir()
        (good / "cvinfo").write_text(
            "https://comicvine.gamespot.com/amazing-spider-man-sins-rising-prelude/4050-128879/\n")
        bare = tmp_path / "No cvinfo"
        bare.mkdir()
        cbl._cv_folder_cache.update(at=0.0, map=None)
        try:
            with patch("core.database.list_comic_dirs", return_value=[str(good), str(bare)]):
                assert cbl.cv_volume_folders() == {128879: [str(good)]}
        finally:
            cbl._cv_folder_cache.update(at=0.0, map=None)


class TestFilenameTierFindsColonNames:

    def test_colon_filename_is_found_for_a_dash_term(self):
        """The character map turns ':' into ' -' in the search term, so no
        substring search reaches a file that kept its colon; the word search
        does, and the exact checks then accept it."""
        loader = CBLLoader(EMPTY)
        row = {"name": "Morbius: The Living Vampire #001 (2013).cbz",
               "path": "/data/Marvel/Morbius - The Living Vampire (2013)/"
                       "Morbius: The Living Vampire #001 (2013).cbz",
               "type": "file"}
        other = {"name": "Morbius: The Living Vampire #001 (1992).cbz",
                 "path": "/data/Marvel/Morbius - The Living Vampire (1992)/"
                         "Morbius: The Living Vampire #001 (1992).cbz",
                 "type": "file"}
        with patch("models.cbl.search_file_index", return_value=[]), \
             patch("models.cbl.search_file_index_all_words", return_value=[other, row]), \
             patch("models.cbl.search_by_comic_metadata", return_value=[]):
            assert loader.match_file("Morbius: The Living Vampire", "1", "2013", "2013") == row["path"]
            assert loader.match_file("Morbius: The Living Vampire", "1", "1992", "1992") == other["path"]


class TestDuplicateCopies:

    def test_a_tie_prefers_the_file_without_a_duplicate_counter(self):
        """"X #002 (2013) (1).cbz" sorts before "X #002 (2013).cbz"
        (' ' < '.'), so first-wins picked the duplicate."""
        loader = CBLLoader(EMPTY)
        base = "/data/Marvel/Morbius - The Living Vampire (2013)/"
        rows = [{"name": n, "path": base + n, "type": "file"} for n in (
            "Morbius: The Living Vampire #002 (2013) (1).cbz",
            "Morbius: The Living Vampire #002 (2013).cbz",
        )]
        with patch("models.cbl.search_file_index", return_value=[]), \
             patch("models.cbl.search_file_index_all_words", return_value=rows), \
             patch("models.cbl.search_by_comic_metadata", return_value=[]):
            assert loader.match_file("Morbius: The Living Vampire", "2", "2013", "2013") == \
                base + "Morbius: The Living Vampire #002 (2013).cbz"
