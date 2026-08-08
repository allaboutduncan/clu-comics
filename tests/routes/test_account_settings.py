"""Per-user personalization on /account.

Covers the two self-service endpoints (/api/account/appearance, /api/account/
dashboard), the override -> site-default fallback as it reaches the rendered
page, and the implicit-owner (no-login) path.
"""
import pytest

from core.database import (
    create_user,
    get_user_preference,
    get_user_setting_override,
    set_user_preference,
    set_user_setting,
)


def _login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


def _uid(username):
    from core.database import get_user_by_username
    return get_user_by_username(username)["id"]


# ---------------------------------------------------------------------------
# Multi-user: real login, real RBAC
# ---------------------------------------------------------------------------
class TestMultiUser:
    @pytest.fixture(autouse=True)
    def _users(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("owner", password="ownerpass", role="owner")
        create_user("reader", password="readerpass", role="reader")
        create_user("reader2", password="reader2pass", role="reader")
        set_user_preference("bootstrap_theme", "flatly", category="personalization")
        yield

    # -- Appearance --------------------------------------------------------

    def test_reader_can_save_own_theme(self, client):
        _login(client, "reader", "readerpass")
        resp = client.post("/api/account/appearance", json={"bootstrapTheme": "darkly"})

        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert get_user_setting_override("bootstrap_theme", user_id=_uid("reader")) == "darkly"

    def test_saving_own_theme_leaves_site_default_alone(self, client):
        _login(client, "reader", "readerpass")
        client.post("/api/account/appearance", json={"bootstrapTheme": "darkly"})

        assert get_user_preference("bootstrap_theme") == "flatly"

    def test_one_users_theme_does_not_affect_another(self, client):
        _login(client, "reader", "readerpass")
        client.post("/api/account/appearance", json={"bootstrapTheme": "darkly"})

        assert get_user_setting_override("bootstrap_theme", user_id=_uid("reader2")) is None

    def test_unknown_theme_rejected_and_not_stored(self, client):
        _login(client, "reader", "readerpass")
        resp = client.post("/api/account/appearance", json={"bootstrapTheme": "not-a-theme"})

        assert resp.status_code == 400
        assert resp.get_json()["success"] is False
        assert get_user_setting_override("bootstrap_theme", user_id=_uid("reader")) is None

    def test_use_site_default_clears_override(self, client):
        _login(client, "reader", "readerpass")
        client.post("/api/account/appearance", json={"bootstrapTheme": "darkly"})

        resp = client.post("/api/account/appearance", json={"useSiteDefault": True})

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["override"] is False
        assert body["theme"] == "flatly"
        assert get_user_setting_override("bootstrap_theme", user_id=_uid("reader")) is None

    # -- Dashboard ---------------------------------------------------------

    def test_reader_can_save_own_dashboard(self, client):
        from routes.collection import DEFAULT_DASHBOARD_ORDER

        _login(client, "reader", "readerpass")
        resp = client.post("/api/account/dashboard", json={
            "dashboardOrder": ["library", "favorites"],
            "dashboardHidden": ["discover"],
        })

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["dashboardOrder"][:2] == ["library", "favorites"]
        # Sections missing from the payload are backfilled, never dropped.
        assert set(body["dashboardOrder"]) == set(DEFAULT_DASHBOARD_ORDER)
        assert body["dashboardHidden"] == ["discover"]

    def test_dashboard_junk_ids_are_dropped(self, client):
        _login(client, "reader", "readerpass")
        resp = client.post("/api/account/dashboard", json={
            "dashboardOrder": ["library", "../etc/passwd", "bogus"],
            "dashboardHidden": ["also-bogus"],
        })

        body = resp.get_json()
        assert "../etc/passwd" not in body["dashboardOrder"]
        assert "bogus" not in body["dashboardOrder"]
        assert body["dashboardHidden"] == []

    def test_dashboard_is_per_user(self, client):
        _login(client, "reader", "readerpass")
        client.post("/api/account/dashboard", json={
            "dashboardOrder": ["library"], "dashboardHidden": ["favorites"],
        })

        assert get_user_setting_override("dashboard_hidden", user_id=_uid("reader")) == ["favorites"]
        assert get_user_setting_override("dashboard_hidden", user_id=_uid("reader2")) is None

    def test_dashboard_use_site_default_clears_both_keys(self, client):
        _login(client, "reader", "readerpass")
        client.post("/api/account/dashboard", json={
            "dashboardOrder": ["library"], "dashboardHidden": ["favorites"],
        })

        resp = client.post("/api/account/dashboard", json={"useSiteDefault": True})

        assert resp.status_code == 200
        assert resp.get_json()["override"] is False
        uid = _uid("reader")
        assert get_user_setting_override("dashboard_order", user_id=uid) is None
        assert get_user_setting_override("dashboard_hidden", user_id=uid) is None

    # -- Page render -------------------------------------------------------

    def test_account_page_renders_all_three_panes(self, client):
        _login(client, "reader", "readerpass")
        html = client.get("/account").get_data(as_text=True)

        assert client.get("/account").status_code == 200
        assert 'id="appearance"' in html
        assert 'id="dashboard"' in html
        assert 'id="tokens"' in html

    def test_account_page_shows_following_site_default(self, client):
        _login(client, "reader", "readerpass")
        html = client.get("/account").get_data(as_text=True)
        assert "Following site default" in html

    def test_account_page_shows_custom_once_overridden(self, client):
        _login(client, "reader", "readerpass")

        before = client.get("/account").get_data(as_text=True)
        assert 'id="themeStateBadge"' in before
        assert "Following site default" in before

        client.post("/api/account/appearance", json={"bootstrapTheme": "darkly"})
        after = client.get("/account").get_data(as_text=True)

        # The appearance badge flips to Custom; the dashboard one does not.
        assert after.count("Following site default") == before.count("Following site default") - 1
        assert after.count("Custom") == before.count("Custom") + 1

    def test_account_page_uses_no_native_dialogs(self, client):
        """CLAUDE.md forbids alert()/confirm()/prompt() — token revoke uses a modal."""
        _login(client, "reader", "readerpass")
        html = client.get("/account").get_data(as_text=True)

        assert "confirm(" not in html
        assert "prompt(" not in html
        assert "revokeTokenModal" in html

    def test_theme_select_is_populated(self, client):
        _login(client, "reader", "readerpass")
        html = client.get("/account").get_data(as_text=True)

        assert 'id="accountTheme"' in html
        assert 'value="darkly"' in html
        assert "Darkly (Dark)" in html

    def test_unauthenticated_gets_401(self, client):
        assert client.post("/api/account/appearance", json={"bootstrapTheme": "darkly"}).status_code == 401


# ---------------------------------------------------------------------------
# Header identity
# ---------------------------------------------------------------------------
class TestHeaderIdentity:
    @pytest.fixture(autouse=True)
    def _users(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("owner", password="ownerpass", role="owner")
        create_user("reader", password="readerpass", role="reader")
        yield

    def test_username_and_logout_shown_when_logged_in(self, client):
        _login(client, "reader", "readerpass")
        # A page other than /account, so "My Account" here can only come from
        # the header rather than the page's own heading.
        html = client.get("/reading-lists").get_data(as_text=True)

        assert 'id="userMenu"' in html
        assert "bi-person-circle" in html
        assert ">reader<" in html
        assert "/logout" in html
        assert "My Account" in html

    def test_logout_is_not_in_the_gear_menu(self, client):
        """Logout moved into the user dropdown; the gear must not duplicate it."""
        _login(client, "reader", "readerpass")
        html = client.get("/reading-lists").get_data(as_text=True)
        assert html.count("/logout") == 1


class TestImplicitOwnerHeader:
    @pytest.fixture(autouse=True)
    def _single_user(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("owner", password="ownerpass", role="owner")
        yield

    def test_no_username_chip_without_login(self, client):
        """Single-user installs never logged in — "signed in as owner" would lie."""
        html = client.get("/reading-lists").get_data(as_text=True)

        assert 'id="userMenu"' not in html
        assert "/logout" not in html
        # ...but /account must still be reachable, via the gear menu.
        assert "My Account" in html
        assert "/account" in html

    def test_account_page_loads_without_a_session(self, client):
        assert client.get("/account").status_code == 200

    def test_settings_save_without_a_session(self, client):
        assert client.post(
            "/api/account/appearance", json={"bootstrapTheme": "darkly"}
        ).status_code == 200
        assert client.post(
            "/api/account/dashboard", json={"dashboardOrder": ["library"]}
        ).status_code == 200


# ---------------------------------------------------------------------------
# Theme resolution reaching the rendered page
# ---------------------------------------------------------------------------
class TestRenderedTheme:
    """The context processor lives in app.py, which route tests can't import.

    These exercise the same resolution helper the processor calls, so the chain
    (override -> app.config site default) is still pinned.
    """

    @pytest.fixture(autouse=True)
    def _users(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("owner", password="ownerpass", role="owner")
        create_user("reader", password="readerpass", role="reader")
        set_user_preference("bootstrap_theme", "flatly", category="personalization")
        yield

    def test_override_selected_in_the_picker(self, client):
        set_user_setting("bootstrap_theme", "cyborg", user_id=_uid("reader"))
        _login(client, "reader", "readerpass")
        html = client.get("/account").get_data(as_text=True)

        assert '<option value="cyborg" selected>' in html

    def test_site_default_selected_without_an_override(self, client):
        _login(client, "reader", "readerpass")
        html = client.get("/account").get_data(as_text=True)

        assert '<option value="flatly" selected>' in html


# ---------------------------------------------------------------------------
# Owner shadowing: saving the site default resets the owner's own override
# ---------------------------------------------------------------------------
class TestOwnerShadowing:

    def test_site_default_save_clears_callers_override(self, db_connection):
        """Without this, an owner with a personal theme sets the site default on
        /config and sees nothing change, because their override still wins."""
        from core.database import delete_user_setting

        create_user("owner", password="ownerpass", role="owner")
        uid = _uid("owner")
        set_user_setting("bootstrap_theme", "darkly", user_id=uid)

        # What app.py:save_styling_config does after writing the global pref.
        set_user_preference("bootstrap_theme", "lux", category="personalization")
        delete_user_setting("bootstrap_theme", user_id=uid)

        assert get_user_setting_override("bootstrap_theme", user_id=uid) is None
