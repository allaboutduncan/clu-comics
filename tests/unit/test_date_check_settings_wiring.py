"""The date-check settings must survive the round trip from the Config page.

The Config page does not post a form for these fields: the System & Performance
tab collects specific element ids into a JSON payload and posts it to
/api/config/system-perf. A setting can therefore be rendered on the page, saved
to config.ini by hand, and still be impossible to change from the UI -- which is
what happened to these two before this test existed.

These are source-level assertions on purpose. They pin the three places that
must agree, without standing up a browser.
"""
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIELDS = ["dateCheckMode", "dateCheckToleranceYears"]
KEYS = ["DATE_CHECK_MODE", "DATE_CHECK_TOLERANCE_YEARS"]


@pytest.fixture(scope="module")
def config_html():
    return (ROOT / "templates" / "config.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_py():
    return (ROOT / "app.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("field", FIELDS)
def test_input_exists_on_the_page(config_html, field):
    assert f'id="{field}"' in config_html


@pytest.mark.parametrize("field", FIELDS)
def test_field_is_collected_into_the_save_payload(config_html, field):
    """Rendering the input is not enough -- saveSystemPerfSettings must send it."""
    payload = config_html.split("async function saveSystemPerfSettings()")[1]
    payload = payload.split("fetch(")[0]
    assert field in payload, f"{field} is rendered but never sent to the server"


@pytest.mark.parametrize("field", FIELDS)
def test_endpoint_reads_the_field(app_py, field):
    handler = app_py.split('@app.route("/api/config/system-perf"')[1][:4000]
    assert f'"{field}"' in handler, f"/api/config/system-perf ignores {field}"


@pytest.mark.parametrize("key", KEYS)
def test_setting_has_a_default_in_config_py(key):
    config_py = (ROOT / "core" / "config.py").read_text(encoding="utf-8")
    assert f'"{key}"' in config_py


@pytest.mark.parametrize("key", KEYS)
def test_setting_ships_in_config_ini(key):
    """Every other setting is listed there; a missing key is invisible to users
    editing the file directly."""
    config_ini = (ROOT / "config.ini").read_text(encoding="utf-8")
    assert re.search(rf"^{key}\s*=", config_ini, re.MULTILINE), (
        f"{key} missing from config.ini"
    )


def test_mode_values_agree_between_ui_and_server(config_html, app_py):
    """The select's options and the server's allow-list must not drift."""
    select = config_html.split('id="dateCheckMode"')[1].split("</select>")[0]
    ui_values = set(re.findall(r'value="([a-z]+)"', select))
    assert ui_values == {"off", "log", "enforce"}

    handler = app_py.split('@app.route("/api/config/system-perf"')[1][:4000]
    allowed = handler.split('_date_mode in (')[1].split(')')[0]
    server_values = set(re.findall(r'"([a-z]+)"', allowed))
    assert server_values == ui_values
