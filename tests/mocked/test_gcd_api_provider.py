"""Tests for the GCD REST API provider adapter (models/providers/gcd_api_provider.py)."""
import pytest
from unittest.mock import patch, MagicMock


# Sample API responses for mocking
# Sample data matching the real GCD API response structure
SAMPLE_SERIES = {
    "api_url": "https://www.comics.org/api/series/70876/",
    "name": "Batman",
    "year_began": 2016,
    "publisher": "https://www.comics.org/api/publisher/10/",
    "notes": "The Dark Knight",
    "active_issues": [
        "https://www.comics.org/api/issue/100001/",
        "https://www.comics.org/api/issue/100002/",
    ],
    "issue_descriptors": [
        "1",
        "2",
    ],
}

# Search results have publisher as dict with name in some contexts
SAMPLE_SEARCH_SERIES = {
    "api_url": "https://www.comics.org/api/series/70876/",
    "name": "Batman",
    "year_began": 2016,
    "publisher": "https://www.comics.org/api/publisher/10/",
    "notes": "The Dark Knight",
}

# Issue search result (IssueOnly format from /series/name/{name}/issue/{number}/)
SAMPLE_ISSUE_SEARCH_RESULT = {
    "api_url": "https://www.comics.org/api/issue/100001/",
    "series_name": "Batman",
    "descriptor": "1",
    "publication_date": "June 2016",
    "price": "2.99 USD",
    "page_count": "32",
    "variant_of": None,
    "series": "https://www.comics.org/api/series/70876/",
}

SAMPLE_ISSUE = {
    "id": 100001,
    "api_url": "https://www.comics.org/api/issue/100001/",
    "descriptor": "1",
    "series_name": "Batman",
    "series": "https://www.comics.org/api/series/70876/",
    "publication_date": "June 2016",
    "key_date": "2016-06-15",
    "on_sale_date": "2016-06-01",
    "page_count": "32",
    "cover": "https://www.comics.org/issue/100001/cover/4/",
    "story_set": [
        {
            "type": "cover",
            "sequence_number": 0,
            "title": "None",
            "pencils": "David Finch",
            "inks": "Matt Banning",
            "colors": "Jordie Bellaire",
            "script": "None",
            "letters": "None",
            "editing": "None",
            "characters": "None",
            "genre": "",
            "synopsis": "None",
        },
        {
            "type": "story",
            "sequence_number": 1,
            "title": "I Am Gotham Part One",
            "script": "Tom King",
            "pencils": "David Finch",
            "inks": "Matt Banning",
            "colors": "Jordie Bellaire",
            "letters": "John Workman",
            "editing": "James Tynion IV",
            "characters": "Batman [Bruce Wayne]; Alfred Pennyworth; Commissioner Gordon",
            "genre": "superhero",
            "synopsis": "Batman saves a crashing plane.",
        },
    ],
}


