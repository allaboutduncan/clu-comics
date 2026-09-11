"""Tests for Cloudflare-challenge detection in the getcomics downloader.

`comicfiles.ru` and similar getcomics mirrors sit behind a Cloudflare managed
challenge that no automated HTTP client can bypass. `is_cloudflare_challenge`
lets `download_getcomics` recognize that case, stop retrying, and surface a
clear "download manually" error instead of the old `... after 3 attempts: None`.

Lives in `core.download_utils` (imported by api.py) so it can be tested without
triggering api.py's import-time side effects (worker threads, DB, cloudscraper).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.download_utils import (
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
