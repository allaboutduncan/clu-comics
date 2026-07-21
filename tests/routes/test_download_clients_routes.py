"""Tests for routes/download_clients.py -- Usenet client + indexer endpoints."""
import pytest
from unittest.mock import patch, MagicMock


class TestListDownloadClients:

    @patch("core.database.get_download_client_config",
           return_value={"host": "localhost", "api_key": "SECRET", "category": "comics"})
    @patch("core.database.get_all_download_clients_status", return_value=[
        {"client_type": "sabnzbd", "is_active": 1, "is_valid": 1, "last_tested": "2026-01-01"},
    ])
    def test_list_merges_status(self, mock_status, mock_cfg, client):
        resp = client.get("/api/download-clients")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        types = {c["type"]: c for c in data["clients"]}
        assert "sabnzbd" in types and "nzbget" in types
        assert types["sabnzbd"]["has_config"] is True
        assert types["sabnzbd"]["is_active"] is True
        assert types["sabnzbd"]["is_valid"] is True
        # Actual values are returned so the form can pre-fill (category shown in full)
        assert types["sabnzbd"]["config"]["category"] == "comics"
        # nzbget has no status row -> not configured
        assert types["nzbget"]["has_config"] is False
        assert types["nzbget"]["config"] is None
        # config_fields drives the dynamic UI
        assert "api_key" in types["sabnzbd"]["config_fields"]


