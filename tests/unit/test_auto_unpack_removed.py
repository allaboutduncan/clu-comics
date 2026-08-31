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

# Every file that used to name the setting, plus the shipped config file.
SOURCES = [
    "app.py",
    "api.py",
    "monitor.py",
    "core/download_utils.py",
    "templates/config.html",
    "config.ini",
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


def test_config_py_only_names_it_as_a_retired_key():
    """core/config.py is the one file allowed to say AUTO_UNPACK -- to delete it
    from an upgraded config.ini. load_config() writes missing *default* keys back
    to disk, so a leftover default would silently resurrect it on next start."""
    import core.config as cc

    text = (ROOT / "core" / "config.py").read_text(encoding="utf-8")
    assert 'REMOVED_SETTINGS = ("AUTO_UNPACK",)' in text
    assert "AUTO_UNPACK" in cc.REMOVED_SETTINGS
    # Nothing reads it any more: no default entry, no getboolean, no app.config.
    code = [ln for ln in text.splitlines()
            if "AUTO_UNPACK" in ln and not ln.lstrip().startswith("#")]
    assert code == ['REMOVED_SETTINGS = ("AUTO_UNPACK",)'], code


def test_retired_keys_are_stripped_from_an_upgraded_config_ini(tmp_path, monkeypatch):
    """An existing install must not keep a dead key that looks meaningful."""
    import configparser

    import core.config as cc

    cfg = configparser.RawConfigParser()
    cfg.optionxform = str
    cfg["SETTINGS"] = {"AUTO_UNPACK": "True", "AUTOCONVERT": "True"}

    wrote = []
    monkeypatch.setattr(cc, "config", cfg)
    monkeypatch.setattr(cc, "write_config", lambda: wrote.append(True))

    cc.strip_removed_settings()

    assert "AUTO_UNPACK" not in cfg["SETTINGS"]
    assert cfg["SETTINGS"]["AUTOCONVERT"] == "True", "other settings untouched"
    assert wrote == [True], "the stripped file is persisted"


def test_stripping_is_a_no_op_when_the_key_is_absent(monkeypatch):
    import configparser

    import core.config as cc

    cfg = configparser.RawConfigParser()
    cfg.optionxform = str
    cfg["SETTINGS"] = {"AUTOCONVERT": "True"}

    wrote = []
    monkeypatch.setattr(cc, "config", cfg)
    monkeypatch.setattr(cc, "write_config", lambda: wrote.append(True))

    cc.strip_removed_settings()

    assert wrote == [], "no pointless rewrite of config.ini on every start"
