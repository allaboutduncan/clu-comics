"""Tests for the local-ComicVine-DB auto-update endpoints in routes/metadata.py.

These three sit under ``/api/providers/`` rather than behind the generic
``/api/preferences/<key>`` route on purpose: ``core/auth.py`` gates that prefix
as owner-only, while ``/api/preferences/`` is not on the list and would let a
Clerk switch on a site-wide 541 MB download. ``tests/routes/test_rbac.py``
pins the gate; these pin the behaviour.
"""

from unittest.mock import patch

import pytest


class TestUpdateStatus:
    def test_returns_the_status(self, client):
        fake = {
            "enabled": False,
            "path_configured": True,
            "database_present": True,
            "database_size": 1234,
            "database_size_human": "1.2 KB",
            "last_check": None,
            "last_update": None,
            "last_error": None,
            "running": False,
            "interval_days": 14,
        }
        with patch("core.comicvine_db_update.get_status", return_value=fake):
            resp = client.get("/api/providers/comicvine_sqlite/update-status")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["status"]["interval_days"] == 14

    def test_never_leaks_the_source_url(self, client):
        """The page is allowed to know the cadence, not the host."""
        resp = client.get("/api/providers/comicvine_sqlite/update-status")
        body = resp.get_data(as_text=True).lower()
        assert "nerdfire" not in body
        assert "localcv" not in body

    def test_returns_500_on_an_unexpected_error(self, client):
        with patch("core.comicvine_db_update.get_status",
                   side_effect=RuntimeError("boom")):
            resp = client.get("/api/providers/comicvine_sqlite/update-status")

        assert resp.status_code == 500
        assert resp.get_json()["success"] is False


class TestAutoUpdateToggle:
    def test_enables(self, client):
        with patch("core.database.set_user_preference") as save:
            resp = client.post("/api/providers/comicvine_sqlite/auto-update",
                               json={"enabled": True})

        assert resp.status_code == 200
        assert resp.get_json()["enabled"] is True
        stored = {call.args[0]: call.args[1] for call in save.call_args_list}
        from core.comicvine_db_update import PREF_AUTO_UPDATE
        assert stored[PREF_AUTO_UPDATE] is True

    def test_disables(self, client):
        with patch("core.database.set_user_preference") as save:
            resp = client.post("/api/providers/comicvine_sqlite/auto-update",
                               json={"enabled": False})

        assert resp.status_code == 200
        assert resp.get_json()["enabled"] is False
        from core.comicvine_db_update import PREF_AUTO_UPDATE
        stored = {call.args[0]: call.args[1] for call in save.call_args_list}
        assert stored[PREF_AUTO_UPDATE] is False

    def test_rejects_a_payload_with_no_value(self, client):
        with patch("core.database.set_user_preference") as save:
            resp = client.post("/api/providers/comicvine_sqlite/auto-update",
                               json={})

        assert resp.status_code == 400
        save.assert_not_called()


class TestRunUpdate:
    def test_starts_a_background_run_and_returns_an_op_id(self, client):
        with patch("core.comicvine_db_update.is_running", return_value=False), \
             patch("core.comicvine_db_update.run_update",
                   return_value={"success": True, "updated": False,
                                 "message": "up to date"}):
            resp = client.post("/api/providers/comicvine_sqlite/update", json={})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["op_id"]

    def test_refuses_while_one_is_already_running(self, client):
        """Two runs would walk the same destination directory."""
        with patch("core.comicvine_db_update.is_running", return_value=True), \
             patch("core.comicvine_db_update.run_update") as run:
            resp = client.post("/api/providers/comicvine_sqlite/update", json={})

        assert resp.status_code == 409
        assert resp.get_json()["success"] is False
        run.assert_not_called()

    def test_forwards_the_force_flag(self, client):
        import time

        seen = {}

        def _capture(progress=None, force=False):
            seen["force"] = force
            return {"success": True, "updated": True, "message": "done"}

        with patch("core.comicvine_db_update.is_running", return_value=False), \
             patch("core.comicvine_db_update.run_update", side_effect=_capture):
            resp = client.post("/api/providers/comicvine_sqlite/update",
                               json={"force": True})
            assert resp.status_code == 200
            # The work runs on a daemon thread; give it a moment to land.
            for _ in range(50):
                if "force" in seen:
                    break
                time.sleep(0.02)

        assert seen.get("force") is True

    def test_does_not_shadow_the_generic_provider_routes(self, client):
        """The literal path must not be swallowed by /api/providers/<type>/test."""
        with patch("core.comicvine_db_update.get_status", return_value={}):
            resp = client.get("/api/providers/comicvine_sqlite/update-status")
        assert resp.status_code == 200
