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

    @patch("models.comicvine.is_simyan_available", return_value=True)
    @patch("models.comicvine._make_cv_client")
    def test_client_built_via_timeout_bounded_factory(
        self, mock_make, mock_avail, comicvine_creds
    ):
        """The adapter must build its Simyan client through the timeout-bounded
        factory — otherwise get_issues/get_issue/test_connection can hang a
        worker indefinitely on a slow ComicVine (regression: 'Applying…' stuck).
        """
        from models.providers.comicvine_provider import ComicVineProvider

        sentinel = MagicMock()
        mock_make.return_value = sentinel

        p = ComicVineProvider(credentials=comicvine_creds)
        client = p._get_client()

        assert client is sentinel
        mock_make.assert_called_once_with(comicvine_creds.api_key)


class TestMakeCvClientUserAgent:
    """_make_cv_client must set a browser-like User-Agent so ComicVine (behind
    Cloudflare) doesn't 403 the default Simyan UA from some hosts."""

    def test_passes_user_agent_kwarg(self):
        import models.comicvine as cv
        from models.providers.base import DEFAULT_PROVIDER_USER_AGENT

        sentinel = MagicMock()
        with patch.object(cv, "Comicvine", create=True) as mock_cv:
            mock_cv.return_value = sentinel
            client = cv._make_cv_client("KEY")

        assert client is sentinel
        _, kwargs = mock_cv.call_args
        assert kwargs.get("user_agent") == DEFAULT_PROVIDER_USER_AGENT
        # Modern Simyan (3.x) removed the `cache` kwarg; passing it raises TypeError
        # and disables the provider. The primary construction path must not send it.
        assert "cache" not in kwargs

    def test_fallback_patches_session_on_typeerror(self):
        """Older Simyan without a user_agent kwarg: patch the session instead."""
        import models.comicvine as cv
        from models.providers.base import DEFAULT_PROVIDER_USER_AGENT

        fallback_client = MagicMock()
        with patch.object(cv, "Comicvine", create=True) as mock_cv:
            mock_cv.side_effect = [TypeError("unexpected user_agent"), fallback_client]
            client = cv._make_cv_client("KEY")

        assert client is fallback_client
        fallback_client._session.headers.update.assert_called_once_with(
            {"User-Agent": DEFAULT_PROVIDER_USER_AGENT}
        )


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

    def test_api_path_returns_full_metadata(self, comicvine_creds):
        """API-key path must return issue-level fields (Summary, Number, Title,
        Writer, Characters, etc.) — regression for the double-mapping bug where
        get_metadata_by_volume_id's already-mapped ComicInfo dict was being run
        through map_to_comicinfo a second time and stripped to volume-level data.
        """
        from models.providers.comicvine_provider import ComicVineProvider

        full_metadata = {
            "Series": "Incorruptible",
            "Number": "1",
            "Volume": 2009,
            "Title": "Incorruptible, Part One",
            "Publisher": "Boom! Studios",
            "Summary": "Max Damage turns over a new leaf.",
            "Year": 2009,
            "Month": 12,
            "Day": 1,
            "Writer": "Mark Waid",
            "Penciller": "Jean Diaz",
            "Characters": "Max Damage, Plutonian",
            "LanguageISO": "en",
            "Notes": "Metadata from ComicVine CVDB. Volume ID: 30514 — retrieved 2026-05-30.",
            "_image_url": "https://example.com/cover.jpg",
        }

        p = ComicVineProvider(credentials=comicvine_creds)
        issue = IssueResult(
            provider=ProviderType.COMICVINE, id="307483", series_id="30514",
            issue_number="1", title=None, cover_date="2009-12-01", summary=None,
        )
        series = SearchResult(
            provider=ProviderType.COMICVINE, id="30514", title="Incorruptible",
            year=2009, publisher="Boom! Studios",
        )

        with patch("models.comicvine.get_metadata_by_volume_id", return_value=full_metadata) as mock_get:
            result = p.to_comicinfo(issue, series)

        # Forwarded publisher_name so map_to_comicinfo's volume_data path can populate Publisher.
        mock_get.assert_called_once_with(
            comicvine_creds.api_key, 30514, "1",
            start_year=2009, publisher_name="Boom! Studios",
        )
        # Volume-level fields survive (these did even before the fix).
        assert result["Series"] == "Incorruptible"
        assert result["Publisher"] == "Boom! Studios"
        assert result["Volume"] == 2009
        # Issue-level fields — the regression. Before the fix these were all dropped.
        assert result["Number"] == "1"
        assert result["Title"] == "Incorruptible, Part One"
        assert result["Summary"] == "Max Damage turns over a new leaf."
        assert result["Year"] == 2009
        assert result["Writer"] == "Mark Waid"
        assert result["Penciller"] == "Jean Diaz"
        assert result["Characters"] == "Max Damage, Plutonian"
        # Internal field should not leak through.
        assert "_image_url" not in result
