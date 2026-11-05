"""Tests for models/comicvine.py -- mocked Simyan library."""
import importlib
import sys
import threading
import xml.etree.ElementTree as ET
import zipfile

import pytest
from unittest.mock import patch, MagicMock
from tests.mocked.conftest import make_mock_cv_volume, make_mock_cv_issue



def _call(fn, context="test", blocking=None):
    """One ComicVineClient method call whose library work is ``fn()``.

    The real client and policy run; only Simyan is a mock.
    """
    import models.comicvine as cv

    library = MagicMock()
    library.get_volume.side_effect = lambda *a, **k: fn()
    return cv.ComicVineClient(library).using(
        context=context, blocking=blocking
    ).get_volume(1)


class TestIsSimyanAvailable:

    def test_returns_bool(self):
        from models.comicvine import is_simyan_available
        assert isinstance(is_simyan_available(), bool)


class TestClientCache:
    """Every Simyan client is a CachedLimiterSession, and constructing one
    starts a background pyrate_limiter Leaker thread plus SQLite connections
    for the HTTP cache and the rate-limit bucket -- none of which are ever
    released. Building one per call leaked a thread and two connections per
    series during a library sweep (observed: 4.6GB RSS), and left N independent
    buckets racing past ComicVine's limit ("Slow down cowboy"). The client must
    be shared."""

    def test_same_key_returns_same_client_and_builds_once(self):
        import models.comicvine as cv

        with patch.object(cv, "_make_cv_client") as mock_make:
            mock_make.side_effect = lambda key: MagicMock(name=f"client-{key}")
            first = cv.get_cv_client("KEY")
            second = cv.get_cv_client("KEY")

        assert first is second
        assert mock_make.call_count == 1

    def test_different_keys_get_different_clients(self):
        import models.comicvine as cv

        with patch.object(cv, "_make_cv_client") as mock_make:
            mock_make.side_effect = lambda key: MagicMock(name=f"client-{key}")
            a = cv.get_cv_client("KEY-A")
            b = cv.get_cv_client("KEY-B")

        assert a is not b

    def test_invalidate_closes_sessions_and_forces_rebuild(self):
        """Dropping a client without closing it leaks exactly what the cache
        exists to prevent -- LimiterMixin.close() stops the Leaker thread."""
        import models.comicvine as cv

        first = MagicMock(name="first")
        second = MagicMock(name="second")
        with patch.object(cv, "_make_cv_client", side_effect=[first, second]):
            assert cv.get_cv_client("KEY")._library is first
            cv.invalidate_cv_client_cache()
            first._session.close.assert_called_once()
            assert cv.get_cv_client("KEY")._library is second

    def test_concurrent_callers_share_one_client(self):
        """Background sweeps, the wanted-cache refresh and live web requests can
        all reach for a client at once."""
        import models.comicvine as cv

        built = []
        start = threading.Barrier(8)

        def build(key):
            client = MagicMock(name=f"client-{len(built)}")
            built.append(client)
            return client

        results = []
        results_lock = threading.Lock()

        def worker():
            start.wait()
            client = cv.get_cv_client("KEY")
            with results_lock:
                results.append(client)

        with patch.object(cv, "_make_cv_client", side_effect=build):
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert len(results) == 8
        assert all(c is results[0] for c in results)
        # Losers of the race must have had their session closed, or their
        # Leaker threads outlive them.
        for client in built:
            if client is not results[0]._library:
                client._session.close.assert_called_once()


class TestMaxDelay:
    """Once the client is shared, Simyan's own per_hour=200 bucket finally
    engages -- and a saturated bucket raises instead of waiting past
    max_delay (default timeout*2 = 40s). Raise the ceiling, but never to
    unbounded: a live web request must not hang forever."""

    def test_default_max_delay_applied_to_session(self, monkeypatch):
        import models.comicvine as cv

        monkeypatch.delenv("COMICVINE_MAX_DELAY", raising=False)
        with patch.object(cv, "Comicvine", create=True) as mock_cv:
            client = cv._make_cv_client("KEY")

        assert client._session.max_delay == 60.0

    def test_env_override_honoured(self, monkeypatch):
        import models.comicvine as cv

        monkeypatch.setenv("COMICVINE_MAX_DELAY", "120")
        with patch.object(cv, "Comicvine", create=True):
            client = cv._make_cv_client("KEY")

        assert client._session.max_delay == 120.0

    def test_garbage_env_falls_back_to_default(self, monkeypatch):
        import models.comicvine as cv

        monkeypatch.setenv("COMICVINE_MAX_DELAY", "not-a-number")
        assert cv._cv_max_delay() == 60.0


