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
        # ComicTagger writes the ComicVine URL to Web; that is what is read.
        ("https://comicvine.gamespot.com/x/4000-781429/",
         "Tagged with ComicTagger 1.6.0 using info from Comic Vine on "
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
        # A ComicVine mention elsewhere in the text is not provenance: only
        # provider_from_notes ("Metadata from <X>") says who wrote the id.
        "Metadata from Metron. Issue ID: 12345. Also on ComicVine.",
        "Imported. Issue ID: 12345 (see Comic Vine)",
        "",
        None,
    ])
    def test_another_providers_issue_id_is_not_a_comicvine_id(self, notes):
        assert comicvine_issue_id(None, notes) is None

    def test_a_non_comicvine_web_url_is_ignored(self):
        assert comicvine_issue_id("https://metron.cloud/issue/4000-1/", None) is None

    def test_one_notes_to_id_parser(self):
        """The backfill and the matcher share one Notes -> issue id parser."""
        from core import comicinfo, metadata_backfill
        assert metadata_backfill.extract_issue_id is comicinfo.extract_issue_id


class TestNormalizeIssueNumber:

    @pytest.mark.parametrize("raw, expected", [
        ("001", "1"), ("1", "1"), ("0", "0"), ("000", "0"),
        ("050.LR", "50.lr"), ("50.LR", "50.lr"), ("#7", "7"),
        ("1.MU", "1.mu"), (" 12 ", "12"), ("Annual", "annual"), (None, ""),
        ("-1", "-1"), ("-01", "-1"), ("#-1", "-1"),
    ])
    def test_forms(self, raw, expected):
        assert normalize_issue_number(raw) == expected

    def test_a_variant_suffix_is_a_different_issue(self):
        assert normalize_issue_number("50.LR") != normalize_issue_number("50")

    def test_the_sign_is_part_of_the_number(self):
        assert normalize_issue_number("-1") != normalize_issue_number("1")


class TestSeriesLikePattern:

    def test_punctuation_and_spacing_become_wildcards(self):
        assert _series_like_pattern("Amazing Spider-Man: Sins Rising Prelude") == \
            "%Amazing%Spider%Man%Sins%Rising%Prelude%"

    def test_case_is_left_to_sqlite(self):
        """SQLite's LOWER() folds ASCII only, so the query applies it to both
        sides; lowercasing here would turn "Élektra" into a needle the column
        can never equal."""
        assert _series_like_pattern("Élektra") == "%Élektra%"

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
        # The first token is the issue; later numbers are not
        ("Secret Wars 003 (of 9) (2015).cbz", "Secret Wars", "3"),
        ("Batman 050 - Part 2.cbz", "Batman", "50"),
        # A volume marker is skipped, not read as the issue
        ("Batman Vol. 2 001.cbz", "Batman", "1"),
        ("Batman v2 001.cbz", "Batman", "1"),
        # Negative issues keep their sign
        ("Batman #-1 (1997).cbz", "Batman", "-1"),
        ("Batman -1 (1997).cbz", "Batman", "-1"),
        # A hyphen inside the name is not a sign
        ("X-23 001 (2010).cbz", "X-23", "1"),
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
        # Only the first issue token counts
        ("Secret Wars 003 (of 9) (2015).cbz", "Secret Wars", "9"),
        ("Batman 050 - Part 2.cbz", "Batman", "2"),
        ("Batman Vol. 2 001.cbz", "Batman", "2"),
        # #1 and #-1 are different issues, both ways
        ("Batman #-1 (1997).cbz", "Batman", "1"),
        ("Batman -1 (1997).cbz", "Batman", "1"),
        ("Batman #001 (2016).cbz", "Batman", "-1"),
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

    def test_cv_volume_folder_skips_annuals(self):
        """"Batman (2016)/" with the volume's cvinfo also holds the annual,
        whose <Number> is 1 too. It must not fill regular #1."""
        loader = CBLLoader(EMPTY)
        annual = {"name": "Batman Annual 001 (2017).cbz",
                  "path": "/f/Batman Annual 001 (2017).cbz",
                  "ci_series": "Batman Annual", "ci_number": "1"}
        tagged_annual = {"name": "Batman 2017 Special.cbz",
                         "path": "/f/Batman 2017 Special.cbz",
                         "ci_series": "Batman Annual", "ci_number": "1"}
        regular = {"name": "Batman 001 (2016).cbz", "path": "/f/Batman 001 (2016).cbz",
                   "ci_series": "Batman", "ci_number": "1"}
        with patch.object(cbl, "cv_volume_folders", return_value={91273: ["/f"]}), \
             patch.object(loader, "_match_by_metadata", return_value=None), \
             patch.object(loader, "_match_by_filename", return_value=None):
            with patch("core.database.files_in_dirs",
                       return_value=[annual, tagged_annual]):
                assert loader.match_file("Batman", "1", cv_volume_id=91273) is None
            with patch("core.database.files_in_dirs",
                       return_value=[annual, tagged_annual, regular]):
                assert loader.match_file("Batman", "1", cv_volume_id=91273) == regular["path"]
            # A list asking for the annual (whose cvinfo names the annual's
            # own volume) still gets it
            with patch("core.database.files_in_dirs", return_value=[annual]):
                assert loader.match_file("Batman Annual", "1",
                                         cv_volume_id=91273) == annual["path"]

    def test_cv_volume_folder_uses_the_first_issue_token(self):
        loader = CBLLoader(EMPTY)
        rows = [{"name": "Secret Wars 003 (of 9) (2015).cbz",
                 "path": "/f/Secret Wars 003 (of 9) (2015).cbz", "ci_number": ""}]
        with patch.object(cbl, "cv_volume_folders", return_value={1: ["/f"]}), \
             patch("core.database.files_in_dirs", return_value=rows), \
             patch.object(loader, "_match_by_metadata", return_value=None), \
             patch.object(loader, "_match_by_filename", return_value=None):
            assert loader.match_file("Secret Wars", "9", cv_volume_id=1) is None
            assert loader.match_file("Secret Wars", "3", cv_volume_id=1) == rows[0]["path"]

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

    def test_cvinfo_name_is_case_insensitive(self, tmp_path):
        folder = tmp_path / "Sins (2020)"
        folder.mkdir()
        (folder / "CVInfo").write_text("https://comicvine.gamespot.com/x/4050-128879/\n")
        cbl._cv_folder_cache.update(at=0.0, map=None)
        try:
            with patch("core.database.list_comic_dirs", return_value=[str(folder)]):
                assert cbl.cv_volume_folders() == {128879: [str(folder)]}
        finally:
            cbl._cv_folder_cache.update(at=0.0, map=None)

    def test_a_failed_folder_read_is_not_cached(self, tmp_path):
        folder = tmp_path / "Sins (2020)"
        folder.mkdir()
        (folder / "cvinfo").write_text("https://comicvine.gamespot.com/x/4050-128879/\n")
        cbl._cv_folder_cache.update(at=0.0, map=None)
        try:
            with patch("core.database.list_comic_dirs", return_value=None):
                assert cbl.cv_volume_folders() == {}
            assert cbl._cv_folder_cache["map"] is None
            with patch("core.database.list_comic_dirs", return_value=[str(folder)]):
                assert cbl.cv_volume_folders() == {128879: [str(folder)]}
        finally:
            cbl._cv_folder_cache.update(at=0.0, map=None)

    def test_readers_do_not_wait_on_a_rebuild(self):
        """While one thread rebuilds, others get the previous map at once."""
        cbl._cv_folder_cache.update(at=0.0, map={1: ["/old"]})  # stale
        try:
            assert cbl._cv_folder_build_lock.acquire(blocking=False)
            try:
                with patch.object(cbl, "_build_cv_volume_map") as build:
                    assert cbl.cv_volume_folders() == {1: ["/old"]}
                build.assert_not_called()
            finally:
                cbl._cv_folder_build_lock.release()
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


