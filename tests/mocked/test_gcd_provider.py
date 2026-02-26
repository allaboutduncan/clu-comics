"""Tests for GCDProvider adapter -- mocked MySQL/gcd module."""
import pytest
from unittest.mock import patch, MagicMock

from models.providers.base import ProviderType, SearchResult, IssueResult
from tests.mocked.conftest import make_mock_mysql_connection


class TestGCDProviderInit:

    def test_provider_attributes(self):
        from models.providers.gcd_provider import GCDProvider

        p = GCDProvider()
        assert p.provider_type == ProviderType.GCD
        assert p.display_name == "Grand Comics Database"
        assert p.requires_auth is True
        assert "host" in p.auth_fields
        assert "database" in p.auth_fields


class TestGCDProviderTestConnection:

    @patch("models.gcd.is_mysql_available", return_value=True)
    @patch("models.gcd.check_mysql_status", return_value={"gcd_mysql_available": True})
    @patch("models.gcd.get_connection")
    def test_successful_connection(self, mock_conn, mock_status, mock_avail, gcd_creds):
        from models.providers.gcd_provider import GCDProvider

        mock_conn.return_value = MagicMock()
        p = GCDProvider(credentials=gcd_creds)
        assert p.test_connection() is True

    @patch("models.gcd.is_mysql_available", return_value=False)
    def test_mysql_unavailable(self, mock_avail, gcd_creds):
        from models.providers.gcd_provider import GCDProvider

        p = GCDProvider(credentials=gcd_creds)
        assert p.test_connection() is False

    @patch("models.gcd.is_mysql_available", return_value=True)
    @patch("models.gcd.check_mysql_status", return_value={"gcd_mysql_available": True})
    @patch("models.gcd.get_connection", return_value=None)
    def test_no_connection(self, mock_conn, mock_status, mock_avail, gcd_creds):
        from models.providers.gcd_provider import GCDProvider

        p = GCDProvider(credentials=gcd_creds)
        assert p.test_connection() is False


class TestGCDProviderSearchSeries:

    @patch("models.gcd.is_mysql_available", return_value=True)
    @patch("models.gcd.check_mysql_status", return_value={"gcd_mysql_available": True})
    @patch("models.gcd.search_series")
    def test_search_returns_results(self, mock_search, mock_status, mock_avail, gcd_creds):
        from models.providers.gcd_provider import GCDProvider

        mock_search.return_value = {
            "id": 200, "name": "Batman", "year_began": 1940,
            "publisher_name": "DC Comics", "issue_count": 713,
        }

        p = GCDProvider(credentials=gcd_creds)
        results = p.search_series("Batman")

        assert len(results) == 1
        assert results[0].title == "Batman"
        assert results[0].provider == ProviderType.GCD

    @patch("models.gcd.is_mysql_available", return_value=True)
    @patch("models.gcd.check_mysql_status", return_value={"gcd_mysql_available": True})
    @patch("models.gcd.search_series", return_value=None)
    def test_no_results(self, mock_search, mock_status, mock_avail, gcd_creds):
        from models.providers.gcd_provider import GCDProvider

        p = GCDProvider(credentials=gcd_creds)
        assert p.search_series("Nonexistent") == []


class TestGCDProviderGetSeries:

    @patch("models.gcd.is_mysql_available", return_value=True)
    @patch("models.gcd.check_mysql_status", return_value={"gcd_mysql_available": True})
    @patch("models.gcd.get_connection")
    def test_get_series_by_id(self, mock_conn, mock_status, mock_avail, gcd_creds):
        from models.providers.gcd_provider import GCDProvider

        conn, cursor = make_mock_mysql_connection(
            fetchone_result={
                "id": 200, "name": "Batman", "year_began": 1940,
                "year_ended": None, "publisher_name": "DC Comics", "issue_count": 713,
            }
        )
        mock_conn.return_value = conn

        p = GCDProvider(credentials=gcd_creds)
        result = p.get_series("200")

        assert isinstance(result, SearchResult)
        assert result.title == "Batman"
        assert result.year == 1940

    @patch("models.gcd.is_mysql_available", return_value=True)
    @patch("models.gcd.check_mysql_status", return_value={"gcd_mysql_available": True})
    @patch("models.gcd.get_connection")
    def test_series_not_found(self, mock_conn, mock_status, mock_avail, gcd_creds):
        from models.providers.gcd_provider import GCDProvider

        conn, cursor = make_mock_mysql_connection(fetchone_result=None)
        mock_conn.return_value = conn

        p = GCDProvider(credentials=gcd_creds)
        assert p.get_series("9999") is None


