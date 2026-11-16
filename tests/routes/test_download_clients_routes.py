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
        assert types["nzbget"]["secrets_set"] == []
        # config_fields drives the dynamic UI
        assert "api_key" in types["sabnzbd"]["config_fields"]

    @patch("core.database.get_download_client_config",
           return_value={"host": "h", "api_key": "SECRET", "username": "u",
                         "password": "PASS", "category": "comics"})
    @patch("core.database.get_all_download_clients_status", return_value=[
        {"client_type": "nzbget", "is_active": 1, "is_valid": 1, "last_tested": None},
    ])
    def test_list_never_returns_secrets(self, mock_status, mock_cfg, client):
        """Secrets never leave the server; a blank secret field keeps the
        stored value on save."""
        resp = client.get("/api/download-clients")
        assert "SECRET" not in resp.get_data(as_text=True)
        assert "PASS" not in resp.get_data(as_text=True)
        nzbget = next(c for c in resp.get_json()["clients"] if c["type"] == "nzbget")
        assert "api_key" not in nzbget["config"] and "password" not in nzbget["config"]
        assert nzbget["config"]["username"] == "u"
        assert sorted(nzbget["secrets_set"]) == ["api_key", "password"]


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

    @patch("core.database.update_download_client_validity")
    @patch("models.download_clients.get_download_client_by_name")
    @patch("core.database.get_download_client_config",
           return_value={"host": "h", "target_directory": "/downloads/temp/"})
    def test_form_values_override_saved_config(self, mock_cfg, mock_get, mock_validity, client):
        # Test must report on what the user is looking at. Testing the stored
        # value instead re-reports the old path and the fix looks ineffective.
        mock_get.return_value = MagicMock(test_connection=MagicMock(return_value=True))
        client.post("/api/download-clients/airdcpp/test",
                    json={"target_directory": "F:\\downloads\\temp\\"})
        cfg = mock_get.call_args[0][1]
        assert cfg.target_directory == "F:\\downloads\\temp\\"
        assert cfg.host == "h"  # untouched fields survive the merge

    @patch("core.database.update_download_client_validity")
    @patch("models.download_clients.get_download_client_by_name")
    @patch("core.database.get_download_client_config",
           return_value={"host": "h", "api_key": "k"})
    def test_bodyless_post_still_tests_saved_config(self, mock_cfg, mock_get, mock_validity, client):
        mock_get.return_value = MagicMock(test_connection=MagicMock(return_value=True))
        resp = client.post("/api/download-clients/sabnzbd/test")
        assert resp.get_json()["valid"] is True
        assert mock_get.call_args[0][1].host == "h"

    @patch("core.database.get_download_client_config", return_value=None)
    def test_form_values_alone_are_enough_to_test(self, mock_cfg, client):
        # Nothing saved yet: a first-time config should still be testable.
        with patch("models.download_clients.get_download_client_by_name") as mock_get, \
                patch("core.database.update_download_client_validity"):
            mock_get.return_value = MagicMock(test_connection=MagicMock(return_value=True))
            resp = client.post("/api/download-clients/sabnzbd/test",
                               json={"host": "newhost", "api_key": "k"})
        assert resp.status_code == 200
        assert mock_get.call_args[0][1].host == "newhost"


