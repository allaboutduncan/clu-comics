"""Tests for models/metron.py -- mocked Mokkari API."""
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from unittest.mock import patch, MagicMock, mock_open

from mokkari.exceptions import ApiError, RateLimitError

from tests.mocked.conftest import make_mock_series, make_mock_issue


class TestGetApi:

    @patch("models.metron.MokkariSession")
    def test_returns_session(self, mock_session_class):
        from models.metron import get_api

        mock_session_class.return_value = MagicMock()
        api = get_api(api_token="tok")
        assert api is not None
        mock_session_class.assert_called_once()

    def test_empty_credentials(self):
        from models.metron import get_api
        assert get_api("", "") is None
        assert get_api(None, None) is None

    @patch("models.metron.MokkariSession")
    def test_reuses_session_for_same_token(self, mock_session_class):
        """Repeated calls with the same token must not construct a new
        Session each time -- concurrent threads sharing credentials rely on
        this to share mokkari's rate-limit tracking."""
        from models.metron import get_api

        mock_session_class.return_value = MagicMock()
        first = get_api(api_token="tok")
        second = get_api(api_token="tok")
        assert first is second
        mock_session_class.assert_called_once()

    @patch("models.metron.MokkariSession")
    def test_separate_sessions_for_different_tokens(self, mock_session_class):
        from models.metron import get_api

        mock_session_class.side_effect = [MagicMock(), MagicMock()]
        first = get_api(api_token="tok-1")
        second = get_api(api_token="tok-2")
        assert first is not second
        assert mock_session_class.call_count == 2


