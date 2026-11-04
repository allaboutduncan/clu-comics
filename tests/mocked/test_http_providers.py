"""MangaDex, MangaUpdates, Bedetheque and AniList send through HttpClient (#704).

Each used to keep its own gap in an unlocked class attribute (AniList had
none), so concurrent threads could send together. Now every request is one
paced send on a session shared by the provider, at the rate Settings shows.
"""
from unittest.mock import MagicMock, patch

import pytest
import requests

from core.version import CLU_USER_AGENT
from models.providers.anilist_provider import AniListProvider
from models.providers.bedetheque_provider import BedethequeProvider
from models.providers.mangadex_provider import MangaDexProvider
from models.providers.mangaupdates_provider import MangaUpdatesProvider

PROVIDERS = [MangaDexProvider, MangaUpdatesProvider, BedethequeProvider, AniListProvider]


def _resp(status=200, headers=None, body=b'{"data": {}}', content_type="application/json"):
    r = requests.Response()
    r.status_code = status
    r.headers.update({"Content-Type": content_type, **(headers or {})})
    r._content = body
    r.url = "https://example.test/"
    r.reason = "x"
    return r


@pytest.mark.parametrize("provider", PROVIDERS, ids=lambda p: p.__name__)
class TestEveryHttpProvider:

    def test_spacing_is_the_rate_settings_shows(self, provider):
        limiter = provider._http().pacer.limiter
        assert limiter.max_requests == 1
        assert limiter.window_seconds == pytest.approx(60.0 / provider.rate_limit)

    def test_identifies_as_clu_not_a_browser(self, provider):
        ua = provider._http().session.headers["User-Agent"]
        assert ua == CLU_USER_AGENT
        assert "Mozilla" not in ua and "ComicUtils" not in ua

    def test_one_shared_client_for_every_instance(self, provider):
        assert provider()._http() is provider()._http()

    def test_no_class_attribute_pacing_left(self, provider):
        assert not hasattr(provider, "_last_request_time")


def test_displayed_rates():
    """The numbers the user agreed in #704; Settings shows exactly these."""
    assert MangaDexProvider.rate_limit == 180
    assert MangaUpdatesProvider.rate_limit == 30
    assert BedethequeProvider.rate_limit == 30
    assert AniListProvider.rate_limit == 30


class TestMangaDex:

    @patch("requests.Session.request")
    def test_a_429_is_held_and_retried(self, mock_request):
        mock_request.side_effect = [
            _resp(429, {"X-RateLimit-Retry-After": "0"}),
            _resp(200, body=b'{"result": "ok", "data": []}'),
        ]
        data = MangaDexProvider()._make_request("GET", "/manga", {"limit": 1})
        assert data == {"result": "ok", "data": []}
        assert mock_request.call_count == 2
        method, url = mock_request.call_args[0][:2]
        assert (method, url) == ("GET", "https://api.mangadex.org/manga")

    @patch("requests.Session.request")
    def test_an_error_returns_none(self, mock_request):
        mock_request.return_value = _resp(404)
        assert MangaDexProvider()._make_request("GET", "/manga/x") is None

    @patch("requests.Session.request")
    def test_invalid_json_returns_none(self, mock_request):
        mock_request.return_value = _resp(200, body=b"<html>")
        assert MangaDexProvider()._make_request("GET", "/manga") is None


class TestMangaUpdates:

    @patch("requests.Session.request")
    def test_posts_json(self, mock_request):
        mock_request.return_value = _resp(200, body=b'{"results": []}')
        assert MangaUpdatesProvider()._make_request(
            "POST", "/series/search", {"search": "x"}) == {"results": []}
        assert mock_request.call_args[1]["json"] == {"search": "x"}
        assert mock_request.call_args[0][1] == "https://api.mangaupdates.com/v1/series/search"


class TestBedetheque:

    @patch("requests.Session.request")
    def test_returns_the_page_html(self, mock_request):
        mock_request.return_value = _resp(200, body=b"<html>ok</html>", content_type="text/html")
        url = "https://www.bedetheque.com/serie-1.html"
        assert BedethequeProvider()._make_request(url) == "<html>ok</html>"
        assert mock_request.call_args[0][:2] == ("GET", url)

    @patch("requests.Session.request")
    def test_a_503_is_retried(self, mock_request):
        mock_request.side_effect = [
            _resp(503, {"Retry-After": "0"}, content_type="text/html"),
            _resp(200, body=b"<html/>", content_type="text/html"),
        ]
        assert BedethequeProvider()._make_request(BedethequeProvider.BASE_URL) == "<html/>"
        assert mock_request.call_count == 2

    @patch("requests.Session.request")
    def test_a_failure_returns_none(self, mock_request):
        mock_request.side_effect = requests.ConnectionError("down")
        assert BedethequeProvider()._make_request(BedethequeProvider.BASE_URL) is None


class TestAniList:

    @patch("requests.Session.request")
    def test_a_token_travels_with_the_request_not_the_shared_session(self, mock_request):
        from models.providers.base import ProviderCredentials

        mock_request.return_value = _resp(200, body=b'{"data": {"Page": {}}}')
        p = AniListProvider(ProviderCredentials(api_key="tok"))
        assert p._make_request("query { x }") == {"Page": {}}
        assert mock_request.call_args[1]["headers"] == {"Authorization": "Bearer tok"}
        assert "Authorization" not in AniListProvider._http().session.headers

    @patch("requests.Session.request")
    def test_graphql_errors_return_none(self, mock_request):
        mock_request.return_value = _resp(200, body=b'{"errors": [{"message": "x"}]}')
        assert AniListProvider()._make_request("query { x }") is None

    @patch("requests.Session.request")
    def test_a_429_is_held_and_retried(self, mock_request):
        mock_request.side_effect = [
            _resp(429, {"Retry-After": "0"}),
            _resp(200, body=b'{"data": {"ok": 1}}'),
        ]
        assert AniListProvider()._make_request("query { x }") == {"ok": 1}
