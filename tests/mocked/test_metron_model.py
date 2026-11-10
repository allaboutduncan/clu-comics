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


def _client(library):
    """A real ``MetronClient`` over ``library``, so the call policy runs."""
    from models.metron import MetronClient

    return MetronClient(library)


def _call(fn, context="test context", default=None, **flags):
    """Run ``fn`` as the body of one guarded ``MetronClient`` method call."""
    library = MagicMock()
    library.series.side_effect = lambda *a, **kw: fn()
    return _client(library).using(context=context, default=default, **flags).series()


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

        assert purge_series_cache(8859, api=_client(session)) == 2
        assert cache.get(self.SERIES_URL) is None
        assert cache.get(self.ISSUES_URL) is None

    def test_leaves_other_series_detail_alone(self, tmp_path):
        from models.metron import purge_series_cache

        session, cache = _cache_session(tmp_path)
        other_detail = "https://metron.cloud/api/series/88591/"
        _store(cache, self.SERIES_URL, {"desc": "old summary"}, "series")
        _store(cache, other_detail, {"desc": "different series"}, "series")

        assert purge_series_cache(8859, api=_client(session)) == 1
        assert cache.get(other_detail) == {"desc": "different series"}

    def test_next_fetch_sees_the_new_body(self, tmp_path):
        from models.metron import purge_series_cache

        session, cache = _cache_session(tmp_path)
        _store(cache, self.SERIES_URL, {"desc": "old summary"}, "series")
        purge_series_cache(8859, api=_client(session))
        _store(cache, self.SERIES_URL, {"desc": "new summary"}, "series")

        assert cache.get(self.SERIES_URL) == {"desc": "new summary"}

    def test_session_without_a_cache_is_a_no_op(self):
        from models.metron import purge_series_cache

        assert purge_series_cache(8859, api=_client(SimpleNamespace(cache=None))) == 0

    def test_non_numeric_series_id_is_a_no_op(self, tmp_path):
        from models.metron import purge_series_cache

        session, cache = _cache_session(tmp_path)
        _store(cache, self.SERIES_URL, {"desc": "old summary"}, "series")

        assert purge_series_cache("not-an-id", api=_client(session)) == 0
        assert cache.get(self.SERIES_URL) == {"desc": "old summary"}

    def test_a_failing_cache_never_raises(self):
        from models.metron import purge_series_cache

        cache = MagicMock()
        cache.delete.side_effect = sqlite3.OperationalError("database is locked")
        session = SimpleNamespace(cache=cache, api_url="https://metron.cloud/api/{}/")

        with patch("core.database.get_issues_for_series", return_value=[]):
            assert purge_series_cache(8859, api=_client(session)) == 0

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

        assert purge_issue_cache(172615, api=_client(session)) == 1
        assert cache.get(self.ISSUE_URL) is None

    def test_leaves_other_issues_alone(self, tmp_path):
        from models.metron import purge_issue_cache

        session, cache = _cache_session(tmp_path)
        other_issue = "https://metron.cloud/api/issue/1726150/"
        filtered = "https://metron.cloud/api/credit/?issue_id=172615"
        _store(cache, self.ISSUE_URL, {"credits": []}, "issue")
        _store(cache, other_issue, {"credits": ["someone"]}, "issue")
        _store(cache, filtered, {"results": []}, "credit", "list")

        assert purge_issue_cache(172615, api=_client(session)) == 1
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

        assert purge_issue_cache(172615, api=_client(real)) == 1

    def test_session_without_a_cache_is_a_no_op(self):
        from models.metron import purge_issue_cache

        assert purge_issue_cache(172615, api=_client(SimpleNamespace(cache=None))) == 0

    def test_non_numeric_issue_id_is_a_no_op(self, tmp_path):
        from models.metron import purge_issue_cache

        session, cache = _cache_session(tmp_path)
        _store(cache, self.ISSUE_URL, {"credits": []}, "issue")

        assert purge_issue_cache("not-an-id", api=_client(session)) == 0
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
            assert purge_series_cache(8859, api=_client(session)) == 2

        assert cache.get(self.ISSUE_URL) is None

    def test_series_purge_survives_a_database_failure(self, tmp_path):
        from models.metron import purge_series_cache

        session, cache = _cache_session(tmp_path)
        _store(cache, "https://metron.cloud/api/series/8859/", {"desc": "old"}, "series")

        with patch("core.database.get_issues_for_series", side_effect=RuntimeError("db down")):
            assert purge_series_cache(8859, api=_client(session)) == 1


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

        result = fetch_issue_detail(_client(api), 172615)

        assert cache.get(self.ISSUE_URL) is None
        api.issue.assert_called_once_with(172615)
        assert result.id == 172615

    def test_non_numeric_id_never_calls_the_api(self, tmp_path):
        from models.metron import fetch_issue_detail

        api, _ = self._session_with_cache(tmp_path)
        assert fetch_issue_detail(_client(api), "not-an-id") is None
        api.issue.assert_not_called()

    def test_get_issue_metadata_uses_the_purging_fetch(self, tmp_path):
        """The double fetch's second call is the one written to disk."""
        from models.metron import get_issue_metadata

        api, cache = self._session_with_cache(tmp_path)
        _store(cache, self.ISSUE_URL, {"credits": [], "number": "3"}, "issue")
        api.issues_list.return_value = [SimpleNamespace(id=172615)]
        api.issue.return_value = {"number": "3", "credits": [{"creator": "Bengal"}]}

        result = get_issue_metadata(_client(api), 15845, "3")

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

        changed = list_issues_modified_since(_client(api), "2026-08-25")

        api.issues_list.assert_called_once_with({"modified_gt": "2026-08-25"})
        assert changed == {
            172615: "2026-08-26T08:35:11",
            999: "2026-08-26T09:00:00",
        }

    def test_no_api_or_no_date_never_calls_out(self):
        from models.metron import list_issues_modified_since

        api = MagicMock()
        assert list_issues_modified_since(None, "2026-08-25") == {}
        assert list_issues_modified_since(_client(api), "") == {}
        api.issues_list.assert_not_called()

    def test_an_api_error_is_an_empty_result(self):
        """The credit-less pass still runs, so a failed feed degrades rather
        than taking the sweep down."""
        from mokkari.exceptions import ApiError

        from models.metron import list_issues_modified_since

        api = MagicMock()
        api.issues_list.side_effect = ApiError("boom")

        assert list_issues_modified_since(_client(api), "2026-08-25") == {}

    def test_entries_without_an_id_are_dropped(self):
        from models.metron import list_issues_modified_since

        api = MagicMock()
        api.issues_list.return_value = [
            SimpleNamespace(id=None, modified="x"),
            SimpleNamespace(id=7, modified=None),
        ]

        assert list_issues_modified_since(_client(api), "2026-08-25") == {7: ""}


