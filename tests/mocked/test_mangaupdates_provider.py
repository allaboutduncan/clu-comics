"""Tests for MangaUpdatesProvider adapter -- mocked requests."""
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


SAMPLE_SERIES = {
    "series_id": 12345,
    "title": "One Punch Man",
    "year": "2012",
    "description": "<b>Bold</b> hero story &amp; comedy",
    "image": {"url": {"original": "https://example.com/cover.jpg"}},
    "status": "Ongoing",
    "type": "Manga",
    "authors": [{"name": "ONE"}, {"name": "Murata Yusuke"}],
    "genres": [{"genre": "Action"}, {"genre": "Comedy"}],
    "associated": [{"title": "OPM"}, {"title": "ワンパンマン"}],
    "publishers": [{"publisher_name": "Shueisha"}],
    "latest_chapter": "30",
}


class TestMangaUpdatesProviderInit:

    def test_provider_attributes(self):
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        p = MangaUpdatesProvider()
        assert p.provider_type == ProviderType.MANGAUPDATES
        assert p.display_name == "MangaUpdates"
        assert p.requires_auth is False
        assert p.auth_fields == []
        assert p.rate_limit == 30


class TestMangaUpdatesProviderTestConnection:

    @patch("time.sleep")
    @patch("requests.request")
    def test_successful_connection(self, mock_request, mock_sleep):
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        mock_request.return_value = _mock_response({"results": []})

        p = MangaUpdatesProvider()
        assert p.test_connection() is True

    @patch("time.sleep")
    @patch("requests.request", side_effect=Exception("Network error"))
    def test_connection_failure(self, mock_request, mock_sleep):
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        p = MangaUpdatesProvider()
        assert p.test_connection() is False


class TestMangaUpdatesProviderSearchSeries:

    @patch("time.sleep")
    @patch("requests.request")
    def test_search_returns_results(self, mock_request, mock_sleep):
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        mock_request.return_value = _mock_response({
            "results": [{"record": SAMPLE_SERIES}]
        })

        p = MangaUpdatesProvider()
        results = p.search_series("One Punch Man")

        assert len(results) == 1
        assert results[0].title == "One Punch Man"
        assert results[0].year == 2012
        assert results[0].provider == ProviderType.MANGAUPDATES
        assert results[0].id == "12345"
        assert results[0].cover_url == "https://example.com/cover.jpg"

    @patch("time.sleep")
    @patch("requests.request")
    def test_search_ignores_year_filter(self, mock_request, mock_sleep):
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        mock_request.return_value = _mock_response({
            "results": [{"record": SAMPLE_SERIES}]
        })

        p = MangaUpdatesProvider()
        # Year doesn't match series start year, but results should NOT be filtered
        # because MU year is series start year, not volume publication year
        results = p.search_series("One Punch Man", year=2020)
        assert len(results) == 1
        assert results[0].title == "One Punch Man"

    @patch("time.sleep")
    @patch("requests.request")
    def test_search_empty_results(self, mock_request, mock_sleep):
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        mock_request.return_value = _mock_response({"results": []})

        p = MangaUpdatesProvider()
        assert p.search_series("Nothing") == []

    @patch("time.sleep")
    @patch("requests.request")
    def test_search_strips_html(self, mock_request, mock_sleep):
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        mock_request.return_value = _mock_response({
            "results": [{"record": {
                **SAMPLE_SERIES,
                "title": "<i>Fancy</i> Title &amp; More",
            }}]
        })

        p = MangaUpdatesProvider()
        results = p.search_series("Fancy")
        assert results[0].title == "Fancy Title & More"


class TestMangaUpdatesProviderGetSeries:

    @patch("time.sleep")
    @patch("requests.request")
    def test_get_series_by_id(self, mock_request, mock_sleep):
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        mock_request.return_value = _mock_response(SAMPLE_SERIES)

        p = MangaUpdatesProvider()
        result = p.get_series("12345")

        assert isinstance(result, SearchResult)
        assert result.title == "One Punch Man"
        assert result.year == 2012
        assert result.publisher == "Shueisha"
        assert result.issue_count == 30

    @patch("time.sleep")
    @patch("requests.request")
    def test_series_not_found(self, mock_request, mock_sleep):
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        mock_request.return_value = _mock_response(None)

        p = MangaUpdatesProvider()
        assert p.get_series("99999") is None


