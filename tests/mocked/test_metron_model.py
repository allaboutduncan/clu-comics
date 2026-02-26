"""Tests for models/metron.py -- mocked Mokkari API."""
import pytest
from unittest.mock import patch, MagicMock, mock_open
from tests.mocked.conftest import make_mock_series, make_mock_issue


class TestIsModkariAvailable:

    def test_returns_bool(self):
        from models.metron import is_mokkari_available
        assert isinstance(is_mokkari_available(), bool)


class TestGetApi:

    @patch("models.metron.MOKKARI_AVAILABLE", True)
    @patch("models.metron.MokkariSession", create=True)
    def test_returns_session(self, mock_session_class):
        from models.metron import get_api

        mock_session_class.return_value = MagicMock()
        api = get_api("user", "pass")
        assert api is not None
        mock_session_class.assert_called_once()

    @patch("models.metron.MOKKARI_AVAILABLE", True)
    def test_empty_credentials(self):
        from models.metron import get_api
        assert get_api("", "") is None
        assert get_api(None, None) is None

    @patch("models.metron.MOKKARI_AVAILABLE", False)
    def test_mokkari_not_installed(self):
        from models.metron import get_api
        assert get_api("user", "pass") is None


class TestIsConnectionError:

    def test_timeout_detected(self):
        from models.metron import is_connection_error
        assert is_connection_error(Exception("Connection timeout")) is True

    def test_normal_error_not_connection(self):
        from models.metron import is_connection_error
        assert is_connection_error(Exception("Invalid credentials")) is False

    def test_various_network_errors(self):
        from models.metron import is_connection_error
        assert is_connection_error(Exception("connection refused")) is True
        assert is_connection_error(Exception("Name resolution failed")) is True
        assert is_connection_error(Exception("ReadTimeout occurred")) is True


class TestParseCvinfo:

    def test_parse_metron_id(self, tmp_path):
        from models.metron import parse_cvinfo_for_metron_id

        cvinfo = tmp_path / "cvinfo"
        cvinfo.write_text("https://comicvine.gamespot.com/batman/4050-12345/\nseries_id: 100\n")

        assert parse_cvinfo_for_metron_id(str(cvinfo)) == 100

    def test_no_series_id(self, tmp_path):
        from models.metron import parse_cvinfo_for_metron_id

        cvinfo = tmp_path / "cvinfo"
        cvinfo.write_text("https://comicvine.gamespot.com/batman/4050-12345/\n")

        assert parse_cvinfo_for_metron_id(str(cvinfo)) is None

    def test_parse_comicvine_id(self, tmp_path):
        from models.metron import parse_cvinfo_for_comicvine_id

        cvinfo = tmp_path / "cvinfo"
        cvinfo.write_text("https://comicvine.gamespot.com/batman/4050-12345/\nseries_id: 100\n")

        assert parse_cvinfo_for_comicvine_id(str(cvinfo)) == 12345


class TestGetSeriesIdByComicvineId:

    def test_found(self):
        from models.metron import get_series_id_by_comicvine_id

        mock_api = MagicMock()
        mock_series = make_mock_series(id=42)
        mock_api.series_list.return_value = [mock_series]

        assert get_series_id_by_comicvine_id(mock_api, 12345) == 42

    def test_not_found(self):
        from models.metron import get_series_id_by_comicvine_id

        mock_api = MagicMock()
        mock_api.series_list.return_value = []

        assert get_series_id_by_comicvine_id(mock_api, 99999) is None


class TestSearchSeriesByName:

    def test_returns_best_match(self):
        from models.metron import search_series_by_name

        mock_api = MagicMock()
        s = make_mock_series(id=100, name="Batman", year_began=2016)
        mock_api.series_list.return_value = [s]

        result = search_series_by_name(mock_api, "Batman")
        assert result is not None
        assert result["id"] == 100
        assert result["name"] == "Batman"

    def test_year_ranking(self):
        from models.metron import search_series_by_name

        mock_api = MagicMock()
        s1 = make_mock_series(id=1, name="Batman", year_began=1940)
        s2 = make_mock_series(id=2, name="Batman", year_began=2016)
        mock_api.series_list.return_value = [s1, s2]

        result = search_series_by_name(mock_api, "Batman", year=2016)
        assert result["id"] == 2  # Closer to 2016

    def test_no_results(self):
        from models.metron import search_series_by_name

        mock_api = MagicMock()
        mock_api.series_list.return_value = []

        assert search_series_by_name(mock_api, "Nonexistent") is None

    def test_no_api(self):
        from models.metron import search_series_by_name
        assert search_series_by_name(None, "Batman") is None


class TestGetSeriesDetails:

    def test_returns_details(self):
        from models.metron import get_series_details

        mock_api = MagicMock()
        mock_api.series.return_value = make_mock_series(id=100, cv_id=12345)

        result = get_series_details(mock_api, 100)
        assert result["id"] == 100
        assert result["cv_id"] == 12345

    def test_not_found(self):
        from models.metron import get_series_details

        mock_api = MagicMock()
        mock_api.series.return_value = None

        assert get_series_details(mock_api, 9999) is None


class TestGetIssueMetadata:

    def test_double_fetch_pattern(self):
        from models.metron import get_issue_metadata

        mock_api = MagicMock()
        mock_issue_list = [MagicMock(id=500)]
        mock_api.issues_list.return_value = mock_issue_list
        full_issue = make_mock_issue(id=500)
        mock_api.issue.return_value = full_issue

        result = get_issue_metadata(mock_api, 100, "1")
        assert result is not None
        mock_api.issues_list.assert_called_once()
        mock_api.issue.assert_called_once_with(500)

    def test_issue_not_found(self):
        from models.metron import get_issue_metadata

        mock_api = MagicMock()
        mock_api.issues_list.return_value = []

        assert get_issue_metadata(mock_api, 100, "999") is None


