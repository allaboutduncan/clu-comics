"""Guided Setup wizard, PR A: the page, the partial settings save, the
per-section status and the upgraded-install banner (``routes/setup.py``).

The lifecycle, preflight and browse routes are in ``test_setup.py``.
"""
import os
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
        for step in ("preflight", "usecase", "libraries", "metadata", "summary"):
            assert f'data-step="{step}"' in html
        assert "js/setup-wizard.js" in html
        assert SETTINGS_URL in html and STATUS_URL in html

    def test_unbuilt_sections_have_no_step(self, client):
        """A section without a step is linked from the Summary, not rendered."""
        html = client.get(PAGE_URL).get_data(as_text=True)
        assert 'data-step="folder_monitoring"' not in html
        assert 'data-step="downloads"' not in html

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


class TestStatusApi:
    def test_lists_sections_in_canonical_order(self, client):
        from core.setup_status import SECTIONS

        data = client.get(STATUS_URL).get_json()
        assert [s["id"] for s in data["sections"]] == [s[0] for s in SECTIONS]

    def test_unbuilt_sections_carry_no_status(self, client):
        monitoring = _section(client.get(STATUS_URL).get_json(), "folder_monitoring")
        assert monitoring["built"] is False
        assert monitoring["status"] is None
        assert monitoring["settings_tab"] == "file-processing"

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