class TestDownloadClientConfig:

    def test_unknown_type_get(self, client):
        resp = client.get("/api/download-clients/bogus/config")
        assert resp.status_code == 400

    def test_unknown_type_post(self, client):
        resp = client.post("/api/download-clients/bogus/config", json={"host": "x"})
        assert resp.status_code == 400

    @patch("core.database.get_download_client_config", return_value=None)
    @patch("core.database.save_download_client_config", return_value=True)
    def test_save(self, mock_save, mock_get, client):
        resp = client.post("/api/download-clients/sabnzbd/config",
                           json={"host": "localhost", "port": 8080, "api_key": "k"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        mock_save.assert_called_once()
        assert mock_save.call_args[0][0] == "sabnzbd"

    @patch("core.database.get_download_client_config",
           return_value={"host": "old", "port": 8080, "api_key": "SECRET", "category": "comics"})
    @patch("core.database.save_download_client_config", return_value=True)
    def test_save_merges_with_existing(self, mock_save, mock_get, client):
        # A partial edit (just the host) must not wipe the other stored fields.
        resp = client.post("/api/download-clients/sabnzbd/config",
                           json={"host": "newhost"})
        assert resp.status_code == 200
        saved = mock_save.call_args[0][1]
        assert saved["host"] == "newhost"
        assert saved["port"] == 8080
        assert saved["api_key"] == "SECRET"
        assert saved["category"] == "comics"

    def test_save_empty_body(self, client):
        resp = client.post("/api/download-clients/sabnzbd/config", json={})
        assert resp.status_code == 400

    @patch("core.database.get_download_client_config_masked",
           return_value={"host": "loca...host", "api_key": "SECR...1234"})
    def test_get_masked(self, mock_masked, client):
        resp = client.get("/api/download-clients/sabnzbd/config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["has_config"] is True
        assert "..." in data["config"]["api_key"]

    @patch("core.database.get_download_client_config_masked", return_value=None)
    def test_get_missing(self, mock_masked, client):
        resp = client.get("/api/download-clients/nzbget/config")
        assert resp.status_code == 200
        assert resp.get_json()["has_config"] is False

    @patch("core.database.delete_download_client_config", return_value=True)
    def test_delete(self, mock_del, client):
        resp = client.delete("/api/download-clients/sabnzbd/config")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True


class TestDownloadClientTest:

    @patch("core.database.update_download_client_validity")
    @patch("models.download_clients.get_download_client_by_name")
    @patch("core.database.get_download_client_config", return_value={"host": "h", "api_key": "k"})
    def test_success(self, mock_cfg, mock_get, mock_validity, client):
        mock_get.return_value = MagicMock(test_connection=MagicMock(return_value=True))
        resp = client.post("/api/download-clients/sabnzbd/test")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["valid"] is True
        mock_validity.assert_called_once_with("sabnzbd", True)

    @patch("core.database.update_download_client_validity")
    @patch("models.download_clients.get_download_client_by_name")
    @patch("core.database.get_download_client_config", return_value={"host": "h", "api_key": "k"})
    def test_failure(self, mock_cfg, mock_get, mock_validity, client):
        mock_get.return_value = MagicMock(
            test_connection=MagicMock(return_value=False), last_error=None)
        resp = client.post("/api/download-clients/sabnzbd/test")
        assert resp.status_code == 200
        assert resp.get_json()["valid"] is False
        mock_validity.assert_called_once_with("sabnzbd", False)

    @patch("core.database.update_download_client_validity")
    @patch("models.download_clients.get_download_client_by_name")
    @patch("core.database.get_download_client_config", return_value={"host": "h", "api_key": "k"})
    def test_failure_surfaces_reason(self, mock_cfg, mock_get, mock_validity, client):
        # The route should return the client's last_error, not a generic message.
        mock_client = MagicMock(test_connection=MagicMock(return_value=False))
        mock_client.last_error = "Could not connect to http://h:6789/jsonrpc"
        mock_get.return_value = mock_client
        resp = client.post("/api/download-clients/sabnzbd/test")
        assert "Could not connect" in resp.get_json()["error"]

    @patch("core.database.get_download_client_config", return_value=None)
    def test_not_configured(self, mock_cfg, client):
        resp = client.post("/api/download-clients/sabnzbd/test")
        assert resp.status_code == 400


class TestDownloadClientActivate:

    @patch("core.database.set_active_download_client", return_value=True)
    @patch("core.database.get_download_client_config", return_value={"host": "h"})
    def test_activate(self, mock_cfg, mock_set, client):
        resp = client.post("/api/download-clients/nzbget/activate")
        assert resp.status_code == 200
        assert resp.get_json()["active"] == "nzbget"
        mock_set.assert_called_once_with("nzbget")

    @patch("core.database.get_download_client_config", return_value=None)
    def test_activate_unconfigured(self, mock_cfg, client):
        resp = client.post("/api/download-clients/nzbget/activate")
        assert resp.status_code == 400


class TestIndexers:

    @patch("core.database.get_all_indexers", return_value=[
        {"id": 1, "name": "NZBgeek", "url": "https://x", "priority": 0,
         "enabled": True, "is_valid": True, "api_key": "KEY...1111"},
    ])
    def test_list(self, mock_all, client):
        resp = client.get("/api/indexers")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["indexers"]) == 1
        assert any(t["type"] == "newznab" for t in data["types"])

    @patch("core.database.add_indexer", return_value=7)
    def test_create(self, mock_add, client):
        resp = client.post("/api/indexers",
                           json={"name": "NZBgeek", "url": "https://x", "api_key": "k"})
        assert resp.status_code == 200
        assert resp.get_json()["id"] == 7
        mock_add.assert_called_once()

    @patch("core.database.add_indexer", return_value=7)
    def test_create_defaults_comics_category(self, mock_add, client):
        client.post("/api/indexers",
                    json={"name": "NZBgeek", "url": "https://x", "api_key": "k"})
        assert mock_add.call_args.kwargs["config"]["categories"] == "7030"

    def test_create_missing_fields(self, client):
        resp = client.post("/api/indexers", json={"name": "x"})
        assert resp.status_code == 400

    @patch("core.database.get_indexer_masked", return_value=None)
    def test_get_404(self, mock_get, client):
        resp = client.get("/api/indexers/999")
        assert resp.status_code == 404

    @patch("core.database.update_indexer", return_value=True)
    @patch("core.database.get_indexer", return_value={"id": 1, "name": "old", "api_key": "k"})
    def test_update(self, mock_get, mock_upd, client):
        resp = client.put("/api/indexers/1", json={"name": "new"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        mock_upd.assert_called_once()

    @patch("core.database.delete_indexer", return_value=True)
    @patch("core.database.get_indexer", return_value={"id": 1})
    def test_delete(self, mock_get, mock_del, client):
        resp = client.delete("/api/indexers/1")
        assert resp.status_code == 200
        mock_del.assert_called_once_with(1)

    @patch("core.database.set_indexer_order", return_value=True)
    def test_reorder(self, mock_order, client):
        resp = client.post("/api/indexers/reorder", json={"order": [3, 1, 2]})
        assert resp.status_code == 200
        mock_order.assert_called_once_with([3, 1, 2])

    def test_reorder_missing_list(self, client):
        resp = client.post("/api/indexers/reorder", json={})
        assert resp.status_code == 400

    @patch("core.database.update_indexer_validity")
    @patch("models.indexers.get_indexer_impl")
    @patch("core.database.get_indexer", return_value={
        "id": 1, "name": "NZBgeek", "url": "https://x", "api_key": "k",
        "categories": None, "enabled": True, "indexer_type": "newznab"})
    def test_test_success(self, mock_get, mock_impl, mock_validity, client):
        mock_impl.return_value = MagicMock(test_connection=MagicMock(return_value=True))
        resp = client.post("/api/indexers/1/test")
        assert resp.status_code == 200
        assert resp.get_json()["valid"] is True
        mock_validity.assert_called_once_with(1, True)

    @patch("core.database.get_indexer", return_value=None)
    def test_test_404(self, mock_get, client):
        resp = client.post("/api/indexers/999/test")
        assert resp.status_code == 404


class TestUsenetDownloads:

    @patch("models.usenet.get_usenet_downloads", return_value=[
        {"download_id": "x", "filename": "Batman 1.cbz", "status": "downloading"},
    ])
    def test_list(self, mock_dl, client):
        resp = client.get("/api/usenet/downloads")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["downloads"][0]["filename"] == "Batman 1.cbz"

    @patch("models.usenet.usenet_precedes_getcomics", return_value=True)
    @patch("core.database.get_active_download_client", return_value={"client_type": "nzbget"})
    @patch("core.database.get_enabled_indexers", return_value=[{"id": 1}])
    @patch("models.usenet.search_usenet_for_issue", return_value={
        "all_results": [
            {"title": "Batman 002", "nzb_url": "u2", "score": 10, "decision": "REJECT"},
            {"title": "Batman 001", "nzb_url": "u1", "score": 90, "decision": "ACCEPT"},
        ],
    })
    def test_search(self, mock_search, mock_idx, mock_client, mock_first, client):
        resp = client.post("/api/usenet/search", json={"series": "Batman", "issue": "1"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["usenet_first"] is True
        assert data["has_indexers"] is True
        assert data["has_client"] is True
        # sorted best-first
        assert data["results"][0]["nzb_url"] == "u1"

    @patch("models.usenet.usenet_precedes_getcomics", return_value=False)
    @patch("core.database.get_active_download_client", return_value=None)
    @patch("core.database.get_enabled_indexers", return_value=[])
    def test_search_no_indexers(self, mock_idx, mock_client, mock_first, client):
        resp = client.post("/api/usenet/search", json={"series": "Batman", "issue": "1"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["has_indexers"] is False
        assert data["results"] == []

    def test_search_missing_series(self, client):
        resp = client.post("/api/usenet/search", json={"issue": "1"})
        assert resp.status_code == 400

    @patch("models.usenet.grab_nzb", return_value="dl-123")
    def test_grab(self, mock_grab, client):
        resp = client.post("/api/usenet/grab",
                           json={"nzb_url": "u1", "filename": "Batman 1.cbz",
                                 "series": "Batman", "issue": "1"})
        assert resp.status_code == 200
        assert resp.get_json()["download_id"] == "dl-123"

    def test_grab_missing_fields(self, client):
        resp = client.post("/api/usenet/grab", json={"nzb_url": "u1"})
        assert resp.status_code == 400

    @patch("models.usenet.grab_nzb", return_value=None)
    def test_grab_no_client(self, mock_grab, client):
        resp = client.post("/api/usenet/grab",
                           json={"nzb_url": "u1", "filename": "x.cbz"})
        assert resp.status_code == 502
