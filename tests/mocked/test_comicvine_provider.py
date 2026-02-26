"""Tests for ComicVineProvider adapter -- mocked Simyan/ComicVine."""
import sys
import pytest
from unittest.mock import patch, MagicMock

from models.providers.base import ProviderType, ProviderCredentials, SearchResult, IssueResult
from tests.mocked.conftest import make_mock_cv_volume, make_mock_cv_issue

# Fake simyan module so 'from simyan.comicvine import ...' works even when
# simyan is not installed.  Injected into sys.modules for the duration of
# tests that exercise code paths containing that import.
_fake_simyan = MagicMock()
_SIMYAN_MODULES = {
    "simyan": _fake_simyan,
    "simyan.comicvine": _fake_simyan.comicvine,
}


class TestComicVineProviderInit:

    def test_provider_attributes(self):
        from models.providers.comicvine_provider import ComicVineProvider

        p = ComicVineProvider()
        assert p.provider_type == ProviderType.COMICVINE
        assert p.display_name == "ComicVine"
        assert p.requires_auth is True
        assert p.auth_fields == ["api_key"]

    def test_no_client_without_credentials(self):
        from models.providers.comicvine_provider import ComicVineProvider

        p = ComicVineProvider()
        assert p._get_client() is None


class TestComicVineProviderTestConnection:

    @patch.dict(sys.modules, _SIMYAN_MODULES)
    def test_successful_connection(self, comicvine_creds):
        from models.providers.comicvine_provider import ComicVineProvider

        mock_cv = MagicMock()
        mock_cv.search.return_value = [MagicMock()]

        p = ComicVineProvider(credentials=comicvine_creds)
        p._cv = mock_cv  # Bypass _get_client import of simyan
        assert p.test_connection() is True

    def test_no_credentials(self):
        from models.providers.comicvine_provider import ComicVineProvider

        p = ComicVineProvider()
        assert p.test_connection() is False


class TestComicVineProviderSearchSeries:

    @patch("models.comicvine.is_simyan_available", return_value=True)
    @patch("models.comicvine.search_volumes")
    def test_search_returns_results(self, mock_search, mock_avail, comicvine_creds):
        from models.providers.comicvine_provider import ComicVineProvider

        mock_search.return_value = [
            {"id": 4050, "name": "Batman", "start_year": 2016,
             "publisher_name": "DC Comics", "count_of_issues": 50,
             "image_url": "https://example.com/img.jpg", "description": "Dark Knight"},
        ]

        p = ComicVineProvider(credentials=comicvine_creds)
        results = p.search_series("Batman")

        assert len(results) == 1
        assert results[0].title == "Batman"
        assert results[0].provider == ProviderType.COMICVINE

    @patch("models.comicvine.is_simyan_available", return_value=True)
    @patch("models.comicvine.search_volumes", return_value=[])
    def test_no_results(self, mock_search, mock_avail, comicvine_creds):
        from models.providers.comicvine_provider import ComicVineProvider

        p = ComicVineProvider(credentials=comicvine_creds)
        assert p.search_series("Nonexistent") == []


class TestComicVineProviderGetSeries:

    @patch("models.comicvine.is_simyan_available", return_value=True)
    @patch("models.comicvine.get_volume_details")
    def test_get_series_by_id(self, mock_details, mock_avail, comicvine_creds):
        from models.providers.comicvine_provider import ComicVineProvider

        mock_details.return_value = {
            "id": 4050, "name": "Batman", "start_year": 2016,
            "publisher_name": "DC", "count_of_issues": 50,
            "image_url": None, "description": "Test",
        }

        p = ComicVineProvider(credentials=comicvine_creds)
        result = p.get_series("4050")

        assert isinstance(result, SearchResult)
        assert result.title == "Batman"

    @patch("models.comicvine.is_simyan_available", return_value=True)
    @patch("models.comicvine.get_volume_details", return_value=None)
    def test_series_not_found(self, mock_details, mock_avail, comicvine_creds):
        from models.providers.comicvine_provider import ComicVineProvider

        p = ComicVineProvider(credentials=comicvine_creds)
        assert p.get_series("9999") is None


class TestComicVineProviderGetIssues:

    def test_returns_issues(self, comicvine_creds):
        from models.providers.comicvine_provider import ComicVineProvider

        mock_cv = MagicMock()
        mock_cv.list_issues.return_value = [
            make_mock_cv_issue(id=1, issue_number="1"),
            make_mock_cv_issue(id=2, issue_number="2"),
        ]

        p = ComicVineProvider(credentials=comicvine_creds)
        p._cv = mock_cv  # Inject mock client directly
        results = p.get_issues("4050")

        assert len(results) == 2
        assert all(isinstance(r, IssueResult) for r in results)

    def test_empty_volume(self, comicvine_creds):
        from models.providers.comicvine_provider import ComicVineProvider

        mock_cv = MagicMock()
        mock_cv.list_issues.return_value = []

        p = ComicVineProvider(credentials=comicvine_creds)
        p._cv = mock_cv
        assert p.get_issues("4050") == []


class TestComicVineProviderGetIssue:

    def test_get_single_issue(self, comicvine_creds):
        from models.providers.comicvine_provider import ComicVineProvider

        mock_cv = MagicMock()
        mock_cv.issue.return_value = make_mock_cv_issue(id=1001, issue_number="5")

        p = ComicVineProvider(credentials=comicvine_creds)
        p._cv = mock_cv
        result = p.get_issue("1001")

        assert isinstance(result, IssueResult)
        assert result.id == "1001"
        assert result.issue_number == "5"

    def test_issue_not_found(self, comicvine_creds):
        from models.providers.comicvine_provider import ComicVineProvider

        mock_cv = MagicMock()
        mock_cv.issue.return_value = None

        p = ComicVineProvider(credentials=comicvine_creds)
        p._cv = mock_cv
        assert p.get_issue("9999") is None


class TestComicVineProviderToComicinfo:

    def test_fallback_without_api(self, comicvine_creds):
        from models.providers.comicvine_provider import ComicVineProvider

        p = ComicVineProvider()  # no credentials => fallback path
        issue = IssueResult(
            provider=ProviderType.COMICVINE, id="1001", series_id="4050",
            issue_number="5", title="Rebirth", cover_date="2020-06-15",
            summary="Batman returns",
        )
        series = SearchResult(
            provider=ProviderType.COMICVINE, id="4050", title="Batman",
            year=2016, publisher="DC Comics",
        )

        result = p.to_comicinfo(issue, series)
        assert result["Series"] == "Batman"
        assert result["Number"] == "5"
        assert result["Publisher"] == "DC Comics"
        assert result["Year"] == 2020
