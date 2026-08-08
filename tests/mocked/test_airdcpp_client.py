"""Tests for models/download_clients/airdcpp_client.py -- mocked HTTP."""
import pytest
from unittest.mock import MagicMock, patch

from models.download_clients import (
    ClientType,
    DownloadClientConfig,
    get_download_client,
)
from models.download_clients import airdcpp_client
from models.download_clients.airdcpp_client import (
    _normalize_airdcpp_status,
    _percent,
    _result_type,
    _user_count,
)


@pytest.fixture(autouse=True)
def _fast_search(monkeypatch):
    """Collapse the hub-response wait window.

    ``_collect_results`` deliberately keeps polling for the full budget when a
    search returns nothing, since hubs answer at different speeds — that would
    add 8 real seconds per empty-result test.
    """
    monkeypatch.setattr(airdcpp_client, "_SEARCH_WAIT_TOTAL", 0.0)
    monkeypatch.setattr(airdcpp_client, "_SEARCH_POLL_INTERVAL", 0.0)


def _client(**cfg):
    defaults = {
        "host": "localhost", "port": 5600,
        "username": "admin", "password": "pw",
        "target_directory": "/downloads/dcpp",
    }
    defaults.update(cfg)
    return get_download_client(ClientType.AIRDCPP, DownloadClientConfig(**defaults))


def _resp(status_code=200, payload=None, content=b"{}"):
    """Build a fake requests.Response."""
    resp = MagicMock(status_code=status_code, content=content)
    if payload is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = payload
    return resp


class TestAirDCPPTestConnection:

    @patch("requests.request")
    def test_valid_hub_list(self, mock_req):
        mock_req.return_value = _resp(payload=[{"id": 1, "hub_url": "adcs://hub"}])
        assert _client().test_connection() is True

    @patch("requests.request")
    def test_empty_hub_list_is_still_valid(self, mock_req):
        # A working client with no hubs connected yet must not read as broken.
        mock_req.return_value = _resp(payload=[])
        assert _client().test_connection() is True

    @patch("requests.request")
    def test_bad_credentials(self, mock_req):
        mock_req.return_value = _resp(status_code=401)
        c = _client()
        assert c.test_connection() is False
        assert "uthentication" in c.last_error

    @patch("requests.request")
    def test_non_json_response(self, mock_req):
        mock_req.return_value = _resp(payload=None)
        c = _client()
        assert c.test_connection() is False
        assert "Non-JSON" in c.last_error

    @patch("requests.request")
    def test_unexpected_shape(self, mock_req):
        mock_req.return_value = _resp(payload={"not": "a list"})
        assert _client().test_connection() is False

    @patch("requests.request", side_effect=Exception("boom"))
    def test_exception_swallowed(self, mock_req):
        # A bad host returns False, not an exception.
        assert _client().test_connection() is False

    def test_missing_credentials(self):
        assert _client(username=None).test_connection() is False
        assert _client(password=None).test_connection() is False


class TestAirDCPPMetadata:

    def test_config_fields(self):
        cls = get_download_client(ClientType.AIRDCPP).__class__
        assert "target_directory" in cls.config_fields
        assert "hub_urls" in cls.config_fields
        # AirDC++ authenticates with a username/password, not an API key.
        assert "api_key" not in cls.config_fields

    def test_client_group_is_dcpp(self):
        # The whole point of the group: DC++ must not fight SABnzbd/NZBGet for
        # the single active slot.
        assert get_download_client(ClientType.AIRDCPP).client_group == "dcpp"
        assert get_download_client(ClientType.SABNZBD).client_group == "usenet"

    def test_client_info_exposes_group(self):
        info = get_download_client(ClientType.AIRDCPP).get_client_info()
        assert info["client_group"] == "dcpp"
        assert info["type"] == "airdcpp"

    def test_base_url_includes_api_prefix(self):
        assert _client()._api_url("/hubs") == "http://localhost:5600/api/v1/hubs"

    def test_ssl_and_url_base(self):
        c = _client(use_ssl=True, url_base="airdc")
        assert c._api_url("/hubs") == "https://localhost:5600/airdc/api/v1/hubs"


