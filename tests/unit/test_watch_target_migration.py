"""WATCH/TARGET: imported from a legacy config.ini once, then user_preferences only."""
from unittest.mock import patch

import pytest

import core.config as cc
import core.settings as cs
from core.database import get_user_preference, set_user_preference


@pytest.fixture
def fresh_db(tmp_path):
    db_path = str(tmp_path / "test.db")
    with patch("core.database.get_db_path", return_value=db_path):
        from core.database import init_db
        init_db()
        cs.invalidate()
        yield db_path
        cs.invalidate()


def test_import_moves_legacy_values_into_user_preferences(fresh_db, tmp_path):
    ini = tmp_path / "config.ini"
    ini.write_text(
        "[SETTINGS]\nWATCH = /legacy/watch\nTARGET = /legacy/target\nIGNORED_TERMS = Annual\n"
    )
    cc.migrate_ini_to_user_preferences(str(ini))

    assert get_user_preference("watch") == "/legacy/watch"
    assert get_user_preference("target") == "/legacy/target"
    assert not ini.exists()  # renamed away: nothing reads it again


def test_import_does_not_clobber_an_existing_watch(fresh_db, tmp_path):
    set_user_preference("watch", "/already/set")
    ini = tmp_path / "config.ini"
    ini.write_text("[SETTINGS]\nWATCH = /stale\nTARGET = /legacy/target\n")
    cc.migrate_ini_to_user_preferences(str(ini))

    assert get_user_preference("watch") == "/already/set"
    assert get_user_preference("target") == "/legacy/target"


def test_get_watch_dir_and_get_target_dir_return_user_preference(fresh_db):
    set_user_preference("watch", "/x/watch", category="file_processing")
    set_user_preference("target", "/x/target", category="file_processing")
    assert cc.get_watch_dir() == "/x/watch"
    assert cc.get_target_dir() == "/x/target"


def test_helpers_return_empty_when_unset(fresh_db):
    assert cc.get_watch_dir() == ""
    assert cc.get_target_dir() == ""


def test_legacy_reads_see_the_preferences(fresh_db):
    """api.py still reads config.get('SETTINGS', 'TARGET')."""
    set_user_preference("watch", "/m/watch", category="file_processing")
    set_user_preference("target", "/m/target", category="file_processing")
    assert cc.config.get("SETTINGS", "WATCH") == "/m/watch"
    assert cc.config.get("SETTINGS", "TARGET") == "/m/target"


def test_import_runs_only_once(fresh_db, tmp_path):
    ini = tmp_path / "config.ini"
    ini.write_text("[SETTINGS]\nTARGET = /first/target\nWATCH = /first/watch\n")
    cc.migrate_ini_to_user_preferences(str(ini))

    set_user_preference("target", "/user/changed", category="file_processing")
    # A stale file reappearing (restored volume, old backup) must not win.
    ini.write_text("[SETTINGS]\nTARGET = /stale/leftover\n")
    assert cc.migrate_ini_to_user_preferences(str(ini)) is None
    assert get_user_preference("target") == "/user/changed"