def _page(results):
    return {"count": len(results), "next": None, "previous": None, "results": results}


class TestChangedSinceIsNeverServedFromCache:
    """mokkari 5 honours its TTL but has no per-call bypass, so a call asking
    "what changed?" must drop its own cached answer first. Driven through a real
    Session so the keys are mokkari's, not a copy of its key-building."""

    def _session(self, tmp_path):
        from mokkari.session import Session

        _, cache = _cache_session(tmp_path)
        return Session("tok", cache=cache), cache

    def test_same_day_rerun_reaches_metron_again(self, tmp_path):
        from mokkari.session import Session

        from models.metron import list_issues_modified_since

        real, cache = self._session(tmp_path)
        bodies = [_page([{"id": 1, "modified": "a"}]),
                  _page([{"id": 1, "modified": "a"}, {"id": 2, "modified": "b"}])]
        with patch.object(Session, "_request_data", side_effect=bodies) as http, \
             patch.object(Session, "issues_list",
                          lambda self, p: self._get_results(["issue"], p)["results"]):
            assert list_issues_modified_since(_client(real), "2026-10-25") == {1: "a"}
            assert list_issues_modified_since(_client(real), "2026-10-25") == {1: "a", 2: "b"}
        assert http.call_count == 2

    def test_leaves_other_issue_lists_cached(self, tmp_path):
        from mokkari.session import Session

        from models.metron import purge_modified_since_cache

        real, cache = self._session(tmp_path)
        with patch.object(Session, "_request_data", return_value=_page([{"id": 1}])):
            real._get_results(["issue"], {"modified_gt": "2026-10-25"})
            real._get_results(["issue"], {"series_id": 8859})

        assert purge_modified_since_cache("issue", "2026-10-25", api=_client(real)) == 1
        assert cache.get(real._url(["issue"]) + "?series_id=8859") is not None

    def test_reading_list_purge_reaches_per_user_keys(self, tmp_path):
        """reading_list keys carry a hash of the token, so the bare URL a
        ``delete()`` would need never matches -- the purge has to invalidate."""
        from mokkari.session import Session

        from models.metron import purge_reading_list_cache

        real, cache = self._session(tmp_path)
        with patch.object(Session, "_request_data",
                          side_effect=[{"id": 3, "modified": "old"}, _page([{"id": 9}])]):
            real._get(["reading_list", 3])
            real._get_results(["reading_list", 3, "items"])

        assert cache.delete(real._url(["reading_list", 3])) is False
        assert purge_reading_list_cache(api=_client(real)) == 2

        with patch.object(Session, "_request_data",
                          return_value={"id": 3, "modified": "new"}) as http:
            assert real._get(["reading_list", 3])["modified"] == "new"
        http.assert_called_once()

    def test_reading_list_sweep_purges_before_listing(self):
        from models.metron import list_reading_lists_modified_since

        session = MagicMock()
        session.reading_lists_list.return_value = []
        api = _client(session)
        with patch("models.metron.purge_reading_list_cache") as purge:
            purge.side_effect = lambda **kw: session.reading_lists_list.assert_not_called()
            assert list_reading_lists_modified_since(api, "2026-10-25") == {}
        purge.assert_called_once_with(api=api)

    def test_arc_purge_drops_only_that_arc(self, tmp_path):
        from mokkari.session import Session

        from models.metron import purge_arc_issues_cache

        real, cache = self._session(tmp_path)
        with patch.object(Session, "_request_data", return_value=_page([{"id": 1}])):
            real._get_results(["arc", 7, "issue_list"], resource="issue")
            real._get_results(["arc", 70, "issue_list"], resource="issue")

        assert purge_arc_issues_cache(7, api=_client(real)) == 1
        assert cache.get(real._url(["arc", 7, "issue_list"])) is None
        assert cache.get(real._url(["arc", 70, "issue_list"])) is not None
        assert purge_arc_issues_cache("nope", api=_client(real)) == 0


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

        assert get_series_id_by_comicvine_id(_client(mock_api), 12345) == 42

    def test_not_found(self):
        from models.metron import get_series_id_by_comicvine_id

        mock_api = MagicMock()
        mock_api.series_list.return_value = []

        assert get_series_id_by_comicvine_id(_client(mock_api), 99999) is None


