"""Tests for MangaDexProvider adapter -- mocked mangadex library."""
import pytest
from unittest.mock import patch, MagicMock

from models.providers.base import ProviderType, SearchResult, IssueResult


def _make_mock_manga(*, manga_id="abc-123", title=None, year=2020,
                     description=None, cover_id=None, last_chapter="10"):
    m = MagicMock()
    m.manga_id = manga_id
    m.title = title or {"en": "Test Manga"}
    m.year = year
    m.description = description or {"en": "A test manga description"}
    m.cover_id = cover_id
    m.last_chapter = last_chapter
    return m


def _make_mock_chapter(*, id="ch-1", chapter="1", manga_id="abc-123",
                       title="Chapter One", publish_at=None):
    c = MagicMock()
    c.id = id
    c.chapter = chapter
    c.manga_id = manga_id
    c.title = title
    c.publish_at = publish_at
    return c


class TestMangaDexProviderInit:

    def test_provider_attributes(self):
        from models.providers.mangadex_provider import MangaDexProvider

        p = MangaDexProvider()
        assert p.provider_type == ProviderType.MANGADEX
        assert p.requires_auth is False
        assert p.auth_fields == []
        assert p.rate_limit == 60


class TestMangaDexProviderTestConnection:

    def test_successful_connection(self):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_api = MagicMock()
        mock_api.random_manga.return_value = _make_mock_manga()

        p = MangaDexProvider()
        p._manga_api = mock_api  # Bypass import of mangadex
        assert p.test_connection() is True

    def test_connection_failure(self):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_api = MagicMock()
        mock_api.random_manga.side_effect = Exception("API error")

        p = MangaDexProvider()
        p._manga_api = mock_api
        assert p.test_connection() is False


class TestMangaDexProviderSearchSeries:

    def test_search_returns_results(self):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_api = MagicMock()
        mock_api.get_manga_list.return_value = [
            _make_mock_manga(manga_id="id-1", title={"en": "Naruto"}, year=1999, last_chapter="700"),
        ]

        p = MangaDexProvider()
        p._manga_api = mock_api
        results = p.search_series("Naruto")

        assert len(results) == 1
        assert results[0].title == "Naruto"
        assert results[0].year == 1999
        assert results[0].provider == ProviderType.MANGADEX

    def test_search_no_results(self):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_api = MagicMock()
        mock_api.get_manga_list.return_value = []

        p = MangaDexProvider()
        p._manga_api = mock_api
        assert p.search_series("Nonexistent") == []

    def test_search_year_filter(self):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_api = MagicMock()
        mock_api.get_manga_list.return_value = [
            _make_mock_manga(manga_id="id-1", year=1999),
            _make_mock_manga(manga_id="id-2", year=2020),
        ]

        p = MangaDexProvider()
        p._manga_api = mock_api
        results = p.search_series("Test", year=1999)

        # Year filter should exclude non-matching
        assert len(results) == 1
        assert results[0].year == 1999

    def test_localized_title_fallback(self):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_api = MagicMock()
        mock_api.get_manga_list.return_value = [
            _make_mock_manga(manga_id="id-1", title={"ja": "ナルト"}),
        ]

        p = MangaDexProvider()
        p._manga_api = mock_api
        results = p.search_series("Naruto")

        assert len(results) == 1
        assert results[0].title == "ナルト"  # Falls back to first available


class TestMangaDexProviderGetSeries:

    def test_get_series_by_id(self):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_api = MagicMock()
        mock_api.view_manga_by_id.return_value = _make_mock_manga(
            manga_id="abc-123", title={"en": "One Piece"}, year=1997,
        )

        p = MangaDexProvider()
        p._manga_api = mock_api
        result = p.get_series("abc-123")

        assert isinstance(result, SearchResult)
        assert result.title == "One Piece"

    def test_series_not_found(self):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_api = MagicMock()
        mock_api.view_manga_by_id.return_value = None

        p = MangaDexProvider()
        p._manga_api = mock_api
        assert p.get_series("nonexistent") is None


class TestMangaDexProviderGetIssues:

    def test_returns_chapters(self):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_api = MagicMock()
        mock_api.get_manga_volumes_and_chapters.return_value = {
            "volumes": {
                "1": {
                    "chapters": {
                        "1": {"id": "ch-1"},
                        "2": {"id": "ch-2"},
                    }
                },
            }
        }

        p = MangaDexProvider()
        p._manga_api = mock_api
        results = p.get_issues("abc-123")

        assert len(results) == 2
        assert all(isinstance(r, IssueResult) for r in results)

    def test_empty_volumes(self):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_api = MagicMock()
        mock_api.get_manga_volumes_and_chapters.return_value = {"volumes": {}}

        p = MangaDexProvider()
        p._manga_api = mock_api
        assert p.get_issues("abc-123") == []


class TestMangaDexProviderGetIssue:

    def test_get_chapter(self):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_ch_api = MagicMock()
        mock_ch_api.get_chapter.return_value = _make_mock_chapter(
            id="ch-5", chapter="5", manga_id="abc-123",
        )

        p = MangaDexProvider()
        p._chapter_api = mock_ch_api
        result = p.get_issue("ch-5")

        assert isinstance(result, IssueResult)
        assert result.issue_number == "5"
        assert result.series_id == "abc-123"

    def test_chapter_not_found(self):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_ch_api = MagicMock()
        mock_ch_api.get_chapter.return_value = None

        p = MangaDexProvider()
        p._chapter_api = mock_ch_api
        assert p.get_issue("nonexistent") is None


class TestMangaDexProviderToComicinfo:

    def test_manga_format(self):
        from models.providers.mangadex_provider import MangaDexProvider

        mock_api = MagicMock()
        mock_api.view_manga_by_id.return_value = _make_mock_manga(
            manga_id="abc-123", title={"en": "Test Manga"}, year=2020,
        )

        p = MangaDexProvider()
        p._manga_api = mock_api
        issue = IssueResult(
            provider=ProviderType.MANGADEX, id="ch-1", series_id="abc-123",
            issue_number="3", title="Chapter Three",
        )

        result = p.to_comicinfo(issue)
        assert result["Number"] == "v3"
        assert result["Manga"] == "YesAndRightToLeft"
        assert result["Series"] == "Test Manga"
        assert result["Title"] == "Chapter Three"