class TestGCDApiProvider:

    def _make_provider(self):
        from models.providers.gcd_api_provider import GCDApiProvider
        from models.providers.base import ProviderCredentials
        creds = ProviderCredentials(username="testuser", password="testpass")
        return GCDApiProvider(credentials=creds)

    @patch("models.gcd_api.GCDApiClient")
    def test_search_series_maps_to_search_result(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.search_series.return_value = [SAMPLE_SEARCH_SERIES]
        mock_client_cls.return_value = mock_client

        provider = self._make_provider()
        provider._client_instance = mock_client

        results = provider.search_series("Batman")

        assert len(results) == 1
        r = results[0]
        assert r.title == "Batman"
        assert r.year == 2016
        assert r.id == "70876"
        assert r.provider.value == "gcd_api"

    @patch("models.gcd_api.GCDApiClient")
    def test_search_series_with_year(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.search_series.return_value = [SAMPLE_SERIES]
        mock_client_cls.return_value = mock_client

        provider = self._make_provider()
        provider._client_instance = mock_client

        provider.search_series("Batman", year=2016)
        mock_client.search_series.assert_called_once_with("Batman", 2016)

    @patch("models.gcd_api.GCDApiClient")
    def test_search_series_empty_results(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.search_series.return_value = []
        mock_client_cls.return_value = mock_client

        provider = self._make_provider()
        provider._client_instance = mock_client

        results = provider.search_series("NonexistentComic")
        assert results == []

    @patch("models.gcd_api.GCDApiClient")
    def test_get_series_maps_correctly(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.get_series.return_value = SAMPLE_SERIES
        mock_client_cls.return_value = mock_client

        provider = self._make_provider()
        provider._client_instance = mock_client

        result = provider.get_series("70876")
        assert result.title == "Batman"
        assert result.year == 2016

    @patch("models.gcd_api.GCDApiClient")
    def test_get_series_not_found(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.get_series.return_value = None
        mock_client_cls.return_value = mock_client

        provider = self._make_provider()
        provider._client_instance = mock_client

        result = provider.get_series("99999999")
        assert result is None

    @patch("models.gcd_api.GCDApiClient")
    def test_get_issue_maps_with_cover_url(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.get_issue.return_value = SAMPLE_ISSUE
        mock_client_cls.return_value = mock_client

        provider = self._make_provider()
        provider._client_instance = mock_client

        result = provider.get_issue("100001")
        assert result.id == "100001"
        assert result.issue_number == "1"
        assert result.cover_url == "https://www.comics.org/issue/100001/cover/4/"
        assert result.series_id == "70876"

    @patch("models.gcd_api.GCDApiClient")
    def test_get_issues_from_series(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.get_series.return_value = SAMPLE_SERIES
        mock_client_cls.return_value = mock_client

        provider = self._make_provider()
        provider._client_instance = mock_client

        results = provider.get_issues("70876")
        assert len(results) == 2
        assert results[0].issue_number == "1"
        assert results[1].issue_number == "2"

    @patch("models.gcd_api.GCDApiClient")
    def test_get_issue_metadata_builds_comicinfo(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.get_series.return_value = SAMPLE_SERIES
        mock_client.get_issue.return_value = SAMPLE_ISSUE
        # search_issue returns the IssueOnly result matching our series
        mock_client.search_issue.return_value = [SAMPLE_ISSUE_SEARCH_RESULT]
        mock_client_cls.return_value = mock_client

        provider = self._make_provider()
        provider._client_instance = mock_client

        metadata = provider.get_issue_metadata("70876", "1")
        assert metadata is not None
        assert metadata["Series"] == "Batman"
        assert metadata["Number"] == "1"
        assert metadata["Writer"] == "Tom King"
        assert metadata["Penciller"] == "David Finch"
        assert metadata["Inker"] == "Matt Banning"
        assert metadata["Colorist"] == "Jordie Bellaire"
        assert metadata["Letterer"] == "John Workman"
        assert metadata["Title"] == "I Am Gotham Part One"
        assert metadata["Summary"] == "Batman saves a crashing plane."
        assert "Batman [Bruce Wayne]" in metadata["Characters"]
        assert metadata["Genre"] == "superhero"
        assert metadata["PageCount"] == "32"
        assert metadata["Year"] == 2016
        assert "_cover_url" in metadata

    @patch("models.gcd_api.GCDApiClient")
    def test_to_comicinfo_uses_full_metadata(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.get_series.return_value = SAMPLE_SERIES
        mock_client.get_issue.return_value = SAMPLE_ISSUE
        mock_client.search_issue.return_value = [SAMPLE_ISSUE_SEARCH_RESULT]
        mock_client_cls.return_value = mock_client

        provider = self._make_provider()
        provider._client_instance = mock_client

        from models.providers.base import IssueResult, ProviderType
        issue_result = IssueResult(
            provider=ProviderType.GCD_API,
            id="100001",
            series_id="70876",
            issue_number="1",
        )
        result = provider.to_comicinfo(issue_result)
        assert result["Series"] == "Batman"
        assert result["Writer"] == "Tom King"

    @patch("models.gcd_api.GCDApiClient")
    def test_to_comicinfo_fallback_minimal(self, mock_client_cls):
        """When API calls fail, falls back to minimal IssueResult data."""
        mock_client = MagicMock()
        mock_client.get_series.return_value = None
        mock_client.get_issue.return_value = None
        mock_client_cls.return_value = mock_client

        provider = self._make_provider()
        provider._client_instance = mock_client

        from models.providers.base import IssueResult, SearchResult, ProviderType
        issue_result = IssueResult(
            provider=ProviderType.GCD_API,
            id="100001",
            series_id="70876",
            issue_number="1",
            cover_date="2016-06-15",
        )
        series_result = SearchResult(
            provider=ProviderType.GCD_API,
            id="70876",
            title="Batman",
            year=2016,
            publisher="DC",
        )
        result = provider.to_comicinfo(issue_result, series_result)
        assert result["Series"] == "Batman"
        assert result["Number"] == "1"
        assert result["Publisher"] == "DC"
        assert result["Year"] == 2016

    @patch("models.gcd_api.GCDApiClient")
    def test_test_connection_success(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.search_series.return_value = [SAMPLE_SERIES]
        mock_client_cls.return_value = mock_client

        provider = self._make_provider()
        provider._client_instance = mock_client

        assert provider.test_connection() is True

    @patch("models.gcd_api.GCDApiClient")
    def test_test_connection_failure(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.search_series.side_effect = Exception("Connection refused")
        mock_client_cls.return_value = mock_client

        provider = self._make_provider()
        provider._client_instance = mock_client

        assert provider.test_connection() is False

    def test_test_connection_no_credentials(self):
        from models.providers.gcd_api_provider import GCDApiProvider
        provider = GCDApiProvider()
        # No credentials and no saved creds - mock the DB call
        with patch("core.database.get_provider_credentials", return_value=None):
            assert provider.test_connection() is False


class TestHelperFunctions:

    def test_clean_issue_number(self):
        from models.providers.gcd_api_provider import _clean_issue_number
        assert _clean_issue_number("3") == "3"
        assert _clean_issue_number("3 [Jorge Jiménez Cover]") == "3"
        assert _clean_issue_number("1 [Jim Lee & Scott Williams Cardstock Variant Cover]") == "1"
        assert _clean_issue_number("12 (2nd printing)") == "12"
        assert _clean_issue_number("3 [Variant] (2nd printing)") == "3"
        assert _clean_issue_number("") == ""
        assert _clean_issue_number(None) == ""
        assert _clean_issue_number("1/2") == "1/2"

    def test_extract_id_from_url(self):
        from models.providers.gcd_api_provider import _extract_id_from_url
        assert _extract_id_from_url("https://www.comics.org/api/series/70876/") == "70876"
        assert _extract_id_from_url("https://www.comics.org/api/issue/100001/") == "100001"
        assert _extract_id_from_url(None) is None
        assert _extract_id_from_url("") is None

    def test_parse_credits_text(self):
        from models.providers.gcd_api_provider import _parse_credits_text
        assert _parse_credits_text("Tom King") == ["Tom King"]
        assert _parse_credits_text("Tom King; Scott Snyder") == ["Tom King", "Scott Snyder"]
        assert _parse_credits_text("?") == []
        assert _parse_credits_text("") == []
        assert _parse_credits_text(None) == []
        assert _parse_credits_text("None") == []

    def test_parse_credits_text_strips_parenthetical(self):
        from models.providers.gcd_api_provider import _parse_credits_text
        result = _parse_credits_text("Bob Kane (as Bob Kane); Bill Finger")
        assert result == ["Bob Kane", "Bill Finger"]

    def test_parse_credits_text_strips_trailing_question_mark(self):
        from models.providers.gcd_api_provider import _parse_credits_text
        assert _parse_credits_text("Mike Royer ?") == ["Mike Royer"]
        assert _parse_credits_text("John Doe ?; Jane Smith") == ["John Doe", "Jane Smith"]
        assert _parse_credits_text("Tom King ?; Scott Snyder ?") == ["Tom King", "Scott Snyder"]

    def test_provider_type_is_gcd_api(self):
        from models.providers.gcd_api_provider import GCDApiProvider
        from models.providers.base import ProviderType
        assert GCDApiProvider.provider_type == ProviderType.GCD_API
        assert GCDApiProvider.provider_type.value == "gcd_api"

    def test_provider_auth_fields(self):
        from models.providers.gcd_api_provider import GCDApiProvider
        assert GCDApiProvider.auth_fields == ["username", "password"]
        assert GCDApiProvider.requires_auth is True

    def test_provider_display_name(self):
        from models.providers.gcd_api_provider import GCDApiProvider
        assert "API" in GCDApiProvider.display_name
        assert "Grand Comics Database" in GCDApiProvider.display_name
