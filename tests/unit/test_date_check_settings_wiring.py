"""The date-check settings must survive the round trip from the Config page.

The Config page does not post a form for these fields: the File Processing
tab collects specific element ids into a JSON payload and posts it to
/api/config/file-processing. A setting can therefore be rendered on the page,
saved to config.ini by hand, and still be impossible to change from the UI --
which is what happened to these two before this test existed.

The tab is load-bearing here: the payload builder and the endpoint that reads
it must be the *same* tab's pair, so moving a field between tabs without moving
its save block leaves it silently unsaveable.

These are source-level assertions on purpose. They pin the three places that
must agree, without standing up a browser.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIELDS = ["dateCheckMode", "dateCheckToleranceYears"]
PREFS = ["date_check_mode", "date_check_tolerance_years"]
LEGACY_KEYS = ["DATE_CHECK_MODE", "DATE_CHECK_TOLERANCE_YEARS"]


@pytest.fixture(scope="module")
def config_html():
    return (ROOT / "templates" / "config.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_py():
    return (ROOT / "app.py").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def save_handler(app_py):
    """Source of the /api/config/file-processing handler, bounded by the next route."""
    return app_py.split('@app.route("/api/config/file-processing"')[1].split("@app.route")[0]


@pytest.mark.parametrize("field", FIELDS)
def test_input_exists_on_the_page(config_html, field):
    assert f'id="{field}"' in config_html


@pytest.mark.parametrize("field", FIELDS)
def test_field_is_collected_into_the_save_payload(config_html, field):
    """Rendering the input is not enough -- saveSystemPerfSettings must send it."""
    payload = config_html.split("async function saveFileProcessingSettings()")[1]
    payload = payload.split("fetch(")[0]
    assert field in payload, f"{field} is rendered but never sent to the server"


@pytest.mark.parametrize("field", FIELDS)
def test_endpoint_reads_the_field(save_handler, field):
    assert f'"{field}"' in save_handler, f"/api/config/file-processing ignores {field}"


@pytest.mark.parametrize("key", LEGACY_KEYS)
def test_setting_is_not_in_the_deprecated_config_ini(key):
    """CLAUDE.md deprecates config.ini: new settings live in user_preferences.

    This test was originally the other way round, asserting the keys were
    present in config.ini — it would have kept enforcing the deprecated
    pattern.
    """
    config_ini = (ROOT / "config.ini").read_text(encoding="utf-8")
    config_py = (ROOT / "core" / "config.py").read_text(encoding="utf-8")
    assert not re.search(rf"^{key}\s*=", config_ini, re.MULTILINE)
    assert f'"{key}"' not in config_py


@pytest.mark.parametrize("pref", PREFS)
def test_endpoint_persists_the_preference(save_handler, pref):
    assert "set_user_preference" in save_handler
    assert "PREF_MODE" in save_handler and "PREF_TOLERANCE" in save_handler


@pytest.mark.parametrize("pref", PREFS)
def test_preference_key_is_defined_once(pref):
    """Both reader and writer must use the constants, not literals."""
    md = (ROOT / "core" / "metadata_dates.py").read_text(encoding="utf-8")
    assert f'"{pref}"' in md


def test_mode_values_agree_between_ui_and_server(config_html, save_handler):
    """The select's options and the server's allow-list must not drift."""
    select = config_html.split('id="dateCheckMode"')[1].split("</select>")[0]
    ui_values = set(re.findall(r'value="([a-z]+)"', select))
    assert ui_values == {"off", "log", "enforce"}

    allowed = save_handler.split('_date_mode in (')[1].split(')')[0]
    server_values = set(re.findall(r'"([a-z]+)"', allowed))
    assert server_values == ui_values


def test_fields_live_on_the_file_processing_tab(config_html):
    """The inputs must sit inside the tab whose Save button posts them.

    They started life on System & Performance; a field left behind on the old
    tab still renders, but its Save button no longer sends it.
    """
    tab = config_html.split('id="file-processing" role="tabpanel"')[1]
    tab = tab.split('role="tabpanel"')[0]
    for field in FIELDS:
        assert f'id="{field}"' in tab, f"{field} is not on the File Processing tab"
