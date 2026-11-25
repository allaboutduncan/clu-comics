"""routes/kavita.py: the owner's settings and each user's own Kavita link."""
import sys
from unittest.mock import MagicMock, patch

import pytest

pytest.importorskip("cryptography")

from core import kavita_store, kavita_sync
from core.auth import required_role_for_request
from core.database import create_user, get_user_by_username

SERVER = "http://kavita.local:5000"
CONFIG = {"server_url": SERVER, "frequency": "daily", "time": "05:30", "weekday": 0}


def _login(client, username):
    resp = client.post("/login", data={"username": username, "password": f"{username}pass"})
    assert resp.status_code in (200, 302)
    return client


@pytest.fixture(autouse=True)
def _users(db_connection, monkeypatch):
    monkeypatch.delenv("CLU_USERNAME", raising=False)
    monkeypatch.delenv("CLU_PASSWORD", raising=False)
    for name, role in (("owner", "owner"), ("alice", "reader"), ("bob", "reader")):
        create_user(name, password=f"{name}pass", role=role)
    yield


@pytest.fixture
def app_module(app):
    """The stand-in for app.py that routes import from (tests/routes/conftest.py)."""
    return sys.modules["app"]


@pytest.fixture
def owner(client):
    return _login(client, "owner")


@pytest.fixture
def alice(client):
    return _login(client, "alice")


@pytest.fixture
def kavita_ok():
    """A Kavita that accepts any key and says it belongs to 'kavita-user'."""
    with patch("models.kavita.KavitaClient") as cls:
        cls.return_value.test_connection.return_value = (True, "Connected successfully", "kavita-user")
        yield cls


@pytest.fixture
def threads():
    """Sync threads the routes start, captured instead of run."""
    with patch("routes.kavita.threading.Thread") as thread_cls:
        yield thread_cls


def _uid(name):
    return get_user_by_username(name)["id"]


class TestPolicy:
    @pytest.mark.parametrize("method,path,expected", [
        ("GET", "/api/kavita/config", "owner"),
        ("POST", "/api/kavita/config", "owner"),
        ("GET", "/api/kavita/status", "owner"),
        ("POST", "/api/kavita/sync", "owner"),
        ("GET", "/api/account/kavita", "reader"),
        ("POST", "/api/account/kavita", "reader"),
        ("DELETE", "/api/account/kavita", "reader"),
        ("POST", "/api/account/kavita/sync", "reader"),
    ])
    def test_required_role(self, app, method, path, expected):
        with app.test_request_context(path, method=method):
            assert required_role_for_request() == expected

    @pytest.mark.parametrize("method,path", [
        ("get", "/api/kavita/config"),
        ("post", "/api/kavita/config"),
        ("get", "/api/kavita/status"),
        ("post", "/api/kavita/sync"),
    ])
    def test_a_reader_is_refused_the_site_settings(self, alice, method, path):
        assert getattr(alice, method)(path, json={}).status_code == 403

    def test_signed_out_gets_nothing(self, client):
        assert client.get("/api/account/kavita").status_code in (302, 401)
        assert client.post("/api/account/kavita", json={"auth_key": "k"}).status_code in (302, 401)


