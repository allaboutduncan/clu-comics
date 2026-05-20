"""Tests for the WATCH/TARGET migration from config.ini to user_preferences."""
import importlib
import os
from unittest.mock import patch

import pytest


@pytest.fixture
def fresh_config(tmp_path):
    """Reload core.config with CONFIG_DIR pointed at a fresh tmp_path."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    with patch.dict(os.environ, {"CONFIG_DIR": str(cfg_dir)}):
        import core.config as cc
        importlib.reload(cc)
        yield cc, cfg_dir
        # Reload again to restore for other tests
        importlib.reload(cc)


@pytest.fixture
def fresh_db(tmp_path):
    """Initialize an empty test DB and patch get_db_path."""
    db_path = str(tmp_path / "test.db")
    with patch("core.database.get_db_path", return_value=db_path):
        from core.database import init_db
        init_db()
        yield db_path


def test_migration_moves_legacy_values_into_user_preferences(fresh_config, fresh_db, tmp_path):
    """An existing config.ini with WATCH/TARGET should be migrated."""
    cc, cfg_dir = fresh_config

    # Pre-seed config.ini with legacy values
    cfg_file = cfg_dir / "config.ini"
    cfg_file.write_text(
        "[SETTINGS]\n"
        "WATCH = /legacy/watch\n"
        "TARGET = /legacy/target\n"
        "IGNORED_TERMS = Annual\n"
    )

    with patch.object(cc, "CONFIG_FILE", str(cfg_file)):
        cc.load_config()

        from core.database import get_user_preference
        assert get_user_preference("watch") == "/legacy/watch"
        assert get_user_preference("target") == "/legacy/target"
        assert get_user_preference("watch_target_migrated_to_prefs") is True

        # config.ini should no longer contain WATCH/TARGET keys
        on_disk = cfg_file.read_text()
        assert "WATCH" not in on_disk.split("\n")[0:10] or "WATCH = " not in on_disk
        assert "TARGET = " not in on_disk


def test_get_watch_dir_and_get_target_dir_return_user_preference(fresh_config, fresh_db):
    """Helpers should read from user_preferences."""
    cc, _ = fresh_config
    from core.database import set_user_preference
    set_user_preference("watch", "/x/watch", category="file_processing")
    set_user_preference("target", "/x/target", category="file_processing")

    assert cc.get_watch_dir() == "/x/watch"
    assert cc.get_target_dir() == "/x/target"


def test_helpers_return_empty_when_unset(fresh_config, fresh_db):
    """Helpers return '' when no preference is stored."""
    cc, _ = fresh_config
    assert cc.get_watch_dir() == ""
    assert cc.get_target_dir() == ""


def test_in_memory_config_mirrors_user_preferences(fresh_config, fresh_db, tmp_path):
    """Legacy reads via config.get('SETTINGS', 'TARGET') should still work."""
    cc, cfg_dir = fresh_config
    cfg_file = cfg_dir / "config.ini"
    cfg_file.write_text("[SETTINGS]\nIGNORED_TERMS = Annual\n")

    from core.database import set_user_preference
    set_user_preference("watch", "/m/watch", category="file_processing")
    set_user_preference("target", "/m/target", category="file_processing")

    with patch.object(cc, "CONFIG_FILE", str(cfg_file)):
        cc.load_config()

        # Legacy access pattern (api.py and a few other files still use this)
        assert cc.config.get("SETTINGS", "WATCH") == "/m/watch"
        assert cc.config.get("SETTINGS", "TARGET") == "/m/target"


def test_migration_runs_only_once(fresh_config, fresh_db, tmp_path):
    """The migration is idempotent and the flag prevents re-runs."""
    cc, cfg_dir = fresh_config
    cfg_file = cfg_dir / "config.ini"
    cfg_file.write_text("[SETTINGS]\nTARGET = /first/target\nWATCH = /first/watch\n")

    with patch.object(cc, "CONFIG_FILE", str(cfg_file)):
        cc.load_config()

        from core.database import set_user_preference, get_user_preference
        # User changes their target via the config page (writes to user_preferences)
        set_user_preference("target", "/user/changed", category="file_processing")

        # Even if config.ini somehow gets a stale TARGET written to it, a second
        # load_config() must NOT clobber the user's preference.
        cfg_file.write_text("[SETTINGS]\nTARGET = /stale/leftover\n")
        cc.load_config()

        assert get_user_preference("target") == "/user/changed"