class TestSearchSeriesByName:

    def test_returns_best_match(self):
        from models.metron import search_series_by_name

        mock_api = MagicMock()
        s = make_mock_series(id=100, name="Batman", year_began=2016)
        mock_api.series_list.return_value = [s]

        result = search_series_by_name(_client(mock_api), "Batman")
        assert result is not None
        assert result["id"] == 100
        assert result["name"] == "Batman"

    def test_year_ranking(self):
        from models.metron import search_series_by_name

        mock_api = MagicMock()
        s1 = make_mock_series(id=1, name="Batman", year_began=1940)
        s2 = make_mock_series(id=2, name="Batman", year_began=2016)
        mock_api.series_list.return_value = [s1, s2]

        result = search_series_by_name(_client(mock_api), "Batman", year=2016)
        assert result["id"] == 2  # Closer to 2016

    def test_no_results(self):
        from models.metron import search_series_by_name

        mock_api = MagicMock()
        mock_api.series_list.return_value = []

        assert search_series_by_name(_client(mock_api), "Nonexistent") is None

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

        results = search_series_list(_client(mock_api), "Batman")
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

        results = search_series_list(_client(mock_api), "Batman", year=2016)
        assert results[0]["id"] == 2  # closest year first

    def test_no_results(self):
        from models.metron import search_series_list

        mock_api = MagicMock()
        mock_api.series_list.return_value = []
        assert search_series_list(_client(mock_api), "Nonexistent") == []

    def test_no_api(self):
        from models.metron import search_series_list
        assert search_series_list(None, "Batman") == []


class TestGetSeriesDetails:

    def test_returns_details(self):
        from models.metron import get_series_details

        mock_api = MagicMock()
        mock_api.series.return_value = make_mock_series(id=100, cv_id=12345)

        result = get_series_details(_client(mock_api), 100)
        assert result["id"] == 100
        assert result["cv_id"] == 12345

    def test_not_found(self):
        from models.metron import get_series_details

        mock_api = MagicMock()
        mock_api.series.return_value = None

        assert get_series_details(_client(mock_api), 9999) is None


class TestGetSeries:

    def test_returns_full_model(self):
        from models.metron import get_series

        mock_api = MagicMock()
        model = make_mock_series(id=100, cv_id=12345)
        mock_api.series.return_value = model

        assert get_series(_client(mock_api), 100) is model
        mock_api.series.assert_called_once_with(100)

    def test_missing_api_or_id(self):
        from models.metron import get_series

        assert get_series(None, 100) is None
        assert get_series(MagicMock(), None) is None

    def test_waits_and_retries_on_rate_limit(self):
        # A transient RateLimitError must be retried by the policy, not raised,
        # so bulk callers don't error out mid-scan.
        from models.metron import get_series
        from mokkari.exceptions import RateLimitError

        mock_api = MagicMock()
        model = make_mock_series(id=100)
        mock_api.series.side_effect = [RateLimitError("rate limited", retry_after=0), model]

        with patch("models.metron.time.sleep"):
            result = get_series(_client(mock_api), 100)
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

        result = get_issue_metadata(_client(mock_api), 100, "1")
        assert result is not None
        mock_api.issues_list.assert_called_once()
        mock_api.issue.assert_called_once_with(500)

    def test_issue_not_found(self):
        from models.metron import get_issue_metadata

        mock_api = MagicMock()
        mock_api.issues_list.return_value = []

        assert get_issue_metadata(_client(mock_api), 100, "999") is None


class TestGetAllIssuesForSeries:

    def test_returns_issues(self):
        from models.metron import get_all_issues_for_series

        mock_api = MagicMock()
        mock_api.issues_list.return_value = [MagicMock(id=1), MagicMock(id=2)]

        result = get_all_issues_for_series(_client(mock_api), 100)
        assert len(result) == 2

    def test_empty_series(self):
        from models.metron import get_all_issues_for_series

        mock_api = MagicMock()
        mock_api.issues_list.return_value = []

        assert get_all_issues_for_series(_client(mock_api), 100) == []


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