class TestMangaUpdatesProviderGetIssues:

    @patch("time.sleep")
    @patch("requests.request")
    def test_synthetic_volumes(self, mock_request, mock_sleep):
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        mock_request.return_value = _mock_response(SAMPLE_SERIES)

        p = MangaUpdatesProvider()
        results = p.get_issues("12345")

        assert len(results) == 30
        assert results[0].issue_number == "1"
        assert results[0].id == "12345-1"
        assert results[29].issue_number == "30"
        assert results[29].id == "12345-30"

    @patch("time.sleep")
    @patch("requests.request")
    def test_zero_volumes(self, mock_request, mock_sleep):
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        series_no_vols = {**SAMPLE_SERIES, "latest_chapter": None}
        mock_request.return_value = _mock_response(series_no_vols)

        p = MangaUpdatesProvider()
        assert p.get_issues("12345") == []


class TestMangaUpdatesProviderGetIssueMetadata:

    @patch("time.sleep")
    @patch("requests.request")
    def test_full_metadata(self, mock_request, mock_sleep):
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        # get_issue_metadata makes a single GET /series/{id} call
        mock_request.return_value = _mock_response(SAMPLE_SERIES)

        p = MangaUpdatesProvider()
        metadata = p.get_issue_metadata("12345", "3")

        assert metadata is not None
        assert metadata["Series"] == "One Punch Man"
        assert metadata["Number"] == "v3"
        assert metadata["Year"] == 2012
        assert metadata["Publisher"] == "Shueisha"
        assert metadata["Writer"] == "ONE, Murata Yusuke"
        assert metadata["Penciller"] == "ONE, Murata Yusuke"
        assert metadata["Genre"] == "Action, Comedy"
        assert metadata["AlternateSeries"] == "OPM; ワンパンマン"
        assert metadata["Manga"] == "Yes"
        assert metadata["Count"] == 30
        assert "Ongoing" in metadata["Notes"]
        assert "MangaUpdates" in metadata["Notes"]
        assert "mangaupdates.com/series/12345" in metadata["Web"]

    @patch("time.sleep")
    @patch("requests.request")
    def test_summary_html_stripped(self, mock_request, mock_sleep):
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        mock_request.return_value = _mock_response(SAMPLE_SERIES)

        p = MangaUpdatesProvider()
        metadata = p.get_issue_metadata("12345", "1")

        assert "<b>" not in metadata.get("Summary", "")
        assert "&amp;" not in metadata.get("Summary", "")
        assert "Bold hero story & comedy" == metadata["Summary"]

    @patch("time.sleep")
    @patch("requests.request")
    def test_manga_type_flag(self, mock_request, mock_sleep):
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        # Test Manhwa type
        manhwa_series = {**SAMPLE_SERIES, "type": "Manhwa"}
        mock_request.return_value = _mock_response(manhwa_series)

        p = MangaUpdatesProvider()
        metadata = p.get_issue_metadata("12345", "1")
        assert metadata["Manga"] == "Yes"


class TestMangaUpdatesProviderGetIssue:

    @patch("time.sleep")
    @patch("requests.request")
    def test_parse_synthetic_id(self, mock_request, mock_sleep):
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        mock_request.return_value = _mock_response(SAMPLE_SERIES)

        p = MangaUpdatesProvider()
        result = p.get_issue("12345-5")

        assert isinstance(result, IssueResult)
        assert result.issue_number == "5"
        assert result.series_id == "12345"

    def test_invalid_id_format(self):
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        p = MangaUpdatesProvider()
        assert p.get_issue("nohyphen") is None


