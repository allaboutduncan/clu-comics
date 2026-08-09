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
        {"download_id": "x", "filename": "Batman 1.cbz", "status": "downloading",
         "client_type": "sabnzbd", "percent": 42, "stage": "Repairing",
         "bytes_total": 104857600, "bytes_downloaded": 52428800},
    ])
    def test_list(self, mock_dl, client):
        resp = client.get("/api/usenet/downloads")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        dl = data["downloads"][0]
        assert dl["filename"] == "Batman 1.cbz"
        # Enriched live-progress fields flow through to the status page.
        assert dl["client_type"] == "sabnzbd"
        assert dl["percent"] == 42
        assert dl["stage"] == "Repairing"
        assert dl["bytes_total"] == 104857600
        assert dl["bytes_downloaded"] == 52428800

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

    @patch("models.usenet.usenet_precedes_getcomics", return_value=False)
    @patch("core.database.get_active_download_client", return_value=None)
    @patch("core.database.get_enabled_indexers", return_value=[{"id": 1}])
    @patch("models.usenet.search_usenet_for_issue", return_value={"all_results": []})
    def test_search_passes_issue_year(self, mock_search, mock_idx, mock_client,
                                      mock_first, client):
        # Scoring compares the year numerically, so it must arrive as an int —
        # a string would penalize every result, including the right one.
        resp = client.post("/api/usenet/search",
                           json={"series": "Iron Man", "issue": "8",
                                 "issue_year": "2026"})
        assert resp.status_code == 200
        assert mock_search.call_args.kwargs["issue_year"] == 2026

    @patch("models.usenet.usenet_precedes_getcomics", return_value=False)
    @patch("core.database.get_active_download_client", return_value=None)
    @patch("core.database.get_enabled_indexers", return_value=[{"id": 1}])
    @patch("models.usenet.search_usenet_for_issue", return_value={"all_results": []})
    def test_search_year_coercion(self, mock_search, mock_idx, mock_client,
                                  mock_first, client):
        for sent, expected in [
            (2026, 2026),
            ("2026-01-14", 2026),   # a store date, not just a year
            ("", None),
            (None, None),
            ("not-a-year", None),
            (1492, None),           # outside the plausible range
        ]:
            client.post("/api/usenet/search",
                        json={"series": "Iron Man", "issue": "8",
                              "issue_year": sent})
            assert mock_search.call_args.kwargs["issue_year"] == expected, sent

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


class TestDownloadSources:

    @patch("models.dcpp.dcpp_enabled_and_configured", return_value=True)
    @patch("models.usenet.usenet_enabled_and_configured", return_value=False)
    @patch("core.database.get_user_preference", return_value='["dcpp","getcomics","usenet"]')
    def test_lists_order_and_availability(self, mock_pref, mock_un, mock_dc, client):
        resp = client.get("/api/download-clients/sources")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["order"] == ["dcpp", "getcomics", "usenet"]
        assert data["available"] == {
            "getcomics": True, "usenet": False, "dcpp": True,
        }
        assert "dcpp" in data["known"]

    @patch("models.dcpp.dcpp_enabled_and_configured", return_value=False)
    @patch("models.usenet.usenet_enabled_and_configured", return_value=False)
    @patch("core.database.get_user_preference", return_value=None)
    def test_default_is_getcomics_only(self, mock_pref, mock_un, mock_dc, client):
        data = client.get("/api/download-clients/sources").get_json()
        assert data["order"] == ["getcomics"]
        assert data["available"]["getcomics"] is True

    @patch("models.dcpp.dcpp_enabled_and_configured", return_value=False)
    @patch("models.usenet.usenet_enabled_and_configured", return_value=False)
    @patch("core.database.get_user_preference", return_value=None)
    def test_search_order_covers_every_source(self, mock_pref, mock_un, mock_dc, client):
        # order gates auto-download; search_order drives the manual modal and
        # must never drop a source, or a default install stops searching
        # Usenet and DC++ entirely.
        data = client.get("/api/download-clients/sources").get_json()
        assert data["order"] == ["getcomics"]
        assert set(data["search_order"]) == {"getcomics", "usenet", "dcpp"}

    @patch("models.dcpp.dcpp_enabled_and_configured", return_value=True)
    @patch("models.usenet.usenet_enabled_and_configured", return_value=True)
    @patch("core.database.get_user_preference", return_value='["dcpp","usenet","getcomics"]')
    def test_search_order_follows_priority(self, mock_pref, mock_un, mock_dc, client):
        data = client.get("/api/download-clients/sources").get_json()
        assert data["search_order"] == ["dcpp", "usenet", "getcomics"]


