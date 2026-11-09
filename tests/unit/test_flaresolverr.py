"""core/flaresolverr.py: solving a Cloudflare challenge with the bundled solver.

FlareSolverr itself is never run here. ``solve`` takes an injected POST, and
``clear_challenge`` is driven with ``solve`` patched.
"""
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import requests

from core import flaresolverr as fs


MIRROR = "https://fs2.comicfiles.ru/2024.07.17/Some%20Comic%20005.cbr"


@pytest.fixture(autouse=True)
def _clean_cache():
    fs.invalidate()
    yield
    fs.invalidate()


def _ok_payload(cookies=None, ua="Mozilla/5.0 (X11; Linux x86_64) Chrome/152.0"):
    return {
        "status": "ok",
        "message": "Challenge solved!",
        "solution": {
            "url": "https://fs2.comicfiles.ru/",
            "status": 200,
            "cookies": cookies if cookies is not None else [
                {"name": "cf_clearance", "value": "abc", "domain": ".comicfiles.ru", "path": "/"},
                {"name": "__cf_bm", "value": "xyz", "domain": ".comicfiles.ru", "path": "/"},
            ],
            "userAgent": ua,
        },
    }


def _post_returning(payload):
    post = MagicMock()
    post.return_value = SimpleNamespace(json=lambda: payload)
    return post


def _clearance(host="fs2.comicfiles.ru", ua="SolverUA/1.0"):
    return fs.Clearance(
        host=host,
        cookies=[{"name": "cf_clearance", "value": "v1", "domain": ".comicfiles.ru", "path": "/"}],
        user_agent=ua,
        expires_at=time.monotonic() + 600,
    )


class TestSolve:
    def test_solves_the_site_root_not_the_archive(self):
        # Pointing Chromium at the .cbr would download the whole comic just to
        # read a cookie. cf_clearance is per site, so the root is enough.
        post = _post_returning(_ok_payload())
        fs.solve(MIRROR, base_url="http://127.0.0.1:8191", post=post)
        url, = post.call_args.args
        body = post.call_args.kwargs["json"]
        assert url == "http://127.0.0.1:8191/v1"
        assert body["cmd"] == "request.get"
        assert body["url"] == "https://fs2.comicfiles.ru/"

    def test_returns_cookies_and_user_agent(self):
        c = fs.solve(MIRROR, base_url="http://x", post=_post_returning(_ok_payload()))
        assert c.host == "fs2.comicfiles.ru"
        assert "cf_clearance" in c.cookie_names()
        assert c.user_agent.startswith("Mozilla/5.0")
        assert c.is_fresh()

    def test_error_status_returns_none(self):
        payload = {"status": "error", "message": "Error solving the challenge. Timeout"}
        assert fs.solve(MIRROR, base_url="http://x", post=_post_returning(payload)) is None

    def test_request_failure_returns_none(self):
        post = MagicMock(side_effect=requests.ConnectionError("refused"))
        assert fs.solve(MIRROR, base_url="http://x", post=post) is None

    def test_non_json_returns_none(self):
        post = MagicMock()
        post.return_value = SimpleNamespace(json=MagicMock(side_effect=ValueError("not json")))
        assert fs.solve(MIRROR, base_url="http://x", post=post) is None

    def test_solver_unavailable_returns_none_without_posting(self):
        post = MagicMock()
        with patch("core.flaresolverr_supervisor.ensure_running", return_value=None):
            assert fs.solve(MIRROR, post=post) is None
        post.assert_not_called()

    def test_timeout_setting_is_passed_to_the_solver(self):
        post = _post_returning(_ok_payload())
        with patch("core.flaresolverr._timeout_seconds", return_value=45):
            fs.solve(MIRROR, base_url="http://x", post=post)
        assert post.call_args.kwargs["json"]["maxTimeout"] == 45000
        assert post.call_args.kwargs["timeout"] > 45


class TestApplyClearance:
    def test_cookies_land_on_the_session(self):
        s = requests.Session()
        fs.apply_clearance(s, _clearance())
        assert s.cookies.get("cf_clearance", domain=".comicfiles.ru") == "v1"

    def test_user_agent_is_replaced_not_merged(self):
        # Cloudflare binds cf_clearance to the UA that earned it. cloudscraper's
        # own UA (kept deliberately in download_getcomics) must lose here.
        s = requests.Session()
        s.headers["User-Agent"] = "cloudscraper-chrome/112"
        fs.apply_clearance(s, _clearance(ua="SolverUA/1.0"))
        assert s.headers["User-Agent"] == "SolverUA/1.0"

    def test_a_cookie_without_domain_falls_back_to_the_host(self):
        s = requests.Session()
        c = _clearance()
        c.cookies = [{"name": "cf_clearance", "value": "v2"}]
        fs.apply_clearance(s, c)
        assert s.cookies.get("cf_clearance", domain="fs2.comicfiles.ru") == "v2"


