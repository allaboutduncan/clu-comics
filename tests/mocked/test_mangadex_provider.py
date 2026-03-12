"""Tests for MangaDexProvider adapter -- mocked REST API requests."""
import pytest
from unittest.mock import patch, MagicMock

from models.providers.base import ProviderType, SearchResult, IssueResult


def _mock_response(json_data, status_code=200):
    """Build a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


def _mock_response_error(status_code=404):
    """Build a mock requests.Response that raises on raise_for_status."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return resp


SAMPLE_MANGA = {
    "result": "ok",
    "data": {
        "id": "abc-def-123",
        "type": "manga",
        "attributes": {
            "title": {"en": "Test Manga"},
            "altTitles": [
                {"ja": "テストマンガ"},
                {"en": "Test Comic"},
            ],
            "description": {"en": "A test manga description"},
            "originalLanguage": "ja",
            "lastVolume": "10",
            "lastChapter": "100",
            "year": 2020,
            "status": "ongoing",
            "tags": [
                {
                    "attributes": {
                        "name": {"en": "Action"},
                        "group": "genre",
                    }
                },
                {
                    "attributes": {
                        "name": {"en": "Comedy"},
                        "group": "genre",
                    }
                },
                {
                    "attributes": {
                        "name": {"en": "Long Strip"},
                        "group": "format",
                    }
                },
            ],
        },
        "relationships": [
            {"type": "author", "attributes": {"name": "Author One"}},
            {"type": "artist", "attributes": {"name": "Artist One"}},
            {"type": "cover_art", "attributes": {"fileName": "cover.jpg"}},
        ],
    },
}

SAMPLE_SEARCH_RESPONSE = {
    "result": "ok",
    "data": [SAMPLE_MANGA["data"]],
}

SAMPLE_AGGREGATE = {
    "result": "ok",
    "volumes": {
        "1": {"volume": "1", "count": 10, "chapters": {}},
        "2": {"volume": "2", "count": 8, "chapters": {}},
        "none": {"volume": "none", "count": 5, "chapters": {}},
    },
}


class TestMangaDexProviderInit:

    def test_provider_attributes(self):
        from models.providers.mangadex_provider import MangaDexProvider

        p = MangaDexProvider()
        assert p.provider_type == ProviderType.MANGADEX
        assert p.display_name == "MangaDex"
        assert p.requires_auth is False
        assert p.auth_fields == []
        assert p.rate_limit == 60


class TestMangaDexProviderTestConnection:

    @patch("time.sleep")
    @patch("requests.request")
    def test_successful_connection(self, mock_request, mock_sleep):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_request.return_value = _mock_response({"result": "ok", "data": []})

        p = MangaDexProvider()
        assert p.test_connection() is True

    @patch("time.sleep")
    @patch("requests.request", side_effect=Exception("Network error"))
    def test_connection_failure(self, mock_request, mock_sleep):
        from models.providers.mangadex_provider import MangaDexProvider

        p = MangaDexProvider()
        assert p.test_connection() is False


