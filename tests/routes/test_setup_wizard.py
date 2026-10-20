"""Guided Setup wizard: the page, the partial settings save, the
per-section status and the upgraded-install banner (``routes/setup.py``).

The lifecycle, preflight and browse routes are in ``test_setup.py``.
"""
import os
import re
from unittest.mock import patch

import pytest

PAGE_URL = "/setup"
STATE_URL = "/api/setup/state"
SETTINGS_URL = "/api/setup/settings"
STATUS_URL = "/api/setup/status"

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _set_state(value):
    from core.settings import set_setting

    set_setting("GUIDED_SETUP_STATE", value)


def _state():
    from core.setup_state import get_setup_state

    return get_setup_state()["state"]


def _section(data, section_id):
    return next(s for s in data["sections"] if s["id"] == section_id)


@pytest.mark.parametrize("url", [SETTINGS_URL, STATUS_URL])
def test_route_is_owner_only_by_prefix(url):
    from core.auth import _OWNER_PREFIXES

    assert url.startswith(_OWNER_PREFIXES)


class TestWizardPage:
    def test_renders_every_built_step(self, client):
        html = client.get(PAGE_URL).get_data(as_text=True)
        for step in ("preflight", "usecase", "libraries", "metadata", "folder_monitoring",
                     "downloads", "schedules", "users", "notifications", "personalization",
                     "summary"):
            assert f'data-step="{step}"' in html
        assert "js/setup-wizard.js" in html
        assert SETTINGS_URL in html and STATUS_URL in html

    def test_downloads_step_has_its_urls(self, client):
        html = client.get(PAGE_URL).get_data(as_text=True)
        for url in ('"/api/download-clients"', '"/api/download-clients/__TYPE__/test"',
                    '"/api/download-clients/__TYPE__/config"',
                    '"/api/download-clients/__TYPE__/activate"',
                    '"/api/indexers"', '"/api/indexers/test"', '"/api/getcomics/simulate"'):
            assert url in html

    def test_downloads_is_off_the_later_list(self):
        """The use-case step no longer says Downloads is left for Settings."""
        js = self._js()
        later = js[js.index("const LATER = {"):js.index("};", js.index("const LATER = {"))]
        assert "download" not in later

    def test_dry_run_is_offered_only_with_a_subscribed_series(self, client):
        with patch("core.database.get_all_mapped_series", return_value=[]):
            html = client.get(PAGE_URL).get_data(as_text=True)
        assert 'id="dl-simulate"' not in html
        assert 'id="dl-simulate-later"' in html

        series = [{"id": 1, "name": "Saga", "mapped_path": "/data/Saga", "monitored": None}]
        with patch("core.database.get_all_mapped_series", return_value=series):
            html = client.get(PAGE_URL).get_data(as_text=True)
        assert 'id="dl-simulate"' in html

    def test_unmonitored_series_do_not_offer_the_dry_run(self, client):
        series = [{"id": 1, "name": "Saga", "mapped_path": "/data/Saga", "monitored": 0}]
        with patch("core.database.get_all_mapped_series", return_value=series):
            html = client.get(PAGE_URL).get_data(as_text=True)
        assert 'id="dl-simulate"' not in html

    def test_js_registry_matches_status_sections(self):
        """The step registry and setup_status agree on the built sections and their order."""
        from core.setup_status import SECTIONS

        js = self._js()
        registry = js[js.index("const SECTIONS = ["):js.index("];", js.index("const SECTIONS = ["))]
        ids = re.findall(r"\{ id: '([a-z_]+)'", registry)
        assert ids == [sid for sid, _label, built, _tab in SECTIONS if built]

    def test_personalization_is_on_every_path(self):
        js = self._js()
        assert "{ id: 'personalization', label: 'Personalization', usecases: [], always: true }" in js
        assert "fresh || s.always ||" in js

    def _js(self):
        with open(os.path.join(ROOT, "static", "js", "setup-wizard.js"), encoding="utf-8") as f:
            return f.read()

    def test_notifications_step_is_prefilled(self, client):
        from core.database import set_user_preference
        from core.notifications import EVENT_DEFS

        set_user_preference("notify_urls", ["ntfy://ntfy.sh/clu-test"], category="notifications")
        html = client.get(PAGE_URL).get_data(as_text=True)
        assert "ntfy://ntfy.sh/clu-test" in html
        for event_id in EVENT_DEFS:
            assert f'data-event-id="{event_id}"' in html

    def test_personalization_step_has_the_theme_picker(self, client):
        from core.settings import set_setting

        set_setting("BOOTSTRAP_THEME", "darkly")
        html = client.get(PAGE_URL).get_data(as_text=True)
        assert 'id="setup-theme"' in html
        assert '<option value="darkly" selected>' in html
        assert "js/clu-personalization.js" in html

    def test_rename_preview_gets_the_saved_cleanup(self, client):
        html = client.get(PAGE_URL).get_data(as_text=True)
        assert "js/clu-rename-preview.js" in html
        assert '"renameCleanup"' in html and '"char_map"' in html

    def test_schedules_step_carries_the_timezone_holder(self, client):
        """CLU.tzOffsetHours() reads id="timezone"; without it every time shows raw UTC."""
        html = client.get(PAGE_URL).get_data(as_text=True)
        assert 'id="timezone"' in html
        js = self._js()
        assert "CLU.localToUtc(local, CLU.tzOffsetHours())" in js
        assert "CLU.utcToLocal(st.time, CLU.tzOffsetHours())" in js

    def test_every_built_status_section_has_a_step(self):
        """setup_status marks a section built; the page must then have its step."""
        from core.setup_status import SECTIONS

        for section_id, _label, built, _tab in SECTIONS:
            partial = os.path.join(ROOT, "templates", "setup", f"_{section_id}.html")
            assert os.path.exists(partial) == built, section_id

    def test_upgraded_install_that_opens_it_has_started(self, client):
        _set_state("upgraded")
        client.get(PAGE_URL)
        assert _state() == "in_progress"


