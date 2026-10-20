"""Guided Setup API (``routes/setup.py``).

The check logic is covered in ``tests/unit/test_setup_preflight.py``; these
assert the route, its payload shape and that it is owner-only.
"""
from unittest.mock import patch

import pytest

URL = "/api/setup/preflight"


def test_route_is_owner_only_by_prefix():
    from core.auth import _OWNER_PREFIXES

    assert URL.startswith(_OWNER_PREFIXES)


class TestPreflight:
    def test_returns_checks(self, client):
        resp = client.get(URL)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert isinstance(data["blocking"], bool)
        ids = [c["id"] for c in data["checks"]]
        for expected in ("database", "config_writable", "cache_writable",
                         "data_mount", "watch_dir", "target_dir",
                         "owner_account", "run_as_user"):
            assert expected in ids
        for c in data["checks"]:
            assert c["status"] in ("ok", "warn", "fail", "skip")

    def test_passes_the_payload_through(self, client):
        payload = {"success": True, "blocking": True, "summary": {}, "checks": []}
        with patch("core.setup_preflight.run_preflight", return_value=payload):
            resp = client.get(URL)
        assert resp.get_json() == payload


class TestAccess:
    @pytest.fixture(autouse=True)
    def _users(self, db_connection, monkeypatch):
        from core.database import create_user

        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("owner", password="ownerpass", role="owner")
        create_user("clerk", password="clerkpass", role="clerk")
        create_user("reader", password="readerpass", role="reader")

    def _login(self, client, username, password):
        client.post("/login", data={"username": username, "password": password})

    @pytest.mark.parametrize("user,pw", [("reader", "readerpass"),
                                         ("clerk", "clerkpass")])
    def test_non_owner_is_refused(self, client, user, pw):
        self._login(client, user, pw)
        assert client.get(URL).status_code == 403

    def test_owner_is_allowed(self, client):
        self._login(client, "owner", "ownerpass")
        assert client.get(URL).status_code == 200


class TestLogsPageSystemCheck:
    """The Logs page's System Check tab renders the preflight.

    ``/logs`` lives in app.py (a stub in the test app), so the real template is
    rendered directly. Clerks can read the logs, but the tab and the debug
    package are owner-only, so they are not offered to a clerk.
    """

    def _render(self, app, role):
        from flask import g, render_template

        with app.test_request_context("/logs"):
            g.current_user = {"role": role}
            return render_template("logs.html", config=app.config)

    def test_owner_sees_tab_and_debug_package(self, app):
        html = self._render(app, "owner")
        assert 'id="check-tab"' in html
        assert URL in html
        assert "/api/admin/debug-package" in html

    def test_clerk_does_not(self, app):
        html = self._render(app, "clerk")
        assert 'id="check-tab"' not in html
        assert URL not in html
        assert "/api/admin/debug-package" not in html