class TestMangaDexProviderSearchSeries:

    @patch("time.sleep")
    @patch("requests.request")
    def test_search_returns_results(self, mock_request, mock_sleep):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_request.return_value = _mock_response(SAMPLE_SEARCH_RESPONSE)

        p = MangaDexProvider()
        results = p.search_series("Test Manga")

        assert len(results) == 1
        assert results[0].title == "Test Manga"
        assert results[0].year == 2020
        assert results[0].provider == ProviderType.MANGADEX
        assert results[0].id == "abc-def-123"
        assert results[0].issue_count == 10
        assert results[0].cover_url == "https://uploads.mangadex.org/covers/abc-def-123/cover.jpg"

    @patch("time.sleep")
    @patch("requests.request")
    def test_search_alternate_title(self, mock_request, mock_sleep):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_request.return_value = _mock_response(SAMPLE_SEARCH_RESPONSE)

        p = MangaDexProvider()
        results = p.search_series("Test Manga")

        assert len(results) == 1
        # Native Japanese title should be extracted as alternate
        assert results[0].alternate_title == "テストマンガ"

    @patch("time.sleep")
    @patch("requests.request")
    def test_search_year_filter(self, mock_request, mock_sleep):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_request.return_value = _mock_response(SAMPLE_SEARCH_RESPONSE)

        p = MangaDexProvider()
        # Year doesn't match
        results = p.search_series("Test", year=1999)
        assert len(results) == 0

        # Year matches
        results = p.search_series("Test", year=2020)
        assert len(results) == 1

    @patch("time.sleep")
    @patch("requests.request")
    def test_search_empty_results(self, mock_request, mock_sleep):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_request.return_value = _mock_response({"result": "ok", "data": []})

        p = MangaDexProvider()
        assert p.search_series("Nothing") == []

    @patch("time.sleep")
    @patch("requests.request")
    def test_localized_title_fallback(self, mock_request, mock_sleep):
        from models.providers.mangadex_provider import MangaDexProvider

        # Manga with only Japanese title
        ja_manga = {
            "result": "ok",
            "data": [{
                "id": "ja-only-123",
                "type": "manga",
                "attributes": {
                    "title": {"ja": "ナルト"},
                    "altTitles": [],
                    "description": {},
                    "originalLanguage": "ja",
                    "lastVolume": None,
                    "year": 1999,
                    "status": "completed",
                    "tags": [],
                },
                "relationships": [],
            }],
        }
        mock_request.return_value = _mock_response(ja_manga)

        p = MangaDexProvider()
        results = p.search_series("Naruto")

        assert len(results) == 1
        assert results[0].title == "ナルト"


class TestMangaDexProviderGetSeries:

    @patch("time.sleep")
    @patch("requests.request")
    def test_get_series_by_id(self, mock_request, mock_sleep):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_request.return_value = _mock_response(SAMPLE_MANGA)

        p = MangaDexProvider()
        result = p.get_series("abc-def-123")

        assert isinstance(result, SearchResult)
        assert result.title == "Test Manga"
        assert result.year == 2020
        assert result.issue_count == 10
        assert result.alternate_title == "テストマンガ"

    @patch("time.sleep")
    @patch("requests.request")
    def test_series_not_found(self, mock_request, mock_sleep):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_request.return_value = _mock_response(None)

        p = MangaDexProvider()
        assert p.get_series("nonexistent") is None


class TestMangaDexProviderGetIssues:

    @patch("time.sleep")
    @patch("requests.request")
    def test_aggregate_volumes(self, mock_request, mock_sleep):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_request.return_value = _mock_response(SAMPLE_AGGREGATE)

        p = MangaDexProvider()
        results = p.get_issues("abc-def-123")

        # "none" volume should be skipped
        assert len(results) == 2
        assert all(isinstance(r, IssueResult) for r in results)
        assert results[0].issue_number == "1"
        assert results[0].id == "abc-def-123-v1"
        assert results[1].issue_number == "2"
        assert results[1].id == "abc-def-123-v2"

    @patch("time.sleep")
    @patch("requests.request")
    def test_empty_volumes(self, mock_request, mock_sleep):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_request.return_value = _mock_response({"volumes": {}})

        p = MangaDexProvider()
        assert p.get_issues("abc-def-123") == []

    @patch("time.sleep")
    @patch("requests.request")
    def test_only_none_volume(self, mock_request, mock_sleep):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_request.return_value = _mock_response({
            "volumes": {"none": {"volume": "none", "count": 5, "chapters": {}}}
        })

        p = MangaDexProvider()
        assert p.get_issues("abc-def-123") == []


class TestMangaDexProviderGetIssue:

    def test_parse_synthetic_id(self):
        from models.providers.mangadex_provider import MangaDexProvider

        p = MangaDexProvider()
        result = p.get_issue("abc-def-123-v5")

        assert isinstance(result, IssueResult)
        assert result.issue_number == "5"
        assert result.series_id == "abc-def-123"

    def test_invalid_id_format(self):
        from models.providers.mangadex_provider import MangaDexProvider

        p = MangaDexProvider()
        assert p.get_issue("nohyphen") is None