class TestGCDProviderGetIssues:

    @patch("models.gcd.is_mysql_available", return_value=True)
    @patch("models.gcd.check_mysql_status", return_value={"gcd_mysql_available": True})
    @patch("models.gcd.get_connection")
    def test_returns_issues(self, mock_conn, mock_status, mock_avail, gcd_creds):
        from models.providers.gcd_provider import GCDProvider

        conn, cursor = make_mock_mysql_connection(rows=[
            {"id": 1, "number": "1", "title": "Origin", "key_date": "1940-04", "on_sale_date": None},
            {"id": 2, "number": "2", "title": None, "key_date": "1940-06", "on_sale_date": None},
        ])
        mock_conn.return_value = conn

        p = GCDProvider(credentials=gcd_creds)
        results = p.get_issues("200")

        assert len(results) == 2
        assert results[0].issue_number == "1"
        assert results[0].title == "Origin"

    @patch("models.gcd.is_mysql_available", return_value=True)
    @patch("models.gcd.check_mysql_status", return_value={"gcd_mysql_available": True})
    @patch("models.gcd.get_connection")
    def test_empty_series(self, mock_conn, mock_status, mock_avail, gcd_creds):
        from models.providers.gcd_provider import GCDProvider

        conn, cursor = make_mock_mysql_connection(rows=[])
        mock_conn.return_value = conn

        p = GCDProvider(credentials=gcd_creds)
        assert p.get_issues("200") == []


class TestGCDProviderGetIssue:

    @patch("models.gcd.is_mysql_available", return_value=True)
    @patch("models.gcd.check_mysql_status", return_value={"gcd_mysql_available": True})
    @patch("models.gcd.get_connection")
    def test_get_single_issue(self, mock_conn, mock_status, mock_avail, gcd_creds):
        from models.providers.gcd_provider import GCDProvider

        conn, cursor = make_mock_mysql_connection(
            fetchone_result={
                "id": 1, "series_id": 200, "number": "1",
                "title": "Origin", "key_date": "1940-04", "on_sale_date": None,
            }
        )
        mock_conn.return_value = conn

        p = GCDProvider(credentials=gcd_creds)
        result = p.get_issue("1")

        assert isinstance(result, IssueResult)
        assert result.issue_number == "1"
        assert result.series_id == "200"


class TestGCDProviderToComicinfo:

    @patch("models.gcd.is_mysql_available", return_value=True)
    @patch("models.gcd.check_mysql_status", return_value={"gcd_mysql_available": True})
    @patch("models.gcd.get_issue_metadata")
    def test_uses_gcd_metadata(self, mock_meta, mock_status, mock_avail, gcd_creds):
        from models.providers.gcd_provider import GCDProvider

        mock_meta.return_value = {
            "Series": "Batman", "Number": "1", "Publisher": "DC Comics",
            "Year": 1940, "Writer": "Bill Finger",
        }

        p = GCDProvider(credentials=gcd_creds)
        issue = IssueResult(
            provider=ProviderType.GCD, id="1", series_id="200",
            issue_number="1", title="Origin",
        )

        result = p.to_comicinfo(issue)
        assert result["Series"] == "Batman"
        assert result["Writer"] == "Bill Finger"

    def test_fallback_without_metadata(self):
        from models.providers.gcd_provider import GCDProvider

        p = GCDProvider()
        issue = IssueResult(
            provider=ProviderType.GCD, id="1", series_id="200",
            issue_number="1", title="Origin", cover_date="1940-04",
        )
        series = SearchResult(
            provider=ProviderType.GCD, id="200", title="Batman",
            year=1940, publisher="DC Comics",
        )

        result = p.to_comicinfo(issue, series)
        assert result["Series"] == "Batman"
        assert result["Number"] == "1"
        assert result["Publisher"] == "DC Comics"