def _window(limit=None, remaining=None, reset=None):
    return SimpleNamespace(limit=limit, remaining=remaining, reset=reset)


def _status(burst=None, sustained=None):
    return SimpleNamespace(
        burst=burst or _window(), sustained=sustained or _window()
    )


_PUBLISHER_URL = "https://metron.cloud/api/publisher/"


def _http_response(status, body=None, headers=None):
    import requests

    response = requests.Response()
    response.status_code = status
    response._content = json.dumps(body if body is not None else {}).encode()
    response.headers.update(headers or {})
    response.url = _PUBLISHER_URL
    return response


def _publisher_page(n, next_page=None):
    return {
        "count": 3,
        "next": f"{_PUBLISHER_URL}?page={next_page}" if next_page else None,
        "previous": None,
        "results": [{"id": n, "name": f"Pub {n}", "modified": "2024-01-01T00:00:00Z"}],
    }


def _counting_limiter(burst_period=0.06):
    """The real limiter on a tiny window, counting the sends it gates."""
    from models.metron import _CluRateLimiter

    class Counting(_CluRateLimiter):
        def __init__(self):
            super().__init__(burst_period=burst_period)
            self.acquired = 0

        def acquire(self, status):
            self.acquired += 1
            super().acquire(status)

    return Counting()


class TestPerSendPacing:
    """#703: CLU took one slot per guarded call while mokkari followed every
    ``next`` link inside it, so one list call sent dozens of unpaced pages.
    Pacing now lives on the session and runs once per HTTP send."""

    def _real_session(self, limiter):
        from mokkari.session import Session

        try:
            return Session("tok", rate_limiter=limiter)
        except TypeError:
            pytest.skip("mokkari is shimmed in this environment")

    def test_every_page_of_a_list_takes_a_slot(self):
        import requests

        limiter = _counting_limiter()
        session = self._real_session(limiter)
        pages = [
            _http_response(200, _publisher_page(1, 2)),
            _http_response(200, _publisher_page(2, 3)),
            _http_response(200, _publisher_page(3)),
        ]

        with patch.object(requests.Session, "request", side_effect=pages) as send:
            result = _call(lambda: session.publishers_list({}), "all publishers", default=[])

        assert [p.id for p in result] == [1, 2, 3]
        assert send.call_count == 3
        assert limiter.acquired == 3  # not 1

    def test_a_429_on_one_page_retries_that_page_not_the_whole_call(self):
        """The two retry layers used to stack: mokkari retried the page, then
        ``_handle_rate_limit`` slept and re-ran the list from page 1."""
        import requests

        limiter = _counting_limiter()
        session = self._real_session(limiter)
        pages = [
            _http_response(200, _publisher_page(1, 2)),
            _http_response(429, {"detail": "slow down"}, {"Retry-After": "0"}),
            _http_response(200, _publisher_page(2)),
        ]
        fn = MagicMock(side_effect=lambda: session.publishers_list({}))

        with patch.object(requests.Session, "request", side_effect=pages) as send, \
                patch("models.metron.time.sleep") as sleep:
            result = _call(fn, "all publishers", default=[])

        assert [p.id for p in result] == [1, 2]
        assert fn.call_count == 1
        assert send.call_count == 3
        sleep.assert_not_called()

    def test_the_seed_spaces_sends_before_metron_reports_a_limit(self):
        """mokkari's limiter learns the burst limit from the first response and
        spaces nothing until then, so threads starting together all went out
        at once."""
        from mokkari.rate_limit import RateLimitStatus
        from models.metron import _CluRateLimiter

        limiter = _CluRateLimiter(burst_period=0.4)  # seeded 20 -> 20ms apart
        status = RateLimitStatus()
        start = time.monotonic()
        for _ in range(3):
            limiter.acquire(status)
            limiter.release(status)
        assert time.monotonic() - start >= 0.035

    def test_a_reported_limit_replaces_the_seed(self):
        from mokkari.rate_limit import RateLimitStatus, RateLimitWindow
        from models.metron import _CluRateLimiter

        limiter = _CluRateLimiter(burst_period=0.2)
        status = RateLimitStatus(burst=RateLimitWindow(limit=2, remaining=2))
        start = time.monotonic()
        limiter.acquire(status)
        limiter.release(status)
        limiter.acquire(status)  # 0.2 / 2 = 100ms after the first
        limiter.release(status)
        assert time.monotonic() - start >= 0.09

    def test_a_slot_is_held_past_metrons_window(self):
        """Metron frees a slot 60s after a request arrives, mokkari 60s after
        it was sent. Without slack a busy client reuses each slot a moment
        early and draws about one 429 per window."""
        from mokkari.rate_limit import DEFAULT_BURST_PERIOD
        from models.metron import _BURST_SLACK_S, _CluRateLimiter

        assert _BURST_SLACK_S > 0
        limiter = _CluRateLimiter()
        assert limiter._burst.period == DEFAULT_BURST_PERIOD + _BURST_SLACK_S

        # Window full of sends at t=0: nothing fits until the slack has passed.
        limiter._burst.limit = 2
        limiter._burst.record(0.0)
        limiter._burst.record(0.0)
        assert limiter._burst.wait_seconds(DEFAULT_BURST_PERIOD) == pytest.approx(_BURST_SLACK_S)
        assert limiter._burst.wait_seconds(DEFAULT_BURST_PERIOD + _BURST_SLACK_S) == 0.0

    def test_the_seed_is_the_limit_settings_shows(self):
        from models.metron import _DEFAULT_BURST_LIMIT
        from models.providers.metron_provider import MetronProvider

        assert _DEFAULT_BURST_LIMIT == MetronProvider.rate_limit

    @patch("models.metron.MokkariSession")
    def test_each_token_gets_its_own_limiter(self, session_class):
        """mokkari: construct one limiter per Session. One cached Session per
        token is then one limiter per Metron account."""
        from models.metron import _CluRateLimiter, get_api

        session_class.side_effect = lambda *a, **kw: MagicMock()
        get_api(api_token="a")
        get_api(api_token="a")
        get_api(api_token="b")

        assert session_class.call_count == 2
        limiters = [c.kwargs["rate_limiter"] for c in session_class.call_args_list]
        assert all(isinstance(lim, _CluRateLimiter) for lim in limiters)
        assert limiters[0] is not limiters[1]


