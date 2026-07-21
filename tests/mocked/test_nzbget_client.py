"""Tests for models/download_clients/nzbget_client.py -- mocked HTTP."""
import pytest
from unittest.mock import MagicMock, patch

from models.download_clients import (
    ClientType,
    DownloadClientConfig,
    get_download_client,
)


def _client(**cfg):
    defaults = {"host": "localhost", "port": 6789, "username": "nzbget", "password": "pw"}
    defaults.update(cfg)
    return get_download_client(ClientType.NZBGET, DownloadClientConfig(**defaults))


class TestNZBGetTestConnection:

    @patch("requests.post")
    def test_valid_version(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200, json=MagicMock(return_value={"result": "21.1", "id": 1}))
        assert _client().test_connection() is True
        # Basic auth built from username/password, hitting /jsonrpc
        _, kwargs = mock_post.call_args
        assert kwargs["auth"] == ("nzbget", "pw")
        assert mock_post.call_args[0][0].endswith("/jsonrpc")

    @patch("requests.post")
    def test_401(self, mock_post):
        mock_post.return_value = MagicMock(status_code=401,
                                           json=MagicMock(return_value={}))
        assert _client().test_connection() is False

    @patch("requests.post")
    def test_bad_json(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200, json=MagicMock(side_effect=ValueError("no json")))
        assert _client().test_connection() is False

    @patch("requests.post", side_effect=Exception("connection refused"))
    def test_exception_swallowed(self, mock_post):
        c = _client()
        assert c.test_connection() is False
        assert c.last_error  # a readable reason is recorded

    @patch("requests.post")
    def test_no_auth_when_credentials_blank(self, mock_post):
        # NZBGet with auth disabled (like Sonarr/Radarr): no Basic auth sent.
        mock_post.return_value = MagicMock(
            status_code=200, json=MagicMock(return_value={"result": "21.1"}))
        c = _client(username=None, password=None)
        assert c.test_connection() is True
        assert mock_post.call_args.kwargs["auth"] is None

    @patch("requests.post")
    def test_401_sets_reason(self, mock_post):
        mock_post.return_value = MagicMock(status_code=401,
                                           json=MagicMock(return_value={}))
        c = _client()
        assert c.test_connection() is False
        assert "401" in c.last_error


class TestNZBGetMetadata:

    def test_config_fields(self):
        cls = get_download_client(ClientType.NZBGET).__class__
        assert "username" in cls.config_fields
        assert "password" in cls.config_fields

    def test_ssl_base_url(self):
        c = _client(use_ssl=True, url_base="nzbget")
        assert c._base_url() == "https://localhost:6789/nzbget"


class TestNZBGetSubmit:

    @patch("requests.post")
    def test_add_url(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200, raise_for_status=MagicMock(),
            json=MagicMock(return_value={"result": 7}))
        res = _client(category="comics").add_nzb("https://x/a.nzb", "Batman 1.cbz")
        assert res.success is True
        assert res.client_id == "7"
        body = mock_post.call_args.kwargs["json"]
        assert body["method"] == "append"
        # append params: [name, content(url), category, priority, ...]
        assert body["params"][1] == "https://x/a.nzb"
        assert body["params"][2] == "comics"
        # Modern NZBGet requires the trailing PPParameters array.
        assert body["params"][-1] == []
        assert len(body["params"]) == 10

    @patch("requests.post")
    def test_add_bytes_base64(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200, raise_for_status=MagicMock(),
            json=MagicMock(return_value={"result": 9}))
        res = _client().add_nzb(b"<nzb/>", "Batman 1.cbz")
        assert res.success is True
        import base64
        assert mock_post.call_args.kwargs["json"]["params"][1] == \
            base64.b64encode(b"<nzb/>").decode("ascii")

    @patch("requests.post")
    def test_add_failure(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200, raise_for_status=MagicMock(),
            json=MagicMock(return_value={"result": 0}))
        res = _client().add_nzb("https://x/a.nzb", "x.cbz")
        assert res.success is False

    @patch("requests.post")
    def test_add_surfaces_jsonrpc_error(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200, raise_for_status=MagicMock(),
            json=MagicMock(return_value={
                "error": {"code": -32602, "message": "Invalid parameters"}, "id": 1}))
        res = _client().add_nzb(b"<nzb/>", "x.cbz")
        assert res.success is False
        assert "Invalid parameters" in res.error


class TestNZBGetHistory:

    @patch("requests.post")
    def test_history_maps_status(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200, raise_for_status=MagicMock(),
            json=MagicMock(return_value={"result": [
                {"NZBID": 7, "Name": "Batman 1", "Status": "SUCCESS/ALL",
                 "FinalDir": "/done/Batman 1", "Category": "comics"},
                {"NZBID": 8, "Name": "Superman 5", "Status": "FAILURE/UNPACK",
                 "DestDir": "/int/Superman 5"},
            ]}))
        by_id = {h.client_id: h for h in _client().get_history()}
        assert by_id["7"].status == "complete"
        assert by_id["7"].storage_path == "/done/Batman 1"
        assert by_id["8"].status == "failed"
