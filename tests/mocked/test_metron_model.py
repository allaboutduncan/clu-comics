"""Tests for models/metron.py -- mocked Mokkari API."""
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from unittest.mock import patch, MagicMock, mock_open

from mokkari.exceptions import RateLimitError

from tests.mocked.conftest import make_mock_series, make_mock_issue


class TestGetApi:

    @patch("models.metron.MokkariSession")
    def test_returns_session(self, mock_session_class):
        from models.metron import get_api

        mock_session_class.return_value = MagicMock()
        api = get_api("user", "pass")
        assert api is not None
        mock_session_class.assert_called_once()

    def test_empty_credentials(self):
        from models.metron import get_api
        assert get_api("", "") is None
        assert get_api(None, None) is None

    @patch("models.metron.MokkariSession")
    def test_reuses_session_for_same_credentials(self, mock_session_class):
        """Repeated calls with the same creds must not construct a new
        Session each time -- concurrent threads sharing credentials rely on
        this to share mokkari's rate-limit tracking."""
        from models.metron import get_api

        mock_session_class.return_value = MagicMock()
        first = get_api("user", "pass")
        second = get_api("user", "pass")
        assert first is second
        mock_session_class.assert_called_once()

    @patch("models.metron.MokkariSession")
    def test_separate_sessions_for_different_credentials(self, mock_session_class):
        from models.metron import get_api

        mock_session_class.side_effect = [MagicMock(), MagicMock()]
        first = get_api("user1", "pass1")
        second = get_api("user2", "pass2")
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
        get_api("user", "pass")

        _, kwargs = mock_session_class.call_args
        cache = kwargs.get("cache")
        assert cache is not None
        assert cache.expire == 1
        assert str(tmp_path / "mokkari") in cache.con.execute(
            "PRAGMA database_list"
        ).fetchone()[2]

    @patch("models.metron.MokkariSession")
    def test_expiry_env_override(self, mock_session_class, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        monkeypatch.setenv("METRON_CACHE_EXPIRE_DAYS", "7")
        from models.metron import get_api

        mock_session_class.return_value = MagicMock()
        get_api("user", "pass")

        _, kwargs = mock_session_class.call_args
        assert kwargs["cache"].expire == 7

    def test_expiry_never_falsy(self, monkeypatch):
        """mokkari treats a falsy expire as 'never expire' -- a cache that never
        drops a row would pin stale metadata forever."""
        from models.metron import _metron_cache_expire_days

        monkeypatch.setenv("METRON_CACHE_EXPIRE_DAYS", "0")
        assert _metron_cache_expire_days() >= 1
        monkeypatch.setenv("METRON_CACHE_EXPIRE_DAYS", "garbage")
        assert _metron_cache_expire_days() >= 1

    @patch("models.metron.MokkariSession")
    @patch("models.metron.SqliteCache", side_effect=OSError(30, "Read-only file system"))
    def test_unwritable_cache_degrades_to_uncached_not_to_no_metron(
        self, mock_cache, mock_session_class
    ):
        from models.metron import get_api

        mock_session_class.return_value = MagicMock()
        api = get_api("user", "pass")

        assert api is not None
        _, kwargs = mock_session_class.call_args
        assert kwargs["cache"] is None

    @patch("models.metron.MokkariSession")
    def test_invalidate_runs_cache_cleanup(self, mock_session_class):
        """mokkari only expires rows in SqliteCache.__init__, so on a long-lived
        shared session this is the only thing that ever drops stale responses."""
        from models.metron import get_api, invalidate_session_cache

        session = MagicMock()
        mock_session_class.return_value = session
        get_api("user", "pass")

        invalidate_session_cache()

        session.cache.cleanup.assert_called_once()

    @patch("models.metron.MokkariSession")
    def test_invalidate_survives_a_session_without_a_cache(self, mock_session_class):
        from models.metron import get_api, invalidate_session_cache

        session = MagicMock()
        session.cache = None
        mock_session_class.return_value = session
        get_api("user", "pass")

        invalidate_session_cache()  # must not raise


class TestPurgeSeriesCache:
    """A user-initiated refresh has to reach Metron, not mokkari's cache.

    ``SqliteCache.get()`` never checks the expire column and ``store()`` is a
    plain INSERT whose row ``get()`` returns first, so a body cached once is
    replayed for the life of the process unless its rows are deleted.
    """

    SERIES_URL = "https://metron.cloud/api/series/8859/"
    ISSUES_URL = "https://metron.cloud/api/issue/?page=1&series_id=8859"

    def _session_with_cache(self, tmp_path):
        from mokkari.sqlite_cache import SqliteCache

        cache = SqliteCache(db_name=str(tmp_path / "mokkari_cache.db"), expire=1)
        return SimpleNamespace(cache=cache), cache

    def test_drops_series_and_issue_rows(self, tmp_path):
        from models.metron import purge_series_cache

        session, cache = self._session_with_cache(tmp_path)
        cache.store(self.SERIES_URL, {"desc": "old summary"})
        cache.store(self.ISSUES_URL, {"results": []})

        assert purge_series_cache(8859, api=session) == 2
        assert cache.get(self.SERIES_URL) is None
        assert cache.get(self.ISSUES_URL) is None

    def test_leaves_other_series_alone(self, tmp_path):
        """LIKE '%series_id=8859%' also matches 88591 -- only the regex saves us."""
        from models.metron import purge_series_cache

        session, cache = self._session_with_cache(tmp_path)
        other_detail = "https://metron.cloud/api/series/88591/"
        other_issues = "https://metron.cloud/api/issue/?page=1&series_id=88591"
        cache.store(self.SERIES_URL, {"desc": "old summary"})
        cache.store(other_detail, {"desc": "different series"})
        cache.store(other_issues, {"results": []})

        assert purge_series_cache(8859, api=session) == 1
        assert cache.get(other_detail) == {"desc": "different series"}
        assert cache.get(other_issues) == {"results": []}

    def test_drops_every_duplicate_row_for_a_key(self, tmp_path):
        """store() appends, so one leftover row keeps serving the stale body."""
        from models.metron import purge_series_cache

        session, cache = self._session_with_cache(tmp_path)
        cache.store(self.SERIES_URL, {"desc": "old summary"})
        cache.store(self.SERIES_URL, {"desc": "older still"})

        assert purge_series_cache(8859, api=session) == 2
        assert cache.get(self.SERIES_URL) is None

    def test_next_fetch_sees_the_new_body(self, tmp_path):
        from models.metron import purge_series_cache

        session, cache = self._session_with_cache(tmp_path)
        cache.store(self.SERIES_URL, {"desc": "old summary"})
        purge_series_cache(8859, api=session)
        cache.store(self.SERIES_URL, {"desc": "new summary"})

        assert cache.get(self.SERIES_URL) == {"desc": "new summary"}

    def test_session_without_a_cache_is_a_no_op(self):
        from models.metron import purge_series_cache

        assert purge_series_cache(8859, api=SimpleNamespace(cache=None)) == 0

    def test_non_numeric_series_id_is_a_no_op(self, tmp_path):
        from models.metron import purge_series_cache

        session, cache = self._session_with_cache(tmp_path)
        cache.store(self.SERIES_URL, {"desc": "old summary"})

        assert purge_series_cache("not-an-id", api=session) == 0
        assert cache.get(self.SERIES_URL) == {"desc": "old summary"}

    @patch("models.metron.MokkariSession")
    def test_defaults_to_every_cached_session(self, mock_session_class, tmp_path):
        from models.metron import get_api, invalidate_session_cache, purge_series_cache

        session, cache = self._session_with_cache(tmp_path)
        cache.store(self.SERIES_URL, {"desc": "old summary"})
        mock_session_class.return_value = session
        invalidate_session_cache()
        get_api("user", "pass")

        assert purge_series_cache(8859) == 1
        assert cache.get(self.SERIES_URL) is None
        invalidate_session_cache()


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
        mock_app.config = {
            "METRON_USERNAME": "user",
            "METRON_PASSWORD": "pass",
        }
        mock_session_class.return_value = MagicMock()

        api = get_flask_api(mock_app)
        assert api is not None
        mock_session_class.assert_called_once()

    @patch("models.metron.MokkariSession")
    def test_with_current_app(self, mock_session_class):
        from flask import Flask
        from models.metron import get_flask_api

        test_app = Flask(__name__)
        test_app.config["METRON_USERNAME"] = "user"
        test_app.config["METRON_PASSWORD"] = "pass"
        mock_session_class.return_value = MagicMock()

        with test_app.app_context():
            api = get_flask_api()
            assert api is not None

    def test_missing_credentials(self):
        from models.metron import get_flask_api

        mock_app = MagicMock()
        mock_app.config = {
            "METRON_USERNAME": "",
            "METRON_PASSWORD": "",
        }
        assert get_flask_api(mock_app) is None

    def test_whitespace_only_credentials(self):
        from models.metron import get_flask_api

        mock_app = MagicMock()
        mock_app.config = {
            "METRON_USERNAME": "  ",
            "METRON_PASSWORD": "  ",
        }
        assert get_flask_api(mock_app) is None

    def test_missing_username_only(self):
        from models.metron import get_flask_api

        mock_app = MagicMock()
        mock_app.config = {
            "METRON_USERNAME": "",
            "METRON_PASSWORD": "pass",
        }
        assert get_flask_api(mock_app) is None


class TestIsMetronConfigured:

    def test_both_credentials_present(self):
        from models.metron import is_metron_configured

        mock_app = MagicMock()
        mock_app.config = {
            "METRON_USERNAME": "user",
            "METRON_PASSWORD": "pass",
        }
        assert is_metron_configured(mock_app) is True

    def test_no_credentials(self):
        from models.metron import is_metron_configured

        mock_app = MagicMock()
        mock_app.config = {
            "METRON_USERNAME": "",
            "METRON_PASSWORD": "",
        }
        assert is_metron_configured(mock_app) is False

    def test_only_password(self):
        from models.metron import is_metron_configured

        mock_app = MagicMock()
        mock_app.config = {
            "METRON_USERNAME": "",
            "METRON_PASSWORD": "pass",
        }
        assert is_metron_configured(mock_app) is False

    def test_only_username(self):
        from models.metron import is_metron_configured

        mock_app = MagicMock()
        mock_app.config = {
            "METRON_USERNAME": "user",
            "METRON_PASSWORD": "",
        }
        assert is_metron_configured(mock_app) is False

    def test_whitespace_stripping(self):
        from models.metron import is_metron_configured

        mock_app = MagicMock()
        mock_app.config = {
            "METRON_USERNAME": "  user  ",
            "METRON_PASSWORD": "  pass  ",
        }
        assert is_metron_configured(mock_app) is True

    def test_with_current_app(self):
        from flask import Flask
        from models.metron import is_metron_configured

        test_app = Flask(__name__)
        test_app.config["METRON_USERNAME"] = "user"
        test_app.config["METRON_PASSWORD"] = "pass"

        with test_app.app_context():
            assert is_metron_configured() is True