class TestCallPolicy:
    """ComicVine on the shared call policy (#704).

    Pacing is Simyan's own, per HTTP send. CLU used to take a second slot per
    *call* from a 180/h limiter, so a paginated list cost one slot however many
    pages it sent -- the #703 anti-pattern. What CLU adds is the auth lockout,
    the hourly quota latch and a bounded wait for live web requests.
    """

    @pytest.fixture(autouse=True)
    def _fresh_policy(self):
        import models.comicvine as cv

        cv._hourly.reset()
        with patch.object(cv._auth_block, "blocked", return_value=False), \
             patch.object(cv._auth_block, "latch") as latch:
            self.latch = latch
            yield
        cv._hourly.reset()

    def test_there_is_no_per_call_limiter_any_more(self):
        import models.comicvine as cv

        assert not hasattr(cv, "_comicvine_rate_limiter")
        assert _call(lambda: "ok", "test") == "ok"

    def test_an_invalid_key_latches_the_lockout_and_still_raises(self):
        import models.comicvine as cv
        from simyan.errors import AuthenticationError

        with pytest.raises(AuthenticationError):
            _call(
                MagicMock(side_effect=AuthenticationError("Invalid API Key")), "test"
            )
        self.latch.assert_called_once()
        assert self.latch.call_args[0][0] == 401

    def test_calls_are_skipped_while_locked(self):
        import models.comicvine as cv

        fn = MagicMock(return_value="ok")
        with patch.object(cv._auth_block, "blocked", return_value=True):
            with pytest.raises(cv.ComicVineUnavailable, match="rejected"):
                _call(fn, "test")
        fn.assert_not_called()

    @patch("models.comicvine.time.sleep")
    def test_simyans_limiter_refusing_latches_the_hourly_quota(self, mock_sleep):
        """Its per-second bucket never needs max_delay, so a refusal means the
        200/hour bucket is spent: latch it instead of one max_delay per call."""
        import models.comicvine as cv

        refused = Exception("Service took too long to respond")
        refused.__cause__ = Exception("Rate limit not cleared within max_delay=60s")
        fn = MagicMock(side_effect=refused)

        with pytest.raises(Exception, match="too long"):
            _call(fn, "test")
        assert fn.call_count == 1, "a spent hourly bucket is not retried"
        mock_sleep.assert_not_called()
        assert cv._hourly.reached()

        later = MagicMock(return_value="ok")
        with pytest.raises(cv.ComicVineUnavailable, match="hourly"):
            _call(later, "test")
        later.assert_not_called()
        assert cv.hourly_wait_seconds() >= 59

    def test_a_genuine_timeout_is_a_connection_error_not_a_quota(self):
        import requests
        import models.comicvine as cv

        err = Exception("Service took too long to respond")
        err.__cause__ = requests.Timeout("Read timed out")
        with pytest.raises(Exception, match="too long"):
            _call(MagicMock(side_effect=err), "test")
        assert not cv._hourly.reached()

    def test_a_web_request_does_not_wait_out_a_full_bucket(self):
        import models.comicvine as cv

        fn = MagicMock(return_value="ok")
        with patch("models.comicvine._bucket_wait_seconds", return_value=600.0), \
             patch("flask.has_request_context", return_value=True):
            with pytest.raises(cv.ComicVineBusy):
                _call(fn, "test")
        fn.assert_not_called()

    def test_a_background_caller_rides_out_the_bucket(self):
        """A sweep waits inside Simyan's limiter; only web requests are capped."""
        import models.comicvine as cv

        with patch("models.comicvine._bucket_wait_seconds", return_value=600.0):
            assert _call(lambda: "ok", "test") == "ok"
            assert _call(lambda: "ok", "test", blocking=True) == "ok"
            with pytest.raises(cv.ComicVineBusy):
                _call(lambda: "ok", "test", blocking=False)

    def test_a_short_wait_does_not_refuse_a_web_request(self):
        import models.comicvine as cv

        with patch("models.comicvine._bucket_wait_seconds", return_value=1.0), \
             patch("flask.has_request_context", return_value=True):
            assert _call(lambda: "ok", "test") == "ok"

    @patch("models.comicvine.time.sleep")
    def test_back_off_is_linear(self, mock_sleep):
        import models.comicvine as cv

        always = MagicMock(side_effect=Exception("Rate limit exceeded. Slow down cowboy."))
        with patch("models.comicvine._cv_retry_config", return_value=(3, 5.0)):
            with pytest.raises(Exception, match="Slow down"):
                _call(always, "test")
        assert [c.args[0] for c in mock_sleep.call_args_list] == [5.0, 10.0, 15.0]
        assert always.call_count == 4