class TestFilenameTierStopsEarly:

    def test_no_wider_search_once_a_candidate_passes(self):
        """Every search is a full LIKE scan; per entry, they only run while
        nothing has passed the exact checks."""
        loader = CBLLoader(EMPTY)
        hit = {"name": "Batman 001 (2016).cbz", "path": "/d/Batman 001 (2016).cbz",
               "type": "file"}
        with patch("models.cbl.search_file_index", return_value=[hit]) as exact, \
             patch("models.cbl.search_file_index_all_words") as words, \
             patch("models.cbl.search_by_comic_metadata", return_value=[]):
            assert loader.match_file("Batman", "1", "2016", "2016") == hit["path"]
        assert exact.call_count == 1
        words.assert_not_called()

    def test_widens_while_nothing_passes(self):
        loader = CBLLoader(EMPTY)
        wrong = {"name": "Batman 010 (2016).cbz", "path": "/d/Batman 010 (2016).cbz",
                 "type": "file"}
        right = {"name": "Batman: 001 (2016).cbz", "path": "/d/Batman: 001 (2016).cbz",
                 "type": "file"}
        with patch("models.cbl.search_file_index", return_value=[wrong]), \
             patch("models.cbl.search_file_index_all_words", return_value=[right]) as words, \
             patch("models.cbl.search_by_comic_metadata", return_value=[]):
            assert loader.match_file("Batman", "1", "2016", "2016") == right["path"]
        words.assert_called()

    def test_an_annual_is_not_the_regular_issue(self):
        loader = CBLLoader(EMPTY)
        annual = {"name": "Batman Annual 001 (2017).cbz",
                  "path": "/d/Batman Annual 001 (2017).cbz", "type": "file"}
        with patch("models.cbl.search_file_index", return_value=[annual]), \
             patch("models.cbl.search_file_index_all_words", return_value=[annual]), \
             patch("models.cbl.search_by_comic_metadata", return_value=[]):
            assert loader.match_file("Batman", "1") is None
            assert loader.match_file("Batman Annual", "1") == annual["path"]


class TestMapModalEscaping:

    def test_file_paths_are_escaped_before_innerhtml(self):
        """A folder named <img onerror=...> would run script in the Map Issue
        and Add Issue modals."""
        import re
        from pathlib import Path
        js = Path(__file__).resolve().parents[2].joinpath(
            "static", "js", "reading_list.js").read_text()
        raw = [m for m in re.findall(r"\$\{\s*file\.(?:path|name)\b[^}]*\}", js)
               # reduced to [A-Za-z0-9_] for an id -- nothing left to inject
               if "replace(/[^a-zA-Z0-9]/g" not in m]
        assert raw == [], raw
        assert js.count("CLU.escapeHtml(file.path)") >= 3

    @pytest.mark.parametrize("template", ["reading_lists.html", "reading_list_view.html"])
    def test_every_page_loading_the_script_loads_the_escaper(self, template):
        from pathlib import Path
        html = Path(__file__).resolve().parents[2].joinpath(
            "templates", template).read_text()
        assert html.index("js/clu-utils.js") < html.index("js/reading_list.js")