class TestAirDCPPSearch:

    @patch("models.download_clients.airdcpp_client.time.sleep")
    @patch("requests.request")
    def test_search_flow(self, mock_req, _sleep):
        results = [{
            "id": 42, "name": "Batman 001 (2016).cbz", "size": 50_000_000,
            "tth": "ABC", "type": "file", "users": 3, "relevance": 90,
        }]

        def route(method, url, **kwargs):
            if method == "POST" and url.endswith("/search"):
                return _resp(payload={"id": "inst-1"})
            if method == "POST" and url.endswith("/hub_search"):
                return _resp(payload={}, content=b"")
            if method == "GET" and "/results/" in url:
                return _resp(payload=results)
            raise AssertionError(f"unexpected {method} {url}")

        mock_req.side_effect = route
        instance_id, found = _client().search("Batman 1")

        assert instance_id == "inst-1"
        assert len(found) == 1
        assert found[0]["result_id"] == "42"
        assert found[0]["name"] == "Batman 001 (2016).cbz"
        assert found[0]["tth"] == "ABC"
        assert found[0]["users"] == 3

    @patch("models.download_clients.airdcpp_client.time.sleep")
    @patch("requests.request")
    def test_hub_urls_filter_sent(self, mock_req, _sleep):
        sent = {}

        def route(method, url, **kwargs):
            if method == "POST" and url.endswith("/search"):
                return _resp(payload={"id": "i"})
            if method == "POST" and url.endswith("/hub_search"):
                sent.update(kwargs.get("json") or {})
                return _resp(payload={}, content=b"")
            return _resp(payload=[])

        mock_req.side_effect = route
        _client(hub_urls="adcs://a:411, adcs://b:411").search("Batman 1")

        assert sent["hub_urls"] == ["adcs://a:411", "adcs://b:411"]
        # Comic extensions keep unrelated media out of the results.
        assert "cbz" in sent["query"]["extensions"]

    @patch("models.download_clients.airdcpp_client.time.sleep")
    @patch("requests.request")
    def test_no_hub_filter_when_unset(self, mock_req, _sleep):
        sent = {}

        def route(method, url, **kwargs):
            if method == "POST" and url.endswith("/search"):
                return _resp(payload={"id": "i"})
            if method == "POST" and url.endswith("/hub_search"):
                sent.update(kwargs.get("json") or {})
                return _resp(payload={}, content=b"")
            return _resp(payload=[])

        mock_req.side_effect = route
        _client().search("Batman 1")
        assert "hub_urls" not in sent

    @patch("requests.request")
    def test_instance_creation_failure(self, mock_req):
        mock_req.return_value = _resp(status_code=500)
        instance_id, found = _client().search("Batman 1")
        assert instance_id is None
        assert found == []


class TestAirDCPPDownloadResult:

    @patch("requests.request")
    def test_download_returns_bundle_id(self, mock_req):
        mock_req.return_value = _resp(payload={"id": 777, "merged": False})
        result = _client().download_result("inst-1", "42")
        assert result.success is True
        assert result.client_id == "777"

    @patch("requests.request")
    def test_target_directory_from_config(self, mock_req):
        mock_req.return_value = _resp(payload={"id": 1})
        _client().download_result("inst-1", "42")
        assert mock_req.call_args.kwargs["json"]["target_directory"] == "/downloads/dcpp"

    @patch("requests.request")
    def test_missing_bundle_id_is_failure(self, mock_req):
        # Accepted but untrackable is a failure -- never invent a bundle id.
        mock_req.return_value = _resp(payload={})
        result = _client().download_result("inst-1", "42")
        assert result.success is False

    @patch("requests.request")
    def test_http_error(self, mock_req):
        mock_req.return_value = _resp(status_code=404)
        result = _client().download_result("inst-1", "42")
        assert result.success is False
        assert result.error