class TestQuietWhilePaused:
    """A latched quota fails every call at once; a sweep must not log an
    ERROR per item for a condition the latch already announced."""

    def test_skipped_calls_log_at_debug_real_failures_at_their_level(self):
        import models.comicvine as cv

        with patch.object(cv.app_logger, "debug") as debug, \
             patch.object(cv.app_logger, "error") as error, \
             patch.object(cv.app_logger, "warning") as warning:
            cv.log_cv_failure("skipped", cv.ComicVineUnavailable("paused"))
            cv.log_cv_failure("busy", cv.ComicVineBusy("busy"))
            cv.log_cv_failure("boom", ValueError("x"))
            cv.log_cv_failure("meh", ValueError("x"), "warning")
        debug.assert_called_once_with("skipped")
        assert [c.args[0] for c in error.call_args_list] == ["busy", "boom"]
        warning.assert_called_once_with("meh")

    def test_search_volumes_is_quiet_while_the_quota_is_latched(self):
        import models.comicvine as cv

        cv._hourly.latch_for(3600)
        try:
            with patch.object(cv, "SIMYAN_AVAILABLE", True), \
                 patch.object(cv, "get_cv_client", return_value=cv.ComicVineClient(MagicMock())), \
                 patch.object(cv._auth_block, "blocked", return_value=False), \
                 patch.object(cv.app_logger, "error") as error:
                # search_volumes raises by contract; it must just not shout.
                with pytest.raises(cv.ComicVineUnavailable):
                    cv.search_volumes("key", "Batman")
            error.assert_not_called()
        finally:
            cv._hourly.reset()


class TestBucketWait:
    """hourly_wait_seconds reads Simyan's real bucket without taking a slot."""

    def test_reads_the_oldest_item_that_must_age_out(self, tmp_path):
        import models.comicvine as cv
        from pyrate_limiter import RateItem
        from simyan.comicvine import Comicvine

        client = Comicvine(api_key="k", cache_path=tmp_path / "c.sqlite",
                           ratelimit_path=tmp_path / "r.sqlite")
        try:
            bucket = client._session.limiter.bucket_factory["comicvine"]
            base = bucket.now() - 3_000_000  # 50 min ago
            for i in range(200):
                bucket.put(RateItem("comicvine", base + i * 1100, 1))
            with patch.dict(cv._CV_CLIENT_CACHE, {"k": cv.ComicVineClient(client)}, clear=True):
                assert cv._bucket_wait_seconds(1) == pytest.approx(600, abs=2)
                assert cv._bucket_wait_seconds(3) == pytest.approx(602, abs=2)
            with patch.dict(cv._CV_CLIENT_CACHE, {}, clear=True):
                assert cv._bucket_wait_seconds(1) == 0
        finally:
            client._session.close()


class TestRateLimitDetectionThroughCause:
    """Simyan's limiter raises Timeout("Rate limit not cleared within
    max_delay=..."), but Comicvine._get_request re-raises it as the far less
    specific ServiceError("Service took too long to respond"). Matching only the
    outer message would turn a throttle into a hard failure and drop the volume."""

    def test_detects_rate_limit_on_cause_chain(self):
        from models.comicvine import _is_rate_limit_error

        cause = Exception("Rate limit not cleared within max_delay=60s")
        outer = Exception("Service took too long to respond")
        outer.__cause__ = cause

        assert _is_rate_limit_error(outer)

    def test_genuine_network_timeout_is_not_a_rate_limit(self):
        """A real read timeout must stay a hard error, not be retried as a
        throttle."""
        from models.comicvine import _is_rate_limit_error

        cause = Exception("HTTPSConnectionPool(host='comicvine.gamespot.com'): Read timed out")
        outer = Exception("Service took too long to respond")
        outer.__cause__ = cause

        assert not _is_rate_limit_error(outer)

    def test_survives_a_self_referential_cause_chain(self):
        from models.comicvine import _is_rate_limit_error

        exc = Exception("boom")
        exc.__cause__ = exc

        assert not _is_rate_limit_error(exc)


class TestRateLimitRetry:
    """ComicVine throttles bursts ("Slow down cowboy"); the bulk-metadata flow
    must re-attempt before failing over to a lesser provider (GCD)."""

    def test_detects_rate_limit_message(self):
        from models.comicvine import _is_rate_limit_error
        assert _is_rate_limit_error(Exception("Rate limit exceeded. Slow down cowboy."))
        assert _is_rate_limit_error(Exception("RATE LIMIT"))
        assert not _is_rate_limit_error(Exception("404 Not Found"))

    @patch("models.comicvine.time.sleep")
    def test_retries_then_succeeds(self, mock_sleep):

        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise Exception("Rate limit exceeded. Slow down cowboy.")
            return "ok"

        assert _call(flaky, "test") == "ok"
        assert calls["n"] == 3
        assert mock_sleep.call_count == 2  # backed off before each retry

    @patch("models.comicvine.time.sleep")
    def test_non_rate_limit_error_not_retried(self, mock_sleep):

        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise ValueError("something else")

        with pytest.raises(ValueError):
            _call(boom, "test")
        assert calls["n"] == 1  # raised immediately, no retry
        mock_sleep.assert_not_called()

    @patch("models.comicvine.time.sleep")
    @patch("models.comicvine._cv_retry_config", return_value=(2, 0.0))
    def test_gives_up_after_retries(self, mock_cfg, mock_sleep):

        calls = {"n": 0}

        def always():
            calls["n"] += 1
            raise Exception("Rate limit exceeded. Slow down cowboy.")

        with pytest.raises(Exception, match="Rate limit"):
            _call(always, "test")
        assert calls["n"] == 3  # first attempt + 2 retries

    @patch("models.comicvine.time.sleep")
    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource")
    @patch("models.comicvine.Comicvine", create=True)
    def test_search_volumes_retries_rate_limit(self, mock_cv_class, mock_resource, mock_sleep):
        from models.comicvine import search_volumes

        mock_cv = MagicMock()
        mock_cv.search_volumes.side_effect = [
            Exception("Rate limit exceeded. Slow down cowboy."),
            [make_mock_cv_volume(id=4050, name="Batman", start_year=2016)],
        ]
        mock_cv_class.return_value = mock_cv

        results = search_volumes("fake-key", "Batman")
        assert len(results) == 1
        assert results[0]["id"] == 4050
        assert mock_cv.search_volumes.call_count == 2  # retried once, then succeeded