class TestDownloadClientTestStoredSecrets:
    """A stored secret is only reused for the stored destination.

    The settings form leaves a secret field out to mean "keep the saved one",
    so the route merges the request over the stored config. That merge must
    not carry the saved password or API key to any other scheme/host/port, or
    to another username.
    """

    URL_SAB = "/api/download-clients/sabnzbd/test"
    URL_NZBGET = "/api/download-clients/nzbget/test"

    SAB_STORED = {"host": "nas.local", "port": 8080, "use_ssl": False,
                  "api_key": "stored-test-key", "category": "comics"}
    NZBGET_STORED = {"host": "nas.local", "port": 6789, "use_ssl": False,
                     "username": "nzbuser", "password": "stored-test-pass"}

    def _post(self, client, url, stored, body):
        with patch("core.database.get_download_client_config", return_value=stored), \
                patch("core.database.update_download_client_validity") as validity, \
                patch("models.download_clients.get_download_client_by_name") as factory:
            factory.return_value = MagicMock(
                test_connection=MagicMock(return_value=True), last_error=None)
            resp = client.post(url, json=body)
        return resp, factory, validity

    # -- allowed ---------------------------------------------------------

    def test_unsaved_full_config_is_tested(self, client):
        resp, factory, _ = self._post(client, self.URL_SAB, None, {
            "host": "other.local", "port": 9090, "api_key": "typed-test-key"})
        assert resp.status_code == 200
        assert resp.get_json()["valid"] is True
        cfg = factory.call_args[0][1]
        assert cfg.host == "other.local" and cfg.api_key == "typed-test-key"

    def test_typed_secret_may_go_to_a_new_host(self, client):
        resp, factory, _ = self._post(client, self.URL_SAB, self.SAB_STORED, {
            "host": "other.local", "port": 9090, "use_ssl": True,
            "api_key": "typed-test-key"})
        assert resp.status_code == 200
        assert factory.call_args[0][1].api_key == "typed-test-key"

    def test_stored_secret_with_same_destination(self, client):
        # What the form sends when only a non-connection field was edited.
        resp, factory, _ = self._post(client, self.URL_SAB, self.SAB_STORED, {
            "host": "nas.local", "port": 8080, "use_ssl": False,
            "category": "manga"})
        assert resp.status_code == 200
        cfg = factory.call_args[0][1]
        assert cfg.api_key == "stored-test-key"
        assert cfg.category == "manga"

    def test_bodyless_test_uses_stored_secret(self, client):
        resp, factory, _ = self._post(client, self.URL_SAB, self.SAB_STORED, None)
        assert resp.status_code == 200
        assert factory.call_args[0][1].api_key == "stored-test-key"

    @pytest.mark.parametrize("body", [
        {"host": "NAS.LOCAL"},                       # host case
        {"host": "nas.local:8080", "port": None},    # port moved into the host
        {"host": "http://nas.local"},                # scheme spelled out
        {"url_base": "sabnzbd"},                     # path only, same server
    ])
    def test_equivalent_destination_is_the_same(self, client, body):
        resp, factory, _ = self._post(client, self.URL_SAB, self.SAB_STORED, body)
        assert resp.status_code == 200, resp.get_json()
        assert factory.call_args[0][1].api_key == "stored-test-key"

    def test_stored_password_same_host_and_user(self, client):
        resp, factory, _ = self._post(client, self.URL_NZBGET, self.NZBGET_STORED, {
            "host": "nas.local", "port": 6789, "username": "nzbuser",
            "use_ssl": False})
        assert resp.status_code == 200
        assert factory.call_args[0][1].password == "stored-test-pass"

    # -- refused ---------------------------------------------------------

    @pytest.mark.parametrize("body", [
        {"host": "other.local"},                     # host
        {"port": 9090},                              # port
        {"use_ssl": True},                           # scheme
        {"host": "https://nas.local"},               # scheme via the host field
        {"host": "nas.local:9090", "port": None},    # port via the host field
        {"host": "nas.local@other.local"},           # userinfo trick
        {"host": "nas.local\\@other.local"},         # parser-differential trick
        {"port": "8080@other.local"},                # authority smuggled in the port
    ])
    def test_changed_destination_refused_without_new_secret(self, client, body):
        resp, factory, validity = self._post(client, self.URL_SAB, self.SAB_STORED, body)
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["success"] is False
        assert "Re-enter the API key" in data["error"]
        assert "stored-test-key" not in resp.get_data(as_text=True)
        factory.assert_not_called()       # no client, so no outbound request
        validity.assert_not_called()

    def test_changed_username_refused_without_new_password(self, client):
        resp, factory, _ = self._post(client, self.URL_NZBGET, self.NZBGET_STORED, {
            "username": "someone-else"})
        assert resp.status_code == 400
        assert "Re-enter the password" in resp.get_json()["error"]
        factory.assert_not_called()

    def test_changed_host_refused_for_password_client(self, client):
        resp, factory, _ = self._post(client, self.URL_NZBGET, self.NZBGET_STORED, {
            "host": "other.local", "port": 6789, "username": "nzbuser"})
        assert resp.status_code == 400
        assert "password" in resp.get_json()["error"]
        factory.assert_not_called()

    def test_airdcpp_changed_host_refused(self, client):
        stored = {"host": "dc.local", "port": 5600, "username": "dcuser",
                  "password": "stored-test-pass"}
        resp, factory, _ = self._post(
            client, "/api/download-clients/airdcpp/test", stored, {"host": "other.local"})
        assert resp.status_code == 400
        factory.assert_not_called()

    def test_explicitly_blank_secret_is_not_a_reuse(self, client):
        # A field sent empty replaces the stored value; nothing saved goes out.
        resp, factory, _ = self._post(client, self.URL_SAB, self.SAB_STORED, {
            "host": "other.local", "api_key": ""})
        assert resp.status_code == 200
        assert not factory.call_args[0][1].api_key

    def test_non_object_body_refused(self, client):
        resp, factory, _ = self._post(client, self.URL_SAB, self.SAB_STORED, ["x"])
        assert resp.status_code == 400
        factory.assert_not_called()


