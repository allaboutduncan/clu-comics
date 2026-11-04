"""core/http_client.py: the plain-requests providers' call policy (#704).

These run the real pacer (``real_pacing``) with short intervals, against a
session whose ``request`` is replaced, so nothing leaves the machine.
"""
import threading
import time
from email.utils import formatdate
from unittest.mock import MagicMock, patch

import pytest
import requests

from core.http_client import HttpClient, retry_after_seconds
from core.provider_client import AuthLockout, QuotaLatch

pytestmark = pytest.mark.real_pacing


def _resp(status=200, headers=None, body=b"{}"):
    r = requests.Response()
    r.status_code = status
    r.headers.update(headers or {})
    r._content = body
    r.url = "https://example.test/x"
    r.reason = "x"
    return r


def _client(per_minute=600, responses=None, **kw):
    client = HttpClient("test", "Test", "https://example.test", per_minute, **kw)
    sends = []

    def fake_request(method, url, **kwargs):
        sends.append((time.monotonic(), method, url))
        return responses.pop(0) if responses else _resp()

    client.session = MagicMock()
    client.session.request.side_effect = fake_request
    return client, sends


class TestPacing:
    def test_every_send_takes_its_own_spaced_slot(self):
        """A three-page fetch is three paced sends, not one slot for the lot."""
        client, sends = _client(per_minute=600)  # one send per 0.1s
        for page in range(3):
            assert client.get("/x", params={"page": page}, context="t") is not None
        assert len(sends) == 3
        gaps = [b[0] - a[0] for a, b in zip(sends, sends[1:])]
        assert all(g >= 0.09 for g in gaps), gaps

    def test_concurrent_threads_do_not_send_together(self):
        """The old unlocked class-attribute gap let threads send at once."""
        client, sends = _client(per_minute=600)
        threads = [
            threading.Thread(target=lambda: client.get("/x", context="t"))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        times = sorted(s[0] for s in sends)
        assert len(times) == 4
        assert all(b - a >= 0.09 for a, b in zip(times, times[1:])), times

    def test_spacing_is_shared_by_every_client_of_a_provider(self):
        a, sends_a = _client(per_minute=600)
        b, sends_b = _client(per_minute=600)
        a.get("/x", context="t")
        b.get("/x", context="t")
        assert sends_b[0][0] - sends_a[0][0] >= 0.09


class TestBackOff:
    def test_429_holds_then_retries_without_a_second_sleep(self):
        client, sends = _client(
            per_minute=6000,
            responses=[_resp(429, {"Retry-After": "0.3"}), _resp(200)],
        )
        with patch.object(HttpClient, "_log_retry", autospec=True,
                          side_effect=HttpClient._log_retry) as log_retry:
            resp = client.get("/x", context="t")
        assert resp is not None and resp.status_code == 200
        assert len(sends) == 2
        assert sends[1][0] - sends[0][0] >= 0.29
        # RETRY(0): the pacer waited out Retry-After; call() added no sleep.
        assert log_retry.call_args[0][1] == 0

    def test_the_hold_applies_to_every_thread(self):
        client, _ = _client(per_minute=6000)
        client.pacer.hold_for(0.3)
        other, sends = _client(per_minute=6000)  # same provider name
        start = time.monotonic()
        other.get("/x", context="t")
        assert sends[0][0] - start >= 0.29

    def test_a_long_retry_after_latches_the_quota_and_stops(self):
        quota = QuotaLatch("Test", "hourly")
        client, sends = _client(
            responses=[_resp(429, {"Retry-After": "3600"})], quota=quota,
        )
        assert client.get("/x", context="t", default="D") == "D"
        assert quota.reached()
        assert client.get("/x", context="t", default="D") == "D"
        assert len(sends) == 1
        assert client.pacer.hold_remaining() == 0  # latched, not slept on

    def test_503_is_retried(self):
        client, sends = _client(
            per_minute=6000,
            responses=[_resp(503, {"Retry-After": "0"}), _resp(200)],
        )
        assert client.get("/x", context="t").status_code == 200
        assert len(sends) == 2

    def test_a_live_request_does_not_wait_out_a_long_hold(self):
        from flask import Flask

        client, sends = _client(interactive_wait=0.05)
        client.pacer.hold_for(30)
        start = time.monotonic()
        with Flask("t").test_request_context():
            assert client.get("/x", context="t", default="D") == "D"
        assert time.monotonic() - start < 1
        assert sends == []


class TestRetryAfterParsing:
    def test_seconds(self):
        assert retry_after_seconds(_resp(429, {"Retry-After": "7"})) == 7

    def test_http_date(self):
        when = formatdate(time.time() + 60, usegmt=True)
        assert 55 <= retry_after_seconds(_resp(429, {"Retry-After": when})) <= 61

    def test_mangadex_epoch(self):
        epoch = str(int(time.time()) + 30)
        wait = retry_after_seconds(_resp(429, {"X-RateLimit-Retry-After": epoch}))
        assert 28 <= wait <= 31

    def test_none(self):
        assert retry_after_seconds(_resp(429)) is None


class TestAuth:
    def _lockout(self):
        lockout = MagicMock(spec=AuthLockout)
        lockout.blocked.return_value = False
        return lockout

    def test_401_latches_and_returns_default(self):
        lockout = self._lockout()
        client, _ = _client(
            responses=[_resp(401, {"Content-Type": "application/json"},
                             b'{"detail": "Invalid username/password."}')],
            lockout=lockout,
        )
        assert client.get("/x", context="t", raise_api_errors=True) is None
        lockout.latch.assert_called_once()
        assert lockout.latch.call_args[0][:2] == (401, "Invalid username/password.")

    def test_a_cloudflare_403_is_not_a_rejection(self):
        lockout = self._lockout()
        client, _ = _client(
            responses=[_resp(403, {"Content-Type": "text/html"}, b"<html>")],
            lockout=lockout,
        )
        with pytest.raises(requests.HTTPError):
            client.get("/x", context="t", raise_api_errors=True)
        lockout.latch.assert_not_called()

    def test_a_json_403_is_a_rejection(self):
        lockout = self._lockout()
        client, _ = _client(
            responses=[_resp(403, {"Content-Type": "application/json"})],
            lockout=lockout,
        )
        assert client.get("/x", context="t") is None
        lockout.latch.assert_called_once()

    def test_no_sends_while_locked(self):
        lockout = self._lockout()
        lockout.blocked.return_value = True
        client, sends = _client(lockout=lockout)
        assert client.get("/x", context="t", default="D") == "D"
        assert sends == []

    def test_a_provider_without_a_lockout_treats_401_as_an_error(self):
        client, _ = _client(responses=[_resp(401)])
        with pytest.raises(requests.HTTPError):
            client.get("/x", context="t", raise_api_errors=True)


class TestConnectionErrors:
    def test_connection_error_returns_default_or_raises(self):
        client, _ = _client()
        client.session.request.side_effect = requests.ConnectionError("down")
        assert client.get("/x", context="t", default="D") == "D"
        with pytest.raises(requests.ConnectionError):
            client.get("/x", context="t", raise_api_errors=True)


class TestGcdApiUsesTheSettingsRate:
    def test_spacing_matches_the_displayed_rate_limit(self):
        from models.gcd_api import GCDApiClient
        from models.providers.gcd_api_provider import GCDApiProvider

        client = GCDApiClient("u", "p")
        assert client._http.pacer.limiter.window_seconds == pytest.approx(
            60.0 / GCDApiProvider.rate_limit
        )
        assert client._http.pacer.limiter.max_requests == 1
