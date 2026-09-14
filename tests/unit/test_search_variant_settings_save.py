"""The Search Variant Settings fields are saved by the Download & API tab.

Publication Types, Variant Types and One-Shot Folders sit on that tab, but its
Save button never sent them, so every edit was silently dropped. They still
live in config.ini, where the scorer and bulk metadata read them.

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
def test_endpoint_writes_the_config_ini_key(save_handler, field, key):
    assert f'("{field}", "{key}")' in save_handler


def test_endpoint_writes_only_the_fields_it_was_sent(save_handler):
    assert "if field in data:" in save_handler


def test_endpoint_writes_before_saving_the_file(save_handler):
    assert save_handler.index("if field in data:") < save_handler.index("write_config()")


def test_a_saved_variant_list_applies_without_a_restart(monkeypatch):
    """The scorer reads config.ini on every call, so no cache needs clearing."""
    import models.getcomics as gc
    from core.config import config

    monkeypatch.setitem(config["SETTINGS"], "VARIANT_TYPES", "annual,tpb")
    assert gc.get_variant_types() == ["annual", "tpb"]
    monkeypatch.setitem(config["SETTINGS"], "VARIANT_TYPES", "omnibus")
    assert gc.get_variant_types() == ["omnibus"]