class TestRateLimitClassification:

    def test_a_refusal_from_the_limiter_latches_the_daily_window(self):
        """mokkari raises the base RateLimitError only for a request it never
        sent: its limiter refusing because the daily window is spent."""
        from models.metron import daily_limit_reached

        fn = MagicMock(side_effect=RateLimitError("daily limit", retry_after=30))
        with patch("models.metron.time.sleep") as sleep:
            assert _call(fn, "test context", default="fallback") == "fallback"
        assert fn.call_count == 1
        sleep.assert_not_called()
        assert daily_limit_reached() is True

        later = MagicMock(return_value="ok")
        assert _call(later, "later", default="fallback") == "fallback"
        later.assert_not_called()

    def test_a_429_from_metron_retries_without_sleeping(self):
        """The session's limiter already blocks every thread for Retry-After;
        sleeping in CLU too is how the retry layers stacked."""
        from models.metron import daily_limit_reached

        class Server429(RateLimitError):
            """Stands in for mokkari's private ``_ServerRateLimitError``."""

        fn = MagicMock(side_effect=[Server429("429", retry_after=10), "ok"])
        with patch("models.metron.time.sleep") as sleep:
            assert _call(fn, "test context") == "ok"
        assert fn.call_count == 2
        sleep.assert_not_called()
        assert daily_limit_reached() is False

    def test_a_429_that_keeps_coming_gives_up_after_three_attempts(self):

        class Server429(RateLimitError):
            pass

        fn = MagicMock(side_effect=Server429("429", retry_after=10))
        with patch("models.metron.time.sleep"):
            assert _call(fn, "test context", default="fallback") == "fallback"
        assert fn.call_count == 3