class TestDownloadClientRoles:
    """Client and indexer configuration is owner-only, like the pages that
    manage it; the two routes Clerk pages use stay Clerk-level."""

    @pytest.fixture(autouse=True)
    def _users(self, db_connection, monkeypatch):
        from core.database import create_user
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("owner", password="ownerpass", role="owner")
        create_user("clerk", password="clerkpass", role="clerk")
        yield

    @staticmethod
    def _login(client, username, password):
        client.post("/login", data={"username": username, "password": password})

    @pytest.mark.parametrize("method,path", [
        ("GET", "/api/download-clients"),
        ("GET", "/api/download-clients/sabnzbd/config"),
        ("POST", "/api/download-clients/sabnzbd/config"),
        ("DELETE", "/api/download-clients/sabnzbd/config"),
        ("POST", "/api/download-clients/sabnzbd/test"),
        ("POST", "/api/download-clients/sabnzbd/activate"),
        ("GET", "/api/indexers"),
        ("POST", "/api/indexers"),
        ("GET", "/api/indexers/1"),
        ("PUT", "/api/indexers/1"),
        ("DELETE", "/api/indexers/1"),
        ("POST", "/api/indexers/reorder"),
        ("POST", "/api/indexers/test"),
        ("POST", "/api/indexers/1/test"),
    ])
    def test_clerk_denied_configuration(self, client, method, path):
        self._login(client, "clerk", "clerkpass")
        with patch("models.download_clients.get_download_client_by_name") as factory, \
                patch("models.indexers.get_indexer_impl") as impl:
            resp = client.open(path, method=method, json={"host": "other.local"})
        assert resp.status_code == 403
        factory.assert_not_called()
        impl.assert_not_called()

    def test_owner_can_test(self, client):
        self._login(client, "owner", "ownerpass")
        with patch("core.database.get_download_client_config",
                   return_value={"host": "nas.local", "api_key": "stored-test-key"}), \
                patch("core.database.update_download_client_validity"), \
                patch("models.download_clients.get_download_client_by_name") as factory:
            factory.return_value = MagicMock(test_connection=MagicMock(return_value=True))
            resp = client.post("/api/download-clients/sabnzbd/test")
        assert resp.status_code == 200
        assert resp.get_json()["valid"] is True

    @patch("models.dcpp.dcpp_enabled_and_configured", return_value=False)
    @patch("models.usenet.usenet_enabled_and_configured", return_value=False)
    def test_clerk_keeps_source_order(self, mock_un, mock_dc, client):
        self._login(client, "clerk", "clerkpass")
        assert client.get("/api/download-clients/sources").status_code == 200

    @patch("models.dcpp.clear_dcpp_jobs", return_value=0)
    @patch("models.usenet.clear_usenet_jobs", return_value=0)
    def test_clerk_keeps_clear_downloads(self, mock_usenet, mock_dcpp, client):
        self._login(client, "clerk", "clerkpass")
        resp = client.post("/api/download-clients/downloads/clear",
                           json={"bucket": "completed"})
        assert resp.status_code == 200

    @pytest.mark.parametrize("method,path", [
        ("GET", "/api/usenet/downloads"),
        ("GET", "/api/dcpp/downloads"),
    ])
    def test_clerk_keeps_download_status(self, client, method, path):
        self._login(client, "clerk", "clerkpass")
        with patch("models.usenet.get_usenet_downloads", return_value=[]), \
                patch("models.dcpp.get_dcpp_downloads", return_value=[]):
            assert client.open(path, method=method).status_code == 200


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


