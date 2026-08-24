"""Tests for routes/notifications.py (notification settings endpoints)."""

from unittest.mock import patch

import pytest


class TestSaveNotificationConfig:
    def test_persists_all_three_preferences(self, client):
        from core.notifications import (
            EVENT_DOWNLOAD_COMPLETE, EVENT_DOWNLOAD_FAILED, EVENT_WANTED_ADDED,
            PREF_ENABLED, PREF_EVENTS, PREF_URLS,
        )

        with patch("core.database.set_user_preference") as save:
            response = client.post("/api/config/notifications", json={
                "enabled": True,
                "urls": "ntfy://ntfy.sh/a\n\n# a note\nntfy://ntfy.sh/b",
                "events": {
                    EVENT_DOWNLOAD_COMPLETE: True,
                    EVENT_DOWNLOAD_FAILED: False,
                    EVENT_WANTED_ADDED: True,
                },
            })

        assert response.status_code == 200
        assert response.get_json()["success"] is True

        stored = {call.args[0]: call.args[1] for call in save.call_args_list}
        assert stored[PREF_ENABLED] is True
        assert stored[PREF_URLS] == ["ntfy://ntfy.sh/a", "ntfy://ntfy.sh/b"]
        assert stored[PREF_EVENTS][EVENT_DOWNLOAD_FAILED] is False
        assert stored[PREF_EVENTS][EVENT_DOWNLOAD_COMPLETE] is True

    def test_drops_unknown_event_ids(self, client):
        from core.notifications import EVENT_DEFS, PREF_EVENTS

        with patch("core.database.set_user_preference") as save:
            client.post("/api/config/notifications", json={
                "enabled": True,
                "urls": "ntfy://ntfy.sh/a",
                "events": {"bogus_event": True},
            })

        stored = {call.args[0]: call.args[1] for call in save.call_args_list}
        assert set(stored[PREF_EVENTS]) == set(EVENT_DEFS)

    def test_can_be_saved_disabled_with_no_urls(self, client):
        from core.notifications import PREF_ENABLED, PREF_URLS

        with patch("core.database.set_user_preference") as save:
            response = client.post("/api/config/notifications", json={
                "enabled": False,
                "urls": "",
                "events": {},
            })

        assert response.status_code == 200
        stored = {call.args[0]: call.args[1] for call in save.call_args_list}
        assert stored[PREF_ENABLED] is False
        assert stored[PREF_URLS] == []

    def test_rejects_enabled_with_no_urls(self, client):
        """Enabling with nowhere to send is a silent no-op — reject it loudly."""
        with patch("core.database.set_user_preference") as save:
            response = client.post("/api/config/notifications", json={
                "enabled": True,
                "urls": "  \n# nothing\n",
                "events": {},
            })

        assert response.status_code == 400
        body = response.get_json()
        assert body["success"] is False
        assert "at least one notification URL" in body["error"]
        save.assert_not_called()

    def test_rejects_a_missing_body(self, client):
        response = client.post(
            "/api/config/notifications",
            data="not json",
            content_type="application/json",
        )
        assert response.status_code == 400
        assert response.get_json()["success"] is False

    def test_database_failure_returns_500(self, client):
        with patch(
            "core.database.set_user_preference", side_effect=RuntimeError("no db")
        ):
            response = client.post("/api/config/notifications", json={
                "enabled": True,
                "urls": "ntfy://ntfy.sh/a",
                "events": {},
            })

        assert response.status_code == 500
        assert response.get_json()["success"] is False


class TestTestNotification:
    def test_sends_to_the_posted_urls(self, client):
        with patch(
            "routes.notifications.send_test", return_value=(True, None)
        ) as send:
            response = client.post(
                "/api/config/notifications/test",
                json={"urls": "ntfy://ntfy.sh/a\nntfy://ntfy.sh/b"},
            )

        assert response.status_code == 200
        assert response.get_json()["success"] is True
        send.assert_called_once_with(["ntfy://ntfy.sh/a", "ntfy://ntfy.sh/b"])

    def test_surfaces_the_delivery_error(self, client):
        with patch(
            "routes.notifications.send_test",
            return_value=(False, "Apprise reported a delivery failure"),
        ):
            response = client.post(
                "/api/config/notifications/test",
                json={"urls": "ntfy://ntfy.sh/a"},
            )

        assert response.status_code == 502
        body = response.get_json()
        assert body["success"] is False
        assert "delivery failure" in body["error"]

    def test_requires_at_least_one_url(self, client):
        with patch("routes.notifications.send_test") as send:
            response = client.post(
                "/api/config/notifications/test", json={"urls": ""}
            )

        assert response.status_code == 400
        send.assert_not_called()

    def test_unexpected_error_returns_500(self, client):
        with patch(
            "routes.notifications.send_test", side_effect=RuntimeError("boom")
        ):
            response = client.post(
                "/api/config/notifications/test",
                json={"urls": "ntfy://ntfy.sh/a"},
            )

        assert response.status_code == 500
        assert response.get_json()["success"] is False


class TestListNotificationEvents:
    def test_returns_the_full_catalog(self, client):
        from core.notifications import EVENT_DEFS

        response = client.get("/api/config/notifications/events")

        assert response.status_code == 200
        body = response.get_json()
        assert body["success"] is True
        assert {e["id"] for e in body["events"]} == set(EVENT_DEFS)
        for entry in body["events"]:
            assert entry["label"]
            assert entry["description"]
