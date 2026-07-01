"""Tests for GCDProvider adapter -- runs against a real temp SQLite GCD DB."""
import pytest
from unittest.mock import patch

from models.providers.base import ProviderType, SearchResult, IssueResult
from tests.mocked.conftest import build_gcd_sqlite


class TestGCDProviderInit:

    def test_provider_attributes(self):
        from models.providers.gcd_provider import GCDProvider

        p = GCDProvider()
        assert p.provider_type == ProviderType.GCD
        assert p.display_name == "Grand Comics Database"
        assert p.requires_auth is True
        assert p.auth_fields == ["database_path"]


class TestGCDProviderTestConnection:

    def test_successful_connection(self, gcd_configured, gcd_creds):
        from models.providers.gcd_provider import GCDProvider
        p = GCDProvider(credentials=gcd_creds)
        assert p.test_connection() is True

    def test_not_configured(self, monkeypatch, gcd_creds):
        from models.providers.gcd_provider import GCDProvider
        monkeypatch.setattr("models.gcd._get_saved_credentials", lambda: None)
        monkeypatch.delenv("GCD_DATABASE_PATH", raising=False)
        p = GCDProvider(credentials=gcd_creds)
        assert p.test_connection() is False

    def test_missing_core_tables(self, tmp_path, monkeypatch, gcd_creds):
        """A dump that lacks a core table must fail the connection test."""
        from models.providers.gcd_provider import GCDProvider
        import sqlite3
        path = str(tmp_path / "broken.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE gcd_series (id INTEGER PRIMARY KEY, name TEXT)")
        conn.commit()
        conn.close()
        monkeypatch.setattr("models.gcd._get_saved_credentials",
                            lambda: {"database_path": path})
        p = GCDProvider(credentials=gcd_creds)
        assert p.test_connection() is False


class TestGCDProviderSearchSeries:

    def test_search_returns_results(self, gcd_configured, gcd_creds):
        from models.providers.gcd_provider import GCDProvider
        p = GCDProvider(credentials=gcd_creds)
        results = p.search_series("Batman")
        assert len(results) == 1
        assert results[0].title == "Batman"
        assert results[0].provider == ProviderType.GCD

    def test_no_results(self, gcd_configured, gcd_creds):
        from models.providers.gcd_provider import GCDProvider
        p = GCDProvider(credentials=gcd_creds)
        assert p.search_series("Nonexistent") == []


class TestGCDProviderGetSeries:

    def test_get_series_by_id(self, gcd_configured, gcd_creds):
        from models.providers.gcd_provider import GCDProvider
        p = GCDProvider(credentials=gcd_creds)
        result = p.get_series("200")
        assert isinstance(result, SearchResult)
        assert result.title == "Batman"
        assert result.year == 1940
        assert result.issue_count == 4

    def test_series_not_found(self, gcd_configured, gcd_creds):
        from models.providers.gcd_provider import GCDProvider
        p = GCDProvider(credentials=gcd_creds)
        assert p.get_series("9999") is None


class TestGCDProviderGetIssues:

    def test_returns_issues_in_numeric_order(self, gcd_configured, gcd_creds):
        """printf zero-padding must sort #2 before #10 (not lexicographically)."""
        from models.providers.gcd_provider import GCDProvider
        p = GCDProvider(credentials=gcd_creds)
        results = p.get_issues("200")
        assert len(results) == 4
        assert [r.issue_number for r in results][:3] == ["1", "2", "10"]

    def test_empty_series(self, gcd_configured, gcd_creds):
        from models.providers.gcd_provider import GCDProvider
        p = GCDProvider(credentials=gcd_creds)
        assert p.get_issues("999") == []


class TestGCDProviderGetIssue:

    def test_get_single_issue(self, gcd_configured, gcd_creds):
        from models.providers.gcd_provider import GCDProvider
        p = GCDProvider(credentials=gcd_creds)
        result = p.get_issue("500")
        assert isinstance(result, IssueResult)
        assert result.issue_number == "1"
        assert result.series_id == "200"


class TestGCDProviderToComicinfo:

    def test_uses_gcd_metadata(self, gcd_configured, gcd_creds):
        from models.providers.gcd_provider import GCDProvider
        p = GCDProvider(credentials=gcd_creds)
        issue = IssueResult(
            provider=ProviderType.GCD, id="500", series_id="200",
            issue_number="1", title="The Beginning",
        )
        result = p.to_comicinfo(issue)
        assert result["Series"] == "Batman"
        assert result["Writer"] == "Bob Kane"

    def test_fallback_without_metadata(self, monkeypatch):
        from models.providers.gcd_provider import GCDProvider
        # Force get_issue_metadata to yield nothing so the fallback path runs.
        monkeypatch.setattr("models.gcd.get_issue_metadata", lambda *a, **k: None)

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