class TestSettingsApi:
    def test_get_returns_every_whitelisted_setting(self, client):
        from core.setup_settings import WIZARD_SETTINGS

        data = client.get(SETTINGS_URL).get_json()
        assert data["success"] is True
        assert set(data["settings"]) == set(WIZARD_SETTINGS)

    def test_partial_save_leaves_absent_keys(self, client):
        client.post(SETTINGS_URL, json={"xml_year": True, "xml_list": False})
        data = client.post(SETTINGS_URL, json={"xml_year": False}).get_json()
        assert data["settings"]["xml_year"] is False
        assert data["settings"]["xml_list"] is False

    def test_saves_auto_metadata_on_move(self, client):
        from core.config import is_auto_metadata_on_move_enabled

        client.post(SETTINGS_URL, json={"auto_metadata_on_move": False})
        assert is_auto_metadata_on_move_enabled() is False
        client.post(SETTINGS_URL, json={"auto_metadata_on_move": True})
        assert is_auto_metadata_on_move_enabled() is True

    def test_unknown_key_is_refused_and_nothing_written(self, client):
        from core.settings import get_setting

        client.post(SETTINGS_URL, json={"xml_year": False})
        resp = client.post(SETTINGS_URL, json={"xml_year": True, "watch": "/x"})
        assert resp.status_code == 400
        assert get_setting("XML_YEAR") is False

    @pytest.mark.parametrize("value", ["true", 1, None, [True]])
    def test_wrong_type_is_refused(self, client, value):
        assert client.post(SETTINGS_URL, json={"xml_year": value}).status_code == 400

    @pytest.mark.parametrize("body", [{}, [], "x"])
    def test_refuses_empty_or_non_object(self, client, body):
        assert client.post(SETTINGS_URL, json=body).status_code == 400

    def test_write_failure_is_500(self, client):
        with patch("core.setup_settings.save_wizard_settings", return_value=False):
            resp = client.post(SETTINGS_URL, json={"xml_year": True})
        assert resp.status_code == 500

    @pytest.mark.parametrize("key, value", [
        ("autoconvert", True),
        ("auto_rename_monitor", False),
        ("enable_custom_rename", True),
        ("custom_rename_pattern", "{series_name} {issue_number} ({volume_year})"),
        ("trash_enabled", False),
        ("trash_max_size_mb", 2048),
        ("timezone", "-5"),
        ("timezone", "5.5"),
        ("timezone", "0"),
        ("timezone", "UTC"),
        ("download_source_priority", "getcomics"),
        ("download_source_priority", "usenet,getcomics,dcpp"),
        ("download_source_priority", "dcpp"),
        ("pixeldrain_api_key", "abc-123"),
        ("pixeldrain_api_key", ""),
        ("download_provider_priority", "mega,pixeldrain"),
        ("download_provider_priority", "download_now"),
        ("download_packs", True),
        ("download_packs", False),
    ])
    def test_round_trips(self, client, key, value):
        data = client.post(SETTINGS_URL, json={key: value}).get_json()
        assert data["success"] is True
        assert data["settings"][key] == value
        assert client.get(SETTINGS_URL).get_json()["settings"][key] == value

    @pytest.mark.parametrize("key, value", [
        ("trash_max_size_mb", 0),
        ("trash_max_size_mb", -1),
        ("timezone", "abc"),
        ("timezone", "20"),
        ("timezone", "-13"),
        ("timezone", "nan"),
        ("timezone", " -5"),
        ("timezone", ""),
        ("download_source_priority", ""),
        ("download_source_priority", "torrents"),
        ("download_source_priority", "getcomics,getcomics"),
        ("download_source_priority", "getcomics,"),
        ("download_source_priority", "getcomics, usenet"),
        ("download_provider_priority", ""),
        ("download_provider_priority", "pixeldrain,mediafire"),
        ("download_provider_priority", "mega,mega"),
        ("pixeldrain_api_key", " abc"),
        ("pixeldrain_api_key", "abc\n"),
    ])
    def test_out_of_range_is_refused(self, client, key, value):
        resp = client.post(SETTINGS_URL, json={key: value})
        assert resp.status_code == 400
        assert key in resp.get_json()["error"]

    def test_a_refused_value_writes_nothing(self, client):
        from core.settings import get_setting

        client.post(SETTINGS_URL, json={"xml_year": False})
        resp = client.post(SETTINGS_URL, json={"xml_year": True, "trash_max_size_mb": 0})
        assert resp.status_code == 400
        assert get_setting("XML_YEAR") is False

    def test_save_refreshes_the_app_config_mirror(self, app, client):
        """helpers.trash and the renamers read app.config, not the store."""
        client.post(SETTINGS_URL, json={"trash_max_size_mb": 4096, "autoconvert": True})
        assert app.config["TRASH_MAX_SIZE_MB"] == 4096
        assert app.config["AUTOCONVERT"] is True

    def test_download_mirrors_reach_the_app_config_mirror(self, app, client):
        """The auto-download reads DOWNLOAD_PROVIDER_PRIORITY from app.config."""
        client.post(SETTINGS_URL, json={"download_provider_priority": "mega",
                                        "pixeldrain_api_key": "k1"})
        assert app.config["DOWNLOAD_PROVIDER_PRIORITY"] == "mega"
        assert app.config["PIXELDRAIN_API_KEY"] == "k1"

    def test_source_priority_is_stored_as_the_list_the_readers_expect(self, client):
        """Stored as Settings stores it, so get_source_priority() and the
        usenet/dcpp on-checks read the wizard's value."""
        from models.download_sources import get_source_priority, source_enabled

        client.post(SETTINGS_URL, json={"download_source_priority": "usenet,getcomics"})
        assert get_source_priority() == ["usenet", "getcomics"]
        assert source_enabled("usenet") and not source_enabled("dcpp")

    def test_source_priority_defaults_to_getcomics_alone(self, client):
        data = client.get(SETTINGS_URL).get_json()
        assert data["settings"]["download_source_priority"] == "getcomics"

    def test_download_packs_is_the_pref_the_sweep_reads(self, client):
        from core.config import is_download_packs_enabled

        client.post(SETTINGS_URL, json={"download_packs": True})
        assert is_download_packs_enabled() is True


