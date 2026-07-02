"""Tests for models/comicvine_sqlite.py against a real temp SQLite DB."""
import sqlite3
import pytest

from tests.mocked.conftest import build_comicvine_sqlite


@pytest.fixture
def not_configured(monkeypatch):
    monkeypatch.setattr("models.comicvine_sqlite._get_saved_credentials", lambda: None)
    monkeypatch.delenv("COMICVINE_DATABASE_PATH", raising=False)


class TestDatabaseStatus:

    def test_available_when_configured(self, comicvine_sqlite_configured):
        from models.comicvine_sqlite import check_database_status, is_database_available
        status = check_database_status()
        assert status["cv_sqlite_available"] is True
        assert status["cv_sqlite_path_configured"] is True
        assert is_database_available() is True

    def test_not_available_when_unconfigured(self, not_configured):
        from models.comicvine_sqlite import check_database_status, is_database_available
        status = check_database_status()
        assert status["cv_sqlite_available"] is False
        assert status["cv_sqlite_path_configured"] is False
        assert is_database_available() is False

    def test_path_configured_but_missing(self, tmp_path, monkeypatch):
        from models.comicvine_sqlite import check_database_status
        missing = tmp_path / "nope.db"
        monkeypatch.setattr("models.comicvine_sqlite._get_saved_credentials",
                            lambda: {"database_path": str(missing)})
        status = check_database_status()
        assert status["cv_sqlite_available"] is False
        assert status["cv_sqlite_path_configured"] is True

    def test_env_var_fallback(self, comicvine_sqlite_db_path, monkeypatch):
        from models.comicvine_sqlite import get_connection_params
        monkeypatch.setattr("models.comicvine_sqlite._get_saved_credentials", lambda: None)
        monkeypatch.setenv("COMICVINE_DATABASE_PATH", str(comicvine_sqlite_db_path))
        assert get_connection_params() == {"database_path": str(comicvine_sqlite_db_path)}


class TestGetConnection:

    def test_opens_read_only(self, comicvine_sqlite_configured):
        from models.comicvine_sqlite import get_connection
        conn = get_connection()
        assert conn is not None
        try:
            with pytest.raises(sqlite3.OperationalError):
                conn.execute("CREATE TABLE should_fail (x INTEGER)")
        finally:
            conn.close()

    def test_none_when_unconfigured(self, not_configured):
        from models.comicvine_sqlite import get_connection
        assert get_connection() is None


class TestSearchVolumes:

    def test_finds_volume(self, comicvine_sqlite_configured):
        from models.comicvine_sqlite import search_volumes
        results = search_volumes("Batman")
        assert len(results) == 1
        vol = results[0]
        # Shape must match comicvine.search_volumes so the CV modal/map_to_comicinfo work.
        for key in ('id', 'name', 'start_year', 'publisher_name', 'count_of_issues',
                    'image_url', 'description'):
            assert key in vol
        assert vol['name'] == "Batman"
        assert vol['publisher_name'] == "DC Comics"

    def test_year_filter(self, comicvine_sqlite_configured):
        from models.comicvine_sqlite import search_volumes
        assert len(search_volumes("Batman", year=2016)) == 1
        assert search_volumes("Batman", year=1999) == []

    def test_alias_match(self, tmp_path, monkeypatch):
        from models.comicvine_sqlite import search_volumes
        path = build_comicvine_sqlite(tmp_path / "cv.db", extra_alias_volumes=True)
        monkeypatch.setattr("models.comicvine_sqlite._get_saved_credentials",
                            lambda: {"database_path": path})
        results = search_volumes("Batman")
        # 3 volumes all match via alias; none is named "Batman".
        assert len(results) == 3
        assert all("batman" not in (v['name'] or '').lower() for v in results)

    def test_not_configured(self, not_configured):
        from models.comicvine_sqlite import search_volumes
        assert search_volumes("Batman") == []


class TestGetIssueByNumber:

    def test_credit_parsing(self, comicvine_sqlite_configured):
        from models.comicvine_sqlite import get_issue_by_number
        data = get_issue_by_number(4050, "1")
        assert data is not None
        # role "writer, penciler" -> Writer only (first-match-wins)
        assert data['writers'] == ["Bob Kane"]
        # role "penciler, cover" -> Penciller only
        assert data['pencillers'] == ["Jerry Robinson"]
        assert data['inkers'] == []
        assert data['colorists'] == []
        assert data['letterers'] == []
        assert data['cover_artists'] == []
        assert data['characters'] == ["Batman", "Robin"]
        assert data['teams'] == ["Justice League"]
        assert data['locations'] == ["Gotham City"]
        # Only the FIRST story arc
        assert data['story_arc'] == "Year One"
        assert data['year'] == 2016
        assert data['month'] == 6
        assert data['day'] == 1
        assert data['page_count'] is None
        assert data['publisher'] == "DC Comics"
        assert data['volume_name'] == "Batman"
        assert data['volume_start_year'] == 2016

    def test_missing_issue(self, comicvine_sqlite_configured):
        from models.comicvine_sqlite import get_issue_by_number
        assert get_issue_by_number(4050, "999") is None

    def test_not_configured(self, not_configured):
        from models.comicvine_sqlite import get_issue_by_number
        assert get_issue_by_number(4050, "1") is None


class TestGetIssueMetadata:

    def test_maps_to_comicinfo(self, comicvine_sqlite_configured):
        from models.comicvine_sqlite import get_issue_metadata
        meta = get_issue_metadata(4050, "1")
        assert meta is not None
        assert meta['Series'] == "Batman"
        assert meta['Number'] == "1"
        assert meta['Title'] == "The Beginning"
        assert meta['Publisher'] == "DC Comics"
        assert meta['Writer'] == "Bob Kane"
        assert meta['Penciller'] == "Jerry Robinson"
        assert 'Inker' not in meta  # empty roles dropped
        assert meta['Characters'] == "Batman, Robin"
        assert meta['Teams'] == "Justice League"
        assert meta['Locations'] == "Gotham City"
        assert meta['StoryArc'] == "Year One"
        assert meta['Year'] == 2016
        assert meta['Month'] == 6
        assert meta['Day'] == 1
        assert meta['LanguageISO'] == "en"
        # Notes must identify the local DB, distinct from the API's "ComicVine CVDB".
        assert "ComicVine (Local DB)" in meta['Notes']
        assert "ComicVine CVDB" not in meta['Notes']
        assert "Volume ID: 4050" in meta['Notes']
        assert meta['_image_url'] == "https://example.com/issue1.jpg"

    def test_not_found(self, comicvine_sqlite_configured):
        from models.comicvine_sqlite import get_issue_metadata
        assert get_issue_metadata(4050, "999") is None

    def test_parity_with_map_to_comicinfo(self, comicvine_sqlite_configured):
        """Output must equal calling comicvine.map_to_comicinfo directly."""
        from models.comicvine_sqlite import (
            get_issue_by_number, _volume_data_from_issue, get_issue_metadata, SOURCE_LABEL
        )
        from models.comicvine import map_to_comicinfo
        issue_data = get_issue_by_number(4050, "1")
        expected = map_to_comicinfo(issue_data, _volume_data_from_issue(issue_data),
                                    source_label=SOURCE_LABEL)
        actual = {k: v for k, v in get_issue_metadata(4050, "1").items() if k != '_image_url'}
        assert actual == expected