class TestOwnerConfig:
    def test_unconfigured_defaults(self, owner):
        cfg = owner.get("/api/kavita/config").get_json()["config"]
        assert cfg["server_url"] == ""
        assert (cfg["frequency"], cfg["time"]) == ("disabled", "05:30")

    def test_a_saved_config_round_trips_and_reschedules(self, owner, app_module):
        app_module.configure_kavita_sync_schedule.reset_mock()

        resp = owner.post("/api/kavita/config", json={**CONFIG, "frequency": "weekly", "weekday": 3})

        assert resp.get_json()["success"] is True
        app_module.configure_kavita_sync_schedule.assert_called_once_with()
        cfg = owner.get("/api/kavita/config").get_json()["config"]
        assert cfg["server_url"] == SERVER
        assert (cfg["frequency"], cfg["time"], cfg["weekday"]) == ("weekly", "05:30", 3)

    def test_library_mappings_round_trip(self, owner):
        from core.database import add_library

        library_id = add_library("Kavita Comics", "/kavita-comics")
        mapping = [{"library_id": library_id, "kavita_path_prefix": "/manga"}]
        assert owner.post("/api/kavita/config", json={**CONFIG, "library_mappings": mapping}).status_code == 200
        cfg = owner.get("/api/kavita/config").get_json()["config"]
        assert {m["library_id"]: m["kavita_path_prefix"] for m in cfg["library_mappings"]}[library_id] == "/manga"

    @pytest.mark.parametrize("bad", [
        {"server_url": "kavita.local:5000"},
        {"server_url": "ftp://kavita.local"},
        {"frequency": "hourly"},
        {"time": "25:00"},
        {"weekday": 9},
        {"library_mappings": "nope"},
    ])
    def test_bad_input_is_a_400_and_saves_nothing(self, owner, app_module, bad):
        app_module.configure_kavita_sync_schedule.reset_mock()
        resp = owner.post("/api/kavita/config", json={**CONFIG, **bad})
        assert resp.status_code == 400
        assert kavita_store.get_server_url() == ""
        app_module.configure_kavita_sync_schedule.assert_not_called()

    def test_changing_the_server_clears_the_import_log(self, owner):
        owner.post("/api/kavita/config", json=CONFIG)
        kavita_store.log_synced(_uid("alice"), 10, "/kavita/a.cbz", "/data/a.cbz")

        owner.post("/api/kavita/config", json=CONFIG)
        assert kavita_store.synced_chapter_ids(_uid("alice")) == {"10"}

        owner.post("/api/kavita/config", json={**CONFIG, "server_url": "http://other:5000"})
        assert kavita_store.synced_chapter_ids(_uid("alice")) == set()

    def test_status_lists_linked_users_without_keys(self, owner):
        kavita_store.save_link(_uid("alice"), "alice-secret-key", "kavita-alice")
        kavita_store.set_link_invalid(_uid("alice"), "HTTP 401")

        resp = owner.get("/api/kavita/status")

        body = resp.get_json()
        assert body["running"] is False
        assert body["next_run"] == "2026-01-15 03:00:00"
        assert [(u["username"], u["kavita_username"], u["connection_valid"]) for u in body["users"]] == [
            ("alice", "kavita-alice", False)]
        assert "alice-secret-key" not in resp.get_data(as_text=True)

    def test_status_explains_the_last_run(self, owner, alice):
        kavita_store.save_link(_uid("alice"), "alice-key", "kavita-alice")
        kavita_store.record_sync_result(
            _uid("alice"), 0, 0, "2025-01-02 03:04:05",
            detail={"chapters": 2, "matched": 0, "no_match": 2})
        expected = {"chapters": 2, "matched": 0, "no_match": 2}
        assert alice.get("/api/account/kavita").get_json()["detail"] == expected


class TestOwnerSync:
    def test_needs_a_server_url(self, owner, threads):
        assert owner.post("/api/kavita/sync", json={}).status_code == 400
        threads.assert_not_called()

    def test_starts_a_background_sync_of_everyone(self, owner, threads, app):
        owner.post("/api/kavita/config", json=CONFIG)

        resp = owner.post("/api/kavita/sync", json={"full": True})

        assert resp.get_json()["success"] is True
        kwargs = threads.call_args[1]
        assert kwargs["target"] is kavita_sync.sync_all
        assert kwargs["kwargs"]["full"] is True
        # The thread has no request, so it is handed the real app object.
        assert kwargs["kwargs"]["app"] is app
        threads.return_value.start.assert_called_once_with()

    def test_a_running_sync_answers_409(self, owner, threads):
        owner.post("/api/kavita/config", json=CONFIG)
        with patch("core.kavita_sync.is_running", return_value=True):
            assert owner.post("/api/kavita/sync", json={}).status_code == 409
        threads.assert_not_called()


