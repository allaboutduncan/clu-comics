"""Tests for models/download_clients/qbittorrent_client.py -- mocked HTTP."""
from unittest.mock import MagicMock, patch

import requests

from models.download_clients import ClientType, DownloadClientConfig, get_download_client
from models.download_clients.qbittorrent_client import _normalize_qbt_status


def _client(**cfg):
    defaults = {
        "host": "localhost", "port": 8080, "username": "admin",
        "password": "adminadmin", "category": "comics",
    }
    defaults.update(cfg)
    return get_download_client(ClientType.QBITTORRENT, DownloadClientConfig(**defaults))


def _session(login_text="Ok.", login_status=200):
    """A MagicMock standing in for requests.Session()."""
    session = MagicMock()
    session.post.return_value = MagicMock(status_code=login_status, text=login_text)
    return session


class TestQBittorrentMetadata:

    def test_client_group_is_torrent(self):
        assert _client().client_group == "torrent"
        assert "username" in _client().config_fields
        assert "password" in _client().config_fields

    def test_get_client_info(self):
        info = _client().get_client_info()
        assert info["client_group"] == "torrent"
        assert info["type"] == "qbittorrent"


class TestQBittorrentTestConnection:

    @patch("requests.Session")
    def test_valid(self, mock_session_cls):
        session = _session()
        session.request.return_value = MagicMock(status_code=200, text="v4.5.2")
        mock_session_cls.return_value = session
        assert _client().test_connection() is True

    @patch("requests.Session")
    def test_bad_credentials(self, mock_session_cls):
        session = _session(login_text="Fails.")
        mock_session_cls.return_value = session
        c = _client()
        assert c.test_connection() is False
        assert "Authentication failed" in c.last_error

    @patch("requests.Session")
    def test_login_http_error(self, mock_session_cls):
        session = _session(login_status=500)
        mock_session_cls.return_value = session
        c = _client()
        assert c.test_connection() is False
        assert "500" in c.last_error

    @patch("requests.Session")
    def test_login_204_success(self, mock_session_cls):
        """WebAPI 2.11+ (qBittorrent 5.x) answers 204 with an empty body on a
        successful login, not 200 + "Ok." -- confirmed against a real 5.2.3
        instance. A 204 must be accepted, not treated as an HTTP error."""
        session = _session(login_text="", login_status=204)
        session.request.return_value = MagicMock(status_code=200, text="v5.2.3")
        mock_session_cls.return_value = session
        assert _client().test_connection() is True

    @patch("requests.Session")
    def test_login_401_unauthorized(self, mock_session_cls):
        """WebAPI 2.11+ answers 401 (not 200 + "Fails.") on bad credentials."""
        session = _session(login_text="Unauthorized", login_status=401)
        mock_session_cls.return_value = session
        c = _client()
        assert c.test_connection() is False
        assert "Authentication failed" in c.last_error

    @patch("requests.Session")
    def test_connection_error(self, mock_session_cls):
        session = MagicMock()
        session.post.side_effect = requests.exceptions.ConnectionError()
        mock_session_cls.return_value = session
        c = _client()
        assert c.test_connection() is False
        assert "Could not connect" in c.last_error

    @patch("requests.Session")
    def test_non_qbittorrent_response(self, mock_session_cls):
        session = _session()
        session.request.return_value = MagicMock(status_code=200, text="<html>not qbt</html>")
        mock_session_cls.return_value = session
        c = _client()
        assert c.test_connection() is False
        assert "Non-qBittorrent" in c.last_error

    def test_missing_credentials(self):
        c = _client(username=None, password=None)
        assert c.test_connection() is False
        assert "required" in c.last_error


class TestQBittorrentAddTorrent:

    @patch("time.sleep", return_value=None)
    @patch("requests.Session")
    def test_resolves_hash_via_tag(self, mock_session_cls, _sleep):
        session = _session()
        add_resp = MagicMock(status_code=200, text="Ok.")
        info_resp = MagicMock(status_code=200)
        info_resp.json.return_value = [{"hash": "abcd1234"}]
        session.request.side_effect = [add_resp, info_resp]
        mock_session_cls.return_value = session

        result = _client().add_torrent("magnet:?xt=urn:btih:abc", "Batman 001")

        assert result.success is True
        assert result.client_id == "abcd1234"
        # The tag param on the add call is what the follow-up info lookup keys on.
        add_call = session.request.call_args_list[0]
        assert add_call.kwargs["data"]["tags"].startswith("clu-")

    @patch("time.sleep", return_value=None)
    @patch("requests.Session")
    def test_hash_not_found_after_retries(self, mock_session_cls, _sleep):
        session = _session()
        add_resp = MagicMock(status_code=200, text="Ok.")
        empty_info_resp = MagicMock(status_code=200)
        empty_info_resp.json.return_value = []
        session.request.side_effect = [add_resp] + [empty_info_resp] * 5
        mock_session_cls.return_value = session

        result = _client().add_torrent("magnet:?xt=urn:btih:abc", "Batman 001")

        assert result.success is False
        assert "tag" in result.error

    @patch("requests.Session")
    def test_rejected_by_qbittorrent(self, mock_session_cls):
        session = _session()
        session.request.return_value = MagicMock(status_code=200, text="Fails.")
        mock_session_cls.return_value = session

        result = _client().add_torrent("magnet:?xt=urn:btih:abc", "Batman 001")

        assert result.success is False
        assert "rejected" in result.error

    @patch("requests.Session")
    def test_uses_configured_category(self, mock_session_cls):
        session = _session()
        add_resp = MagicMock(status_code=200, text="Ok.")
        info_resp = MagicMock(status_code=200)
        info_resp.json.return_value = [{"hash": "abcd1234"}]
        session.request.side_effect = [add_resp, info_resp]
        mock_session_cls.return_value = session

        _client(category="comics").add_torrent("magnet:?xt=urn:btih:abc", "Batman 001")

        add_call = session.request.call_args_list[0]
        assert add_call.kwargs["data"]["category"] == "comics"
        assert add_call.kwargs["data"]["urls"] == "magnet:?xt=urn:btih:abc"


