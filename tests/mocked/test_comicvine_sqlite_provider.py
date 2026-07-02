"""Tests for ComicVineSqliteProvider adapter against a real temp SQLite DB."""
import pytest

from models.providers.base import ProviderType, SearchResult, IssueResult


@pytest.fixture
def cv_creds(comicvine_sqlite_db_path):
    from models.providers.base import ProviderCredentials
    return ProviderCredentials(database_path=str(comicvine_sqlite_db_path))


class TestProviderInit:

    def test_attributes(self):
        from models.providers.comicvine_sqlite_provider import ComicVineSqliteProvider
        p = ComicVineSqliteProvider()
        assert p.provider_type == ProviderType.COMICVINE_SQLITE
        assert p.display_name == "ComicVine (Local DB)"
        assert p.requires_auth is True
        assert p.auth_fields == ["database_path"]

    def test_registered(self):
        from models.providers import get_provider_by_name
        p = get_provider_by_name("comicvine_sqlite")
        assert p is not None
        assert p.provider_type == ProviderType.COMICVINE_SQLITE


class TestTestConnection:

    def test_success(self, comicvine_sqlite_configured, cv_creds):
        from models.providers.comicvine_sqlite_provider import ComicVineSqliteProvider
        assert ComicVineSqliteProvider(credentials=cv_creds).test_connection() is True

    def test_not_configured(self, monkeypatch, cv_creds):
        from models.providers.comicvine_sqlite_provider import ComicVineSqliteProvider
        monkeypatch.setattr("models.comicvine_sqlite._get_saved_credentials", lambda: None)
        monkeypatch.delenv("COMICVINE_DATABASE_PATH", raising=False)
        assert ComicVineSqliteProvider(credentials=cv_creds).test_connection() is False

    def test_missing_tables(self, tmp_path, monkeypatch, cv_creds):
        import sqlite3
        from models.providers.comicvine_sqlite_provider import ComicVineSqliteProvider
        path = str(tmp_path / "broken.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE cv_volume (id INTEGER PRIMARY KEY, name TEXT)")
        conn.commit()
        conn.close()
        monkeypatch.setattr("models.comicvine_sqlite._get_saved_credentials",
                            lambda: {"database_path": path})
        assert ComicVineSqliteProvider(credentials=cv_creds).test_connection() is False


class TestSearchSeries:

    def test_returns_results(self, comicvine_sqlite_configured, cv_creds):
        from models.providers.comicvine_sqlite_provider import ComicVineSqliteProvider
        results = ComicVineSqliteProvider(credentials=cv_creds).search_series("Batman")
        assert len(results) == 1
        assert results[0].title == "Batman"
        assert results[0].provider == ProviderType.COMICVINE_SQLITE

    def test_no_results(self, comicvine_sqlite_configured, cv_creds):
        from models.providers.comicvine_sqlite_provider import ComicVineSqliteProvider
        assert ComicVineSqliteProvider(credentials=cv_creds).search_series("Nonexistent") == []


class TestGetSeriesAndIssues:

    def test_get_series(self, comicvine_sqlite_configured, cv_creds):
        from models.providers.comicvine_sqlite_provider import ComicVineSqliteProvider
        result = ComicVineSqliteProvider(credentials=cv_creds).get_series("4050")
        assert isinstance(result, SearchResult)
        assert result.title == "Batman"
        assert result.year == 2016

    def test_get_issues(self, comicvine_sqlite_configured, cv_creds):
        from models.providers.comicvine_sqlite_provider import ComicVineSqliteProvider
        results = ComicVineSqliteProvider(credentials=cv_creds).get_issues("4050")
        assert len(results) == 1
        assert results[0].issue_number == "1"

    def test_get_issue(self, comicvine_sqlite_configured, cv_creds):
        from models.providers.comicvine_sqlite_provider import ComicVineSqliteProvider
        result = ComicVineSqliteProvider(credentials=cv_creds).get_issue("500")
        assert isinstance(result, IssueResult)
        assert result.issue_number == "1"
        assert result.series_id == "4050"


class TestToComicInfo:

    def test_uses_metadata(self, comicvine_sqlite_configured, cv_creds):
        from models.providers.comicvine_sqlite_provider import ComicVineSqliteProvider
        issue = IssueResult(
            provider=ProviderType.COMICVINE_SQLITE, id="500", series_id="4050",
            issue_number="1", title="The Beginning",
        )
        result = ComicVineSqliteProvider(credentials=cv_creds).to_comicinfo(issue)
        assert result['Series'] == "Batman"
        assert result['Writer'] == "Bob Kane"
        assert '_image_url' not in result