class TestAccountLink:
    def test_unlinked_and_unconfigured(self, alice):
        body = alice.get("/api/account/kavita").get_json()
        assert (body["configured"], body["linked"]) == (False, False)

    def test_linking_needs_the_server_to_be_set_up(self, alice, kavita_ok):
        resp = alice.post("/api/account/kavita", json={"auth_key": "alice-secret-key"})
        assert resp.status_code == 400
        kavita_ok.assert_not_called()

    def test_a_reader_links_their_own_account(self, alice, kavita_ok):
        kavita_store.set_server_url(SERVER)

        resp = alice.post("/api/account/kavita", json={"auth_key": " alice-secret-key "})

        body = resp.get_json()
        assert resp.status_code == 200
        assert (body["linked"], body["kavita_username"], body["connection_valid"]) == (
            True, "kavita-user", True)
        kavita_ok.assert_called_once_with(SERVER, "alice-secret-key")
        assert kavita_store.get_link(_uid("alice"))["auth_key"] == "alice-secret-key"
        # Nobody else was linked.
        assert kavita_store.get_link(_uid("bob")) is None
        assert kavita_store.get_link(_uid("owner")) is None

    def test_no_response_carries_the_key(self, alice, kavita_ok):
        kavita_store.set_server_url(SERVER)
        saved = alice.post("/api/account/kavita", json={"auth_key": "alice-secret-key"})
        assert "alice-secret-key" not in saved.get_data(as_text=True)
        assert "alice-secret-key" not in alice.get("/api/account/kavita").get_data(as_text=True)

    def test_a_key_kavita_rejects_is_not_stored(self, alice):
        kavita_store.set_server_url(SERVER)
        with patch("models.kavita.KavitaClient") as cls:
            cls.return_value.test_connection.return_value = (False, "Kavita rejected the auth key", None)
            resp = alice.post("/api/account/kavita", json={"auth_key": "wrong"})
        assert resp.status_code == 400
        assert "rejected" in resp.get_json()["error"]
        assert kavita_store.get_link(_uid("alice")) is None

    def test_an_empty_key_is_a_400(self, alice, kavita_ok):
        kavita_store.set_server_url(SERVER)
        assert alice.post("/api/account/kavita", json={"auth_key": "  "}).status_code == 400
        kavita_ok.assert_not_called()

    def test_one_user_cannot_see_anothers_link(self, client, kavita_ok):
        kavita_store.set_server_url(SERVER)
        kavita_store.save_link(_uid("bob"), "bob-secret-key", "kavita-bob")

        resp = _login(client, "alice").get("/api/account/kavita")

        assert resp.get_json()["linked"] is False
        assert "kavita-bob" not in resp.get_data(as_text=True)

    def test_unlinking_removes_only_the_callers_link(self, alice):
        kavita_store.set_server_url(SERVER)
        kavita_store.save_link(_uid("alice"), "alice-key")
        kavita_store.save_link(_uid("bob"), "bob-key")

        resp = alice.delete("/api/account/kavita")

        assert resp.get_json()["linked"] is False
        assert kavita_store.get_link(_uid("alice")) is None
        assert kavita_store.get_link(_uid("bob")) is not None


class TestAccountSync:
    def test_needs_a_link(self, alice, threads):
        kavita_store.set_server_url(SERVER)
        assert alice.post("/api/account/kavita/sync", json={}).status_code == 400
        threads.assert_not_called()

    def test_a_rejected_key_must_be_replaced_first(self, alice, threads):
        kavita_store.set_server_url(SERVER)
        kavita_store.save_link(_uid("alice"), "alice-key")
        kavita_store.set_link_invalid(_uid("alice"), "HTTP 401")
        resp = alice.post("/api/account/kavita/sync", json={})
        assert resp.status_code == 400
        threads.assert_not_called()

    def test_syncs_only_the_caller(self, alice, threads):
        kavita_store.set_server_url(SERVER)
        kavita_store.save_link(_uid("alice"), "alice-key")
        kavita_store.save_link(_uid("bob"), "bob-key")

        resp = alice.post("/api/account/kavita/sync", json={})

        assert resp.get_json()["success"] is True
        kwargs = threads.call_args[1]
        assert kwargs["target"] is kavita_sync.sync_user
        assert kwargs["kwargs"]["user_id"] == _uid("alice")
        assert kwargs["kwargs"]["full"] is False

    def test_a_running_sync_answers_409(self, alice, threads):
        kavita_store.set_server_url(SERVER)
        kavita_store.save_link(_uid("alice"), "alice-key")
        with patch("core.kavita_sync.is_running", return_value=True):
            assert alice.post("/api/account/kavita/sync", json={}).status_code == 409
        threads.assert_not_called()


class TestPages:
    def test_the_account_page_has_the_kavita_tab(self, alice):
        html = alice.get("/account").get_data(as_text=True)
        assert 'id="kavitaAccountPanel"' in html
        assert "js/kavita.js" in html
        assert "CLU.kavita.initAccount()" in html
