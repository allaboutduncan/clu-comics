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
