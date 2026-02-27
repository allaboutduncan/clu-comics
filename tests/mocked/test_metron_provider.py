"""Tests for MetronProvider adapter -- mocked Mokkari/metron module."""
import pytest
from unittest.mock import patch, MagicMock

from models.providers.base import ProviderType, ProviderCredentials, SearchResult, IssueResult
from tests.mocked.conftest import make_mock_series, make_mock_issue


class TestMetronProviderInit:

    def test_provider_attributes(self):
        from models.providers.metron_provider import MetronProvider

        p = MetronProvider()
        assert p.provider_type == ProviderType.METRON
        assert p.display_name == "Metron"
        assert p.requires_auth is True
        assert p.auth_fields == ["username", "password"]

    def test_no_api_without_credentials(self):
        from models.providers.metron_provider import MetronProvider

        p = MetronProvider()
        assert p._get_api() is None

    @patch("models.metron.get_api")
    def test_get_api_with_credentials(self, mock_get_api, metron_creds):
        from models.providers.metron_provider import MetronProvider

        mock_api = MagicMock()
        mock_get_api.return_value = mock_api

        p = MetronProvider(credentials=metron_creds)
        api = p._get_api()
        assert api is mock_api
        mock_get_api.assert_called_once_with("testuser", "testpass")


class TestMetronProviderTestConnection:

    @patch("models.metron.get_api")
    def test_successful_connection(self, mock_get_api, metron_creds):
        from models.providers.metron_provider import MetronProvider

        mock_api = MagicMock()
        mock_api.publishers_list.return_value = [MagicMock()]
        mock_get_api.return_value = mock_api

        p = MetronProvider(credentials=metron_creds)
        assert p.test_connection() is True

    @patch("models.metron.get_api", return_value=None)
    def test_mokkari_unavailable(self, mock_get_api, metron_creds):
        from models.providers.metron_provider import MetronProvider

        p = MetronProvider(credentials=metron_creds)
        assert p.test_connection() is False

    def test_no_credentials(self):
        from models.providers.metron_provider import MetronProvider

        p = MetronProvider()
        assert p.test_connection() is False


class TestMetronProviderSearchSeries:

    @patch("models.metron.get_api")
    @patch("models.metron.search_series_by_name")
    def test_search_returns_results(self, mock_search, mock_get_api, metron_creds):
        from models.providers.metron_provider import MetronProvider

        mock_get_api.return_value = MagicMock()
        mock_search.return_value = {
            "id": 100, "name": "Batman", "year_began": 2016,
            "publisher_name": "DC Comics", "issue_count": 50,
        }

        p = MetronProvider(credentials=metron_creds)
        results = p.search_series("Batman")

        assert len(results) == 1
        assert isinstance(results[0], SearchResult)
        assert results[0].title == "Batman"
        assert results[0].id == "100"
        assert results[0].provider == ProviderType.METRON

    @patch("models.metron.get_api")
    @patch("models.metron.search_series_by_name", return_value=None)
    def test_search_no_results(self, mock_search, mock_get_api, metron_creds):
        from models.providers.metron_provider import MetronProvider

        mock_get_api.return_value = MagicMock()
        p = MetronProvider(credentials=metron_creds)
        assert p.search_series("NonexistentSeries") == []

    def test_search_without_credentials(self):
        from models.providers.metron_provider import MetronProvider

        p = MetronProvider()
        assert p.search_series("Batman") == []


class TestMetronProviderGetSeries:

    @patch("models.metron.get_api")
    @patch("models.metron.get_series_details")
    def test_get_series_by_id(self, mock_details, mock_get_api, metron_creds):
        from models.providers.metron_provider import MetronProvider

        mock_get_api.return_value = MagicMock()
        mock_details.return_value = {
            "id": 100, "name": "Batman", "year_began": 2016,
            "publisher_name": "DC Comics", "desc": "The Dark Knight",
        }

        p = MetronProvider(credentials=metron_creds)
        result = p.get_series("100")

        assert isinstance(result, SearchResult)
        assert result.title == "Batman"
        assert result.description == "The Dark Knight"

    @patch("models.metron.get_api")
    @patch("models.metron.get_series_details", return_value=None)
    def test_get_series_not_found(self, mock_details, mock_get_api, metron_creds):
        from models.providers.metron_provider import MetronProvider

        mock_get_api.return_value = MagicMock()
        p = MetronProvider(credentials=metron_creds)
        assert p.get_series("9999") is None