class TestMangaDexProviderGetIssueMetadata:

    @patch("time.sleep")
    @patch("requests.request")
    def test_full_metadata(self, mock_request, mock_sleep):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_request.return_value = _mock_response(SAMPLE_MANGA)

        p = MangaDexProvider()
        metadata = p.get_issue_metadata("abc-def-123", "3")

        assert metadata is not None
        assert metadata["Series"] == "Test Manga"
        assert metadata["Number"] == "v3"
        assert metadata["Year"] == 2020
        assert metadata["Writer"] == "Author One"
        assert metadata["Penciller"] == "Artist One"
        assert metadata["Genre"] == "Action, Comedy"
        assert metadata["Manga"] == "Yes"
        assert metadata["Count"] == 10
        assert "ongoing" in metadata["Notes"]
        assert "MangaDex" in metadata["Notes"]
        assert "mangadex.org/title/abc-def-123" in metadata["Web"]
        assert "A test manga description" == metadata["Summary"]

    @patch("time.sleep")
    @patch("requests.request")
    def test_preferred_title(self, mock_request, mock_sleep):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_request.return_value = _mock_response(SAMPLE_MANGA)

        p = MangaDexProvider()
        metadata = p.get_issue_metadata(
            "abc-def-123", "1",
            preferred_title="My Preferred Title",
            alternate_title="テストマンガ"
        )

        assert metadata["Series"] == "My Preferred Title"
        assert "テストマンガ" in metadata["AlternateSeries"]

    @patch("time.sleep")
    @patch("requests.request")
    def test_manga_flag_japanese(self, mock_request, mock_sleep):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_request.return_value = _mock_response(SAMPLE_MANGA)

        p = MangaDexProvider()
        metadata = p.get_issue_metadata("abc-def-123", "1")
        assert metadata["Manga"] == "Yes"

    @patch("time.sleep")
    @patch("requests.request")
    def test_no_manga_flag_for_non_cjk(self, mock_request, mock_sleep):
        from models.providers.mangadex_provider import MangaDexProvider

        # French comic - not manga
        french_manga = {**SAMPLE_MANGA}
        french_data = {**SAMPLE_MANGA["data"]}
        french_attrs = {**SAMPLE_MANGA["data"]["attributes"], "originalLanguage": "fr"}
        french_data["attributes"] = french_attrs
        french_manga["data"] = french_data

        mock_request.return_value = _mock_response(french_manga)

        p = MangaDexProvider()
        metadata = p.get_issue_metadata("abc-def-123", "1")
        assert "Manga" not in metadata

    @patch("time.sleep")
    @patch("requests.request")
    def test_genre_excludes_non_genre_tags(self, mock_request, mock_sleep):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_request.return_value = _mock_response(SAMPLE_MANGA)

        p = MangaDexProvider()
        metadata = p.get_issue_metadata("abc-def-123", "1")

        # "Long Strip" is a format tag, not genre, so should not appear
        assert "Long Strip" not in metadata.get("Genre", "")
        assert "Action" in metadata["Genre"]
        assert "Comedy" in metadata["Genre"]

    @patch("time.sleep")
    @patch("requests.request")
    def test_alternate_series_deduplication(self, mock_request, mock_sleep):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_request.return_value = _mock_response(SAMPLE_MANGA)

        p = MangaDexProvider()
        metadata = p.get_issue_metadata(
            "abc-def-123", "1",
            preferred_title="Test Manga",
            alternate_title="テストマンガ"
        )

        # テストマンガ appears both as alternate_title param and in altTitles
        # Should only appear once
        alt_parts = metadata["AlternateSeries"].split("; ")
        assert alt_parts.count("テストマンガ") == 1


class TestMangaDexProviderRateLimit:

    @patch("time.monotonic")
    @patch("time.sleep")
    @patch("requests.request")
    def test_rate_limit_sleeps(self, mock_request, mock_sleep, mock_monotonic):
        from models.providers.mangadex_provider import MangaDexProvider

        # Simulate two rapid requests: first at t=100, second at t=100.1
        mock_monotonic.side_effect = [100.0, 100.0, 100.1, 100.25]
        mock_request.return_value = _mock_response({"result": "ok", "data": []})

        # Reset class-level state
        MangaDexProvider._last_request_time = 99.0

        p = MangaDexProvider()
        p._make_request("GET", "/manga", {"limit": 1})
        p._make_request("GET", "/manga", {"limit": 1})

        # Second request should have triggered a sleep
        assert mock_sleep.called