class TestQBittorrentQueueAndStatus:

    @patch("requests.Session")
    def test_get_queue_returns_only_downloading(self, mock_session_cls):
        session = _session()
        resp = MagicMock(status_code=200)
        resp.json.return_value = [
            {"hash": "h1", "name": "Batman 001", "state": "downloading",
             "progress": 0.4, "size": 1000, "downloaded": 400, "save_path": "/dl"},
            {"hash": "h2", "name": "Batman 002", "state": "uploading",
             "progress": 1.0, "size": 1000, "save_path": "/dl"},
        ]
        session.request.return_value = resp
        mock_session_cls.return_value = session

        queue = _client().get_queue()

        assert len(queue) == 1
        assert queue[0].client_id == "h1"
        assert queue[0].percent == 40.0

    @patch("requests.Session")
    def test_get_history_returns_finished_and_failed(self, mock_session_cls):
        session = _session()
        resp = MagicMock(status_code=200)
        resp.json.return_value = [
            {"hash": "h1", "name": "Batman 001", "state": "downloading", "progress": 0.4},
            {"hash": "h2", "name": "Batman 002", "state": "uploading", "progress": 1.0,
             "save_path": "/dl", "content_path": "/dl/Batman 002.cbz"},
            {"hash": "h3", "name": "Batman 003", "state": "error", "progress": 0.1},
        ]
        session.request.return_value = resp
        mock_session_cls.return_value = session

        history = _client().get_history()

        by_id = {s.client_id: s for s in history}
        assert by_id["h2"].status == "complete"
        assert by_id["h2"].storage_path == "/dl/Batman 002.cbz"
        assert by_id["h3"].status == "failed"
        assert "h1" not in by_id

    @patch("requests.Session")
    def test_get_status_by_hash(self, mock_session_cls):
        session = _session()
        resp = MagicMock(status_code=200)
        resp.json.return_value = [{"hash": "h1", "name": "Batman 001",
                                    "state": "stalledUP", "progress": 1.0}]
        session.request.return_value = resp
        mock_session_cls.return_value = session

        status = _client().get_status("h1")

        assert status is not None
        assert status.status == "complete"

    @patch("requests.Session")
    def test_get_status_missing_returns_none(self, mock_session_cls):
        session = _session()
        resp = MagicMock(status_code=200)
        resp.json.return_value = []
        session.request.return_value = resp
        mock_session_cls.return_value = session

        assert _client().get_status("does-not-exist") is None

    @patch("requests.Session")
    def test_session_reauthenticates_on_403(self, mock_session_cls):
        session = _session()
        forbidden = MagicMock(status_code=403)
        ok_resp = MagicMock(status_code=200)
        ok_resp.json.return_value = []
        session.request.side_effect = [forbidden, ok_resp]
        mock_session_cls.return_value = session

        result = _client()._list_torrents()

        assert result == []
        # Two session.post calls: the initial login plus the re-login after 403.
        assert session.post.call_count == 2


class TestNormalizeQbtStatus:

    def test_downloading_states(self):
        assert _normalize_qbt_status("downloading", 0.5) == "downloading"
        assert _normalize_qbt_status("metaDL", None) == "downloading"
        assert _normalize_qbt_status("stalledDL", 0.3) == "downloading"

    def test_complete_states(self):
        assert _normalize_qbt_status("uploading", 1.0) == "complete"
        assert _normalize_qbt_status("stalledUP", 1.0) == "complete"
        assert _normalize_qbt_status("pausedUP", 1.0) == "complete"
        assert _normalize_qbt_status("stoppedUP", 1.0) == "complete"

    def test_progress_fallback_treats_full_progress_as_complete(self):
        # Even an unrecognized state name completes if progress says 100%.
        assert _normalize_qbt_status("someFutureState", 1.0) == "complete"

    def test_failed_states(self):
        assert _normalize_qbt_status("error", 0.0) == "failed"
        assert _normalize_qbt_status("missingFiles", 0.0) == "failed"
