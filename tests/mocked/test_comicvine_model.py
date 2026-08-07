"""Tests for models/comicvine.py -- mocked Simyan library."""
import threading

import pytest
from unittest.mock import patch, MagicMock
from tests.mocked.conftest import make_mock_cv_volume, make_mock_cv_issue


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
            assert cv.get_cv_client("KEY") is first
            cv.invalidate_cv_client_cache()
            first._session.close.assert_called_once()
            assert cv.get_cv_client("KEY") is second

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
            if client is not results[0]:
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


class TestProactiveThrottle:
    """Reactive backoff alone means we only slow down after ComicVine has
    already rejected us. Every call must take a slot from the shared budget
    first, so concurrent sweeps and web requests pace each other."""

    def test_every_attempt_takes_a_slot(self):
        import models.comicvine as cv

        with patch.object(cv._comicvine_rate_limiter, "acquire", return_value=True) as acq:
            cv._cv_call_with_retry(lambda: "ok", "test")

        assert acq.call_count == 1

    @patch("models.comicvine.time.sleep")
    @patch("models.comicvine._cv_retry_config", return_value=(2, 0.0))
    def test_retries_also_take_a_slot(self, mock_cfg, mock_sleep):
        import models.comicvine as cv

        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise Exception("Rate limit exceeded. Slow down cowboy.")
            return "ok"

        with patch.object(cv._comicvine_rate_limiter, "acquire", return_value=True) as acq:
            assert cv._cv_call_with_retry(flaky, "test") == "ok"

        assert acq.call_count == 3

    def test_background_callers_wait_indefinitely(self):
        """A sweep on a daemon thread should ride out the budget, not skip it."""
        import models.comicvine as cv

        with patch.object(cv._comicvine_rate_limiter, "acquire", return_value=True) as acq:
            cv._cv_call_with_retry(lambda: "ok", "test", blocking=True)

        assert acq.call_args.kwargs["timeout"] is None

    def test_interactive_callers_use_a_bounded_wait(self):
        import models.comicvine as cv

        with patch.object(cv._comicvine_rate_limiter, "acquire", return_value=True) as acq:
            cv._cv_call_with_retry(lambda: "ok", "test", blocking=False)

        timeout = acq.call_args.kwargs["timeout"]
        assert timeout is not None and timeout > 0

    def test_saturated_throttle_still_lets_an_interactive_call_through(self):
        """Better a slow answer than a held request thread -- the reactive
        retry below still covers the call."""
        import models.comicvine as cv

        with patch.object(cv._comicvine_rate_limiter, "acquire", return_value=False):
            assert cv._cv_call_with_retry(lambda: "ok", "test", blocking=False) == "ok"

    def test_defaults_to_blocking_outside_a_request_context(self):
        import models.comicvine as cv

        with patch.object(cv._comicvine_rate_limiter, "acquire", return_value=True) as acq:
            cv._cv_call_with_retry(lambda: "ok", "test")

        assert acq.call_args.kwargs["timeout"] is None

    def test_defaults_to_bounded_wait_inside_a_request_context(self):
        """Auto-detected so no call site has to declare itself: a live web
        request is a thread a user is waiting on."""
        import models.comicvine as cv

        with patch("flask.has_request_context", return_value=True):
            with patch.object(
                cv._comicvine_rate_limiter, "acquire", return_value=True
            ) as acq:
                cv._cv_call_with_retry(lambda: "ok", "test")

        assert acq.call_args.kwargs["timeout"] is not None

    def test_hourly_limit_sits_below_comicvines_own_budget(self):
        """The shared limiter has to trip before Simyan's per_hour=200 bucket,
        so the wait lands somewhere we log and bound."""
        import models.comicvine as cv

        assert cv._cv_hourly_limit() < 200


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
        from models.comicvine import _cv_call_with_retry

        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise Exception("Rate limit exceeded. Slow down cowboy.")
            return "ok"

        assert _cv_call_with_retry(flaky, "test") == "ok"
        assert calls["n"] == 3
        assert mock_sleep.call_count == 2  # backed off before each retry

    @patch("models.comicvine.time.sleep")
    def test_non_rate_limit_error_not_retried(self, mock_sleep):
        from models.comicvine import _cv_call_with_retry

        calls = {"n": 0}

        def boom():
            calls["n"] += 1
            raise ValueError("something else")

        with pytest.raises(ValueError):
            _cv_call_with_retry(boom, "test")
        assert calls["n"] == 1  # raised immediately, no retry
        mock_sleep.assert_not_called()

    @patch("models.comicvine.time.sleep")
    @patch("models.comicvine._cv_retry_config", return_value=(2, 0.0))
    def test_gives_up_after_retries(self, mock_cfg, mock_sleep):
        from models.comicvine import _cv_call_with_retry

        calls = {"n": 0}

        def always():
            calls["n"] += 1
            raise Exception("Rate limit exceeded. Slow down cowboy.")

        with pytest.raises(Exception, match="Rate limit"):
            _cv_call_with_retry(always, "test")
        assert calls["n"] == 3  # first attempt + 2 retries

    @patch("models.comicvine.time.sleep")
    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource", create=True)
    @patch("models.comicvine.Comicvine", create=True)
    def test_search_volumes_retries_rate_limit(self, mock_cv_class, mock_resource, mock_sleep):
        from models.comicvine import search_volumes

        mock_cv = MagicMock()
        mock_cv.search.side_effect = [
            Exception("Rate limit exceeded. Slow down cowboy."),
            [make_mock_cv_volume(id=4050, name="Batman", start_year=2016)],
        ]
        mock_cv_class.return_value = mock_cv

        results = search_volumes("fake-key", "Batman")
        assert len(results) == 1
        assert results[0]["id"] == 4050
        assert mock_cv.search.call_count == 2  # retried once, then succeeded