class TestUnsavedIndexerTest:
    """POST /api/indexers/test: test what is in the form, before it is saved."""

    URL = "/api/indexers/test"

    @patch("core.database.update_indexer_validity")
    @patch("core.database.add_indexer")
    @patch("models.indexers.get_indexer_impl")
    def test_valid_records_nothing(self, mock_impl, mock_add, mock_validity, client):
        mock_impl.return_value = MagicMock(test_connection=MagicMock(return_value=True))
        resp = client.post(self.URL, json={"name": "NZBgeek", "url": "https://x", "api_key": "k"})
        assert resp.status_code == 200
        assert resp.get_json()["valid"] is True
        config = mock_impl.call_args[0][1]
        assert config.url == "https://x" and config.api_key == "k"
        # Same default category a save would store.
        assert config.categories == "7030"
        mock_add.assert_not_called()
        mock_validity.assert_not_called()

    @patch("models.indexers.get_indexer_impl")
    def test_invalid_surfaces_the_reason(self, mock_impl, client):
        impl = MagicMock(test_connection=MagicMock(return_value=False))
        impl.last_error = "Invalid API key"
        mock_impl.return_value = impl
        data = client.post(self.URL, json={"url": "https://x", "api_key": "bad"}).get_json()
        assert data["valid"] is False
        assert data["error"] == "Invalid API key"

    @pytest.mark.parametrize("body", [{}, {"url": "  "}, {"name": "x"}])
    def test_url_is_required(self, client, body):
        assert client.post(self.URL, json=body).status_code == 400

    def test_unknown_type_is_refused(self, client):
        resp = client.post(self.URL, json={"url": "https://x", "indexer_type": "torznab-ish"})
        assert resp.status_code == 400

    def test_is_not_swallowed_by_the_id_routes(self, app):
        adapter = app.url_map.bind("localhost")
        endpoint, _ = adapter.match("/api/indexers/test", method="POST")
        assert endpoint == "download_clients.test_unsaved_indexer"


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

    @patch("models.dcpp.get_dcpp_downloads", return_value=[
        {"download_id": None, "untracked": True, "filename": "Some Other Thing.rar",
         "status": "downloading", "client_type": "airdcpp", "percent": 12},
    ])
    def test_list_includes_untracked_bundles(self, mock_dl, client):
        # Bundles queued directly in AirDC++ are surfaced so the panel matches
        # the client's real queue, flagged so the UI keeps them read-only.
        dl = client.get("/api/dcpp/downloads").get_json()["downloads"][0]
        assert dl["untracked"] is True
        assert dl["download_id"] is None


class TestDcppDismiss:

    @patch("models.dcpp.dismiss_dcpp_job", return_value=True)
    def test_dismiss(self, mock_dismiss, client):
        resp = client.post("/api/dcpp/downloads/d1/dismiss")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        mock_dismiss.assert_called_once_with("d1")

    @patch("models.dcpp.dismiss_dcpp_job", return_value=False)
    def test_dismiss_unknown_job(self, mock_dismiss, client):
        assert client.post("/api/dcpp/downloads/nope/dismiss").status_code == 404

    @patch("models.dcpp.dismiss_dcpp_job", side_effect=Exception("boom"))
    def test_dismiss_error(self, mock_dismiss, client):
        assert client.post("/api/dcpp/downloads/d1/dismiss").status_code == 500