class TestDcppDownloads:

    @patch("models.dcpp.get_dcpp_downloads", return_value=[
        {"download_id": "x", "filename": "Batman 1.cbz", "status": "downloading",
         "client_type": "airdcpp", "percent": 25, "stage": "Downloading",
         "bytes_total": 104857600, "bytes_downloaded": 26214400},
    ])
    def test_list(self, mock_dl, client):
        resp = client.get("/api/dcpp/downloads")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        dl = data["downloads"][0]
        # Same item shape as /api/usenet/downloads, so the status page renders
        # both with one code path.
        assert dl["client_type"] == "airdcpp"
        assert dl["percent"] == 25
        assert dl["stage"] == "Downloading"
        assert dl["bytes_downloaded"] == 26214400

    @patch("models.dcpp.get_dcpp_downloads", side_effect=Exception("boom"))
    def test_list_error(self, mock_dl, client):
        assert client.get("/api/dcpp/downloads").status_code == 500


class TestDcppSearch:

    @patch("core.database.get_active_download_client",
           return_value={"client_type": "airdcpp", "config": {}})
    @patch("models.dcpp.search_dcpp_for_issue", return_value={
        "all_results": [
            {"title": "Batman 002", "result_token": "t2", "score": 10, "decision": "REJECT"},
            {"title": "Batman 001", "result_token": "t1", "score": 90, "decision": "ACCEPT"},
        ],
        "errors": [],
    })
    def test_search(self, mock_search, mock_active, client):
        resp = client.post("/api/dcpp/search", json={"series": "Batman", "issue": "1"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["has_client"] is True
        # sorted best-first
        assert data["results"][0]["result_token"] == "t1"
        # Scoped to the DC++ group so an active SABnzbd can't satisfy it.
        assert mock_active.call_args.kwargs["client_group"] == "dcpp"

    @patch("core.database.get_active_download_client", return_value=None)
    def test_search_no_client(self, mock_active, client):
        resp = client.post("/api/dcpp/search", json={"series": "Batman", "issue": "1"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["has_client"] is False
        assert data["results"] == []

    @patch("core.database.get_active_download_client",
           return_value={"client_type": "airdcpp", "config": {}})
    @patch("models.dcpp.search_dcpp_for_issue", return_value={"all_results": [], "errors": []})
    @patch("core.database.get_user_preference", return_value='["getcomics"]')
    def test_search_ignores_source_priority(self, mock_pref, mock_search, mock_active, client):
        # A configured DC++ client must stay searchable by hand even when the
        # user has not ranked DC++ — that list governs auto-download only.
        data = client.post("/api/dcpp/search",
                           json={"series": "Batman", "issue": "1"}).get_json()
        assert data["has_client"] is True
        mock_search.assert_called_once()

    def test_search_missing_series(self, client):
        assert client.post("/api/dcpp/search", json={"issue": "1"}).status_code == 400

    @patch("core.database.get_active_download_client",
           return_value={"client_type": "airdcpp", "config": {}})
    @patch("models.dcpp.search_dcpp_for_issue", return_value={"all_results": [], "errors": []})
    def test_search_year_coercion(self, mock_search, mock_active, client):
        # Scoring compares the year numerically, so it must arrive as an int.
        for sent, expected in [
            (2026, 2026),
            ("2026-01-14", 2026),   # a store date, not just a year
            ("", None),
            (None, None),
            ("not-a-year", None),
            (1492, None),           # outside the plausible range
        ]:
            client.post("/api/dcpp/search",
                        json={"series": "Iron Man", "issue": "8", "issue_year": sent})
            assert mock_search.call_args.kwargs["issue_year"] == expected, sent


class TestDcppGrab:

    @patch("models.dcpp.grab_dcpp", return_value="dl-456")
    def test_grab(self, mock_grab, client):
        resp = client.post("/api/dcpp/grab",
                           json={"result_token": "t1", "filename": "Batman 1.cbz",
                                 "series": "Batman", "issue": "1"})
        assert resp.status_code == 200
        assert resp.get_json()["download_id"] == "dl-456"
        assert mock_grab.call_args[0][0] == "t1"

    def test_grab_missing_fields(self, client):
        assert client.post("/api/dcpp/grab", json={"result_token": "t1"}).status_code == 400
        assert client.post("/api/dcpp/grab", json={"filename": "x.cbz"}).status_code == 400

    @patch("models.dcpp.grab_dcpp", return_value=None)
    def test_grab_rejected(self, mock_grab, client):
        # No active client, an expired result token, or a client-side refusal.
        resp = client.post("/api/dcpp/grab",
                           json={"result_token": "stale", "filename": "x.cbz"})
        assert resp.status_code == 502
        assert resp.get_json()["success"] is False
