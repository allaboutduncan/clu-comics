"""Tests for the GCD REST API client (models/gcd_api.py)."""
import pytest
from unittest.mock import patch, MagicMock


class TestGCDApiClient:

    def _make_client(self):
        from models.gcd_api import GCDApiClient
        return GCDApiClient("testuser", "testpass")

    def test_init_sets_auth(self):
        client = self._make_client()
        assert client.session.auth is not None

    @patch("models.gcd_api.requests.Session")
    def test_search_series_url_no_year(self, mock_session_cls):
        from models.gcd_api import GCDApiClient
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"count": 1, "next": None, "results": [{"name": "Batman"}]}
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_session_cls.return_value = mock_session

        client = GCDApiClient("user", "pass")
        results = client.search_series("Batman")

        call_args = mock_session.get.call_args
        assert "/series/name/Batman/" in call_args[0][0]
        assert len(results) == 1
        assert results[0]["name"] == "Batman"

    @patch("models.gcd_api.requests.Session")
    def test_search_series_url_with_year(self, mock_session_cls):
        from models.gcd_api import GCDApiClient
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"count": 0, "next": None, "results": []}
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_session_cls.return_value = mock_session

        client = GCDApiClient("user", "pass")
        client.search_series("Batman", year=2016)

        call_args = mock_session.get.call_args
        assert "/series/name/Batman/year/2016/" in call_args[0][0]

    @patch("models.gcd_api.requests.Session")
    def test_get_series_url(self, mock_session_cls):
        from models.gcd_api import GCDApiClient
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": 123, "name": "Batman"}
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_session_cls.return_value = mock_session

        client = GCDApiClient("user", "pass")
        result = client.get_series(123)

        call_args = mock_session.get.call_args
        assert "/series/123/" in call_args[0][0]
        assert result["name"] == "Batman"

    @patch("models.gcd_api.requests.Session")
    def test_get_issue_url(self, mock_session_cls):
        from models.gcd_api import GCDApiClient
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": 456, "descriptor": "1"}
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_session_cls.return_value = mock_session

        client = GCDApiClient("user", "pass")
        result = client.get_issue(456)

        call_args = mock_session.get.call_args
        assert "/issue/456/" in call_args[0][0]
        assert result["descriptor"] == "1"

    @patch("models.gcd_api.requests.Session")
    def test_search_issue_url(self, mock_session_cls):
        from models.gcd_api import GCDApiClient
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"count": 0, "next": None, "results": []}
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_session_cls.return_value = mock_session

        client = GCDApiClient("user", "pass")
        client.search_issue("Batman", "42")

        call_args = mock_session.get.call_args
        assert "/series/name/Batman/issue/42/" in call_args[0][0]

    @patch("models.gcd_api.requests.Session")
    def test_search_issue_url_with_year(self, mock_session_cls):
        from models.gcd_api import GCDApiClient
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"count": 0, "next": None, "results": []}
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_session_cls.return_value = mock_session

        client = GCDApiClient("user", "pass")
        client.search_issue("Batman", "42", year=2016)

        call_args = mock_session.get.call_args
        assert "/series/name/Batman/issue/42/year/2016/" in call_args[0][0]

    @patch("models.gcd_api.requests.Session")
    def test_pagination_follows_next(self, mock_session_cls):
        from models.gcd_api import GCDApiClient
        mock_session = MagicMock()

        page1 = MagicMock()
        page1.json.return_value = {
            "count": 2,
            "next": "https://www.comics.org/api/series/name/Batman/?page=2",
            "results": [{"name": "Batman (1940)"}]
        }
        page1.raise_for_status = MagicMock()

        page2 = MagicMock()
        page2.json.return_value = {
            "count": 2,
            "next": None,
            "results": [{"name": "Batman (2016)"}]
        }
        page2.raise_for_status = MagicMock()

        mock_session.get.side_effect = [page1, page2]
        mock_session_cls.return_value = mock_session

        client = GCDApiClient("user", "pass")
        results = client.search_series("Batman")

        assert len(results) == 2
        assert results[0]["name"] == "Batman (1940)"
        assert results[1]["name"] == "Batman (2016)"
        assert mock_session.get.call_count == 2

    @patch("models.gcd_api.requests.Session")
    def test_pagination_stops_at_max_pages(self, mock_session_cls):
        from models.gcd_api import GCDApiClient
        mock_session = MagicMock()

        # Always return a next page
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "count": 100,
            "next": "https://www.comics.org/api/series/name/X/?page=99",
            "results": [{"name": "X"}]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_session_cls.return_value = mock_session

        client = GCDApiClient("user", "pass")
        results = client._get_all_pages("/series/name/X/", max_pages=3)

        assert mock_session.get.call_count == 3
        assert len(results) == 3

    @patch("models.gcd_api.requests.Session")
    def test_http_error_raises(self, mock_session_cls):
        import requests as req
        from models.gcd_api import GCDApiClient
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = req.exceptions.HTTPError("401 Unauthorized")
        mock_session.get.return_value = mock_resp
        mock_session_cls.return_value = mock_session

        client = GCDApiClient("user", "pass")
        with pytest.raises(req.exceptions.HTTPError):
            client.get_series(1)

    @patch("models.gcd_api.requests.Session")
    def test_timeout_raises(self, mock_session_cls):
        import requests as req
        from models.gcd_api import GCDApiClient
        mock_session = MagicMock()
        mock_session.get.side_effect = req.exceptions.Timeout("timed out")
        mock_session_cls.return_value = mock_session

        client = GCDApiClient("user", "pass")
        with pytest.raises(req.exceptions.Timeout):
            client.get_issue(1)

    @patch("models.gcd_api.requests.Session")
    def test_series_name_url_encoded(self, mock_session_cls):
        from models.gcd_api import GCDApiClient
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"count": 0, "next": None, "results": []}
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_session_cls.return_value = mock_session

        client = GCDApiClient("user", "pass")
        client.search_series("Spider-Man / Deadpool")

        call_args = mock_session.get.call_args
        # Spaces and slashes should be encoded
        assert "Spider" in call_args[0][0]
        assert " " not in call_args[0][0].split("/api/")[-1].split("/series/name/")[-1].split("/")[0]

    @patch("models.gcd_api.requests.Session")
    def test_get_publisher_url(self, mock_session_cls):
        from models.gcd_api import GCDApiClient
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": 10, "name": "Marvel"}
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_session_cls.return_value = mock_session

        client = GCDApiClient("user", "pass")
        result = client.get_publisher(10)

        call_args = mock_session.get.call_args
        assert "/publisher/10/" in call_args[0][0]
        assert result["name"] == "Marvel"
