"""Tests for models/comicvine.py -- mocked Simyan library."""
import pytest
from unittest.mock import patch, MagicMock
from tests.mocked.conftest import make_mock_cv_volume, make_mock_cv_issue


class TestIsSimyanAvailable:

    def test_returns_bool(self):
        from models.comicvine import is_simyan_available
        assert isinstance(is_simyan_available(), bool)


class TestSearchVolumes:

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource", create=True)
    @patch("models.comicvine.Comicvine", create=True)
    def test_returns_volumes(self, mock_cv_class, mock_resource):
        from models.comicvine import search_volumes

        mock_cv = MagicMock()
        mock_cv.search.return_value = [
            make_mock_cv_volume(id=4050, name="Batman", start_year=2016),
        ]
        mock_cv_class.return_value = mock_cv

        results = search_volumes("fake-key", "Batman")
        assert len(results) == 1
        assert results[0]["name"] == "Batman"
        assert results[0]["id"] == 4050

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource", create=True)
    @patch("models.comicvine.Comicvine", create=True)
    def test_no_results(self, mock_cv_class, mock_resource):
        from models.comicvine import search_volumes

        mock_cv = MagicMock()
        mock_cv.search.return_value = []
        mock_cv_class.return_value = mock_cv

        assert search_volumes("fake-key", "Nonexistent") == []

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource", create=True)
    @patch("models.comicvine.Comicvine", create=True)
    def test_year_ranking(self, mock_cv_class, mock_resource):
        from models.comicvine import search_volumes

        mock_cv = MagicMock()
        mock_cv.search.return_value = [
            make_mock_cv_volume(id=1, start_year=1940),
            make_mock_cv_volume(id=2, start_year=2016),
        ]
        mock_cv_class.return_value = mock_cv

        results = search_volumes("fake-key", "Batman", year=2016)
        assert results[0]["id"] == 2  # Closer to 2016

    @patch("models.comicvine.SIMYAN_AVAILABLE", False)
    def test_simyan_not_available(self):
        from models.comicvine import search_volumes

        with pytest.raises(Exception, match="Simyan library not installed"):
            search_volumes("fake-key", "Batman")


class TestGetIssueByNumber:

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource", create=True)
    @patch("models.comicvine.Comicvine", create=True)
    def test_finds_issue(self, mock_cv_class, mock_resource):
        from models.comicvine import get_issue_by_number

        mock_cv = MagicMock()
        basic_issue = MagicMock()
        basic_issue.id = 1001
        mock_cv.list_issues.return_value = [basic_issue]

        full_issue = make_mock_cv_issue(id=1001, issue_number="5", name="Rebirth")
        full_issue.creators = []
        full_issue.characters = []
        full_issue.teams = []
        full_issue.locations = []
        full_issue.story_arcs = []
        mock_cv.get_issue.return_value = full_issue
        mock_cv_class.return_value = mock_cv

        result = get_issue_by_number("fake-key", 4050, "5")
        assert result is not None
        assert result["id"] == 1001

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource", create=True)
    @patch("models.comicvine.Comicvine", create=True)
    def test_issue_not_found(self, mock_cv_class, mock_resource):
        from models.comicvine import get_issue_by_number

        mock_cv = MagicMock()
        mock_cv.list_issues.return_value = []
        mock_cv_class.return_value = mock_cv

        assert get_issue_by_number("fake-key", 4050, "999") is None


class TestMapToComicinfo:

    def test_full_mapping(self):
        from models.comicvine import map_to_comicinfo

        issue_data = {
            "id": 1001,
            "name": "Rebirth",
            "issue_number": "1",
            "volume_name": "Batman",
            "volume_id": 4050,
            "publisher": "DC Comics",
            "cover_date": "2020-06-15",
            "year": 2020,
            "month": 6,
            "day": 15,
            "description": "Batman returns",
            "writers": ["Tom King"],
            "pencillers": ["David Finch"],
            "inkers": [],
            "colorists": [],
            "letterers": [],
            "cover_artists": [],
            "characters": ["Batman", "Catwoman"],
            "teams": ["Justice League"],
            "locations": ["Gotham City"],
            "story_arc": "City of Bane",
        }

        result = map_to_comicinfo(issue_data)

        assert result["Series"] == "Batman"
        assert result["Number"] == "1"
        assert result["Title"] == "Rebirth"
        assert result["Year"] == 2020
        assert result["Month"] == 6
        assert result["Publisher"] == "DC Comics"
        assert result["Writer"] == "Tom King"
        assert "Batman" in result["Characters"]
        assert result["StoryArc"] == "City of Bane"
        assert "LanguageISO" in result

    def test_with_volume_data(self):
        from models.comicvine import map_to_comicinfo

        issue_data = {"id": 1, "name": None, "issue_number": "1",
                      "volume_name": None, "volume_id": None,
                      "publisher": None, "year": 2020}
        volume_data = {"id": 4050, "name": "Batman", "start_year": 2016,
                       "publisher_name": "DC Comics"}

        result = map_to_comicinfo(issue_data, volume_data)
        assert result["Series"] == "Batman"
        assert result["Publisher"] == "DC Comics"
        assert result["Volume"] == 2016

    def test_start_year_override(self):
        from models.comicvine import map_to_comicinfo

        issue_data = {"id": 1, "issue_number": "1", "year": 2020}
        result = map_to_comicinfo(issue_data, None, start_year=2016)
        assert result["Volume"] == 2016


