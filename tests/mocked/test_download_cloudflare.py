"""Tests for Cloudflare-challenge detection in the getcomics downloader.

`comicfiles.ru` and similar getcomics mirrors sit behind a Cloudflare managed
challenge that no automated HTTP client can bypass. `is_cloudflare_challenge`
lets `download_getcomics` recognize that case, stop retrying, and surface a
clear "download manually" error instead of the old `... after 3 attempts: None`.

Lives in `core.download_utils` (imported by api.py) so it can be tested without
triggering api.py's import-time side effects (worker threads, DB, cloudscraper).
"""
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.download_utils import (
    SharedScraper,
    close_quietly,
    is_cloudflare_challenge,
    issue_number_to_int,
    replace_session,
)


def _resp(status=403, headers=None, content=b""):
    """Minimal stand-in for a requests.Response (only the bits we read)."""
    return SimpleNamespace(status_code=status, headers=headers or {}, content=content)


class TestIsCloudflareChallenge:
    def test_detects_cf_mitigated_header(self):
        resp = _resp(headers={"cf-mitigated": "challenge", "Server": "cloudflare"})
        assert is_cloudflare_challenge(resp) is True

    def test_detects_just_a_moment_body(self):
        resp = _resp(
            headers={"Server": "cloudflare", "Content-Type": "text/html; charset=UTF-8"},
            content=b"<!DOCTYPE html><html><head><title>Just a moment...</title>",
        )
        assert is_cloudflare_challenge(resp) is True

    def test_detects_challenge_platform_marker(self):
        resp = _resp(
            headers={"Server": "cloudflare", "Content-Type": "text/html"},
            content=b"<script>window.__cf_chl_opt = {}; challenge-platform</script>",
        )
        assert is_cloudflare_challenge(resp) is True

    def test_non_cloudflare_403_is_not_challenge(self):
        resp = _resp(headers={"Server": "nginx", "Content-Type": "text/html"},
                     content=b"<html>Forbidden</html>")
        assert is_cloudflare_challenge(resp) is False

    def test_cloudflare_non_html_is_not_challenge(self):
        # A genuine file served through Cloudflare (e.g. the real download) must
        # not be mistaken for a challenge page.
        resp = _resp(status=200,
                     headers={"Server": "cloudflare", "Content-Type": "application/x-cbr"},
                     content=b"Rar!\x1a\x07\x00")
        assert is_cloudflare_challenge(resp) is False

    def test_missing_headers_do_not_raise(self):
        assert is_cloudflare_challenge(_resp(headers={})) is False


class TestReplaceSession:
    """Regression: a scraper replaced after a Cloudflare challenge was never
    closed. A dropped cloudscraper session is not reclaimed by garbage
    collection, so each one pinned an open TLS connection (CLOSE_WAIT) and its
    SSL context for the life of the worker -- ~1 MB per challenge."""

    def test_returns_the_new_session_and_closes_the_old(self):
        old, new = MagicMock(), MagicMock()
        assert replace_session(old, lambda: new) is new
        old.close.assert_called_once()
        new.close.assert_not_called()

    def test_new_session_is_built_before_the_old_is_closed(self):
        # If building the replacement fails, the old session must still be usable.
        order = []
        old = MagicMock()
        old.close.side_effect = lambda: order.append("close")

        def make():
            order.append("make")
            return MagicMock()

        replace_session(old, make)
        assert order == ["make", "close"]

    def test_failing_close_does_not_abort_the_retry(self):
        old, new = MagicMock(), MagicMock()
        old.close.side_effect = RuntimeError("boom")
        assert replace_session(old, lambda: new) is new


class TestCloseQuietly:
    """Every close in the download paths is bookkeeping on a session the caller
    is already done with, so a raising close() must never take the caller's
    result (or its retry) with it."""

    def test_closes_the_session(self):
        session = MagicMock()
        close_quietly(session)
        session.close.assert_called_once()

    def test_a_raising_close_is_swallowed(self):
        session = MagicMock()
        session.close.side_effect = RuntimeError("socket already gone")
        close_quietly(session)  # must not raise


class TestSharedScraper:
    """Regression: the shared getcomics scraper was a module global that a
    challenged thread rebound unconditionally. Three download workers resolve
    URLs at once and one stale clearance token challenges all of them, so two
    threads would each build a replacement and publish it -- and whichever lost
    the write was dropped without being closed, reinstating the very leak the
    swap exists to avoid, under exactly the concurrency that makes swapping
    necessary."""

    def _shared(self):
        made = []

        def _make():
            session = MagicMock(name=f"session{len(made)}")
            made.append(session)
            return session

        return SharedScraper(_make), made

    def test_builds_one_session_up_front(self):
        shared, made = self._shared()
        assert len(made) == 1
        assert shared.current is made[0]

    def test_retiring_the_current_session_swaps_and_closes_it(self):
        shared, made = self._shared()
        first = shared.current

        assert shared.retire(first) is made[1]
        assert shared.current is made[1]
        first.close.assert_called_once()

    def test_a_second_thread_adopts_the_replacement_instead_of_building_one(self):
        # Both threads were challenged on `first`; the second one to notice must
        # not publish a session of its own over the first one's.
        shared, made = self._shared()
        first = shared.current

        winner = shared.retire(first)
        loser = shared.retire(first)

        assert loser is winner
        assert len(made) == 2, "a lost race must not build a second replacement"
        assert shared.current is winner

    def test_a_session_that_lost_the_race_is_never_left_unclosed(self):
        shared, made = self._shared()
        first = shared.current

        shared.retire(first)
        shared.retire(first)

        unclosed = [s for s in made if s is not shared.current and not s.close.called]
        assert unclosed == [], "every retired session must be closed"

    def test_retiring_twice_over_does_not_close_the_live_session(self):
        shared, made = self._shared()

        shared.retire(shared.current)
        shared.retire(made[0])  # a straggler, still holding the original

        shared.current.close.assert_not_called()

    def test_concurrent_retirement_of_the_same_session_builds_one_replacement(self):
        shared, made = self._shared()
        first = shared.current
        start = threading.Barrier(8)
        seen = []

        def _retire():
            start.wait(timeout=5)
            seen.append(shared.retire(first))

        threads = [threading.Thread(target=_retire) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(made) == 2, "eight threads, one poisoned token, one replacement"
        assert set(id(s) for s in seen) == {id(shared.current)}
        first.close.assert_called_once()

    def test_a_failing_close_does_not_block_the_swap(self):
        shared, made = self._shared()
        first = shared.current
        first.close.side_effect = RuntimeError("socket already gone")

        assert shared.retire(first) is made[1]
        assert shared.current is made[1]


class TestIssueNumberToInt:
    """Regression: a #0 issue (or empty/non-numeric number) used to raise
    `invalid literal for int() with base 10: ''` in the auto-download range
    check and abort the entire run."""

    @pytest.mark.parametrize("value,expected", [
        ("1", 1),
        ("12", 12),
        ("007", 7),
        ("0", 0),        # bare zero must NOT blow up int('') after lstrip('0')
        ("00", 0),
        (0, 0),
        (5, 5),
    ])
    def test_parses_whole_numbers(self, value, expected):
        assert issue_number_to_int(value) == expected

    @pytest.mark.parametrize("value", [
        "",              # missing issue number
        "   ",
        None,
        "1.MU",          # point-one / marketing issues
        "½",
        "Annual",
        "1.5",
    ])
    def test_non_whole_numbers_return_none(self, value):
        assert issue_number_to_int(value) is None