class TestMetronProviderGetIssues:

    @patch("models.metron.get_api")
    @patch("models.metron.get_all_issues_for_series")
    def test_returns_issue_results(self, mock_issues, mock_get_api, metron_creds):
        from models.providers.metron_provider import MetronProvider

        mock_get_api.return_value = MagicMock()
        mock_issues.return_value = [
            {"id": 1, "number": "1", "name": "First", "cover_date": "2020-01-01",
             "store_date": None, "image": None},
            {"id": 2, "number": "2", "name": "Second", "cover_date": "2020-02-01",
             "store_date": None, "image": None},
        ]

        p = MetronProvider(credentials=metron_creds)
        results = p.get_issues("100")

        assert len(results) == 2
        assert all(isinstance(r, IssueResult) for r in results)
        assert results[0].issue_number == "1"
        assert results[1].issue_number == "2"

    @patch("models.metron.get_api")
    @patch("models.metron.get_all_issues_for_series")
    def test_handles_object_issues(self, mock_issues, mock_get_api, metron_creds):
        from models.providers.metron_provider import MetronProvider

        mock_get_api.return_value = MagicMock()
        mock_issue = make_mock_issue(id=10, number="5", name="Five")
        mock_issues.return_value = [mock_issue]

        p = MetronProvider(credentials=metron_creds)
        results = p.get_issues("100")

        assert len(results) == 1
        assert results[0].issue_number == "5"

    @patch("models.metron.get_api")
    @patch("models.metron.get_all_issues_for_series", return_value=[])
    def test_empty_series(self, mock_issues, mock_get_api, metron_creds):
        from models.providers.metron_provider import MetronProvider

        mock_get_api.return_value = MagicMock()
        p = MetronProvider(credentials=metron_creds)
        assert p.get_issues("100") == []


class TestMetronProviderGetIssue:

    @patch("models.metron.get_api")
    def test_get_single_issue(self, mock_get_api, metron_creds):
        from models.providers.metron_provider import MetronProvider

        mock_api = MagicMock()
        mock_api.issue.return_value = make_mock_issue(id=500, number="1")
        mock_get_api.return_value = mock_api

        p = MetronProvider(credentials=metron_creds)
        result = p.get_issue("500")

        assert isinstance(result, IssueResult)
        assert result.id == "500"
        assert result.issue_number == "1"

    @patch("models.metron.get_api")
    def test_issue_not_found(self, mock_get_api, metron_creds):
        from models.providers.metron_provider import MetronProvider

        mock_api = MagicMock()
        mock_api.issue.return_value = None
        mock_get_api.return_value = mock_api

        p = MetronProvider(credentials=metron_creds)
        assert p.get_issue("9999") is None


class TestMetronProviderToComicinfo:

    @patch("models.metron.get_api")
    @patch("models.metron.map_to_comicinfo")
    def test_maps_to_comicinfo(self, mock_map, mock_get_api, metron_creds):
        from models.providers.metron_provider import MetronProvider

        mock_api = MagicMock()
        mock_api.issue.return_value = make_mock_issue()
        mock_get_api.return_value = mock_api
        mock_map.return_value = {"Series": "Batman", "Number": "1", "Publisher": "DC Comics"}

        p = MetronProvider(credentials=metron_creds)
        issue = IssueResult(
            provider=ProviderType.METRON, id="500", series_id="100",
            issue_number="1", title="Test",
        )

        result = p.to_comicinfo(issue)
        assert result["Series"] == "Batman"
        assert result["Publisher"] == "DC Comics"

    def test_fallback_without_api(self):
        from models.providers.metron_provider import MetronProvider

        p = MetronProvider()
        issue = IssueResult(
            provider=ProviderType.METRON, id="500", series_id="100",
            issue_number="1", title="Test", cover_date="2020-01-15",
        )
        series = SearchResult(
            provider=ProviderType.METRON, id="100", title="Batman",
            year=2016, publisher="DC Comics",
        )

        result = p.to_comicinfo(issue, series)
        assert result["Series"] == "Batman"
        assert result["Number"] == "1"
        assert result["Publisher"] == "DC Comics"
        assert result["Year"] == 2020
