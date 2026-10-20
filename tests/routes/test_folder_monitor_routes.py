"""Folder Monitoring endpoints, and the prompt after saving Metron credentials.

``routes/folder_monitor.py`` imports inside its views, so the supervisor is
patched at its real home (``core.monitor_supervisor.*``).
"""
from unittest.mock import patch

import pytest

URL = "/api/config/folder-monitor"

STATUS = {"enabled": True, "running": True, "pid": 4242,
          "watch": "/downloads/temp", "last_exit_code": None}


@pytest.fixture(autouse=True)
def _fresh_settings():
    from core.settings import invalidate

    invalidate()
    yield
    invalidate()


def test_routes_are_owner_only():
    """The setting starts a process that moves files out of WATCH; it must not
    fall through to the Clerk default the way /api/preferences/ does."""
    from core.auth import _OWNER_PREFIXES

    assert URL.startswith(_OWNER_PREFIXES)


class TestStatus:
    def test_returns_supervisor_status(self, client):
        with patch("core.monitor_supervisor.status", return_value=STATUS):
            resp = client.get(URL)
        assert resp.status_code == 200
        assert resp.get_json() == {"success": True, **STATUS}


class TestSet:
    def test_enable_saves_and_pokes(self, client):
        with patch("core.monitor_supervisor.poke") as poke, \
             patch("core.monitor_supervisor.status", return_value=STATUS):
            resp = client.post(URL, json={"enabled": True})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        poke.assert_called_once()
        from core.settings import get_bool
        assert get_bool("MONITOR_ENABLED") is True

    def test_disable(self, client):
        from core.settings import get_bool, set_setting
        set_setting("MONITOR_ENABLED", True)
        with patch("core.monitor_supervisor.poke"), \
             patch("core.monitor_supervisor.status", return_value=STATUS):
            resp = client.post(URL, json={"enabled": False})
        assert resp.status_code == 200
        assert get_bool("MONITOR_ENABLED") is False

    @pytest.mark.parametrize("body", [{}, {"enabled": "yes"}, {"enabled": 1}, None])
    def test_rejects_anything_but_a_bool(self, client, body):
        with patch("core.monitor_supervisor.poke") as poke:
            resp = client.post(URL, json=body) if body is not None else client.post(URL)
        assert resp.status_code == 400
        poke.assert_not_called()

    def test_failed_save_is_reported(self, client):
        with patch("core.settings.set_setting", return_value=False), \
             patch("core.monitor_supervisor.poke") as poke:
            resp = client.post(URL, json={"enabled": True})
        assert resp.status_code == 500
        poke.assert_not_called()


class TestMetronSaveSuggestsMonitoring:
    def _save(self, client, provider="metron", payload=None):
        with patch("core.database.save_provider_credentials", return_value=True), \
             patch("routes.metadata._validate_saved_credentials",
                   return_value=(True, None)), \
             patch("core.config.load_flask_config"), \
             patch("models.metron.clear_auth_block"):
            return client.post(f"/api/providers/{provider}/credentials",
                               json=payload or {"auth_mode": "token", "api_token": "tok"})

    def test_suggested_when_monitoring_is_off(self, client):
        data = self._save(client).get_json()
        assert data["suggest_enable_monitor"] is True

    def test_not_suggested_when_already_on(self, client):
        from core.settings import set_setting
        set_setting("MONITOR_ENABLED", True)
        assert self._save(client).get_json()["suggest_enable_monitor"] is False

    def test_not_suggested_when_the_setting_cannot_be_read(self, client):
        with patch("core.settings.peek_setting", return_value=None):
            assert self._save(client).get_json()["suggest_enable_monitor"] is False

    def test_other_providers_never_carry_it(self, client):
        data = self._save(client, "comicvine", {"api_key": "cv"}).get_json()
        assert "suggest_enable_monitor" not in data


PATHS_URL = "/api/config/folder-paths"


@pytest.fixture
def libraries(monkeypatch):
    """No configured libraries unless a test says otherwise."""
    def _set(*roots):
        monkeypatch.setattr("helpers.library.get_library_roots",
                            lambda: [str(r) for r in roots])
    _set()
    return _set


class TestFolderPaths:
    """WATCH/TARGET saved on their own, for the Guided Setup.

    The File Processing save takes the whole form and resets whatever a body
    leaves out, so it cannot be used to send just the two paths.
    """

    def test_route_is_owner_only(self):
        from core.auth import _OWNER_PREFIXES

        assert PATHS_URL.startswith(_OWNER_PREFIXES)

    def test_get_reports_unset_paths_and_defaults(self, client):
        data = client.get(PATHS_URL).get_json()
        assert data["success"] is True
        assert data["watch"] == "" and data["target"] == ""
        assert data["defaults"] == {"watch": "/downloads/temp",
                                    "target": "/downloads/processed"}

    def test_saves_both(self, app, client, tmp_path, libraries):
        watch, target = str(tmp_path / "w"), str(tmp_path / "t")
        resp = client.post(PATHS_URL, json={"watch": f"  {watch} ", "target": target})
        assert resp.status_code == 200
        data = resp.get_json()
        assert (data["watch"], data["target"]) == (watch, target)
        assert data["warnings"] == []
        assert app.config["WATCH"] == watch and app.config["TARGET"] == target

    def test_an_absent_key_is_left_alone(self, client, tmp_path, libraries):
        from core.database import set_user_preference
        set_user_preference("target", "/keep/me", category="file_processing")
        watch = str(tmp_path / "w")
        data = client.post(PATHS_URL, json={"watch": watch}).get_json()
        assert data["watch"] == watch
        assert data["target"] == "/keep/me"

    def test_empty_string_clears_back_to_the_default(self, app, client, libraries):
        from core.database import set_user_preference
        set_user_preference("watch", "/old", category="file_processing")
        data = client.post(PATHS_URL, json={"watch": ""}).get_json()
        assert data["watch"] == ""
        assert app.config["WATCH"] == "/downloads/temp"

    def test_a_blocked_path_writes_nothing(self, client, tmp_path):
        """Both are validated before either is written."""
        from core.config import get_watch_dir
        with patch("helpers.library.watch_target_verdict",
                   side_effect=[(False, None), (True, "TARGET cannot be /data")]):
            resp = client.post(PATHS_URL, json={"watch": str(tmp_path / "w"),
                                                "target": "/data"})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "TARGET cannot be /data"
        assert get_watch_dir() == ""

    def test_a_path_inside_a_library_saves_with_a_warning(self, client, tmp_path,
                                                         libraries):
        library = tmp_path / "library"
        library.mkdir()
        libraries(library)
        resp = client.post(PATHS_URL, json={"target": str(library)})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["target"] == str(library)
        assert len(data["warnings"]) == 1 and "TARGET" in data["warnings"][0]

    @pytest.mark.parametrize("body", [{}, {"other": "x"}, ["watch"], None])
    def test_rejects_a_body_with_nothing_to_save(self, client, body):
        resp = client.post(PATHS_URL, json=body) if body is not None \
            else client.post(PATHS_URL)
        assert resp.status_code == 400

    @pytest.mark.parametrize("value", [1, True, ["/x"], {"p": "/x"}])
    def test_rejects_a_non_string(self, client, value):
        assert client.post(PATHS_URL, json={"watch": value}).status_code == 400

    def test_failed_save_is_reported(self, client, tmp_path, libraries):
        with patch("core.database.set_user_preference", return_value=False):
            resp = client.post(PATHS_URL, json={"watch": str(tmp_path / "w")})
        assert resp.status_code == 500
