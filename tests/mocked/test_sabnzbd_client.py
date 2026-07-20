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

    def test_pr2_stubs_raise(self):
        c = _client()
        with pytest.raises(NotImplementedError):
            c.add_nzb(b"", "x.nzb")
        with pytest.raises(NotImplementedError):
            c.get_history()
