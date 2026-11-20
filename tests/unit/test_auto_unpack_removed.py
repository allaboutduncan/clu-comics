"""Unpacking archives dropped in WATCH is unconditional -- there is no setting.

AUTO_UNPACK predated automated downloads and shipped *off*, so a fresh install
silently stranded packs in WATCH. It was removed rather than migrated to
user_preferences: with the behaviour unconditional there is nothing left to
store.

A half-removed setting is the failure this pins. The old key was read in five
places across two processes (app.py, api.py, monitor.py, core/config.py) and the
UI in a third (templates/config.html); leaving any one behind gives a toggle
that renders but does nothing, or a monitor that still gates on a key nobody
writes. These are source-level assertions because monitor.py and app.py cannot
be imported in tests.
"""
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]

# Every file that used to name the setting.
SOURCES = [
    "app.py",
    "api.py",
    "monitor.py",
    "core/download_utils.py",
    "templates/config.html",
]


@pytest.mark.parametrize("relpath", SOURCES)
def test_setting_is_gone(relpath):
    text = (ROOT / relpath).read_text(encoding="utf-8")
    assert "AUTO_UNPACK" not in text, f"{relpath} still names the removed setting"
    assert "auto_unpack" not in text, f"{relpath} still names the removed setting"
    assert "autoUnpack" not in text, f"{relpath} still names the removed setting"


def test_the_toggle_is_gone_from_the_config_page():
    html = (ROOT / "templates" / "config.html").read_text(encoding="utf-8")
    assert "Auto-Unpack" not in html


def test_it_is_only_ever_named_as_an_ignored_legacy_ini_key():
    """AUTO_UNPACK survives in exactly one place: the record of legacy ini
    keys that never became a preference. It must not join the schema, or a
    toggle nobody renders would silently gate the monitor again."""
    import core.settings as cs

    assert "AUTO_UNPACK" in cs.NON_SCHEMA_INI_KEYS
    assert "AUTO_UNPACK" not in cs.SETTINGS_SCHEMA
    text = (ROOT / "core" / "config.py").read_text(encoding="utf-8")
    assert "AUTO_UNPACK" not in text