class TestDailyQuota:
    """The burst window is the session limiter's job; the daily window is the
    one CLU still latches, because waiting it out is hours."""

    def test_missing_headers_do_not_latch(self):
        """Every field is None until the first response completes; a TypeError
        here would kill a daemon thread."""
        from models.metron import _observe_sustained, daily_limit_reached

        session = MagicMock()
        session.rate_limit_status = _status()

        _observe_sustained(session)  # must not raise

        assert daily_limit_reached() is False

    def test_session_without_rate_limit_status_is_ignored(self):
        from models.metron import _observe_sustained

        session = MagicMock()
        session.rate_limit_status = None

        _observe_sustained(session)  # must not raise

    def test_naive_reset_datetime_does_not_explode(self):
        """mokkari returns tz-aware datetimes; comparing one against a naive
        now() raises. Guard both ways."""
        from models.metron import _observe_sustained

        session = MagicMock()
        session.rate_limit_status = _status(
            sustained=_window(
                limit=100, remaining=0, reset=datetime.now() + timedelta(hours=2)
            )
        )

        _observe_sustained(session)  # must not raise

    def test_exhausted_daily_quota_skips_the_call_without_sleeping(self):
        """The regression test for the reported log: a spent daily quota was
        surfacing as a 59.5s wait retried 3x per series, for every series in a
        sweep. It must now short-circuit."""
        from models.metron import _observe_sustained

        session = MagicMock()
        session.rate_limit_status = _status(
            sustained=_window(
                limit=100,
                remaining=0,
                reset=datetime.now(timezone.utc) + timedelta(hours=2),
            )
        )
        _observe_sustained(session)

        called = {"n": 0}

        def fn():
            called["n"] += 1
            return "ok"

        with patch("models.metron.time.sleep") as mock_sleep:
            result = _call(fn, "test context", default="fallback")

        assert result == "fallback"
        assert called["n"] == 0
        mock_sleep.assert_not_called()

    def test_calls_resume_once_the_daily_window_has_reset(self):
        from models.metron import _observe_sustained, daily_limit_reached

        session = MagicMock()
        session.rate_limit_status = _status(
            sustained=_window(
                limit=100,
                remaining=0,
                reset=datetime.now(timezone.utc) + timedelta(milliseconds=50),
            )
        )
        _observe_sustained(session)
        assert daily_limit_reached() is True

        time.sleep(0.1)

        assert daily_limit_reached() is False
        assert _call(lambda: "ok", "test context") == "ok"

    def test_sub_threshold_retry_after_gives_up_on_the_first_attempt(self):
        """mokkari reports retry_after as max(burst_wait, sustained_wait), so a
        spent daily quota can arrive as 59.5s -- just under the old 60s 'this is
        the daily cap' threshold. The headers say which window it really is."""
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
            metron._SESSION_CACHE[("u", "p")] = _client(session)

        calls = {"n": 0}

        def fn():
            calls["n"] += 1
            raise RateLimitError("slow down", retry_after=59.5)

        with patch("models.metron.time.sleep") as mock_sleep:
            result = _call(fn, "test context", default="fallback")

        assert result == "fallback"
        assert calls["n"] == 1  # not 3
        mock_sleep.assert_not_called()

    def test_invalidate_clears_the_daily_latch(self):
        """A wrongly-latched window would silently disable Metron for hours."""
        from models.metron import (
            _observe_sustained,
            daily_limit_reached,
            invalidate_session_cache,
        )

        session = MagicMock()
        session.rate_limit_status = _status(
            sustained=_window(
                limit=100,
                remaining=0,
                reset=datetime.now(timezone.utc) + timedelta(hours=2),
            )
        )
        _observe_sustained(session)
        assert daily_limit_reached() is True

        invalidate_session_cache()

        assert daily_limit_reached() is False

    def test_successful_call_observes_the_headers(self):
        from models.metron import daily_limit_reached
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
            metron._SESSION_CACHE[("u", "p")] = _client(session)

        assert _call(lambda: "ok", "test context") == "ok"

        assert daily_limit_reached() is True

    def test_paused_reason_names_the_daily_limit(self):
        import models.metron as metron

        assert metron.paused_reason() is None
        metron._daily.latch_for(3600, "test")
        assert "daily request limit" in metron.paused_reason()


