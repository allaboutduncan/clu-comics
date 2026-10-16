"""The Search Variant Settings fields are saved by the Download & API tab.

Publication Types, Variant Types and One-Shot Folders sit on that tab, but its
Save button never sent them, so every edit was silently dropped. They still
live in user_preferences, where the scorer and bulk metadata read them.

app.py cannot be imported in tests, so the page and the endpoint are asserted
against their source.
"""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

FIELDS = [
    ("publicationTypes", "PUBLICATION_TYPES"),
    ("variantTypes", "VARIANT_TYPES"),
    ("oneshotFolders", "ONESHOT_FOLDERS"),
]


@pytest.fixture(scope="module")
def payload():
    html = (ROOT / "templates" / "config.html").read_text(encoding="utf-8")
    return html.split("async function saveDownloadApiSettings()")[1].split("fetch(")[0]


@pytest.fixture(scope="module")
def save_handler():
    app_py = (ROOT / "app.py").read_text(encoding="utf-8")
    return app_py.split('@app.route("/api/config/download-api"')[1].split("@app.route")[0]


@pytest.mark.parametrize("field,key", FIELDS)
def test_field_is_sent_with_the_tab(payload, field, key):
    line = [ln for ln in payload.splitlines() if f"{field}:" in ln][0]
    assert f"getElementById('{field}')?.value" in line
    # No `|| ''`: a missing element must be left out, not sent as a blank list.
    assert "||" not in line and "??" not in line


@pytest.mark.parametrize("field,key", FIELDS)
def test_endpoint_writes_the_setting(save_handler, field, key):
    assert f'("{field}", "{key}")' in save_handler
    assert "set_setting(key" in save_handler


def test_endpoint_writes_only_the_fields_it_was_sent(save_handler):
    assert "if field in data:" in save_handler


def test_endpoint_no_longer_touches_config_ini(save_handler):
    assert "write_config" not in save_handler
    assert 'config["SETTINGS"]' not in save_handler


def test_a_saved_variant_list_applies_without_a_restart(tmp_path):
    """The scorer reads the stored setting, so a save needs no restart."""
    from unittest.mock import patch

    import core.settings as cs
    import models.getcomics as gc

    db_path = str(tmp_path / "test.db")
    with patch("core.database.get_db_path", return_value=db_path):
        from core.database import init_db
        init_db()
        cs.invalidate()
        gc._clear_config_caches()
        cs.set_setting("VARIANT_TYPES", "annual,tpb")
        assert gc.get_variant_types() == ["annual", "tpb"]
        cs.set_setting("VARIANT_TYPES", "omnibus")
        assert gc.get_variant_types() == ["omnibus"]
        cs.invalidate()
        gc._clear_config_caches()