class TestSessionCache:
    """mokkari consults its cache before dispatching, so a hit costs neither an
    HTTP request nor a rate-limit slot. A library sweep re-reads the same series
    and issue endpoints from several code paths, so this is the cheapest
    reduction in Metron quota burn available."""

    @patch("models.metron.MokkariSession")
    def test_session_gets_a_cache(self, mock_session_class, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.delenv("METRON_CACHE_EXPIRE_DAYS", raising=False)
        from models.metron import get_api

        mock_session_class.return_value = MagicMock()
        get_api(api_token="tok")

        _, kwargs = mock_session_class.call_args
        cache = kwargs.get("cache")
        assert cache is not None
        assert cache.ttl_policy.ttl_for("series", "detail") == timedelta(days=1)
        assert cache.ttl_policy.ttl_for("issue", "list") == timedelta(days=1)
        assert str(tmp_path / "mokkari") in str(cache._db_name)

    @patch("models.metron.MokkariSession")
    def test_expiry_env_override(self, mock_session_class, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.setenv("METRON_CACHE_EXPIRE_DAYS", "7")
        from models.metron import get_api

        mock_session_class.return_value = MagicMock()
        get_api(api_token="tok")

        _, kwargs = mock_session_class.call_args
        assert kwargs["cache"].ttl_policy.ttl_for("issue", "detail") == timedelta(days=7)

    def test_expiry_never_falsy(self, monkeypatch):
        """mokkari treats a None TTL as 'never expire' -- a cache that never
        drops a row would pin stale metadata forever."""
        from models.metron import _metron_cache_expire_days

        monkeypatch.setenv("METRON_CACHE_EXPIRE_DAYS", "0")
        assert _metron_cache_expire_days() >= 1
        monkeypatch.setenv("METRON_CACHE_EXPIRE_DAYS", "garbage")
        assert _metron_cache_expire_days() >= 1

    def test_a_mokkari_4_cache_file_is_reused(self, tmp_path, monkeypatch):
        """Upgrading keeps the same path; mokkari 5 must discard a 4.x
        ``responses`` table rather than refuse the file, which would leave
        every upgraded install running uncached."""
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        from helpers.provider_cache import provider_cache_dir
        from models.metron import _metron_cache

        path = f"{provider_cache_dir('mokkari')}/mokkari_cache.db"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE responses (key, json, expire)")
        con.execute("INSERT INTO responses VALUES ('k', '{}', 0)")
        con.commit()
        con.close()

        cache = _metron_cache()
        assert cache is not None
        cache.close()

    @patch("models.metron.MokkariSession")
    @patch("models.metron.SqliteCache", side_effect=OSError(30, "Read-only file system"))
    def test_unwritable_cache_degrades_to_uncached_not_to_no_metron(
        self, mock_cache, mock_session_class
    ):
        from models.metron import get_api

        mock_session_class.return_value = MagicMock()
        api = get_api(api_token="tok")

        assert api is not None
        _, kwargs = mock_session_class.call_args
        assert kwargs["cache"] is None

    @patch("models.metron.MokkariSession")
    def test_invalidate_runs_cache_cleanup(self, mock_session_class):
        """Expired rows are never served, but only cleanup() reclaims them."""
        from models.metron import get_api, invalidate_session_cache

        session = MagicMock()
        mock_session_class.return_value = session
        get_api(api_token="tok")

        invalidate_session_cache()

        session.cache.cleanup.assert_called_once()

    @patch("models.metron.MokkariSession")
    def test_invalidate_survives_a_session_without_a_cache(self, mock_session_class):
        from models.metron import get_api, invalidate_session_cache

        session = MagicMock()
        session.cache = None
        mock_session_class.return_value = session
        get_api(api_token="tok")

        invalidate_session_cache()  # must not raise


def _cache_session(tmp_path):
    """A stand-in Session holding a real mokkari 5 cache."""
    from mokkari.sqlite_cache import SqliteCache

    cache = SqliteCache(str(tmp_path / "mokkari_cache.db"), default_ttl=timedelta(days=1))
    session = SimpleNamespace(cache=cache, api_url="https://metron.cloud/api/{}/")
    return session, cache


def _store(cache, key, value, resource, kind="detail"):
    cache.store(key, value, resource=resource, kind=kind)


class TestPurgeSeriesCache:
    """A user-initiated refresh has to reach Metron, not mokkari's cache, or a
    Summary edited on Metron would not appear until the TTL ran out."""

    SERIES_URL = "https://metron.cloud/api/series/8859/"
    ISSUES_URL = "https://metron.cloud/api/issue/?page=1&series_id=8859"

    def test_drops_series_and_issue_rows(self, tmp_path):
        from models.metron import purge_series_cache

        session, cache = _cache_session(tmp_path)
        _store(cache, self.SERIES_URL, {"desc": "old summary"}, "series")
        _store(cache, self.ISSUES_URL, {"results": []}, "issue", "list")

        assert purge_series_cache(8859, api=session) == 2
        assert cache.get(self.SERIES_URL) is None
        assert cache.get(self.ISSUES_URL) is None

    def test_leaves_other_series_detail_alone(self, tmp_path):
        from models.metron import purge_series_cache

        session, cache = _cache_session(tmp_path)
        other_detail = "https://metron.cloud/api/series/88591/"
        _store(cache, self.SERIES_URL, {"desc": "old summary"}, "series")
        _store(cache, other_detail, {"desc": "different series"}, "series")

        assert purge_series_cache(8859, api=session) == 1
        assert cache.get(other_detail) == {"desc": "different series"}

    def test_next_fetch_sees_the_new_body(self, tmp_path):
        from models.metron import purge_series_cache

        session, cache = _cache_session(tmp_path)
        _store(cache, self.SERIES_URL, {"desc": "old summary"}, "series")
        purge_series_cache(8859, api=session)
        _store(cache, self.SERIES_URL, {"desc": "new summary"}, "series")

        assert cache.get(self.SERIES_URL) == {"desc": "new summary"}

    def test_session_without_a_cache_is_a_no_op(self):
        from models.metron import purge_series_cache

        assert purge_series_cache(8859, api=SimpleNamespace(cache=None)) == 0

    def test_non_numeric_series_id_is_a_no_op(self, tmp_path):
        from models.metron import purge_series_cache

        session, cache = _cache_session(tmp_path)
        _store(cache, self.SERIES_URL, {"desc": "old summary"}, "series")

        assert purge_series_cache("not-an-id", api=session) == 0
        assert cache.get(self.SERIES_URL) == {"desc": "old summary"}

    def test_a_failing_cache_never_raises(self):
        from models.metron import purge_series_cache

        cache = MagicMock()
        cache.delete.side_effect = sqlite3.OperationalError("database is locked")
        session = SimpleNamespace(cache=cache, api_url="https://metron.cloud/api/{}/")

        with patch("core.database.get_issues_for_series", return_value=[]):
            assert purge_series_cache(8859, api=session) == 0

    @patch("models.metron.MokkariSession")
    def test_defaults_to_every_cached_session(self, mock_session_class, tmp_path):
        from models.metron import get_api, invalidate_session_cache, purge_series_cache

        session, cache = _cache_session(tmp_path)
        _store(cache, self.SERIES_URL, {"desc": "old summary"}, "series")
        mock_session_class.return_value = session
        invalidate_session_cache()
        get_api(api_token="tok")

        with patch("core.database.get_issues_for_series", return_value=[]):
            assert purge_series_cache(8859) == 1
        assert cache.get(self.SERIES_URL) is None
        invalidate_session_cache()


class TestPurgeIssueCache:
    """The ``/issue/<id>/`` body is the one carrying credits.

    Metron records are finished after a comic ships, so an issue fetched on
    release morning can be cached without creators, and a re-tag would replay
    that incomplete body until the TTL ran out.
    """

    ISSUE_URL = "https://metron.cloud/api/issue/172615/"

    def test_drops_the_issue_detail_row(self, tmp_path):
        from models.metron import purge_issue_cache

        session, cache = _cache_session(tmp_path)
        _store(cache, self.ISSUE_URL, {"credits": []}, "issue")

        assert purge_issue_cache(172615, api=session) == 1
        assert cache.get(self.ISSUE_URL) is None

    def test_leaves_other_issues_alone(self, tmp_path):
        from models.metron import purge_issue_cache

        session, cache = _cache_session(tmp_path)
        other_issue = "https://metron.cloud/api/issue/1726150/"
        filtered = "https://metron.cloud/api/credit/?issue_id=172615"
        _store(cache, self.ISSUE_URL, {"credits": []}, "issue")
        _store(cache, other_issue, {"credits": ["someone"]}, "issue")
        _store(cache, filtered, {"results": []}, "credit", "list")

        assert purge_issue_cache(172615, api=session) == 1
        assert cache.get(other_issue) == {"credits": ["someone"]}
        assert cache.get(filtered) == {"results": []}

    def test_key_matches_what_mokkari_stores(self, tmp_path):
        """The purge rebuilds the key, so it must be the one Session writes:
        drive a real Session against a stubbed HTTP layer and purge after."""
        from mokkari.session import Session

        from models.metron import purge_issue_cache

        _, cache = _cache_session(tmp_path)
        real = Session("tok", cache=cache)
        body = {"id": 172615}
        with patch.object(Session, "_request_data", return_value=body):
            real._get(["issue", 172615])
        assert cache.get(real._url(["issue", 172615])) == body

        assert purge_issue_cache(172615, api=real) == 1

    def test_session_without_a_cache_is_a_no_op(self):
        from models.metron import purge_issue_cache

        assert purge_issue_cache(172615, api=SimpleNamespace(cache=None)) == 0

    def test_non_numeric_issue_id_is_a_no_op(self, tmp_path):
        from models.metron import purge_issue_cache

        session, cache = _cache_session(tmp_path)
        _store(cache, self.ISSUE_URL, {"credits": []}, "issue")

        assert purge_issue_cache("not-an-id", api=session) == 0
        assert cache.get(self.ISSUE_URL) == {"credits": []}

    def test_series_purge_also_drops_known_issue_details(self, tmp_path):
        """A series refresh has to clear the issue bodies too, or the summary
        refreshes while every issue keeps its stale credits."""
        from models.metron import purge_series_cache

        session, cache = _cache_session(tmp_path)
        _store(cache, "https://metron.cloud/api/series/8859/", {"desc": "old"}, "series")
        _store(cache, self.ISSUE_URL, {"credits": []}, "issue")

        with patch(
            "core.database.get_issues_for_series",
            return_value=[{"id": 172615}, {"id": 999999}],
        ):
            assert purge_series_cache(8859, api=session) == 2

        assert cache.get(self.ISSUE_URL) is None

    def test_series_purge_survives_a_database_failure(self, tmp_path):
        from models.metron import purge_series_cache

        session, cache = _cache_session(tmp_path)
        _store(cache, "https://metron.cloud/api/series/8859/", {"desc": "old"}, "series")

        with patch("core.database.get_issues_for_series", side_effect=RuntimeError("db down")):
            assert purge_series_cache(8859, api=session) == 1


class TestFetchIssueDetail:
    """Metadata written into an archive must never come from a cached body."""

    ISSUE_URL = "https://metron.cloud/api/issue/172615/"

    def _session_with_cache(self, tmp_path):
        _, cache = _cache_session(tmp_path)
        api = MagicMock()
        api.cache = cache
        api.api_url = "https://metron.cloud/api/{}/"
        return api, cache

    def test_drops_the_cached_body_before_fetching(self, tmp_path):
        from models.metron import fetch_issue_detail

        api, cache = self._session_with_cache(tmp_path)
        _store(cache, self.ISSUE_URL, {"credits": []}, "issue")
        api.issue.return_value = SimpleNamespace(id=172615)

        result = fetch_issue_detail(api, 172615)

        assert cache.get(self.ISSUE_URL) is None
        api.issue.assert_called_once_with(172615)
        assert result.id == 172615

    def test_non_numeric_id_never_calls_the_api(self, tmp_path):
        from models.metron import fetch_issue_detail

        api, _ = self._session_with_cache(tmp_path)
        assert fetch_issue_detail(api, "not-an-id") is None
        api.issue.assert_not_called()

    def test_get_issue_metadata_uses_the_purging_fetch(self, tmp_path):
        """The double fetch's second call is the one written to disk."""
        from models.metron import get_issue_metadata

        api, cache = self._session_with_cache(tmp_path)
        _store(cache, self.ISSUE_URL, {"credits": [], "number": "3"}, "issue")
        api.issues_list.return_value = [SimpleNamespace(id=172615)]
        api.issue.return_value = {"number": "3", "credits": [{"creator": "Bengal"}]}

        result = get_issue_metadata(api, 15845, "3")

        assert cache.get(self.ISSUE_URL) is None
        assert result["credits"] == [{"creator": "Bengal"}]


class TestListIssuesModifiedSince:
    """Metron finishes issue records after a comic ships, and ``modified`` is
    the field that says so. Asking which issues changed is one paged list call
    against a detail fetch per file for anything that guesses from local
    timestamps."""

    def test_filters_on_modified_gt(self):
        from models.metron import list_issues_modified_since

        api = MagicMock()
        api.issues_list.return_value = [
            SimpleNamespace(id=172615, modified="2026-08-26T08:35:11"),
            SimpleNamespace(id=999, modified="2026-08-26T09:00:00"),
        ]

        changed = list_issues_modified_since(api, "2026-08-25")

        api.issues_list.assert_called_once_with({"modified_gt": "2026-08-25"})
        assert changed == {
            172615: "2026-08-26T08:35:11",
            999: "2026-08-26T09:00:00",
        }

    def test_no_api_or_no_date_never_calls_out(self):
        from models.metron import list_issues_modified_since

        api = MagicMock()
        assert list_issues_modified_since(None, "2026-08-25") == {}
        assert list_issues_modified_since(api, "") == {}
        api.issues_list.assert_not_called()

    def test_an_api_error_is_an_empty_result(self):
        """The credit-less pass still runs, so a failed feed degrades rather
        than taking the sweep down."""
        from mokkari.exceptions import ApiError

        from models.metron import list_issues_modified_since

        api = MagicMock()
        api.issues_list.side_effect = ApiError("boom")

        assert list_issues_modified_since(api, "2026-08-25") == {}

    def test_entries_without_an_id_are_dropped(self):
        from models.metron import list_issues_modified_since

        api = MagicMock()
        api.issues_list.return_value = [
            SimpleNamespace(id=None, modified="x"),
            SimpleNamespace(id=7, modified=None),
        ]

        assert list_issues_modified_since(api, "2026-08-25") == {7: ""}


class TestIsConnectionError:

    def test_timeout_detected(self):
        from models.metron import is_connection_error
        from mokkari.exceptions import ApiError
        import requests.exceptions

        exc = ApiError("API error")
        exc.__cause__ = requests.exceptions.ReadTimeout()
        assert is_connection_error(exc) is True

    def test_normal_error_not_connection(self):
        from models.metron import is_connection_error
        assert is_connection_error(Exception("Invalid credentials")) is False

    def test_various_network_errors(self):
        from models.metron import is_connection_error
        from mokkari.exceptions import ApiError
        import requests.exceptions

        exc = ApiError("API error")
        exc.__cause__ = requests.exceptions.ConnectionError()
        assert is_connection_error(exc) is True


class TestParseCvinfo:

    def test_parse_metron_id(self, tmp_path):
        from models.metron import parse_cvinfo_for_metron_id

        cvinfo = tmp_path / "cvinfo"
        cvinfo.write_text("https://comicvine.gamespot.com/batman/4050-12345/\nseries_id: 100\n")

        assert parse_cvinfo_for_metron_id(str(cvinfo)) == 100

    def test_no_series_id(self, tmp_path):
        from models.metron import parse_cvinfo_for_metron_id

        cvinfo = tmp_path / "cvinfo"
        cvinfo.write_text("https://comicvine.gamespot.com/batman/4050-12345/\n")

        assert parse_cvinfo_for_metron_id(str(cvinfo)) is None

    def test_parse_comicvine_id(self, tmp_path):
        from models.metron import parse_cvinfo_for_comicvine_id

        cvinfo = tmp_path / "cvinfo"
        cvinfo.write_text("https://comicvine.gamespot.com/batman/4050-12345/\nseries_id: 100\n")

        assert parse_cvinfo_for_comicvine_id(str(cvinfo)) == 12345


class TestGetSeriesIdByComicvineId:

    def test_found(self):
        from models.metron import get_series_id_by_comicvine_id

        mock_api = MagicMock()
        mock_series = make_mock_series(id=42)
        mock_api.series_list.return_value = [mock_series]

        assert get_series_id_by_comicvine_id(mock_api, 12345) == 42

    def test_not_found(self):
        from models.metron import get_series_id_by_comicvine_id

        mock_api = MagicMock()
        mock_api.series_list.return_value = []

        assert get_series_id_by_comicvine_id(mock_api, 99999) is None


class TestSearchSeriesByName:

    def test_returns_best_match(self):
        from models.metron import search_series_by_name

        mock_api = MagicMock()
        s = make_mock_series(id=100, name="Batman", year_began=2016)
        mock_api.series_list.return_value = [s]

        result = search_series_by_name(mock_api, "Batman")
        assert result is not None
        assert result["id"] == 100
        assert result["name"] == "Batman"

    def test_year_ranking(self):
        from models.metron import search_series_by_name

        mock_api = MagicMock()
        s1 = make_mock_series(id=1, name="Batman", year_began=1940)
        s2 = make_mock_series(id=2, name="Batman", year_began=2016)
        mock_api.series_list.return_value = [s1, s2]

        result = search_series_by_name(mock_api, "Batman", year=2016)
        assert result["id"] == 2  # Closer to 2016

    def test_no_results(self):
        from models.metron import search_series_by_name

        mock_api = MagicMock()
        mock_api.series_list.return_value = []

        assert search_series_by_name(mock_api, "Nonexistent") is None

    def test_no_api(self):
        from models.metron import search_series_by_name
        assert search_series_by_name(None, "Batman") is None


class TestSearchSeriesList:

    def _make(self, **kwargs):
        s = make_mock_series(**kwargs)
        # MagicMock auto-creates truthy attrs; pin the optional ones so the
        # mapped dict has clean values.
        s.image = None
        s.desc = ""
        s.issue_count = 12
        return s

    def test_returns_all_candidates(self):
        from models.metron import search_series_list

        mock_api = MagicMock()
        mock_api.series_list.return_value = [
            self._make(id=1, name="Batman", year_began=1940),
            self._make(id=2, name="Batman Beyond", year_began=1999),
        ]

        results = search_series_list(mock_api, "Batman")
        assert len(results) == 2
        assert {r["id"] for r in results} == {1, 2}
        first = next(r for r in results if r["id"] == 1)
        assert first["name"] == "Batman"
        assert first["start_year"] == 1940
        assert first["publisher_name"] == "DC Comics"
        assert first["count_of_issues"] == 12

    def test_year_ranking(self):
        from models.metron import search_series_list

        mock_api = MagicMock()
        mock_api.series_list.return_value = [
            self._make(id=1, name="Batman", year_began=1940),
            self._make(id=2, name="Batman", year_began=2016),
        ]

        results = search_series_list(mock_api, "Batman", year=2016)
        assert results[0]["id"] == 2  # closest year first

    def test_no_results(self):
        from models.metron import search_series_list

        mock_api = MagicMock()
        mock_api.series_list.return_value = []
        assert search_series_list(mock_api, "Nonexistent") == []

    def test_no_api(self):
        from models.metron import search_series_list
        assert search_series_list(None, "Batman") == []


class TestGetSeriesDetails:

    def test_returns_details(self):
        from models.metron import get_series_details

        mock_api = MagicMock()
        mock_api.series.return_value = make_mock_series(id=100, cv_id=12345)

        result = get_series_details(mock_api, 100)
        assert result["id"] == 100
        assert result["cv_id"] == 12345

    def test_not_found(self):
        from models.metron import get_series_details

        mock_api = MagicMock()
        mock_api.series.return_value = None

        assert get_series_details(mock_api, 9999) is None


class TestGetSeries:

    def test_returns_full_model(self):
        from models.metron import get_series

        mock_api = MagicMock()
        model = make_mock_series(id=100, cv_id=12345)
        mock_api.series.return_value = model

        assert get_series(mock_api, 100) is model
        mock_api.series.assert_called_once_with(100)

    def test_missing_api_or_id(self):
        from models.metron import get_series

        assert get_series(None, 100) is None
        assert get_series(MagicMock(), None) is None

    def test_waits_and_retries_on_rate_limit(self):
        # A transient RateLimitError must be retried (via _api_call), not raised,
        # so bulk callers don't error out mid-scan.
        from models.metron import get_series
        from mokkari.exceptions import RateLimitError

        mock_api = MagicMock()
        model = make_mock_series(id=100)
        mock_api.series.side_effect = [RateLimitError("rate limited", retry_after=0), model]

        with patch("models.metron.time.sleep"):
            result = get_series(mock_api, 100)
        assert result is model
        assert mock_api.series.call_count == 2


class TestGetIssueMetadata:

    def test_double_fetch_pattern(self):
        from models.metron import get_issue_metadata

        mock_api = MagicMock()
        mock_issue_list = [MagicMock(id=500)]
        mock_api.issues_list.return_value = mock_issue_list
        full_issue = make_mock_issue(id=500)
        mock_api.issue.return_value = full_issue

        result = get_issue_metadata(mock_api, 100, "1")
        assert result is not None
        mock_api.issues_list.assert_called_once()
        mock_api.issue.assert_called_once_with(500)

    def test_issue_not_found(self):
        from models.metron import get_issue_metadata

        mock_api = MagicMock()
        mock_api.issues_list.return_value = []

        assert get_issue_metadata(mock_api, 100, "999") is None


class TestGetAllIssuesForSeries:

    def test_returns_issues(self):
        from models.metron import get_all_issues_for_series

        mock_api = MagicMock()
        mock_api.issues_list.return_value = [MagicMock(id=1), MagicMock(id=2)]

        result = get_all_issues_for_series(mock_api, 100)
        assert len(result) == 2

    def test_empty_series(self):
        from models.metron import get_all_issues_for_series

        mock_api = MagicMock()
        mock_api.issues_list.return_value = []

        assert get_all_issues_for_series(mock_api, 100) == []


class TestMapToComicinfo:

    def test_full_mapping(self):
        from models.metron import map_to_comicinfo

        issue_data = {
            "id": 500,
            "number": "1",
            "story_titles": ["The Beginning"],
            "cover_date": "2020-06-15",
            "series": {"name": "Batman", "year_began": 2016, "genres": [{"name": "Superhero"}]},
            "publisher": {"name": "DC Comics"},
            "credits": [
                {"creator": "Tom King", "role": [{"name": "Writer"}]},
                {"creator": "David Finch", "role": [{"name": "Penciller"}]},
            ],
            "characters": [{"name": "Batman"}, {"name": "Catwoman"}],
            "teams": [{"name": "Justice League"}],
            "rating": {"name": "Teen"},
            "desc": "Batman returns to Gotham",
            "resource_url": "https://metron.cloud/issue/500/",
            "modified": "2024-01-01",
            "page_count": 32,
        }

        result = map_to_comicinfo(issue_data)

        assert result["Series"] == "Batman"
        assert result["Number"] == "1"
        assert result["Title"] == "The Beginning"
        assert result["Year"] == 2020
        assert result["Month"] == 6
        assert result["Day"] == 15
        assert result["Publisher"] == "DC Comics"
        assert result["Writer"] == "Tom King"
        assert result["Penciller"] == "David Finch"
        assert "Batman" in result["Characters"]
        assert result["Genre"] == "Superhero"
        assert result["LanguageISO"] == "en"
        assert result["MetronId"] == 500

    def test_minimal_data(self):
        from models.metron import map_to_comicinfo

        result = map_to_comicinfo({"id": 1, "number": "1"})
        assert "Number" in result
        assert result["Number"] == "1"
        assert "Notes" in result

    def test_preserves_cover_and_store_dates(self):
        from models.metron import map_to_comicinfo

        issue_data = {"id": 1, "number": "1", "cover_date": "2020-06-15",
                      "store_date": "2020-06-03"}
        result = map_to_comicinfo(issue_data)
        assert result["CoverDate"] == "2020-06-15"
        assert result["StoreDate"] == "2020-06-03"

    def test_omits_absent_store_date(self):
        from models.metron import map_to_comicinfo

        result = map_to_comicinfo({"id": 1, "number": "1", "cover_date": "2020-06-15"})
        assert result["CoverDate"] == "2020-06-15"
        assert "StoreDate" not in result


class TestExtractCreditsByRole:

    def test_extracts_writers(self):
        from models.metron import extract_credits_by_role

        credits = [
            {"creator": "Tom King", "role": [{"name": "Writer"}]},
            {"creator": "David Finch", "role": [{"name": "Penciller"}]},
        ]
        result = extract_credits_by_role(credits, ["Writer"])
        assert result == "Tom King"

    def test_multiple_matches(self):
        from models.metron import extract_credits_by_role

        credits = [
            {"creator": "Tom King", "role": [{"name": "Writer"}]},
            {"creator": "Scott Snyder", "role": [{"name": "Writer"}]},
        ]
        result = extract_credits_by_role(credits, ["Writer"])
        assert "Tom King" in result
        assert "Scott Snyder" in result

    def test_no_matches(self):
        from models.metron import extract_credits_by_role

        credits = [{"creator": "David Finch", "role": [{"name": "Penciller"}]}]
        result = extract_credits_by_role(credits, ["Writer"])
        assert result == ""

    def test_script_role_maps_to_writer(self):
        # Metron lists many writers under the "Script" role (e.g. Jeff Lemire
        # credited as "Script, Cover" on Black Hammer #1). map_to_comicinfo must
        # treat "Script" as a Writer role or the writer is dropped.
        from models.metron import map_to_comicinfo

        issue_data = {
            "credits": [
                {"creator": "Jeff Lemire", "role": [{"name": "Script"}, {"name": "Cover"}]},
                {"creator": "Dean Ormston", "role": [{"name": "Pencils"}]},
            ],
        }
        result = map_to_comicinfo(issue_data)
        assert result["Writer"] == "Jeff Lemire"


    def test_substring_matches_spelled_out_roles(self):
        """Metron names editorial roles in full, so an exact match finds almost
        none of them."""
        from models.metron import extract_credits_by_role

        credits = [
            {"creator": "Nick Lowe", "role": [{"name": "Editor"}]},
            {"creator": "Kaitlyn Lindtvedt", "role": [{"name": "Assistant Editor"}]},
            {"creator": "C. B. Cebulski", "role": [{"name": "Editor In Chief"}]},
        ]

        assert extract_credits_by_role(credits, ["Editor"]) == "Nick Lowe"
        assert extract_credits_by_role(credits, ["Editor"], substring=True) == (
            "Nick Lowe, Kaitlyn Lindtvedt, C. B. Cebulski"
        )

    def test_substring_is_case_insensitive(self):
        from models.metron import extract_credits_by_role

        credits = [{"creator": "Nick Lowe", "role": [{"name": "SENIOR EDITOR"}]}]
        assert extract_credits_by_role(credits, ["editor"], substring=True) == "Nick Lowe"

    def test_a_creator_with_two_matching_roles_is_listed_once(self):
        from models.metron import extract_credits_by_role

        credits = [
            {"creator": "Katie Kubert", "role": [{"name": "Editor"}, {"name": "Group Editor"}]},
        ]
        assert extract_credits_by_role(credits, ["Editor"], substring=True) == "Katie Kubert"

    def test_editor_roles_land_in_the_editor_tag(self):
        """<Editor> was in the writer's allowlist but the Metron mapper never
        produced it, so every Metron-tagged file lost its editor credits."""
        from models.metron import map_to_comicinfo

        issue_data = {
            "credits": [
                {"creator": "Andrew Marino", "role": [{"name": "Editor"}]},
                {"creator": "Chris Conroy", "role": [{"name": "Executive Editor"}]},
                {"creator": "Katie Kubert", "role": [{"name": "Group Editor"}]},
                {"creator": "Marie Javins", "role": [{"name": "Editor In Chief"}]},
            ],
        }
        result = map_to_comicinfo(issue_data)
        assert result["Editor"] == (
            "Andrew Marino, Chris Conroy, Katie Kubert, Marie Javins"
        )

    def test_non_editorial_titles_stay_out_of_the_editor_tag(self):
        """Metron credits a publisher's officers on the issue. None of those
        titles contains "editor", which is what keeps them out."""
        from models.metron import map_to_comicinfo

        issue_data = {
            "credits": [
                {"creator": "Jim Lee", "role": [
                    {"name": "President"},
                    {"name": "Publisher"},
                    {"name": "Chief Creative Officer"},
                ]},
                {"creator": "Jay Bowen", "role": [{"name": "Designer"}]},
                {"creator": "Nick Lowe", "role": [{"name": "Editor"}]},
            ],
        }
        result = map_to_comicinfo(issue_data)
        assert result["Editor"] == "Nick Lowe"

    def test_no_editor_credits_means_no_tag(self):
        """The writer skips empty values, but None keeps it out of the dict the
        UI and file_index read too."""
        from models.metron import map_to_comicinfo

        issue_data = {"credits": [{"creator": "Tom King", "role": [{"name": "Writer"}]}]}
        assert "Editor" not in map_to_comicinfo(issue_data)


class TestCalculateComicWeek:

    def test_returns_tuple(self):
        from models.metron import calculate_comic_week
        from datetime import datetime

        start, end = calculate_comic_week(datetime(2024, 1, 15))  # Monday
        assert start.weekday() == 6  # Sunday
        assert end.weekday() == 5    # Saturday

    def test_string_date(self):
        from models.metron import calculate_comic_week

        start, end = calculate_comic_week("2024-01-15")
        assert start is not None
        assert end is not None

    def test_defaults_to_now(self):
        from models.metron import calculate_comic_week

        start, end = calculate_comic_week()
        assert start is not None


class TestUpdateCvinfoWithMetronId:

    def test_appends_series_id(self, tmp_path):
        from models.metron import update_cvinfo_with_metron_id

        cvinfo = tmp_path / "cvinfo"
        cvinfo.write_text("https://comicvine.gamespot.com/batman/4050-12345/\n")

        assert update_cvinfo_with_metron_id(str(cvinfo), 100) is True
        content = cvinfo.read_text()
        assert "series_id: 100" in content

    def test_updates_existing(self, tmp_path):
        from models.metron import update_cvinfo_with_metron_id

        cvinfo = tmp_path / "cvinfo"
        cvinfo.write_text("series_id: 50\n")

        assert update_cvinfo_with_metron_id(str(cvinfo), 100) is True
        content = cvinfo.read_text()
        assert "series_id: 100" in content
        assert "series_id: 50" not in content


class TestSlidingWindowRateLimiter:

    def test_allows_up_to_limit_without_blocking(self):
        from models.metron import _SlidingWindowRateLimiter

        limiter = _SlidingWindowRateLimiter(max_requests=3, window_seconds=60.0)
        start = time.monotonic()
        for _ in range(3):
            limiter.acquire()
        assert time.monotonic() - start < 0.5

    def test_blocks_until_window_frees_a_slot(self):
        from models.metron import _SlidingWindowRateLimiter

        limiter = _SlidingWindowRateLimiter(max_requests=2, window_seconds=0.2)
        limiter.acquire()
        limiter.acquire()
        start = time.monotonic()
        limiter.acquire()
        assert time.monotonic() - start >= 0.15

    def test_reset_clears_recorded_requests(self):
        from models.metron import _SlidingWindowRateLimiter

        limiter = _SlidingWindowRateLimiter(max_requests=1, window_seconds=60.0)
        limiter.acquire()
        limiter.reset()
        start = time.monotonic()
        limiter.acquire()
        assert time.monotonic() - start < 0.5

    def test_api_call_acquires_rate_limiter_slot(self):
        """_api_call must throttle through the shared limiter so concurrent
        threads calling different Metron functions still share one budget."""
        from models.metron import _api_call, _metron_rate_limiter

        with patch.object(_metron_rate_limiter, "acquire") as mock_acquire:
            result = _api_call(lambda: "ok", "test context")
        assert result == "ok"
        mock_acquire.assert_called_once()


def _window(limit=None, remaining=None, reset=None):
    return SimpleNamespace(limit=limit, remaining=remaining, reset=reset)


def _status(burst=None, sustained=None):
    return SimpleNamespace(
        burst=burst or _window(), sustained=sustained or _window()
    )


class TestAdaptivePacing:
    """mokkari parses the X-RateLimit-* headers but its own pre-emptive check is
    advisory -- its docstring says callers must cap their own concurrency. These
    tests pin the capping we do from those headers."""

    def test_retunes_the_window_from_the_reported_burst_limit(self):
        from models.metron import _metron_pacer, _metron_rate_limiter

        session = MagicMock()
        session.rate_limit_status = _status(burst=_window(limit=20, remaining=19))

        _metron_pacer.observe(session)

        assert _metron_rate_limiter.max_requests == 16  # 80% of 20

    def test_missing_headers_leave_the_default_budget_alone(self):
        """Every field is None until the first response completes; a TypeError
        here would kill a daemon thread."""
        from models.metron import _metron_pacer, _metron_rate_limiter

        before = _metron_rate_limiter.max_requests
        session = MagicMock()
        session.rate_limit_status = _status()

        _metron_pacer.observe(session)  # must not raise

        assert _metron_rate_limiter.max_requests == before

    def test_session_without_rate_limit_status_is_ignored(self):
        from models.metron import _metron_pacer

        session = MagicMock()
        session.rate_limit_status = None

        _metron_pacer.observe(session)  # must not raise

    def test_naive_reset_datetime_does_not_explode(self):
        """mokkari returns tz-aware datetimes; comparing one against a naive
        now() raises. Guard both ways."""
        from models.metron import _metron_pacer

        session = MagicMock()
        session.rate_limit_status = _status(
            sustained=_window(
                limit=100, remaining=0, reset=datetime.now() + timedelta(hours=2)
            )
        )

        _metron_pacer.observe(session)  # must not raise

    def test_exhausted_daily_quota_skips_the_call_without_sleeping(self):
        """The regression test for the reported log: a spent daily quota was
        surfacing as a 59.5s wait retried 3x per series, for every series in a
        sweep. It must now short-circuit."""
        from models.metron import _api_call, _metron_pacer

        session = MagicMock()
        session.rate_limit_status = _status(
            sustained=_window(
                limit=100,
                remaining=0,
                reset=datetime.now(timezone.utc) + timedelta(hours=2),
            )
        )
        _metron_pacer.observe(session)

        called = {"n": 0}

        def fn():
            called["n"] += 1
            return "ok"

        with patch("models.metron.time.sleep") as mock_sleep:
            result = _api_call(fn, "test context", default="fallback")

        assert result == "fallback"
        assert called["n"] == 0
        mock_sleep.assert_not_called()

    def test_calls_resume_once_the_daily_window_has_reset(self):
        from models.metron import _api_call, _metron_pacer

        session = MagicMock()
        session.rate_limit_status = _status(
            sustained=_window(
                limit=100,
                remaining=0,
                reset=datetime.now(timezone.utc) + timedelta(milliseconds=50),
            )
        )
        _metron_pacer.observe(session)
        assert _metron_pacer.daily_limit_reached() is True

        time.sleep(0.1)

        assert _metron_pacer.daily_limit_reached() is False
        assert _api_call(lambda: "ok", "test context") == "ok"

    def test_sub_threshold_retry_after_gives_up_on_the_first_attempt(self):
        """mokkari reports retry_after as max(burst_wait, sustained_wait), so a
        spent daily quota can arrive as 59.5s -- just under the old 60s 'this is
        the daily cap' threshold. The headers say which window it really is."""
        from models.metron import _api_call, invalidate_session_cache
        import models.metron as metron

        session = MagicMock()
        session.rate_limit_status = _status(
            sustained=_window(
                limit=100,
                remaining=0,
                reset=datetime.now(timezone.utc) + timedelta(hours=2),
            )
        )
        with metron._SESSION_CACHE_LOCK:
            metron._SESSION_CACHE[("u", "p")] = session

        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise RateLimitError("slow down", retry_after=59.5)

        with patch("models.metron.time.sleep") as mock_sleep:
            result = _api_call(fn, "test context", default="fallback")

        assert result == "fallback"
        assert calls["n"] == 1  # not 3
        mock_sleep.assert_not_called()

    def test_invalidate_clears_the_daily_latch(self):
        """A wrongly-latched window would silently disable Metron for hours."""
        from models.metron import _metron_pacer, invalidate_session_cache

        session = MagicMock()
        session.rate_limit_status = _status(
            sustained=_window(
                limit=100,
                remaining=0,
                reset=datetime.now(timezone.utc) + timedelta(hours=2),
            )
        )
        _metron_pacer.observe(session)
        assert _metron_pacer.daily_limit_reached() is True

        invalidate_session_cache()

        assert _metron_pacer.daily_limit_reached() is False

    def test_successful_call_observes_the_headers(self):
        from models.metron import _api_call, _metron_rate_limiter
        import models.metron as metron

        session = MagicMock()
        session.rate_limit_status = _status(burst=_window(limit=40, remaining=39))
        with metron._SESSION_CACHE_LOCK:
            metron._SESSION_CACHE[("u", "p")] = session

        assert _api_call(lambda: "ok", "test context") == "ok"

        assert _metron_rate_limiter.max_requests == 32  # 80% of 40


class TestGetReleases:

    def test_fetches_releases(self):
        from models.metron import get_releases

        mock_api = MagicMock()
        mock_api.issues_list.return_value = [MagicMock(), MagicMock()]

        result = get_releases(mock_api, "2024-01-01", "2024-01-07")
        assert len(result) == 2

    def test_no_api(self):
        from models.metron import get_releases
        assert get_releases(None, "2024-01-01") == []

    def test_passes_publisher_filter(self):
        # The releases page filters by publisher server-side (and learns the
        # series->publisher map from the result), so the name must reach Metron.
        from models.metron import get_releases

        mock_api = MagicMock()
        mock_api.issues_list.return_value = []

        get_releases(mock_api, "2024-01-01", "2024-01-07", publisher_name="Marvel")

        params = mock_api.issues_list.call_args[0][0]
        assert params["publisher_name"] == "Marvel"
        assert params["store_date_range_after"] == "2024-01-01"
        assert params["store_date_range_before"] == "2024-01-07"

    def test_omits_publisher_filter_when_absent(self):
        from models.metron import get_releases

        mock_api = MagicMock()
        mock_api.issues_list.return_value = []

        get_releases(mock_api, "2024-01-01", "2024-01-07")

        assert "publisher_name" not in mock_api.issues_list.call_args[0][0]


class TestGetFlaskApi:

    @patch("models.metron.MokkariSession")
    def test_with_explicit_app(self, mock_session_class):
        from models.metron import get_flask_api

        mock_app = MagicMock()
        mock_app.config = {"METRON_API_TOKEN": "tok"}
        mock_session_class.return_value = MagicMock()

        api = get_flask_api(mock_app)
        assert api is not None
        mock_session_class.assert_called_once()

    @patch("models.metron.MokkariSession")
    def test_with_current_app(self, mock_session_class):
        from flask import Flask
        from models.metron import get_flask_api

        test_app = Flask(__name__)
        test_app.config["METRON_API_TOKEN"] = "tok"
        mock_session_class.return_value = MagicMock()

        with test_app.app_context():
            api = get_flask_api()
            assert api is not None

    def test_missing_credentials(self):
        from models.metron import get_flask_api

        mock_app = MagicMock()
        mock_app.config = {"METRON_API_TOKEN": ""}
        assert get_flask_api(mock_app) is None

    def test_whitespace_only_token(self):
        from models.metron import get_flask_api

        mock_app = MagicMock()
        mock_app.config = {"METRON_API_TOKEN": "  "}
        assert get_flask_api(mock_app) is None

    @patch("models.metron.MokkariSession")
    def test_username_and_password_alone_build_no_client(self, mock_session_class):
        """mokkari 5.0 removed basic auth: a pre-upgrade install with only a
        username saved gets no client, and never reaches Session at all."""
        from models.metron import get_flask_api

        mock_app = MagicMock()
        mock_app.config = {
            "METRON_USERNAME": "user",
            "METRON_PASSWORD": "pass",
            "METRON_API_TOKEN": "",
        }
        assert get_flask_api(mock_app) is None
        mock_session_class.assert_not_called()


class TestIsMetronConfigured:

    def test_token_present(self):
        from models.metron import is_metron_configured

        mock_app = MagicMock()
        mock_app.config = {"METRON_API_TOKEN": "tok"}
        assert is_metron_configured(mock_app) is True

    def test_no_credentials(self):
        from models.metron import is_metron_configured

        mock_app = MagicMock()
        mock_app.config = {"METRON_API_TOKEN": ""}
        assert is_metron_configured(mock_app) is False

    def test_username_and_password_no_longer_count(self):
        from models.metron import is_metron_configured

        mock_app = MagicMock()
        mock_app.config = {
            "METRON_USERNAME": "user",
            "METRON_PASSWORD": "pass",
        }
        assert is_metron_configured(mock_app) is False

    def test_whitespace_stripping(self):
        from models.metron import is_metron_configured

        mock_app = MagicMock()
        mock_app.config = {"METRON_API_TOKEN": "  "}
        assert is_metron_configured(mock_app) is False

    def test_with_current_app(self):
        from flask import Flask
        from models.metron import is_metron_configured

        test_app = Flask(__name__)
        test_app.config["METRON_API_TOKEN"] = "tok"

        with test_app.app_context():
            assert is_metron_configured() is True


# ---------------------------------------------------------------------------
# Authentication lockout
#
# A user mistyped their Metron credentials and CLU kept retrying until Metron's
# fail2ban banned their IP. Metron's own API guidance is that only 429 and 5xx
# are worth retrying, so a 401 has to stop everything until a human changes
# something.
# ---------------------------------------------------------------------------

def _http_api_error(status, body=""):
    """An ApiError shaped the way mokkari raises one, with the HTTPError cause."""
    import requests
    from mokkari.exceptions import ApiError

    response = requests.Response()
    response.status_code = status
    response._content = body.encode("utf-8")
    http_error = requests.exceptions.HTTPError(
        f"{status} Client Error", response=response
    )
    error = ApiError(f"HTTP error: {http_error!r} | Response body: {body}")
    error.__cause__ = http_error
    return error


class _PrefStore:
    """A real user_preferences table on disk, so the lockout's own SQL runs."""

    def __init__(self, path):
        self.path = str(path)
        self.unavailable = False
        conn = sqlite3.connect(self.path)
        conn.execute(
            "CREATE TABLE user_preferences ("
            "key TEXT PRIMARY KEY, value TEXT, category TEXT, updated_at TEXT)"
        )
        conn.commit()
        conn.close()

    def connect(self):
        if self.unavailable:
            raise sqlite3.OperationalError("database is locked")
        return sqlite3.connect(self.path)

    def set(self, key, value, category="general"):
        conn = sqlite3.connect(self.path)
        conn.execute(
            "INSERT OR REPLACE INTO user_preferences (key, value, category) "
            "VALUES (?, ?, ?)",
            (key, json.dumps(value), category),
        )
        conn.commit()
        conn.close()
        return True

    def get(self, key):
        conn = sqlite3.connect(self.path)
        row = conn.execute(
            "SELECT value FROM user_preferences WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        return json.loads(row[0]) if row else None


@pytest.fixture
def prefs(tmp_path):
    """Back the lockout with a real, per-test preferences table."""
    import models.metron as metron

    store = _PrefStore(tmp_path / "prefs.db")

    with patch("core.database.get_db_connection", side_effect=store.connect), \
         patch("core.database.set_user_preference", side_effect=store.set), \
         patch("core.database.update_provider_validity"):
        metron._auth_block.forget()
        try:
            yield store
        finally:
            metron._auth_block.clear()
            metron._auth_block.forget()


class TestAuthFailureStatus:
    """The status has to come off ``__cause__``, not out of the message text --
    mokkari only assembles that string for humans."""

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_rejections_are_recognised(self, status):
        from models.metron import auth_failure_status

        assert auth_failure_status(_http_api_error(status)) == status

    @pytest.mark.parametrize("status", [404, 429, 500])
    def test_other_statuses_are_not_auth_failures(self, status):
        from models.metron import auth_failure_status

        assert auth_failure_status(_http_api_error(status)) is None

    def test_api_error_without_a_cause(self):
        from mokkari.exceptions import ApiError
        from models.metron import auth_failure_status

        assert auth_failure_status(ApiError("Not found.")) is None

    def test_unrelated_exception(self):
        from models.metron import auth_failure_status

        assert auth_failure_status(ValueError("nope")) is None

    def test_connection_errors_are_not_auth_failures(self):
        """A flaky network must never be mistaken for a bad password."""
        import requests
        from mokkari.exceptions import ApiError
        from models.metron import auth_failure_status

        error = ApiError("Connection error")
        error.__cause__ = requests.exceptions.ConnectionError("boom")
        assert auth_failure_status(error) is None

    def test_detail_is_lifted_out_of_a_json_body(self):
        from models.metron import _auth_detail

        error = _http_api_error(401, '{"detail": "Invalid token."}')
        assert _auth_detail(error) == "Invalid token."


class TestAuthLockout:

    def test_a_401_stops_every_later_call(self, prefs):
        """The regression test for the bug report: after one rejection, nothing
        else reaches Metron."""
        from models.metron import _api_call, auth_blocked

        first = MagicMock(side_effect=_http_api_error(401, "Invalid username/password."))
        assert _api_call(first, "first call", default="fallback") == "fallback"
        assert auth_blocked() is True

        second = MagicMock(return_value="should not happen")
        assert _api_call(second, "second call", default="fallback") == "fallback"
        second.assert_not_called()

    def test_a_404_does_not_latch(self, prefs):
        from models.metron import _api_call, auth_blocked

        assert _api_call(
            MagicMock(side_effect=_http_api_error(404)), "missing", default=None
        ) is None
        assert auth_blocked() is False

    def test_the_reason_is_kept_for_the_settings_page(self, prefs):
        from models.metron import _api_call, auth_block_state

        _api_call(
            MagicMock(side_effect=_http_api_error(401, '{"detail": "Invalid token."}')),
            "fetching a series",
        )
        state = auth_block_state()
        assert state["blocked"] is True
        assert "401" in state["error"]
        assert "Invalid token." in state["error"]
        assert state["blocked_at"]

    def test_it_is_persisted_so_a_restart_does_not_resume_hammering(self, prefs):
        """The container that crash-loops with bad credentials is exactly the
        case in the bug report, so the flag has to outlive the process."""
        from models.metron import _api_call, auth_blocked, PREF_AUTH_BLOCKED

        _api_call(MagicMock(side_effect=_http_api_error(401)), "fetching a series")
        assert prefs.get(PREF_AUTH_BLOCKED) is True

        # Simulate a restart: the memo is gone, the preference store is not.
        import models.metron as metron
        metron._auth_block.forget()
        assert auth_blocked() is True

    def test_clearing_re_enables_metron(self, prefs):
        from models.metron import _api_call, auth_blocked, clear_auth_block

        _api_call(MagicMock(side_effect=_http_api_error(401)), "fetching a series")
        assert auth_blocked() is True

        clear_auth_block()
        assert auth_blocked() is False

        called = MagicMock(return_value="ok")
        assert _api_call(called, "after the fix") == "ok"
        called.assert_called_once()

    def test_a_rejected_credential_marks_the_provider_invalid(self, prefs):
        from models.metron import _api_call

        with patch("core.database.update_provider_validity") as mark:
            _api_call(MagicMock(side_effect=_http_api_error(401)), "fetching a series")
        mark.assert_called_once_with("metron", False)

    def test_get_api_refuses_to_hand_out_a_client(self, prefs):
        """Several callers fetch a client once and then drive mokkari directly
        in a loop, so withholding the client is the only reliable brake."""
        from models.metron import get_api, latch_auth_failure

        latch_auth_failure(401, "Invalid token.")
        with patch("models.metron.MokkariSession") as session_class:
            assert get_api(api_token="tok") is None
            session_class.assert_not_called()

    def test_the_connection_test_may_still_build_a_client(self, prefs):
        """Otherwise a user who fixed their credentials could never prove it."""
        from models.metron import get_api, latch_auth_failure

        latch_auth_failure(401, "Invalid token.")
        with patch("models.metron.MokkariSession") as session_class:
            session_class.return_value = MagicMock()
            assert get_api(api_token="tok", ignore_auth_lock=True) is not None

    def test_api_call_may_be_told_to_run_while_blocked(self, prefs):
        from models.metron import _api_call, latch_auth_failure

        latch_auth_failure(401, "Invalid token.")
        called = MagicMock(return_value="ok")
        assert _api_call(called, "testing", ignore_auth_lock=True) == "ok"
        called.assert_called_once()

    def test_api_call_can_re_raise_an_auth_error_for_the_test_button(self, prefs):
        from mokkari.exceptions import ApiError
        from models.metron import _api_call

        with pytest.raises(ApiError):
            _api_call(
                MagicMock(side_effect=_http_api_error(401)),
                "testing",
                ignore_auth_lock=True,
                raise_auth_errors=True,
            )

    def test_is_metron_configured_is_false_while_blocked(self, prefs):
        from models.metron import is_metron_configured, latch_auth_failure

        mock_app = MagicMock()
        mock_app.config = {"METRON_API_TOKEN": "tok"}
        assert is_metron_configured(mock_app) is True

        latch_auth_failure(401, "Invalid username/password.")
        assert is_metron_configured(mock_app) is False

    def test_an_unreadable_store_is_not_cached_as_unblocked(self, prefs):
        """get_user_preference swallows its own errors, so a read taken before
        the database is ready looks exactly like "not blocked". Remembering that
        would leave the lockout switched off for the whole process -- which is
        the failure this feature exists to prevent."""
        import models.metron as metron

        prefs.set(metron.PREF_AUTH_BLOCKED, True)
        prefs.unavailable = True
        metron._auth_block.forget()

        assert metron.auth_blocked() is False        # fails open, as it must

        prefs.unavailable = False
        assert metron.auth_blocked() is True         # ...but tries again

    def test_a_corrupt_stored_value_does_not_raise(self, prefs):
        import models.metron as metron

        conn = sqlite3.connect(prefs.path)
        conn.execute(
            "INSERT OR REPLACE INTO user_preferences (key, value) VALUES (?, ?)",
            (metron.PREF_AUTH_BLOCKED, "not json"),
        )
        conn.commit()
        conn.close()
        metron._auth_block.forget()

        assert metron.auth_blocked() is False

    def test_the_daily_quota_latch_is_unaffected(self, prefs):
        """Two independent brakes; clearing one must not clear the other."""
        from models.metron import _metron_pacer, clear_auth_block, latch_auth_failure

        latch_auth_failure(401, "Invalid token.")
        _metron_pacer.latch_daily_limit(3600, "test")
        clear_auth_block()
        assert _metron_pacer.daily_limit_reached() is True


class TestTokenOnlyAuth:
    """mokkari 5.0 removed username/password: ``Session(api_token, *, ...)``.
    Passing ``username=`` is a TypeError inside every Metron call (#673)."""

    @patch("models.metron.MokkariSession")
    def test_token_is_passed_to_mokkari(self, session_class):
        from models.metron import get_api

        session_class.return_value = MagicMock()
        assert get_api(api_token="tok-123") is not None
        assert session_class.call_args.args == ("tok-123",)

    @patch("models.metron.MokkariSession")
    def test_username_and_password_are_never_passed(self, session_class):
        from models.metron import get_api

        session_class.return_value = MagicMock()
        get_api("user", "pass", "tok-123")
        kwargs = session_class.call_args.kwargs
        assert "username" not in kwargs and "passwd" not in kwargs

    @patch("models.metron.MokkariSession")
    def test_basic_auth_alone_builds_no_client(self, session_class):
        from models.metron import get_api

        assert get_api("user", "pass") is None
        session_class.assert_not_called()

    def test_basic_auth_warning_is_logged_once(self, caplog):
        import models.metron as metron

        metron._BASIC_AUTH_WARNED = False
        try:
            with caplog.at_level("WARNING"):
                metron.get_api("user", "pass")
                metron.get_api("user", "pass")
            hits = [r for r in caplog.records if "no longer supported" in r.getMessage()]
            assert len(hits) == 1
        finally:
            metron._BASIC_AUTH_WARNED = False

    @patch("models.metron.MokkariSession")
    def test_get_flask_api_uses_the_token(self, session_class):
        from models.metron import get_flask_api

        session_class.return_value = MagicMock()
        mock_app = MagicMock()
        mock_app.config = {
            "METRON_USERNAME": "user",
            "METRON_PASSWORD": "pass",
            "METRON_API_TOKEN": "  tok-123  ",
        }
        assert get_flask_api(mock_app) is not None
        assert session_class.call_args.args == ("tok-123",)

    def test_no_credentials_at_all(self):
        from models.metron import get_api

        assert get_api("", "", "") is None

    def test_the_real_mokkari_takes_a_token_only(self):
        """Guards the version floor in requirements.txt: CLU builds Session the
        mokkari 5 way, so a 4.x install would fail every call."""
        import inspect as _inspect

        from mokkari.session import Session

        try:
            params = _inspect.signature(Session.__init__).parameters
        except (TypeError, ValueError):
            pytest.skip("mokkari is shimmed in this environment")
        if "api_token" not in params:
            pytest.skip("mokkari is shimmed in this environment")
        assert "username" not in params and "passwd" not in params


class TestFetchArcsPageAuth:
    """fetch_arcs_page builds its own HTTP request to escape mokkari's
    auto-pagination, so it has to carry the auth rules by hand."""

    def test_the_token_is_sent_as_a_bearer_header(self, prefs):
        """mokkari 5 keeps the token out of ``api.header``; without adding it
        here every arc page is a 401, which latches the lockout for all of
        Metron."""
        from models.metron import fetch_arcs_page

        api = SimpleNamespace(api_token="tok-123", header={"User-Agent": "CLU"})
        response = MagicMock(status_code=200)
        response.json.return_value = {"results": [], "next": None, "count": 0}

        with patch("models.metron.requests_lib.get", return_value=response) as get:
            fetch_arcs_page(api, page=1)

        headers = get.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer tok-123"
        assert headers["User-Agent"] == "CLU"
        assert "auth" not in get.call_args.kwargs
        # The session's own header dict is not mutated.
        assert "Authorization" not in api.header

    def test_the_real_session_header_has_no_token(self):
        """Pins the mokkari 5 behaviour the hand-built request compensates for."""
        from mokkari.session import Session

        session = Session("tok-123")
        assert "Authorization" not in session.header
        assert session.api_token == "tok-123"

    def test_a_401_latches_the_lockout_and_stops_retrying(self, prefs):
        from models.metron import auth_blocked, fetch_arcs_page

        api = SimpleNamespace(api_token="tok-123", header={})
        response = MagicMock(status_code=401, text='{"detail": "Invalid token."}')

        with patch("models.metron.requests_lib.get", return_value=response) as get:
            result = fetch_arcs_page(api, page=1)

        assert get.call_count == 1
        assert result["results"] == []
        assert auth_blocked() is True


class TestListReadingListsModifiedSince:
    """One paged call in place of a detail request per imported list.

    The caller acts on the difference between "nothing changed" and "the call
    failed", so the two answers have to stay distinguishable.
    """

    def test_sends_modified_gt(self):
        from models.metron import list_reading_lists_modified_since

        api = MagicMock()
        api.reading_lists_list.return_value = []
        list_reading_lists_modified_since(api, "2026-05-09")

        assert api.reading_lists_list.call_args[0][0] == {"modified_gt": "2026-05-09"}

    def test_returns_id_to_modified(self):
        from models.metron import list_reading_lists_modified_since

        api = MagicMock()
        api.reading_lists_list.return_value = [
            SimpleNamespace(id=10, modified="2026-05-10 08:00:00+00:00"),
            SimpleNamespace(id=11, modified="2026-05-11 08:00:00+00:00"),
        ]
        assert list_reading_lists_modified_since(api, "2026-05-09") == {
            10: "2026-05-10 08:00:00+00:00",
            11: "2026-05-11 08:00:00+00:00",
        }

    def test_empty_is_nothing_changed_not_a_failure(self):
        from models.metron import list_reading_lists_modified_since

        api = MagicMock()
        api.reading_lists_list.return_value = []
        assert list_reading_lists_modified_since(api, "2026-05-09") == {}

    def test_none_when_the_call_fails(self):
        """An ApiError comes back through _api_call as the default, and the
        default here is None -- a sweep must not read that as evidence."""
        from models.metron import list_reading_lists_modified_since

        api = MagicMock()
        api.reading_lists_list.side_effect = ApiError("nope")
        assert list_reading_lists_modified_since(api, "2026-05-09") is None

    def test_none_without_an_api_or_a_date(self):
        from models.metron import list_reading_lists_modified_since

        assert list_reading_lists_modified_since(None, "2026-05-09") is None
        assert list_reading_lists_modified_since(MagicMock(), "") is None


class TestSearchSeriesListSeparators(TestSearchSeriesList):
    """A filename's " - " must still find Metron's "Title: Subtitle" series."""

    def test_dash_finds_colon_title(self):
        from models.metron import search_series_list

        target = self._make(id=16921, name="Nova: Return of the Rings", year_began=2026)
        mock_api = MagicMock()
        mock_api.series_list.side_effect = lambda params: (
            [target] if params["name"] == "Nova: Return of the Rings" else []
        )

        results = search_series_list(mock_api, "Nova - Return of the Rings", 2026)
        assert [r["id"] for r in results] == [16921]