class TestSearchVolumes:

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource")
    @patch("models.comicvine.Comicvine", create=True)
    def test_returns_volumes(self, mock_cv_class, mock_resource):
        from models.comicvine import search_volumes

        mock_cv = MagicMock()
        mock_cv.search_volumes.return_value = [
            make_mock_cv_volume(id=4050, name="Batman", start_year=2016),
        ]
        mock_cv_class.return_value = mock_cv

        results = search_volumes("fake-key", "Batman")
        assert len(results) == 1
        assert results[0]["name"] == "Batman"
        assert results[0]["id"] == 4050

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource")
    @patch("models.comicvine.Comicvine", create=True)
    def test_no_results(self, mock_cv_class, mock_resource):
        from models.comicvine import search_volumes

        mock_cv = MagicMock()
        mock_cv.search_volumes.return_value = []
        mock_cv_class.return_value = mock_cv

        assert search_volumes("fake-key", "Nonexistent") == []

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource")
    @patch("models.comicvine.Comicvine", create=True)
    def test_retries_with_a_relaxed_query_when_the_verbatim_one_finds_nothing(
        self, mock_cv_class, mock_resource
    ):
        """A stray article in the filename ("Red Range Pirates of *the*
        Fireworld") knocks the real volume out of ComicVine's keyword search
        entirely, so the cascade skipped to the next provider as though the
        series were not in ComicVine at all."""
        from models.comicvine import search_volumes

        mock_cv = MagicMock()
        mock_cv.search_volumes.side_effect = [
            [],
            [make_mock_cv_volume(id=120999, name="Red Range: Pirates of Fireworld",
                                 start_year=2019)],
        ]
        mock_cv_class.return_value = mock_cv

        results = search_volumes("fake-key", "Red Range Pirates of the Fireworld")

        assert [v["id"] for v in results] == [120999]
        assert [c.args[0] for c in mock_cv.search_volumes.call_args_list] == [
            "Red Range Pirates of the Fireworld",
            "red range pirates fireworld",
        ]

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource")
    @patch("models.comicvine.Comicvine", create=True)
    def test_verbatim_hit_does_not_spend_a_second_call(self, mock_cv_class, mock_resource):
        """ComicVine's budget is hourly and shared process-wide -- the relaxed
        query is a fallback, not a second opinion."""
        from models.comicvine import search_volumes

        mock_cv = MagicMock()
        mock_cv.search_volumes.return_value = [make_mock_cv_volume(id=4050, name="The Flash")]
        mock_cv_class.return_value = mock_cv

        assert len(search_volumes("fake-key", "The Flash")) == 1
        assert mock_cv.search_volumes.call_count == 1

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource")
    @patch("models.comicvine.Comicvine", create=True)
    def test_year_ranking(self, mock_cv_class, mock_resource):
        from models.comicvine import search_volumes

        mock_cv = MagicMock()
        mock_cv.search_volumes.return_value = [
            make_mock_cv_volume(id=1, start_year=1940),
            make_mock_cv_volume(id=2, start_year=2016),
        ]
        mock_cv_class.return_value = mock_cv

        results = search_volumes("fake-key", "Batman", year=2016)
        assert results[0]["id"] == 2  # Closer to 2016

    @patch("models.comicvine.SIMYAN_AVAILABLE", False)
    def test_simyan_not_available(self):
        from models.comicvine import search_volumes

        with pytest.raises(Exception, match="Simyan library not installed"):
            search_volumes("fake-key", "Batman")