class TestGetReleases:

    def test_fetches_releases(self):
        from models.metron import get_releases

        mock_api = MagicMock()
        mock_api.issues_list.return_value = [MagicMock(), MagicMock()]

        result = get_releases(_client(mock_api), "2024-01-01", "2024-01-07")
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

        get_releases(_client(mock_api), "2024-01-01", "2024-01-07", publisher_name="Marvel")

        params = mock_api.issues_list.call_args[0][0]
        assert params["publisher_name"] == "Marvel"
        assert params["store_date_range_after"] == "2024-01-01"
        assert params["store_date_range_before"] == "2024-01-07"

    def test_omits_publisher_filter_when_absent(self):
        from models.metron import get_releases

        mock_api = MagicMock()
        mock_api.issues_list.return_value = []

        get_releases(_client(mock_api), "2024-01-01", "2024-01-07")

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
        from models.metron import auth_blocked

        first = MagicMock(side_effect=_http_api_error(401, "Invalid username/password."))
        assert _call(first, "first call", default="fallback") == "fallback"
        assert auth_blocked() is True

        second = MagicMock(return_value="should not happen")
        assert _call(second, "second call", default="fallback") == "fallback"
        second.assert_not_called()

    def test_a_404_does_not_latch(self, prefs):
        from models.metron import auth_blocked

        assert _call(
            MagicMock(side_effect=_http_api_error(404)), "missing", default=None
        ) is None
        assert auth_blocked() is False

    def test_the_reason_is_kept_for_the_settings_page(self, prefs):
        from models.metron import auth_block_state

        _call(
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
        from models.metron import auth_blocked, PREF_AUTH_BLOCKED

        _call(MagicMock(side_effect=_http_api_error(401)), "fetching a series")
        assert prefs.get(PREF_AUTH_BLOCKED) is True

        # Simulate a restart: the memo is gone, the preference store is not.
        import models.metron as metron
        metron._auth_block.forget()
        assert auth_blocked() is True

    def test_clearing_re_enables_metron(self, prefs):
        from models.metron import auth_blocked, clear_auth_block

        _call(MagicMock(side_effect=_http_api_error(401)), "fetching a series")
        assert auth_blocked() is True

        clear_auth_block()
        assert auth_blocked() is False

        called = MagicMock(return_value="ok")
        assert _call(called, "after the fix") == "ok"
        called.assert_called_once()

    def test_a_rejected_credential_marks_the_provider_invalid(self, prefs):

        with patch("core.database.update_provider_validity") as mark:
            _call(MagicMock(side_effect=_http_api_error(401)), "fetching a series")
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

    def test_a_call_may_be_told_to_run_while_blocked(self, prefs):
        from models.metron import latch_auth_failure

        latch_auth_failure(401, "Invalid token.")
        called = MagicMock(return_value="ok")
        assert _call(called, "testing", ignore_auth_lock=True) == "ok"
        called.assert_called_once()

    def test_a_call_can_re_raise_an_auth_error_for_the_test_button(self, prefs):
        from mokkari.exceptions import ApiError

        with pytest.raises(ApiError):
            _call(
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
        import models.metron as metron
        from models.metron import clear_auth_block, daily_limit_reached, latch_auth_failure

        latch_auth_failure(401, "Invalid token.")
        metron._daily.latch_for(3600, "test")
        clear_auth_block()
        assert daily_limit_reached() is True

    def test_paused_reason_names_the_rejected_credentials(self, prefs):
        from models.metron import latch_auth_failure, paused_reason

        latch_auth_failure(401, "Invalid token.")
        assert paused_reason() == "Metron rejected the saved credentials"

    def test_raise_api_errors_re_raises_a_connection_error(self, prefs):
        """Routes keep their own "Metron is unavailable" 503."""
        import requests
        from mokkari.exceptions import ApiError
        from models.metron import auth_blocked

        error = ApiError("Connection error")
        error.__cause__ = requests.exceptions.ConnectionError("boom")
        with pytest.raises(ApiError):
            _call(MagicMock(side_effect=error), "testing", raise_api_errors=True)
        assert auth_blocked() is False

    def test_raise_api_errors_still_latches_a_rejected_credential(self, prefs):
        from models.metron import auth_blocked

        result = _call(
            MagicMock(side_effect=_http_api_error(401)),
            "testing",
            default="fallback",
            raise_api_errors=True,
        )
        assert result == "fallback"
        assert auth_blocked() is True


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


class TestArcsPageAuth:
    """MetronClient.arcs_page builds its own HTTP request to escape mokkari's
    auto-pagination, so it has to carry the auth rules by hand."""

    def test_the_token_is_sent_as_a_bearer_header(self, prefs):
        """mokkari 5 keeps the token out of ``api.header``; without adding it
        here every arc page is a 401, which latches the lockout for all of
        Metron."""

        api = SimpleNamespace(api_token="tok-123", header={"User-Agent": "CLU"})
        response = MagicMock(status_code=200)
        response.json.return_value = {"results": [], "next": None, "count": 0}

        with patch("models.metron.requests_lib.get", return_value=response) as get:
            _client(api).arcs_page(page=1)

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
        from models.metron import auth_blocked

        api = SimpleNamespace(api_token="tok-123", header={})
        response = MagicMock(status_code=401, text='{"detail": "Invalid token."}')

        with patch("models.metron.requests_lib.get", return_value=response) as get:
            result = _client(api).arcs_page(page=1)

        assert get.call_count == 1
        assert result["results"] == []
        assert auth_blocked() is True


class TestArcsPagePacing:
    """The hand-built arc request never passes through mokkari's send, so it
    takes the session's limiter itself -- the same one every other Metron
    request takes."""

    def _api(self):
        return SimpleNamespace(
            api_token="tok", header={}, rate_limiter=MagicMock(), rate_limit_status="S"
        )

    def _ok(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"results": [{"id": 1}], "next": None, "count": 1}
        return response

    def test_takes_and_releases_the_session_limiter(self):

        api = self._api()
        with patch("models.metron.requests_lib.get", return_value=self._ok()):
            result = _client(api).arcs_page(page=1)

        assert result["results"] == [{"id": 1}]
        api.rate_limiter.acquire.assert_called_once_with("S")
        api.rate_limiter.release.assert_called_once()

    def test_a_429_backs_off_every_sender_and_retries_without_sleeping(self):

        api = self._api()
        rejected = MagicMock(status_code=429, headers={"Retry-After": "5"})
        with patch("models.metron.requests_lib.get", side_effect=[rejected, self._ok()]) as get, \
                patch("models.metron.time.sleep") as sleep:
            result = _client(api).arcs_page(page=1)

        assert result["results"] == [{"id": 1}]
        assert get.call_count == 2
        api.rate_limiter.on_rate_limited.assert_called_once_with(5.0)
        assert api.rate_limiter.release.call_count == 2
        sleep.assert_not_called()

    def test_a_refusal_from_the_limiter_latches_the_daily_window(self):
        from models.metron import daily_limit_reached

        api = self._api()
        api.rate_limiter.acquire.side_effect = RateLimitError("daily", retry_after=3600)
        with patch("models.metron.requests_lib.get") as get:
            result = _client(api).arcs_page(page=1)

        get.assert_not_called()
        assert result["results"] == []
        assert daily_limit_reached() is True

    def test_a_daily_429_latches_instead_of_retrying(self):
        from models.metron import daily_limit_reached

        api = self._api()
        rejected = MagicMock(status_code=429, headers={"Retry-After": "7200"})
        with patch("models.metron.requests_lib.get", return_value=rejected) as get:
            _client(api).arcs_page(page=1)

        assert get.call_count == 1
        assert daily_limit_reached() is True


class TestListReadingListsModifiedSince:
    """One paged call in place of a detail request per imported list.

    The caller acts on the difference between "nothing changed" and "the call
    failed", so the two answers have to stay distinguishable.
    """

    def test_sends_modified_gt(self):
        from models.metron import list_reading_lists_modified_since

        api = MagicMock()
        api.reading_lists_list.return_value = []
        list_reading_lists_modified_since(_client(api), "2026-05-09")

        assert api.reading_lists_list.call_args[0][0] == {"modified_gt": "2026-05-09"}

    def test_returns_id_to_modified(self):
        from models.metron import list_reading_lists_modified_since

        api = MagicMock()
        api.reading_lists_list.return_value = [
            SimpleNamespace(id=10, modified="2026-05-10 08:00:00+00:00"),
            SimpleNamespace(id=11, modified="2026-05-11 08:00:00+00:00"),
        ]
        assert list_reading_lists_modified_since(_client(api), "2026-05-09") == {
            10: "2026-05-10 08:00:00+00:00",
            11: "2026-05-11 08:00:00+00:00",
        }

    def test_empty_is_nothing_changed_not_a_failure(self):
        from models.metron import list_reading_lists_modified_since

        api = MagicMock()
        api.reading_lists_list.return_value = []
        assert list_reading_lists_modified_since(_client(api), "2026-05-09") == {}

    def test_none_when_the_call_fails(self):
        """An ApiError comes back through the client as the default, and the
        default here is None -- a sweep must not read that as evidence."""
        from models.metron import list_reading_lists_modified_since

        api = MagicMock()
        api.reading_lists_list.side_effect = ApiError("nope")
        assert list_reading_lists_modified_since(_client(api), "2026-05-09") is None

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

        results = search_series_list(_client(mock_api), "Nova - Return of the Rings", 2026)
        assert [r["id"] for r in results] == [16921]


class TestMetronClient:
    """#704 PR 4b: callers use mokkari's own method names on a client that
    runs every one of them under the call policy."""

    @patch("models.metron.MokkariSession")
    def test_get_api_hands_out_a_client_over_the_cached_session(self, session_class):
        from models.metron import MetronClient, get_api

        session = MagicMock()
        session_class.return_value = session
        api = get_api(api_token="tok")

        assert isinstance(api, MetronClient)
        assert api._library is session
        assert get_api(api_token="tok") is api

    def test_a_method_runs_under_the_policy(self):
        """A paused Metron answers the default without asking the session."""
        import models.metron as metron

        session = MagicMock()
        metron._daily.latch_for(3600, "test")

        assert _client(session).using(default=[]).series_list({"name": "x"}) == []
        session.series_list.assert_not_called()

    def test_writes_clu_never_sends_are_not_reachable(self):
        with pytest.raises(AttributeError):
            _client(MagicMock()).series_post({"name": "x"})

    def test_scrobble_reports_success_and_failure(self):
        from models.metron import scrobble_issue

        session = MagicMock()
        session.collection_scrobble.return_value = SimpleNamespace(id=1)
        assert scrobble_issue(_client(session), 5, "2026-01-01T00:00:00") is True

        session.collection_scrobble.side_effect = ApiError("nope")
        assert scrobble_issue(_client(session), 5, "2026-01-01T00:00:00") is False


class TestAFailedRequestIsNotANotFound:
    """The multi-request helpers were split so each request is guarded on its
    own (#704 PR 4b). A failed request comes back as None, and must not be
    reported as Metron not carrying the series."""

    def test_cv_id_lookup(self):
        from models.metron import get_series_id_by_comicvine_id

        session = MagicMock()
        session.series_list.side_effect = ApiError("boom")
        with patch("models.metron.app_logger") as log:
            assert get_series_id_by_comicvine_id(_client(session), 12345) is None
        assert not any(
            "No Metron series found" in str(c) for c in log.info.call_args_list
        )

    def test_series_search(self):
        from models.metron import search_series_by_name

        session = MagicMock()
        session.series_list.side_effect = ApiError("boom")
        with patch("models.metron.app_logger") as log:
            assert search_series_by_name(_client(session), "Batman") is None
        assert not any(
            "No Metron series found" in str(c) for c in log.info.call_args_list
        )

    def test_a_failed_spelling_moves_on_to_the_next(self):
        """Each spelling is its own request: one failing must not abandon the
        rest, as it did when all of them ran inside one guarded call."""
        from models.metron import search_series_list

        target = MagicMock(id=16921, image=None, desc="", issue_count=3, year_began=2026)
        target.name = "Nova: Return of the Rings"
        session = MagicMock()
        session.series_list.side_effect = [ApiError("boom"), [target]]

        results = search_series_list(_client(session), "Nova - Return of the Rings")

        assert [r["id"] for r in results] == [16921]
        assert session.series_list.call_count == 2