class TestGetAllIssuesForSeries:

    def test_returns_issues(self):
        from models.metron import get_all_issues_for_series

        mock_api = MagicMock()
        mock_api.issues_list.return_value = [MagicMock(id=1), MagicMock(id=2)]

        result = get_all_issues_for_series(mock_api, 100)
        assert len(result) == 2

    def test_empty_series(self):
        from models.metron import get_all_issues_for_series

        mock_api = MagicMock()
        mock_api.issues_list.return_value = []

        assert get_all_issues_for_series(mock_api, 100) == []


class TestMapToComicinfo:

    def test_full_mapping(self):
        from models.metron import map_to_comicinfo

        issue_data = {
            "id": 500,
            "number": "1",
            "story_titles": ["The Beginning"],
            "cover_date": "2020-06-15",
            "series": {"name": "Batman", "year_began": 2016, "genres": [{"name": "Superhero"}]},
            "publisher": {"name": "DC Comics"},
            "credits": [
                {"creator": "Tom King", "role": [{"name": "Writer"}]},
                {"creator": "David Finch", "role": [{"name": "Penciller"}]},
            ],
            "characters": [{"name": "Batman"}, {"name": "Catwoman"}],
            "teams": [{"name": "Justice League"}],
            "rating": {"name": "Teen"},
            "desc": "Batman returns to Gotham",
            "resource_url": "https://metron.cloud/issue/500/",
            "modified": "2024-01-01",
            "page_count": 32,
        }

        result = map_to_comicinfo(issue_data)

        assert result["Series"] == "Batman"
        assert result["Number"] == "1"
        assert result["Title"] == "The Beginning"
        assert result["Year"] == 2020
        assert result["Month"] == 6
        assert result["Day"] == 15
        assert result["Publisher"] == "DC Comics"
        assert result["Writer"] == "Tom King"
        assert result["Penciller"] == "David Finch"
        assert "Batman" in result["Characters"]
        assert result["Genre"] == "Superhero"
        assert result["LanguageISO"] == "en"
        assert result["MetronId"] == 500

    def test_minimal_data(self):
        from models.metron import map_to_comicinfo

        result = map_to_comicinfo({"id": 1, "number": "1"})
        assert "Number" in result
        assert result["Number"] == "1"
        assert "Notes" in result


class TestExtractCreditsByRole:

    def test_extracts_writers(self):
        from models.metron import extract_credits_by_role

        credits = [
            {"creator": "Tom King", "role": [{"name": "Writer"}]},
            {"creator": "David Finch", "role": [{"name": "Penciller"}]},
        ]
        result = extract_credits_by_role(credits, ["Writer"])
        assert result == "Tom King"

    def test_multiple_matches(self):
        from models.metron import extract_credits_by_role

        credits = [
            {"creator": "Tom King", "role": [{"name": "Writer"}]},
            {"creator": "Scott Snyder", "role": [{"name": "Writer"}]},
        ]
        result = extract_credits_by_role(credits, ["Writer"])
        assert "Tom King" in result
        assert "Scott Snyder" in result

    def test_no_matches(self):
        from models.metron import extract_credits_by_role

        credits = [{"creator": "David Finch", "role": [{"name": "Penciller"}]}]
        result = extract_credits_by_role(credits, ["Writer"])
        assert result == ""


class TestCalculateComicWeek:

    def test_returns_tuple(self):
        from models.metron import calculate_comic_week
        from datetime import datetime

        start, end = calculate_comic_week(datetime(2024, 1, 15))  # Monday
        assert start.weekday() == 6  # Sunday
        assert end.weekday() == 5    # Saturday

    def test_string_date(self):
        from models.metron import calculate_comic_week

        start, end = calculate_comic_week("2024-01-15")
        assert start is not None
        assert end is not None

    def test_defaults_to_now(self):
        from models.metron import calculate_comic_week

        start, end = calculate_comic_week()
        assert start is not None


class TestUpdateCvinfoWithMetronId:

    def test_appends_series_id(self, tmp_path):
        from models.metron import update_cvinfo_with_metron_id

        cvinfo = tmp_path / "cvinfo"
        cvinfo.write_text("https://comicvine.gamespot.com/batman/4050-12345/\n")

        assert update_cvinfo_with_metron_id(str(cvinfo), 100) is True
        content = cvinfo.read_text()
        assert "series_id: 100" in content

    def test_updates_existing(self, tmp_path):
        from models.metron import update_cvinfo_with_metron_id

        cvinfo = tmp_path / "cvinfo"
        cvinfo.write_text("series_id: 50\n")

        assert update_cvinfo_with_metron_id(str(cvinfo), 100) is True
        content = cvinfo.read_text()
        assert "series_id: 100" in content
        assert "series_id: 50" not in content


class TestGetReleases:

    def test_fetches_releases(self):
        from models.metron import get_releases

        mock_api = MagicMock()
        mock_api.issues_list.return_value = [MagicMock(), MagicMock()]

        result = get_releases(mock_api, "2024-01-01", "2024-01-07")
        assert len(result) == 2

    def test_no_api(self):
        from models.metron import get_releases
        assert get_releases(None, "2024-01-01") == []