class TestStatusApi:
    def test_lists_sections_in_canonical_order(self, client):
        from core.setup_status import SECTIONS

        data = client.get(STATUS_URL).get_json()
        assert [s["id"] for s in data["sections"]] == [s[0] for s in SECTIONS]

    def test_every_section_is_built_and_has_a_status(self, client):
        for section in client.get(STATUS_URL).get_json()["sections"]:
            assert section["built"] is True, section["id"]
            assert section["status"] in ("configured", "needs_attention", "unconfigured")

    @staticmethod
    def _client_row(group, client_type, valid=1, tested="2026-01-01"):
        return {"client_type": client_type, "client_group": group, "is_active": 1,
                "is_valid": valid, "last_tested": tested}

    @pytest.mark.parametrize("order, rows, indexers, expected", [
        (["getcomics"], [], [], "configured"),
        (["usenet", "getcomics"], [("usenet", "sabnzbd")], [{"id": 1}], "configured"),
        (["usenet", "getcomics"], [("usenet", "sabnzbd")], [], "needs_attention"),
        (["usenet"], [], [{"id": 1}], "needs_attention"),
        (["getcomics", "dcpp"], [("dcpp", "airdcpp")], [], "configured"),
        (["getcomics", "dcpp"], [], [], "needs_attention"),
        # Configured but not switched on: not this section's problem.
        (["getcomics"], [("usenet", "sabnzbd")], [], "configured"),
    ])
    def test_downloads(self, client, order, rows, indexers, expected):
        rows = [self._client_row(g, t) for g, t in rows]
        with patch("models.download_sources.get_source_priority", return_value=order), \
             patch("core.database.get_all_download_clients_status", return_value=rows), \
             patch("core.database.get_enabled_indexers", return_value=indexers):
            data = client.get(STATUS_URL).get_json()
        section = _section(data, "downloads")
        assert section["status"] == expected
        assert section["details"]["order"] == order

    def test_downloads_names_what_is_missing(self, client):
        with patch("models.download_sources.get_source_priority", return_value=["usenet"]), \
             patch("core.database.get_all_download_clients_status", return_value=[]), \
             patch("core.database.get_enabled_indexers", return_value=[]):
            message = _section(client.get(STATUS_URL).get_json(), "downloads")["message"]
        assert "no Usenet client is active" in message
        assert "no indexer is enabled" in message

    @pytest.mark.parametrize("valid, tested, expected", [
        (0, "2026-01-01", "needs_attention"),  # tested and failed
        (0, None, "configured"),               # never tested: not evidence of a failure
        (1, "2026-01-01", "configured"),
    ])
    def test_downloads_failed_client_test(self, client, valid, tested, expected):
        rows = [self._client_row("dcpp", "airdcpp", valid=valid, tested=tested)]
        with patch("models.download_sources.get_source_priority", return_value=["dcpp"]), \
             patch("core.database.get_all_download_clients_status", return_value=rows):
            data = client.get(STATUS_URL).get_json()
        assert _section(data, "downloads")["status"] == expected

    def test_downloads_reads_the_real_default(self, client):
        """A fresh install is GetComics only, which needs nothing: configured."""
        section = _section(client.get(STATUS_URL).get_json(), "downloads")
        assert section["status"] == "configured"
        assert section["details"]["order"] == ["getcomics"]

    @staticmethod
    def _path_check(status):
        from core.setup_preflight import _check

        return lambda: _check("watch_dir", "WATCH folder", status, f"path is {status}")

    @pytest.mark.parametrize("monitor, path_status, expected", [
        ({"enabled": False, "running": False}, "ok", "unconfigured"),
        ({"enabled": True, "running": True}, "ok", "configured"),
        ({"enabled": True, "running": False}, "ok", "needs_attention"),
        ({"enabled": True, "running": True}, "fail", "needs_attention"),
        ({"enabled": True, "running": True}, "warn", "needs_attention"),
    ])
    def test_folder_monitoring(self, client, monitor, path_status, expected):
        monitor = {**monitor, "watch": "/downloads/temp"}
        with patch("core.monitor_supervisor.status", return_value=monitor), \
             patch("core.setup_preflight.check_watch_dir", self._path_check(path_status)), \
             patch("core.setup_preflight.check_target_dir", self._path_check("ok")):
            data = client.get(STATUS_URL).get_json()
        assert _section(data, "folder_monitoring")["status"] == expected

    @pytest.mark.parametrize("rebuild, sync, expected", [
        ("daily", "weekly", "configured"),
        ("daily", "disabled", "unconfigured"),
        ("disabled", "disabled", "unconfigured"),
    ])
    def test_schedules(self, client, rebuild, sync, expected):
        rows = {"rebuild": {"frequency": rebuild}, "sync": {"frequency": sync}}
        with patch("core.database.get_schedule", side_effect=lambda name: rows.get(name)):
            data = client.get(STATUS_URL).get_json()
        section = _section(data, "schedules")
        assert section["status"] == expected
        if sync == "disabled":
            assert "series sync" in section["message"]

    def test_schedules_without_rows_are_unconfigured(self, client):
        with patch("core.database.get_schedule", return_value=None):
            data = client.get(STATUS_URL).get_json()
        assert _section(data, "schedules")["status"] == "unconfigured"

    @pytest.mark.parametrize("needs_setup, login_required, count, expected", [
        (True, False, 1, "unconfigured"),
        (True, True, 1, "needs_attention"),
        (False, False, 1, "configured"),
        (False, True, 3, "configured"),
    ])
    def test_users(self, client, needs_setup, login_required, count, expected):
        owner = {"id": 1, "username": "owner", "needs_setup": needs_setup}
        with patch("core.database.get_owner_user", return_value=owner), \
             patch("core.database.count_users", return_value=count), \
             patch("core.auth.is_login_required", return_value=login_required):
            data = client.get(STATUS_URL).get_json()
        section = _section(data, "users")
        assert section["status"] == expected
        assert section["details"]["count"] == count

    def test_users_without_an_owner_needs_attention(self, client):
        with patch("core.database.get_owner_user", return_value=None):
            data = client.get(STATUS_URL).get_json()
        assert _section(data, "users")["status"] == "needs_attention"

    @pytest.mark.parametrize("enabled, urls, expected", [
        (True, ["ntfy://x"], "configured"),
        (True, [], "unconfigured"),
        (False, ["ntfy://x"], "unconfigured"),
    ])
    def test_notifications(self, client, enabled, urls, expected):
        settings = {"enabled": enabled, "urls": urls, "events": {}}
        with patch("core.notifications.get_settings", return_value=settings):
            data = client.get(STATUS_URL).get_json()
        assert _section(data, "notifications")["status"] == expected

    def test_personalization_names_the_theme(self, client):
        from core.settings import set_setting

        set_setting("BOOTSTRAP_THEME", "darkly")
        section = _section(client.get(STATUS_URL).get_json(), "personalization")
        assert section["status"] == "configured"
        assert "Darkly" in section["message"]

    def test_no_libraries_is_unconfigured(self, client):
        with patch("core.setup_preflight._enabled_libraries", return_value=[]):
            data = client.get(STATUS_URL).get_json()
        assert _section(data, "libraries")["status"] == "unconfigured"

    def test_usable_library_is_configured(self, client, tmp_path):
        lib = {"id": 1, "name": "Comics", "path": str(tmp_path)}
        with patch("core.setup_preflight._enabled_libraries", return_value=[lib]):
            data = client.get(STATUS_URL).get_json()
        assert _section(data, "libraries")["status"] == "configured"

    def test_missing_library_needs_attention(self, client, tmp_path):
        lib = {"id": 1, "name": "Gone", "path": str(tmp_path / "gone")}
        with patch("core.setup_preflight._enabled_libraries", return_value=[lib]):
            data = client.get(STATUS_URL).get_json()
        section = _section(data, "libraries")
        assert section["status"] == "needs_attention"
        assert "does not exist" in section["message"]

    @pytest.mark.parametrize("rows, blocked, expected", [
        ([], False, "unconfigured"),
        ([{"provider_type": "metron", "is_valid": 1}], False, "configured"),
        ([{"provider_type": "metron", "is_valid": 0}], False, "needs_attention"),
        ([{"provider_type": "metron", "is_valid": 1}], True, "needs_attention"),
        ([{"provider_type": "comicvine", "is_valid": 1}], False, "unconfigured"),
    ])
    def test_metadata_follows_metron(self, client, rows, blocked, expected):
        with patch("core.database.get_all_provider_credentials_status", return_value=rows), \
             patch("models.metron.auth_block_state", return_value={"blocked": blocked}):
            data = client.get(STATUS_URL).get_json()
        assert _section(data, "metadata")["status"] == expected

    def test_optional_providers_are_details(self, client):
        rows = [{"provider_type": "comicvine", "is_valid": 1}]
        with patch("core.database.get_all_provider_credentials_status", return_value=rows):
            data = client.get(STATUS_URL).get_json()
        details = _section(data, "metadata")["details"]
        assert details["comicvine"] is True
        assert details["gcd"] is False

    def test_a_raising_check_needs_attention(self, client):
        def boom():
            raise RuntimeError("boom")

        with patch.dict("core.setup_status._STATUS_FNS", {"metadata": boom}):
            data = client.get(STATUS_URL).get_json()
        section = _section(data, "metadata")
        assert section["status"] == "needs_attention"
        assert "boom" in section["message"]


