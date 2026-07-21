"""Tests for models/download_clients/sabnzbd_client.py -- mocked HTTP."""
import pytest
from unittest.mock import MagicMock, patch

from models.download_clients import (
    ClientType,
    DownloadClientConfig,
    get_download_client,
)


def _client(**cfg):
    defaults = {"host": "localhost", "port": 8080, "api_key": "KEY123"}
    defaults.update(cfg)
    return get_download_client(ClientType.SABNZBD, DownloadClientConfig(**defaults))


class TestSABnzbdTestConnection:

    @patch("requests.get")
    def test_valid_queue(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, json=MagicMock(return_value={"queue": {"slots": []}}))
        assert _client().test_connection() is True

    @patch("requests.get")
    def test_bad_api_key(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, json=MagicMock(return_value={"error": "API Key Incorrect"}))
        assert _client().test_connection() is False

    @patch("requests.get")
    def test_non_200(self, mock_get):
        mock_get.return_value = MagicMock(status_code=403,
                                          json=MagicMock(return_value={}))
        assert _client().test_connection() is False

    @patch("requests.get", side_effect=Exception("timeout"))
    def test_exception_swallowed(self, mock_get):
        # Must not raise -- a bad host returns False, not an exception.
        assert _client().test_connection() is False

    def test_missing_api_key(self):
        assert _client(api_key=None).test_connection() is False


class TestSABnzbdMetadata:

    def test_config_fields(self):
        cls = get_download_client(ClientType.SABNZBD).__class__
        assert "host" in cls.config_fields
        assert "api_key" in cls.config_fields

    def test_client_info(self):
        info = _client().get_client_info()
        assert info["type"] == "sabnzbd"
        assert info["name"] == "SABnzbd"
        assert "config_fields" in info


class TestSABnzbdSubmit:

    @patch("requests.get")
    def test_add_url(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, raise_for_status=MagicMock(),
            json=MagicMock(return_value={"status": True, "nzo_ids": ["SABnzbd_nzo_a"]}))
        res = _client(category="comics", priority=1).add_nzb(
            "https://indexer/getnzb/a.nzb", "Batman 1.cbz")
        assert res.success is True
        assert res.client_id == "SABnzbd_nzo_a"
        params = mock_get.call_args.kwargs["params"]
        assert params["mode"] == "addurl"
        assert params["cat"] == "comics"
        assert params["priority"] == 1

    @patch("requests.post")
    def test_add_bytes_uses_addfile(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200, raise_for_status=MagicMock(),
            json=MagicMock(return_value={"status": True, "nzo_ids": ["nzo_b"]}))
        res = _client().add_nzb(b"<nzb/>", "Batman 1.cbz")
        assert res.success is True
        assert mock_post.call_args.kwargs["params"]["mode"] == "addfile"
        assert "files" in mock_post.call_args.kwargs

    @patch("requests.get")
    def test_add_failure(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, raise_for_status=MagicMock(),
            json=MagicMock(return_value={"status": False, "error": "nope"}))
        res = _client().add_nzb("https://x/a.nzb", "x.cbz")
        assert res.success is False
        assert res.error == "nope"


class TestSABnzbdHistory:

    @patch("requests.get")
    def test_history_maps_status(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, raise_for_status=MagicMock(),
            json=MagicMock(return_value={"history": {"slots": [
                {"nzo_id": "a", "name": "Batman 1", "status": "Completed",
                 "storage": "/done/Batman 1", "category": "comics"},
                {"nzo_id": "b", "name": "Superman 5", "status": "Failed",
                 "storage": "", "category": "comics"},
            ]}}))
        hist = _client().get_history()
        by_id = {h.client_id: h for h in hist}
        assert by_id["a"].status == "complete"
        assert by_id["a"].storage_path == "/done/Batman 1"
        assert by_id["b"].status == "failed"

    @patch("requests.get")
    def test_get_status_from_queue(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=200, raise_for_status=MagicMock(),
            json=MagicMock(return_value={"queue": {"slots": [
                {"nzo_id": "a", "filename": "Batman 1", "percentage": "42", "cat": "comics"},
            ]}}))
        st = _client().get_status("a")
        assert st.status == "downloading"
        assert st.percent == 42.0