class TestGetIssueByNumber:

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource")
    @patch("models.comicvine.Comicvine", create=True)
    def test_finds_issue(self, mock_cv_class, mock_resource):
        from models.comicvine import get_issue_by_number

        mock_cv = MagicMock()
        basic_issue = MagicMock()
        basic_issue.id = 1001
        mock_cv.list_issues.return_value = [basic_issue]

        full_issue = make_mock_cv_issue(id=1001, issue_number="5", name="Rebirth")
        full_issue.creators = []
        full_issue.characters = []
        full_issue.teams = []
        full_issue.locations = []
        full_issue.story_arcs = []
        mock_cv.get_issue.return_value = full_issue
        mock_cv_class.return_value = mock_cv

        result = get_issue_by_number("fake-key", 4050, "5")
        assert result is not None
        assert result["id"] == 1001

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource")
    @patch("models.comicvine.Comicvine", create=True)
    def test_issue_not_found(self, mock_cv_class, mock_resource):
        from models.comicvine import get_issue_by_number

        mock_cv = MagicMock()
        mock_cv.list_issues.return_value = []
        mock_cv_class.return_value = mock_cv

        assert get_issue_by_number("fake-key", 4050, "999") is None


class TestIssueToDict:
    """The API path's object -> dict step, including credit bucketing."""

    def _issue(self, **over):
        issue = make_mock_cv_issue(id=29348, issue_number="4", name="Race with the Devil")
        issue.creators = []
        issue.characters = []
        issue.teams = []
        issue.locations = []
        issue.story_arcs = []
        issue.site_url = "https://comicvine.gamespot.com/swords-of-texas-4/4000-29348/"
        for k, v in over.items():
            setattr(issue, k, v)
        return issue

    def test_multi_role_creator_fills_every_bucket(self):
        from models.comicvine import _issue_to_dict

        creator = MagicMock()
        creator.name = "Ben Dunn"
        creator.roles = "penciler, inker"
        data = _issue_to_dict(self._issue(creators=[creator]))

        assert data["pencillers"] == ["Ben Dunn"]
        assert data["inkers"] == ["Ben Dunn"]

    def test_editor_credit_captured(self):
        from models.comicvine import _issue_to_dict

        creator = MagicMock()
        creator.name = "Tim Truman"
        creator.roles = "editor"
        data = _issue_to_dict(self._issue(creators=[creator]))
        assert data["editors"] == ["Tim Truman"]

    def test_site_url_captured_for_web(self):
        from models.comicvine import _issue_to_dict

        data = _issue_to_dict(self._issue())
        assert data["site_url"] == "https://comicvine.gamespot.com/swords-of-texas-4/4000-29348/"

    def test_missing_site_url(self):
        from models.comicvine import _issue_to_dict

        assert _issue_to_dict(self._issue(site_url=None))["site_url"] is None

    def test_all_story_arcs_kept(self):
        from models.comicvine import _issue_to_dict

        first, second = MagicMock(), MagicMock()
        first.name, second.name = "Year One", "Second Arc"
        data = _issue_to_dict(self._issue(story_arcs=[first, second]))
        assert data["story_arc"] == "Year One, Second Arc"