BUNDLES = [
    {
        "id": 1, "name": "Batman 001", "target": "/downloads/dcpp/Batman 001.cbz",
        "size": 1000, "downloaded_bytes": 250,
        "status": {"id": "downloading", "str": "Downloading",
                   "completed": False, "failed": False},
    },
    {
        "id": 2, "name": "Batman 002", "target": "/downloads/dcpp/Batman 002.cbz",
        "size": 1000, "downloaded_bytes": 1000,
        "status": {"id": "completed", "str": "Finished",
                   "completed": True, "failed": False},
    },
    {
        "id": 3, "name": "Batman 003", "target": "/downloads/dcpp/Batman 003.cbz",
        "size": 1000, "downloaded_bytes": 10,
        "status": {"id": "download_failed", "str": "Failed",
                   "completed": False, "failed": True},
    },
]


class TestAirDCPPQueue:

    @patch("requests.request")
    def test_queue_only_active(self, mock_req):
        mock_req.return_value = _resp(payload=BUNDLES)
        queue = _client().get_queue()
        assert [s.client_id for s in queue] == ["1"]
        assert queue[0].percent == 25.0
        assert queue[0].bytes_total == 1000
        assert queue[0].bytes_downloaded == 250
        assert queue[0].stage == "Downloading"
        assert queue[0].storage_path == "/downloads/dcpp/Batman 001.cbz"

    @patch("requests.request")
    def test_history_only_finished(self, mock_req):
        mock_req.return_value = _resp(payload=BUNDLES)
        hist = {s.client_id: s for s in _client().get_history()}
        assert set(hist) == {"2", "3"}
        assert hist["2"].status == "complete"
        assert hist["3"].status == "failed"

    @patch("requests.request")
    def test_get_status_by_id(self, mock_req):
        mock_req.return_value = _resp(payload=BUNDLES)
        assert _client().get_status("2").status == "complete"
        assert _client().get_status("nope") is None

    @patch("requests.request")
    def test_nested_downloaded_bytes_fallback(self, mock_req):
        # Older builds report progress as status.downloaded, not downloaded_bytes.
        mock_req.return_value = _resp(payload=[{
            "id": 9, "name": "x", "size": 200,
            "status": {"id": "downloading", "str": "Downloading",
                       "completed": False, "failed": False, "downloaded": 100},
        }])
        assert _client().get_queue()[0].percent == 50.0

    @patch("requests.request", side_effect=Exception("boom"))
    def test_queue_errors_return_empty(self, mock_req):
        # The poller calls these on a timer; they must never raise.
        assert _client().get_queue() == []
        assert _client().get_history() == []
        assert _client().get_status("1") is None


class TestAirDCPPHelpers:

    @pytest.mark.parametrize("status,expected", [
        ({"completed": True, "failed": False}, "complete"),
        ({"completed": False, "failed": True}, "failed"),
        ({"completed": False, "failed": False}, "downloading"),
        # Flags absent -> fall back to the id string.
        ({"id": "shared"}, "complete"),
        ({"id": "validation_error"}, "failed"),
        ({"id": "queued"}, "downloading"),
        ({}, "downloading"),
        (None, "downloading"),
    ])
    def test_normalize_status(self, status, expected):
        assert _normalize_airdcpp_status(status) == expected

    def test_percent_guards(self):
        assert _percent(50, 100) == 50.0
        assert _percent(None, 100) is None
        assert _percent(10, 0) is None
        assert _percent(10, None) is None
        # Never report more than complete.
        assert _percent(200, 100) == 100.0

    def test_result_type_accepts_object_or_string(self):
        assert _result_type("file") == "file"
        assert _result_type({"id": "directory"}) == "directory"
        assert _result_type(None) == ""

    def test_user_count_accepts_object_or_int(self):
        assert _user_count(5) == 5
        assert _user_count({"user": {}, "count": 3}) == 3
        assert _user_count(None) is None
