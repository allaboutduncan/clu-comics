"""Tests for models/cbl.py -- CBL reading list parsing."""
import pytest
from unittest.mock import patch, MagicMock


SAMPLE_CBL = """\
<?xml version="1.0" encoding="utf-8"?>
<ReadingList>
  <Name>Test Reading List</Name>
  <Books>
    <Book Series="Batman" Number="1" Volume="2020" Year="2020" />
    <Book Series="Superman" Number="5" Volume="2018" Year="2018" />
    <Book Series="Wonder Woman" Number="10" />
  </Books>
</ReadingList>
"""

EMPTY_CBL = """\
<?xml version="1.0" encoding="utf-8"?>
<ReadingList>
  <Name>Empty List</Name>
</ReadingList>
"""


@pytest.fixture(autouse=True)
def _mock_cbl_deps():
    """Mock database search so CBL doesn't try to hit real DB."""
    with patch("models.cbl.search_file_index", return_value=[]), \
         patch("models.cbl.search_by_comic_metadata", return_value=[]):
        yield


class TestCBLLoaderInit:

    def test_parses_name(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(SAMPLE_CBL)
        assert loader.name == "Test Reading List"

    def test_missing_name_defaults_to_unknown(self):
        from models.cbl import CBLLoader
        xml = '<ReadingList><Books></Books></ReadingList>'
        loader = CBLLoader(xml)
        assert loader.name == "Unknown Reading List"

    def test_extracts_publisher_from_filename(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(SAMPLE_CBL, filename="[Marvel] (2021-09) Inferno.cbl")
        assert loader.publisher == "Marvel"

    def test_no_publisher_when_no_brackets(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(SAMPLE_CBL, filename="Some List.cbl")
        assert loader.publisher is None

    def test_no_publisher_when_no_filename(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(SAMPLE_CBL)
        assert loader.publisher is None


class TestParseEntries:

    def test_extracts_all_entries(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(SAMPLE_CBL)
        entries = loader.parse_entries()
        assert len(entries) == 3

    def test_entry_fields(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(SAMPLE_CBL)
        entries = loader.parse_entries()
        assert entries[0]["series"] == "Batman"
        assert entries[0]["issue_number"] == "1"
        assert entries[0]["volume"] == "2020"
        assert entries[0]["year"] == "2020"
        assert entries[0]["matched_file_path"] is None

    def test_missing_optional_fields(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(SAMPLE_CBL)
        entries = loader.parse_entries()
        # Third entry has no Volume or Year attributes
        assert entries[2]["series"] == "Wonder Woman"
        assert entries[2]["issue_number"] == "10"
        assert entries[2]["volume"] is None
        assert entries[2]["year"] is None

    def test_empty_books_returns_empty_list(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(EMPTY_CBL)
        entries = loader.parse_entries()
        assert entries == []

    def test_no_books_element_returns_empty_list(self):
        from models.cbl import CBLLoader
        xml = '<ReadingList><Name>Test</Name></ReadingList>'
        loader = CBLLoader(xml)
        entries = loader.parse_entries()
        assert entries == []


class TestFormatSearchTerm:

    def test_default_pattern(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(SAMPLE_CBL)
        result = loader._format_search_term("Batman", "1", "2020", "2020")
        assert "Batman" in result
        assert "001" in result

    def test_custom_pattern(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(SAMPLE_CBL, rename_pattern="{series_name} ({year}) {issue_number}")
        result = loader._format_search_term("Batman", "5", "2020", "2020")
        assert "Batman" in result
        assert "(2020)" in result
        assert "005" in result

    def test_pads_issue_to_3_digits(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(SAMPLE_CBL)
        result = loader._format_search_term("X-Men", "7", None, None)
        assert "007" in result

    def test_cleans_special_chars_from_series(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(SAMPLE_CBL)
        result = loader._format_search_term("Batman: The Dark Knight", "1", None, None)
        # Colon replaced with ' -', other special chars removed
        assert ":" not in result

    def test_removes_empty_placeholders(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(SAMPLE_CBL, rename_pattern="{series_name} ({year})")
        result = loader._format_search_term("Batman", "1", None, None)
        # {year} should be removed, empty parens should be cleaned
        assert "()" not in result


class TestMatchFile:

    def test_returns_none_when_no_series(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(SAMPLE_CBL)
        assert loader.match_file(None, "1", None, None) is None

    def test_returns_none_when_no_number(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(SAMPLE_CBL)
        assert loader.match_file("Batman", None, None, None) is None

    def test_returns_none_when_no_results(self):
        from models.cbl import CBLLoader
        loader = CBLLoader(SAMPLE_CBL)
        assert loader.match_file("Batman", "1", "2020", "2020") is None

    def test_returns_best_match_with_issue_number(self):
        from models.cbl import CBLLoader
        mock_results = [
            {"path": "/data/DC/Batman/v2020/Batman 001 (2020).cbz", "name": "Batman 001 (2020).cbz"},
            {"path": "/data/DC/Batman/v2020/Batman 002 (2020).cbz", "name": "Batman 002 (2020).cbz"},
        ]
        with patch("models.cbl.search_file_index", return_value=mock_results):
            loader = CBLLoader(SAMPLE_CBL)
            result = loader.match_file("Batman", "1", "2020", "2020")
            assert result is not None
            assert "001" in result

    def test_publisher_in_path_boosts_score(self):
        from models.cbl import CBLLoader
        mock_results = [
            {"path": "/data/DC/Batman/Batman 001.cbz", "name": "Batman 001.cbz"},
            {"path": "/data/Other/Batman/Batman 001.cbz", "name": "Batman 001.cbz"},
        ]
        with patch("models.cbl.search_file_index", return_value=mock_results):
            loader = CBLLoader(SAMPLE_CBL, filename="[DC] List.cbl")
            result = loader.match_file("Batman", "1", None, None)
            assert result is not None
            assert "/DC/" in result


class TestMatchByMetadata:

    def _meta_result(self, path, name, ci_series, ci_number,
                     ci_volume="", ci_year="", ci_publisher=""):
        return {
            "path": path, "name": name, "type": "file", "parent": "/data",
            "size": 1000, "ci_series": ci_series, "ci_number": ci_number,
            "ci_volume": ci_volume, "ci_year": ci_year,
            "ci_publisher": ci_publisher,
        }

    def test_metadata_match_returns_path(self):
        from models.cbl import CBLLoader
        results = [self._meta_result(
            "/data/DC/Batman/Batman 001.cbz", "Batman 001.cbz",
            "Batman", "1", ci_volume="2020", ci_year="2020"
        )]
        with patch("models.cbl.search_by_comic_metadata", return_value=results):
            loader = CBLLoader(SAMPLE_CBL)
            result = loader.match_file("Batman", "1", "2020", "2020")
            assert result == "/data/DC/Batman/Batman 001.cbz"

    def test_metadata_prefers_exact_series(self):
        from models.cbl import CBLLoader
        results = [
            self._meta_result(
                "/data/DC/Batman White Knight/BWK 001.cbz", "BWK 001.cbz",
                "Batman: White Knight", "1"
            ),
            self._meta_result(
                "/data/DC/Batman/Batman 001.cbz", "Batman 001.cbz",
                "Batman", "1"
            ),
        ]
        with patch("models.cbl.search_by_comic_metadata", return_value=results):
            loader = CBLLoader(SAMPLE_CBL)
            result = loader.match_file("Batman", "1", None, None)
            assert result == "/data/DC/Batman/Batman 001.cbz"

    def test_metadata_prefers_volume_match(self):
        from models.cbl import CBLLoader
        results = [
            self._meta_result(
                "/data/DC/Batman/v2016/Batman 001.cbz", "Batman 001.cbz",
                "Batman", "1", ci_volume="2016", ci_year="2016"
            ),
            self._meta_result(
                "/data/DC/Batman/v2020/Batman 001.cbz", "Batman 001.cbz",
                "Batman", "1", ci_volume="2020", ci_year="2020"
            ),
        ]
        with patch("models.cbl.search_by_comic_metadata", return_value=results):
            loader = CBLLoader(SAMPLE_CBL)
            result = loader.match_file("Batman", "1", "2020", "2020")
            assert "v2020" in result

    def test_metadata_prefers_year_match(self):
        from models.cbl import CBLLoader
        results = [
            self._meta_result(
                "/data/DC/Batman/Batman 005 (2016).cbz", "Batman 005 (2016).cbz",
                "Batman", "5", ci_year="2016"
            ),
            self._meta_result(
                "/data/DC/Batman/Batman 005 (2018).cbz", "Batman 005 (2018).cbz",
                "Batman", "5", ci_year="2018"
            ),
        ]
        with patch("models.cbl.search_by_comic_metadata", return_value=results):
            loader = CBLLoader(SAMPLE_CBL)
            result = loader.match_file("Batman", "5", None, "2018")
            assert "2018" in result

    def test_metadata_publisher_boost(self):
        from models.cbl import CBLLoader
        results = [
            self._meta_result(
                "/data/Image/Batman/Batman 001.cbz", "Batman 001.cbz",
                "Batman", "1", ci_publisher="Image"
            ),
            self._meta_result(
                "/data/DC/Batman/Batman 001.cbz", "Batman 001.cbz",
                "Batman", "1", ci_publisher="DC Comics"
            ),
        ]
        with patch("models.cbl.search_by_comic_metadata", return_value=results):
            loader = CBLLoader(SAMPLE_CBL, filename="[DC] Batman.cbl")
            result = loader.match_file("Batman", "1", None, None)
            assert result == "/data/DC/Batman/Batman 001.cbz"

    def test_metadata_no_results_falls_to_filename(self):
        from models.cbl import CBLLoader
        filename_results = [
            {"path": "/data/DC/Batman/Batman 001.cbz", "name": "Batman 001.cbz"},
        ]
        with patch("models.cbl.search_by_comic_metadata", return_value=[]), \
             patch("models.cbl.search_file_index", return_value=filename_results):
            loader = CBLLoader(SAMPLE_CBL)
            result = loader.match_file("Batman", "1", None, None)
            assert result == "/data/DC/Batman/Batman 001.cbz"

    def test_metadata_partial_series_match(self):
        from models.cbl import CBLLoader
        results = [self._meta_result(
            "/data/DC/Batman - The Dark Knight/BTDK 001.cbz", "BTDK 001.cbz",
            "Batman - The Dark Knight", "1"
        )]
        with patch("models.cbl.search_by_comic_metadata", return_value=results):
            loader = CBLLoader(SAMPLE_CBL)
            # Search for "Batman: The Dark Knight" should match "Batman - The Dark Knight"
            result = loader.match_file("Batman: The Dark Knight", "1", None, None)
            assert result is not None

    def test_metadata_dash_in_series_name(self):
        from models.cbl import CBLLoader
        results = [self._meta_result(
            "/data/DC/BLODK/Batman Legends of the Dark Knight 060.cbz",
            "Batman Legends of the Dark Knight 060.cbz",
            "Batman: Legends of the Dark Knight", "60", ci_year="1994"
        )]
        with patch("models.cbl.search_by_comic_metadata", return_value=results):
            loader = CBLLoader(SAMPLE_CBL)
            # CBL has dash, metadata has colon — both should normalize
            result = loader.match_file(
                "Batman - Legends of the Dark Knight", "60", None, "1994"
            )
            assert result is not None
            # Should get exact match score (colon/dash-normalized comparison)
            assert "060" in result

    def test_metadata_number_zero_padding(self):
        from models.cbl import CBLLoader
        results = [self._meta_result(
            "/data/DC/Batman/Batman 001.cbz", "Batman 001.cbz",
            "Batman", "001"
        )]
        with patch("models.cbl.search_by_comic_metadata", return_value=results):
            loader = CBLLoader(SAMPLE_CBL)
            # Searching for "1" should match metadata with "001" (handled by DB query CAST)
            result = loader.match_file("Batman", "1", None, None)
            assert result == "/data/DC/Batman/Batman 001.cbz"