class TestMapToComicinfo:

    def test_full_mapping(self):
        from models.comicvine import map_to_comicinfo

        issue_data = {
            "id": 1001,
            "name": "Rebirth",
            "issue_number": "1",
            "volume_name": "Batman",
            "volume_id": 4050,
            "publisher": "DC Comics",
            "cover_date": "2020-06-15",
            "year": 2020,
            "month": 6,
            "day": 15,
            "description": "Batman returns",
            "writers": ["Tom King"],
            "pencillers": ["David Finch"],
            "inkers": [],
            "colorists": [],
            "letterers": [],
            "cover_artists": [],
            "characters": ["Batman", "Catwoman"],
            "teams": ["Justice League"],
            "locations": ["Gotham City"],
            "story_arc": "City of Bane",
        }

        result = map_to_comicinfo(issue_data)

        assert result["Series"] == "Batman"
        assert result["Number"] == "1"
        assert result["Title"] == "Rebirth"
        assert result["Year"] == 2020
        assert result["Month"] == 6
        assert result["Publisher"] == "DC Comics"
        assert result["Writer"] == "Tom King"
        assert "Batman" in result["Characters"]
        assert result["StoryArc"] == "City of Bane"
        assert "LanguageISO" in result

    def test_maps_editor_translator_and_web(self):
        from models.comicvine import map_to_comicinfo

        issue_data = {
            "id": 29348,
            "issue_number": "4",
            "volume_id": 3892,
            "editors": ["Tim Truman"],
            "translators": ["A. Translator"],
            "site_url": "https://comicvine.gamespot.com/swords-of-texas-4/4000-29348/",
        }
        result = map_to_comicinfo(issue_data)
        assert result["Editor"] == "Tim Truman"
        assert result["Translator"] == "A. Translator"
        assert result["Web"] == "https://comicvine.gamespot.com/swords-of-texas-4/4000-29348/"

    def test_notes_carry_a_machine_readable_issue_id(self):
        """Without it no tagger can tell which ComicVine issue was matched --
        the Volume ID alone is not enough to identify the issue."""
        from models.comicvine import map_to_comicinfo

        result = map_to_comicinfo({"id": 29348, "issue_number": "4", "volume_id": 3892})
        assert "Volume ID: 3892" in result["Notes"]
        assert result["Notes"].endswith("[Issue ID 4000-29348] urn:comicvine:4000-29348")

    def test_notes_omit_issue_markers_without_an_id(self):
        from models.comicvine import map_to_comicinfo

        result = map_to_comicinfo({"issue_number": "4", "volume_id": 3892})
        assert "urn:comicvine" not in result["Notes"]

    def test_genre_is_never_set(self):
        """ComicVine has no genre data; an existing Genre survives instead via
        core.comicinfo.merge_comicinfo_bytes."""
        from models.comicvine import map_to_comicinfo

        assert "Genre" not in map_to_comicinfo({"id": 1, "issue_number": "1"})

    def test_with_volume_data(self):
        from models.comicvine import map_to_comicinfo

        issue_data = {"id": 1, "name": None, "issue_number": "1",
                      "volume_name": None, "volume_id": None,
                      "publisher": None, "year": 2020}
        volume_data = {"id": 4050, "name": "Batman", "start_year": 2016,
                       "publisher_name": "DC Comics"}

        result = map_to_comicinfo(issue_data, volume_data)
        assert result["Series"] == "Batman"
        assert result["Publisher"] == "DC Comics"
        assert result["Volume"] == 2016

    def test_start_year_override(self):
        from models.comicvine import map_to_comicinfo

        issue_data = {"id": 1, "issue_number": "1", "year": 2020}
        result = map_to_comicinfo(issue_data, None, start_year=2016)
        assert result["Volume"] == 2016

    def test_preserves_cover_and_store_dates(self):
        from models.comicvine import map_to_comicinfo

        issue_data = {"id": 1, "issue_number": "1", "year": 2020, "month": 3,
                      "cover_date": "2020-03-01", "store_date": "2020-05-15"}
        result = map_to_comicinfo(issue_data)
        assert result["CoverDate"] == "2020-03-01"
        assert result["StoreDate"] == "2020-05-15"

    def test_omits_absent_dates(self):
        from models.comicvine import map_to_comicinfo

        issue_data = {"id": 1, "issue_number": "1", "year": 2020}
        result = map_to_comicinfo(issue_data)
        # None values are stripped from the output dict
        assert "CoverDate" not in result
        assert "StoreDate" not in result


class TestGetMetadataByVolumeId:
    """Verify Publisher is populated reliably via volume_data or issue fallback."""

    @staticmethod
    def _wire_cv(mock_cv_class, *, issue_publisher="DC Comics"):
        mock_cv = MagicMock()
        basic_issue = MagicMock()
        basic_issue.id = 1001
        mock_cv.list_issues.return_value = [basic_issue]

        full_issue = make_mock_cv_issue(id=1001, issue_number="5",
                                        publisher_name=issue_publisher)
        full_issue.creators = []
        full_issue.characters = []
        full_issue.teams = []
        full_issue.locations = []
        full_issue.story_arcs = []
        mock_cv.get_issue.return_value = full_issue
        mock_cv_class.return_value = mock_cv
        return mock_cv

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource")
    @patch("models.comicvine.Comicvine", create=True)
    def test_publisher_from_kwarg(self, mock_cv_class, mock_resource):
        """publisher_name kwarg must flow into ComicInfo.xml Publisher field."""
        from models.comicvine import get_metadata_by_volume_id

        # Mock issue has no volume.publisher so the ONLY way Publisher gets
        # populated is via the explicit publisher_name kwarg.
        self._wire_cv(mock_cv_class, issue_publisher=None)

        result = get_metadata_by_volume_id(
            "fake-key", 4050, "5",
            start_year=2016,
            publisher_name="Image Comics",
        )
        assert result is not None
        assert result["Publisher"] == "Image Comics"
        assert result["Volume"] == 2016

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource")
    @patch("models.comicvine.Comicvine", create=True)
    def test_publisher_falls_back_to_issue_volume(self, mock_cv_class, mock_resource):
        """With no kwarg, Publisher must still resolve from issue.volume.publisher."""
        from models.comicvine import get_metadata_by_volume_id

        self._wire_cv(mock_cv_class, issue_publisher="DC Comics")

        result = get_metadata_by_volume_id("fake-key", 4050, "5")
        assert result is not None
        assert result["Publisher"] == "DC Comics"

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource")
    @patch("models.comicvine.Comicvine", create=True)
    def test_publisher_missing_when_both_sources_absent(self, mock_cv_class, mock_resource):
        """No kwarg + no issue.volume.publisher = Publisher omitted from output."""
        from models.comicvine import get_metadata_by_volume_id

        self._wire_cv(mock_cv_class, issue_publisher=None)

        result = get_metadata_by_volume_id("fake-key", 4050, "5")
        assert result is not None
        assert "Publisher" not in result


