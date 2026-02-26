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
    with patch("models.cbl.search_file_index", return_value=[]):
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