class TestUpgradeBanner:
    """Upgraded installs get a one-time, dismissible invitation."""

    def _flag(self, app, role="owner", path="/"):
        from routes.setup import inject_setup_banner

        user = {"id": 1, "role": role} if role else None
        with app.test_request_context(path), \
                patch("core.auth.load_current_user", return_value=user):
            return inject_setup_banner()["show_setup_banner"]

    def test_shown_to_owner_of_upgraded_install(self, app):
        _set_state("upgraded")
        assert self._flag(app) is True

    def test_hidden_once_dismissed(self, app, client):
        _set_state("upgraded")
        resp = client.post(STATE_URL, json={"banner_dismissed": True})
        assert resp.get_json()["banner_dismissed"] is True
        assert self._flag(app) is False

    @pytest.mark.parametrize("state", ["none", "in_progress", "skipped", "completed", ""])
    def test_only_on_upgraded(self, app, state):
        _set_state(state)
        assert self._flag(app) is False

    @pytest.mark.parametrize("role", ["reader", "clerk", None])
    def test_owner_only(self, app, role):
        _set_state("upgraded")
        assert self._flag(app, role=role) is False

    def test_not_on_the_setup_page(self, app):
        _set_state("upgraded")
        assert self._flag(app, path="/setup") is False

    def test_a_failed_read_shows_nothing(self, app):
        with patch("core.setup_state.show_upgrade_banner", side_effect=RuntimeError):
            assert self._flag(app) is False

    def test_dismiss_must_be_a_bool(self, client):
        assert client.post(STATE_URL, json={"banner_dismissed": "yes"}).status_code == 400

    def test_rendered_in_base(self):
        with open(os.path.join(ROOT, "templates", "base.html"), encoding="utf-8") as f:
            html = f.read()
        assert "{% if show_setup_banner %}" in html
        assert "banner_dismissed: true" in html


class TestEntryPoints:
    def _read(self, *parts):
        with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
            return f.read()

    def test_settings_page_links_the_wizard(self):
        assert "url_for('setup.setup_page')" in self._read("templates", "config.html")

    def test_gear_menu_links_the_wizard_for_owners_only(self):
        html = self._read("templates", "header.html")
        # The gear menu's owner section runs from the Settings link to the
        # clerk section; the setup link must sit inside it, and only there.
        settings = html.index("href=\"{{ url_for('config_page') }}\"")
        start = html.rindex("{% if _nav_is_owner %}", 0, settings)
        end = html.index("{% if _nav_is_clerk %}", settings)
        link = "url_for('setup.setup_page')"
        assert html.count(link) == 2  # the href and the active-state check
        assert start < html.index(link) < end