class TestClearClientDownloads:
    """POST /api/download-clients/downloads/clear -- both stores, one call.

    api.py's /clear_downloads only ever knew about download_progress, so a
    finished Usenet or DC++ row stayed on the status page: DC++ had to be
    dismissed one row at a time and Usenet could not be dismissed at all.
    """

    @patch("models.dcpp.clear_dcpp_jobs", return_value=1)
    @patch("models.usenet.clear_usenet_jobs", return_value=2)
    def test_clears_both_stores(self, mock_usenet, mock_dcpp, client):
        resp = client.post("/api/download-clients/downloads/clear",
                           json={"bucket": "completed"})
        assert resp.status_code == 200
        assert resp.get_json() == {"success": True, "cleared": 3}

    @patch("models.dcpp.clear_dcpp_jobs", return_value=0)
    @patch("models.usenet.clear_usenet_jobs", return_value=0)
    def test_completed_bucket_includes_import_pending(self, mock_usenet, mock_dcpp, client):
        # The whole point of the change: a row reading "Complete (import
        # pending)" must go when the user clears completed downloads.
        client.post("/api/download-clients/downloads/clear",
                    json={"bucket": "completed"})
        for mock in (mock_usenet, mock_dcpp):
            statuses = mock.call_args[0][0]
            assert "complete" in statuses
            assert "complete_no_move" in statuses
            assert "failed" not in statuses

    @patch("models.dcpp.clear_dcpp_jobs", return_value=0)
    @patch("models.usenet.clear_usenet_jobs", return_value=0)
    def test_failed_bucket_leaves_completed_alone(self, mock_usenet, mock_dcpp, client):
        client.post("/api/download-clients/downloads/clear",
                    json={"bucket": "failed"})
        for mock in (mock_usenet, mock_dcpp):
            statuses = mock.call_args[0][0]
            assert statuses == {"failed"}

    @patch("models.dcpp.clear_dcpp_jobs", return_value=0)
    @patch("models.usenet.clear_usenet_jobs", return_value=0)
    def test_bucket_defaults_to_completed(self, mock_usenet, mock_dcpp, client):
        resp = client.post("/api/download-clients/downloads/clear")
        assert resp.status_code == 200
        assert "complete_no_move" in mock_usenet.call_args[0][0]

    @patch("models.dcpp.clear_dcpp_jobs")
    @patch("models.usenet.clear_usenet_jobs")
    def test_unknown_bucket_is_rejected(self, mock_usenet, mock_dcpp, client):
        # 400, not a quiet "cleared 0" -- that would read as "nothing to clear".
        resp = client.post("/api/download-clients/downloads/clear",
                           json={"bucket": "everything"})
        assert resp.status_code == 400
        mock_usenet.assert_not_called()
        mock_dcpp.assert_not_called()

    @patch("models.usenet.clear_usenet_jobs", side_effect=Exception("boom"))
    def test_error(self, mock_usenet, client):
        assert client.post("/api/download-clients/downloads/clear",
                           json={"bucket": "completed"}).status_code == 500


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

    def test_grab_surfaces_the_real_reason(self, client):
        # A user-fixable failure (a Target Directory the AirDC++ host can't
        # parse) has to reach the toast, not just the log.
        def _refuse(*args, errors=None, **kwargs):
            errors.append("Target Directory '/downloads/temp/' is not a valid path")
            return None

        with patch("models.dcpp.grab_dcpp", side_effect=_refuse):
            resp = client.post("/api/dcpp/grab",
                               json={"result_token": "t1", "filename": "x.cbz"})
        assert resp.status_code == 502
        assert "not a valid path" in resp.get_json()["error"]