@pytest.fixture
def available():
    with patch("core.flaresolverr.is_available", return_value=True):
        yield


class TestClearChallenge:
    def test_disabled_or_missing_does_nothing(self):
        with patch("core.flaresolverr.is_available", return_value=False), \
                patch("core.flaresolverr.solve") as solve:
            assert fs.clear_challenge(MIRROR, requests.Session()) is False
        solve.assert_not_called()

    def test_solves_caches_and_applies(self, available):
        s = requests.Session()
        with patch("core.flaresolverr.solve", return_value=_clearance()) as solve:
            assert fs.clear_challenge(MIRROR, s) is True
        solve.assert_called_once()
        assert s.headers["User-Agent"] == "SolverUA/1.0"
        assert fs.cached_clearance("fs2.comicfiles.ru") is not None

    def test_second_session_reuses_the_cached_clearance(self, available):
        with patch("core.flaresolverr.solve", return_value=_clearance()) as solve:
            fs.clear_challenge(MIRROR, requests.Session())
            other = requests.Session()
            assert fs.clear_challenge(MIRROR, other) is True
        solve.assert_called_once()
        assert other.headers["User-Agent"] == "SolverUA/1.0"

    def test_a_clearance_that_already_failed_is_solved_again(self, available):
        # The session already carries the cached clearance and was challenged
        # anyway. Handing it the same cookie again would loop.
        s = requests.Session()
        first, second = _clearance(ua="UA-1"), _clearance(ua="UA-2")
        with patch("core.flaresolverr.solve", side_effect=[first, second]) as solve:
            fs.clear_challenge(MIRROR, s)
            assert fs.clear_challenge(MIRROR, s) is True
        assert solve.call_count == 2
        assert s.headers["User-Agent"] == "UA-2"

    def test_an_expired_clearance_is_not_reused(self, available):
        stale = _clearance()
        stale.expires_at = time.monotonic() - 1
        with patch("core.flaresolverr.solve", side_effect=[stale, _clearance(ua="fresh")]) as solve:
            fs.clear_challenge(MIRROR, requests.Session())
            fs.clear_challenge(MIRROR, requests.Session())
        assert solve.call_count == 2

    def test_a_failed_solve_is_not_cached(self, available):
        with patch("core.flaresolverr.solve", return_value=None):
            assert fs.clear_challenge(MIRROR, requests.Session()) is False
        assert fs.cached_clearance("fs2.comicfiles.ru") is None

    def test_concurrent_callers_for_one_host_share_one_solve(self, available):
        # Three download workers hitting the same mirror must not start three
        # browsers.
        calls = []
        gate = threading.Event()

        def slow_solve(url):
            calls.append(url)
            gate.wait(2)
            return _clearance()

        results = []
        with patch("core.flaresolverr.solve", side_effect=slow_solve):
            threads = [threading.Thread(target=lambda: results.append(
                fs.clear_challenge(MIRROR, requests.Session()))) for _ in range(3)]
            for t in threads:
                t.start()
            time.sleep(0.2)
            gate.set()
            for t in threads:
                t.join(5)
        assert len(calls) == 1
        assert results == [True, True, True]

    def test_different_hosts_are_solved_separately(self, available):
        with patch("core.flaresolverr.solve", side_effect=[
                _clearance(host="fs2.comicfiles.ru"), _clearance(host="getcomics.org")]) as solve:
            fs.clear_challenge(MIRROR, requests.Session())
            fs.clear_challenge("https://getcomics.org/other-comics/x/", requests.Session())
        assert solve.call_count == 2

    def test_never_raises(self, available):
        with patch("core.flaresolverr.solve", side_effect=RuntimeError("boom")):
            assert fs.clear_challenge(MIRROR, requests.Session()) is False

    def test_url_without_host_is_ignored(self, available):
        with patch("core.flaresolverr.solve") as solve:
            assert fs.clear_challenge("not a url", requests.Session()) is False
        solve.assert_not_called()


class TestEnabledSetting:
    def test_on_by_default(self):
        from core.settings import SETTINGS_SCHEMA
        assert SETTINGS_SCHEMA["FLARESOLVERR_ENABLED"].default is True

    def test_read_failure_means_off(self):
        with patch("core.settings.get_bool", side_effect=RuntimeError("db locked")):
            assert fs.is_enabled() is False

    def test_available_needs_the_install(self):
        with patch("core.flaresolverr.is_enabled", return_value=True), \
                patch("core.flaresolverr_supervisor.is_installed", return_value=False):
            assert fs.is_available() is False