class TestSearchVolumes:

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource", create=True)
    @patch("models.comicvine.Comicvine", create=True)
    def test_returns_volumes(self, mock_cv_class, mock_resource):
        from models.comicvine import search_volumes

        mock_cv = MagicMock()
        mock_cv.search.return_value = [
            make_mock_cv_volume(id=4050, name="Batman", start_year=2016),
        ]
        mock_cv_class.return_value = mock_cv

        results = search_volumes("fake-key", "Batman")
        assert len(results) == 1
        assert results[0]["name"] == "Batman"
        assert results[0]["id"] == 4050

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource", create=True)
    @patch("models.comicvine.Comicvine", create=True)
    def test_no_results(self, mock_cv_class, mock_resource):
        from models.comicvine import search_volumes

        mock_cv = MagicMock()
        mock_cv.search.return_value = []
        mock_cv_class.return_value = mock_cv

        assert search_volumes("fake-key", "Nonexistent") == []

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource", create=True)
    @patch("models.comicvine.Comicvine", create=True)
    def test_year_ranking(self, mock_cv_class, mock_resource):
        from models.comicvine import search_volumes

        mock_cv = MagicMock()
        mock_cv.search.return_value = [
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
    @patch("models.comicvine.ComicvineResource", create=True)
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
    @patch("models.comicvine.ComicvineResource", create=True)
    @patch("models.comicvine.Comicvine", create=True)
    def test_issue_not_found(self, mock_cv_class, mock_resource):
        from models.comicvine import get_issue_by_number

        mock_cv = MagicMock()
        mock_cv.list_issues.return_value = []
        mock_cv_class.return_value = mock_cv

        assert get_issue_by_number("fake-key", 4050, "999") is None


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
    @patch("models.comicvine.ComicvineResource", create=True)
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
    @patch("models.comicvine.ComicvineResource", create=True)
    @patch("models.comicvine.Comicvine", create=True)
    def test_publisher_falls_back_to_issue_volume(self, mock_cv_class, mock_resource):
        """With no kwarg, Publisher must still resolve from issue.volume.publisher."""
        from models.comicvine import get_metadata_by_volume_id

        self._wire_cv(mock_cv_class, issue_publisher="DC Comics")

        result = get_metadata_by_volume_id("fake-key", 4050, "5")
        assert result is not None
        assert result["Publisher"] == "DC Comics"

    @patch("models.comicvine.SIMYAN_AVAILABLE", True)
    @patch("models.comicvine.ComicvineResource", create=True)
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
    @patch("models.comicvine.ComicvineResource", create=True)
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


class TestGenerateComicInfoXml:

    def test_generates_valid_xml(self):
        from models.comicvine import generate_comicinfo_xml

        data = {
            "Series": "Batman",
            "Number": "1",
            "Title": "Rebirth",
            "Year": 2020,
            "Publisher": "DC Comics",
        }

        xml_bytes = generate_comicinfo_xml(data)
        assert isinstance(xml_bytes, bytes)
        assert b"<Series>Batman</Series>" in xml_bytes
        assert b"<Number>1</Number>" in xml_bytes
        assert b"<Publisher>DC Comics</Publisher>" in xml_bytes

    def test_decimal_issue_number_preserved(self):
        """Decimal issue numbers like 12.1 should not be truncated to 12."""
        from models.comicvine import generate_comicinfo_xml

        data = {"Series": "Avengers", "Number": "12.1", "Year": 2011}
        xml_bytes = generate_comicinfo_xml(data)
        assert b"<Number>12.1</Number>" in xml_bytes

    def test_decimal_issue_preserves_leading_zeros(self):
        """012.1 should stay '012.1', not be stripped to '12.1' via float()."""
        from models.comicvine import generate_comicinfo_xml

        data = {"Series": "Avengers", "Number": "012.1", "Year": 2011}
        xml_bytes = generate_comicinfo_xml(data)
        assert b"<Number>012.1</Number>" in xml_bytes

    def test_whole_number_drops_decimal(self):
        """12.0 should be stored as '12', not '12.0'."""
        from models.comicvine import generate_comicinfo_xml

        data = {"Series": "Batman", "Number": "12.0"}
        xml_bytes = generate_comicinfo_xml(data)
        assert b"<Number>12</Number>" in xml_bytes

    def test_omits_none_values(self):
        from models.comicvine import generate_comicinfo_xml

        data = {"Series": "Test", "Writer": None, "Publisher": None}
        xml_bytes = generate_comicinfo_xml(data)
        assert b"<Writer>" not in xml_bytes
        assert b"<Publisher>" not in xml_bytes