class TestMangaUpdatesHitTitle:
    """Tests for hit_title preference over native title."""

    @patch("time.sleep")
    @patch("requests.request")
    def test_search_prefers_hit_title(self, mock_request, mock_sleep):
        """When hit_title differs from title, use hit_title as result title."""
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        mock_request.return_value = _mock_response({
            "results": [{
                "record": {**SAMPLE_SERIES, "title": "Ayakashi Koi Emaki"},
                "hit_title": "Demon Love Spell",
            }]
        })

        p = MangaUpdatesProvider()
        results = p.search_series("Demon Love Spell")

        assert len(results) == 1
        assert results[0].title == "Demon Love Spell"
        assert results[0].alternate_title == "Ayakashi Koi Emaki"

    @patch("time.sleep")
    @patch("requests.request")
    def test_search_same_title_no_alternate(self, mock_request, mock_sleep):
        """When hit_title equals title, alternate_title should be None."""
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        mock_request.return_value = _mock_response({
            "results": [{
                "record": SAMPLE_SERIES,
                "hit_title": "One Punch Man",
            }]
        })

        p = MangaUpdatesProvider()
        results = p.search_series("One Punch Man")

        assert len(results) == 1
        assert results[0].title == "One Punch Man"
        assert results[0].alternate_title is None

    @patch("time.sleep")
    @patch("requests.request")
    def test_search_no_hit_title(self, mock_request, mock_sleep):
        """When hit_title is absent, fall back to record.title."""
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        mock_request.return_value = _mock_response({
            "results": [{"record": SAMPLE_SERIES}]
        })

        p = MangaUpdatesProvider()
        results = p.search_series("One Punch Man")

        assert len(results) == 1
        assert results[0].title == "One Punch Man"
        assert results[0].alternate_title is None

    @patch("time.sleep")
    @patch("requests.request")
    def test_metadata_uses_preferred_title(self, mock_request, mock_sleep):
        """get_issue_metadata should use preferred_title as Series name."""
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        mock_request.return_value = _mock_response({
            **SAMPLE_SERIES,
            "title": "Ayakashi Koi Emaki",
        })

        p = MangaUpdatesProvider()
        metadata = p.get_issue_metadata(
            "12345", "1",
            preferred_title="Demon Love Spell",
            alternate_title="Ayakashi Koi Emaki"
        )

        assert metadata["Series"] == "Demon Love Spell"
        assert "Ayakashi Koi Emaki" in metadata["AlternateSeries"]

    @patch("time.sleep")
    @patch("requests.request")
    def test_alternate_deduplication(self, mock_request, mock_sleep):
        """Native title already in associated array should not be duplicated."""
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        mock_request.return_value = _mock_response({
            **SAMPLE_SERIES,
            "title": "Ayakashi Koi Emaki",
            "associated": [{"title": "Ayakashi Koi Emaki"}, {"title": "Other Title"}],
        })

        p = MangaUpdatesProvider()
        metadata = p.get_issue_metadata(
            "12345", "1",
            preferred_title="Demon Love Spell",
            alternate_title="Ayakashi Koi Emaki"
        )

        alt_parts = metadata["AlternateSeries"].split("; ")
        # Ayakashi Koi Emaki should appear only once
        assert alt_parts.count("Ayakashi Koi Emaki") == 1
        assert "Other Title" in alt_parts


class TestMangaUpdatesProviderRateLimit:

    @patch("time.monotonic")
    @patch("time.sleep")
    @patch("requests.request")
    def test_rate_limit_sleeps(self, mock_request, mock_sleep, mock_monotonic):
        from models.providers.mangaupdates_provider import MangaUpdatesProvider

        # Simulate two rapid requests: first at t=100, second at t=100.5
        mock_monotonic.side_effect = [100.0, 100.0, 100.5, 101.5]
        mock_request.return_value = _mock_response({"results": []})

        # Reset class-level state
        MangaUpdatesProvider._last_request_time = 99.0

        p = MangaUpdatesProvider()
        p._make_request("POST", "/series/search", {"search": "test"})
        p._make_request("POST", "/series/search", {"search": "test2"})

        # Second request should have triggered a sleep
        assert mock_sleep.called