class TestTestSolver:
    def test_not_installed(self):
        with patch("core.flaresolverr_supervisor.is_installed", return_value=False):
            ok, msg = fs.test_solver()
        assert ok is False and "not installed" in msg

    def test_did_not_start(self):
        with patch("core.flaresolverr_supervisor.is_installed", return_value=True), \
                patch("core.flaresolverr_supervisor.ensure_running", return_value=None):
            ok, msg = fs.test_solver()
        assert ok is False and "did not start" in msg

    def test_ready_reports_version(self):
        resp = SimpleNamespace(json=lambda: {"msg": "FlareSolverr is ready!", "version": "3.5.2"})
        with patch("core.flaresolverr_supervisor.is_installed", return_value=True), \
                patch("core.flaresolverr_supervisor.ensure_running", return_value="http://127.0.0.1:8191"), \
                patch("core.flaresolverr.requests.get", return_value=resp):
            ok, msg = fs.test_solver()
        assert ok is True and "3.5.2" in msg


class TestUsageLogging:
    """Each use logs one DEBUG line carrying running totals, so the log shows
    how often the solver runs and whether it helps."""

    @pytest.fixture(autouse=True)
    def _fresh_counters(self):
        fs.reset_usage()
        yield
        fs.reset_usage()

    def _debug_lines(self, debug):
        return [c.args[0] for c in debug.call_args_list]

    def test_a_solve_is_counted_with_its_context(self, available):
        with patch("core.flaresolverr.solve", return_value=_clearance()), \
                patch.object(fs.app_logger, "debug") as debug:
            fs.clear_challenge(MIRROR, requests.Session(), context="download")
        line, = self._debug_lines(debug)
        assert "[download] fs2.comicfiles.ru: solved" in line
        assert "solved=1" in line
        assert fs.usage_stats()["solved"] == 1

    def test_cache_hit_and_resolve_are_told_apart(self, available):
        s = requests.Session()
        with patch("core.flaresolverr.solve", side_effect=[_clearance(), _clearance()]):
            fs.clear_challenge(MIRROR, s, context="download")               # solved
            fs.clear_challenge(MIRROR, requests.Session(), context="page")  # cache hit
            fs.clear_challenge(MIRROR, s, context="download")               # that clearance failed
        stats = fs.usage_stats()
        assert (stats["solved"], stats["cache_hit"], stats["resolved_again"]) == (1, 1, 1)

    def test_failure_is_counted(self, available):
        with patch("core.flaresolverr.solve", return_value=None):
            fs.clear_challenge(MIRROR, requests.Session(), context="resolve")
        assert fs.usage_stats()["solve_failed"] == 1

    def test_unexpected_error_is_counted_as_a_failure(self, available):
        with patch("core.flaresolverr.solve", side_effect=RuntimeError("boom")):
            fs.clear_challenge(MIRROR, requests.Session())
        assert fs.usage_stats()["solve_failed"] == 1

    def test_challenge_met_while_disabled_is_counted_as_skipped(self):
        # Answers "how much would it be used" on an install that switched it off.
        with patch("core.flaresolverr.is_available", return_value=False), \
                patch("core.flaresolverr.is_enabled", return_value=False), \
                patch.object(fs.app_logger, "debug") as debug:
            fs.clear_challenge(MIRROR, requests.Session(), context="search")
        line, = self._debug_lines(debug)
        assert "skipped (disabled in Settings)" in line
        assert fs.usage_stats()["skipped"] == 1

    def test_not_installed_is_named(self):
        with patch("core.flaresolverr.is_available", return_value=False), \
                patch("core.flaresolverr.is_enabled", return_value=True), \
                patch.object(fs.app_logger, "debug") as debug:
            fs.clear_challenge(MIRROR, requests.Session())
        assert "not installed in this build" in self._debug_lines(debug)[0]

    def test_still_challenged_is_counted(self):
        with patch.object(fs.app_logger, "debug") as debug:
            fs.note_still_challenged(MIRROR, "download")
        assert "[download] fs2.comicfiles.ru: still_challenged" in self._debug_lines(debug)[0]
        assert fs.usage_stats()["still_challenged"] == 1

    def test_usage_lines_are_debug_not_info(self, available):
        # The logging budget (CLAUDE.md): per-event lines are DEBUG. The one
        # INFO line per real solve comes from solve() itself.
        with patch("core.flaresolverr.solve", return_value=_clearance()), \
                patch.object(fs.app_logger, "info") as info:
            for _ in range(5):
                fs.clear_challenge(MIRROR, requests.Session())
        info.assert_not_called()