class TestGetVolumeDetails:

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource", create=True)
    @patch("models.comicvine.Comicvine", create=True)
    def test_returns_details(self, mock_cv_class, mock_resource):
        from models.comicvine import get_volume_details

        mock_cv = MagicMock()
        mock_vol = make_mock_cv_volume(start_year=2016, publisher_name="DC Comics")
        mock_cv.get_volume.return_value = mock_vol
        mock_cv_class.return_value = mock_cv

        result = get_volume_details("fake-key", 4050)
        assert result["start_year"] == 2016
        assert result["publisher_name"] == "DC Comics"

    @patch("models.comicvine.SIMYAN_AVAILABLE", False)
    def test_simyan_not_available(self):
        from models.comicvine import get_volume_details

        result = get_volume_details("fake-key", 4050)
        assert result["publisher_name"] is None
        assert result["start_year"] is None


class TestParseCvinfoVolumeId:

    def test_parses_volume_id(self, tmp_path):
        from models.comicvine import parse_cvinfo_volume_id

        cvinfo = tmp_path / "cvinfo"
        cvinfo.write_text("https://comicvine.gamespot.com/batman/4050-12345/\n")

        assert parse_cvinfo_volume_id(str(cvinfo)) == 12345

    def test_no_match(self, tmp_path):
        from models.comicvine import parse_cvinfo_volume_id

        cvinfo = tmp_path / "cvinfo"
        cvinfo.write_text("no url here\n")

        assert parse_cvinfo_volume_id(str(cvinfo)) is None


class TestFindCvinfoInFolder:

    def test_finds_cvinfo(self, tmp_path):
        from models.comicvine import find_cvinfo_in_folder

        (tmp_path / "cvinfo").write_text("test")
        result = find_cvinfo_in_folder(str(tmp_path))
        assert result is not None
        assert result.endswith("cvinfo")

    def test_no_cvinfo(self, tmp_path):
        from models.comicvine import find_cvinfo_in_folder
        assert find_cvinfo_in_folder(str(tmp_path)) is None


class TestRankVolumesByYear:

    def test_sorts_by_closest_year(self):
        from models.comicvine import _rank_volumes_by_year

        volumes = [
            {"name": "A", "start_year": 1940},
            {"name": "B", "start_year": 2016},
            {"name": "C", "start_year": None},
        ]

        result = _rank_volumes_by_year(volumes, 2016)
        assert result[0]["name"] == "B"
        assert result[-1]["name"] == "C"  # None year goes last


class TestGenerateComicInfoXml:

    def test_generates_valid_xml(self):
        from models.comicvine import generate_comicinfo_xml

        data = {
            "Series": "Batman",
            "Number": "1",
            "Title": "Rebirth",
            "Year": 2020,
            "Publisher": "DC Comics",
        }

        xml_bytes = generate_comicinfo_xml(data)
        assert isinstance(xml_bytes, bytes)
        assert b"<Series>Batman</Series>" in xml_bytes
        assert b"<Number>1</Number>" in xml_bytes
        assert b"<Publisher>DC Comics</Publisher>" in xml_bytes

    def test_decimal_issue_number_preserved(self):
        """Decimal issue numbers like 12.1 should not be truncated to 12."""
        from models.comicvine import generate_comicinfo_xml

        data = {"Series": "Avengers", "Number": "12.1", "Year": 2011}
        xml_bytes = generate_comicinfo_xml(data)
        assert b"<Number>12.1</Number>" in xml_bytes

    def test_decimal_issue_preserves_leading_zeros(self):
        """012.1 should stay '012.1', not be stripped to '12.1' via float()."""
        from models.comicvine import generate_comicinfo_xml

        data = {"Series": "Avengers", "Number": "012.1", "Year": 2011}
        xml_bytes = generate_comicinfo_xml(data)
        assert b"<Number>012.1</Number>" in xml_bytes

    def test_whole_number_drops_decimal(self):
        """12.0 should be stored as '12', not '12.0'."""
        from models.comicvine import generate_comicinfo_xml

        data = {"Series": "Batman", "Number": "12.0"}
        xml_bytes = generate_comicinfo_xml(data)
        assert b"<Number>12</Number>" in xml_bytes

    def test_omits_none_values(self):
        from models.comicvine import generate_comicinfo_xml

        data = {"Series": "Test", "Writer": None, "Publisher": None}
        xml_bytes = generate_comicinfo_xml(data)
        assert b"<Writer>" not in xml_bytes
        assert b"<Publisher>" not in xml_bytes