class TestGetVolumeDetails:

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource")
    @patch("models.comicvine.Comicvine", create=True)
    def test_returns_details(self, mock_cv_class, mock_resource):
        from models.comicvine import get_volume_details

        mock_cv = MagicMock()
        mock_vol = make_mock_cv_volume(start_year=2016, publisher_name="DC Comics")
        mock_cv.get_volume.return_value = mock_vol
        mock_cv_class.return_value = mock_cv

        result = get_volume_details("fake-key", 4050)
        assert result["start_year"] == 2016
        assert result["publisher_name"] == "DC Comics"

    @patch("models.comicvine.SIMYAN_AVAILABLE", False)
    def test_simyan_not_available(self):
        from models.comicvine import get_volume_details

        result = get_volume_details("fake-key", 4050)
        assert result["publisher_name"] is None
        assert result["start_year"] is None


class TestParseCvinfoVolumeId:

    def test_parses_volume_id(self, tmp_path):
        from models.comicvine import parse_cvinfo_volume_id

        cvinfo = tmp_path / "cvinfo"
        cvinfo.write_text("https://comicvine.gamespot.com/batman/4050-12345/\n")

        assert parse_cvinfo_volume_id(str(cvinfo)) == 12345

    def test_no_match(self, tmp_path):
        from models.comicvine import parse_cvinfo_volume_id

        cvinfo = tmp_path / "cvinfo"
        cvinfo.write_text("no url here\n")

        assert parse_cvinfo_volume_id(str(cvinfo)) is None


class TestFindCvinfoInFolder:

    def test_finds_cvinfo(self, tmp_path):
        from models.comicvine import find_cvinfo_in_folder

        (tmp_path / "cvinfo").write_text("test")
        result = find_cvinfo_in_folder(str(tmp_path))
        assert result is not None
        assert result.endswith("cvinfo")

    def test_no_cvinfo(self, tmp_path):
        from models.comicvine import find_cvinfo_in_folder
        assert find_cvinfo_in_folder(str(tmp_path)) is None


class TestRankVolumesByYear:

    def test_sorts_by_closest_year(self):
        from models.comicvine import _rank_volumes_by_year

        volumes = [
            {"name": "A", "start_year": 1940},
            {"name": "B", "start_year": 2016},
            {"name": "C", "start_year": None},
        ]

        result = _rank_volumes_by_year(volumes, 2016)
        assert result[0]["name"] == "B"
        assert result[-1]["name"] == "C"  # None year goes last


class TestAddComicInfoToArchive:
    """The batch/app.py write path. Like the routes writer, it rebuilds the
    archive, so tags the new metadata omits must be carried forward."""

    def _cbz(self, tmp_path, existing=None):
        cbz = tmp_path / "Book 001.cbz"
        with zipfile.ZipFile(str(cbz), "w") as zf:
            zf.writestr("001.jpg", b"fake image")
            if existing is not None:
                zf.writestr("ComicInfo.xml", existing)
        return str(cbz)

    def _patch_cache_dir(self, cache_dir):
        """Keep zip assembly (helpers._zip_assembly_dir) inside tmp_path."""
        return patch("core.config.get_cache_dir", return_value=str(cache_dir))

    def test_preserves_tags_the_new_metadata_omits(self, tmp_path):
        from models.comicvine import add_comicinfo_to_archive

        cbz = self._cbz(tmp_path, "<ComicInfo><Series>Old</Series><Genre>Humor</Genre></ComicInfo>")
        with self._patch_cache_dir(tmp_path / "cache"),              patch("helpers.match_parent_permissions"):
            assert add_comicinfo_to_archive(cbz, b"<ComicInfo><Series>New</Series></ComicInfo>")

        with zipfile.ZipFile(cbz) as zf:
            root = ET.fromstring(zf.read("ComicInfo.xml"))
        assert root.find("Series").text == "New"
        assert root.find("Genre").text == "Humor"

    def test_accepts_str_content(self, tmp_path):
        from models.comicvine import add_comicinfo_to_archive

        cbz = self._cbz(tmp_path)
        with self._patch_cache_dir(tmp_path / "cache"),              patch("helpers.match_parent_permissions"):
            assert add_comicinfo_to_archive(cbz, "<ComicInfo><Series>S</Series></ComicInfo>")

        with zipfile.ZipFile(cbz) as zf:
            assert b"<Series>S</Series>" in zf.read("ComicInfo.xml")

    def test_merge_can_be_disabled(self, tmp_path):
        from models.comicvine import add_comicinfo_to_archive

        cbz = self._cbz(tmp_path, "<ComicInfo><Genre>Humor</Genre></ComicInfo>")
        new = b"<ComicInfo><Series>New</Series></ComicInfo>"
        with self._patch_cache_dir(tmp_path / "cache"),              patch("helpers.match_parent_permissions"):
            assert add_comicinfo_to_archive(cbz, new, merge_existing=False)

        with zipfile.ZipFile(cbz) as zf:
            assert zf.read("ComicInfo.xml") == new


# generate_comicinfo_xml moved to core.comicinfo when the two drifting copies
# were merged; its tests live in tests/unit/test_comicinfo_writer.py, which
# also asserts the re-export here is the same object.


class TestSimyanImportTolerance:
    """Regression cover for issue #565.

    Simyan 4.0 removed ``ComicvineResource`` from ``simyan.comicvine``. CLU
    imported it in the same statement as ``Comicvine``, so the ImportError set
    ``SIMYAN_AVAILABLE = False`` and disabled the whole ComicVine integration
    -- surfacing to users as "failed to authenticate" with a valid API key.
    """

    @staticmethod
    def _reload_with(fake_comicvine):
        """Reload models.comicvine against a stand-in ``simyan.comicvine``.

        Returns a snapshot rather than the module: reload mutates the live
        module object in place, so the restoring reload in ``finally`` would
        otherwise overwrite whatever the caller is about to assert on.
        """
        import models.comicvine

        modules = {
            "simyan": MagicMock(),
            "simyan.comicvine": fake_comicvine,
        }
        try:
            with patch.dict(sys.modules, modules):
                mod = importlib.reload(models.comicvine)
                return {
                    "SIMYAN_AVAILABLE": mod.SIMYAN_AVAILABLE,
                    "ComicvineResource": mod.ComicvineResource,
                }
        finally:
            # Restore the module against the real (or absent) simyan so the
            # rest of the suite isn't left holding a reloaded copy.
            importlib.reload(models.comicvine)

    def test_simyan_4x_shape_stays_available(self):
        """Simyan 4.x: Comicvine exists, ComicvineResource does not."""
        snap = self._reload_with(MagicMock(spec=["Comicvine"]))

        assert snap["SIMYAN_AVAILABLE"] is True
        assert snap["ComicvineResource"] is None

    def test_simyan_3x_shape_keeps_the_enum(self):
        """Simyan <= 3.1 still exposes the enum, for the legacy fallback."""
        snap = self._reload_with(MagicMock(spec=["Comicvine", "ComicvineResource"]))

        assert snap["SIMYAN_AVAILABLE"] is True
        assert snap["ComicvineResource"] is not None

    def test_simyan_absent_is_unavailable(self):
        """No simyan at all must still flip the flag off."""
        snap = self._reload_with(None)

        assert snap["SIMYAN_AVAILABLE"] is False
        assert snap["ComicvineResource"] is None


class TestCvSearch:
    """_raw_search bridges Simyan's pre- and post-3.1 search APIs."""

    def test_prefers_per_resource_method(self):
        from models.comicvine import _raw_search

        cv = MagicMock()
        cv.search_volumes.return_value = ["hit"]

        assert _raw_search(cv, "volume", "Batman") == ["hit"]
        cv.search_volumes.assert_called_once_with("Batman")
        cv.search.assert_not_called()

    def test_story_arc_pluralizes(self):
        from models.comicvine import _raw_search

        cv = MagicMock()
        cv.search_story_arcs.return_value = ["arc"]

        assert _raw_search(cv, "story_arc", "Court of Owls") == ["arc"]
        cv.search_story_arcs.assert_called_once_with("Court of Owls")

    @patch("models.comicvine.ComicvineResource")
    def test_falls_back_to_legacy_search(self, mock_resource):
        """Simyan <= 3.0 has only search(resource=...)."""
        from models.comicvine import _raw_search

        cv = MagicMock(spec=["search"])
        cv.search.return_value = ["hit"]

        assert _raw_search(cv, "volume", "Batman") == ["hit"]
        cv.search.assert_called_once_with(
            resource=mock_resource.VOLUME, query="Batman"
        )

    @patch("models.comicvine.ComicvineResource", None)
    def test_raises_when_neither_api_exists(self):
        from models.comicvine import _raw_search

        cv = MagicMock(spec=["get_volume"])

        with pytest.raises(RuntimeError, match="search_volumes"):
            _raw_search(cv, "volume", "Batman")


class TestSearchVolumesIssueCount:
    """Simyan renamed BasicVolume.count_of_issues to issue_count in 3.x."""

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.Comicvine")
    def test_reads_issue_count(self, mock_cv_class):
        from models.comicvine import search_volumes

        mock_cv = MagicMock()
        mock_cv.search_volumes.return_value = [
            make_mock_cv_volume(count_of_issues=77),
        ]
        mock_cv_class.return_value = mock_cv

        results = search_volumes("fake-key", "Batman")
        assert results[0]["count_of_issues"] == 77

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.Comicvine")
    def test_reads_legacy_count_of_issues(self, mock_cv_class):
        from models.comicvine import search_volumes

        mock_cv = MagicMock()
        mock_cv.search_volumes.return_value = [
            make_mock_cv_volume(count_of_issues=77, legacy=True),
        ]
        mock_cv_class.return_value = mock_cv

        results = search_volumes("fake-key", "Batman")
        assert results[0]["count_of_issues"] == 77
