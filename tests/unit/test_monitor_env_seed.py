"""The MONITOR env var is imported into the monitor_enabled setting once.

Afterwards only the setting counts, so an install upgraded with MONITOR=yes
still in its compose file comes up monitoring, and a user who later switches it
off in the UI is not overruled by the env on the next start.
"""
from unittest.mock import patch

import pytest

import core.config as cc
import core.settings as cs
from core.database import get_user_preference


@pytest.fixture
def db(tmp_path):
    with patch("core.database.get_db_path", return_value=str(tmp_path / "t.db")):
        from core.database import init_db
        init_db()
        cs.invalidate()
        yield
        cs.invalidate()


def test_yes_enables_monitoring(db):
    assert cc.seed_monitor_from_env({"MONITOR": "yes"}) is True
    assert cs.get_bool("MONITOR_ENABLED") is True
    assert get_user_preference(cc.PREF_MONITOR_ENV_MIGRATED) is True


@pytest.mark.parametrize("value", [" YES ", "Yes"])
def test_yes_is_case_and_space_insensitive(db, value):
    assert cc.seed_monitor_from_env({"MONITOR": value}) is True


@pytest.mark.parametrize("env", [{}, {"MONITOR": "no"}, {"MONITOR": "yes/no"}])
def test_anything_else_leaves_it_off(db, env):
    assert cc.seed_monitor_from_env(env) is False
    assert cs.get_bool("MONITOR_ENABLED") is False
    # Still marked done, so a later MONITOR=yes is not imported over a choice.
    assert get_user_preference(cc.PREF_MONITOR_ENV_MIGRATED) is True


def test_runs_once(db):
    cc.seed_monitor_from_env({})
    assert cc.seed_monitor_from_env({"MONITOR": "yes"}) is False
    assert cs.get_bool("MONITOR_ENABLED") is False


def test_never_overwrites_a_stored_value(db):
    cs.set_setting("MONITOR_ENABLED", False)
    assert cc.seed_monitor_from_env({"MONITOR": "yes"}) is False
    assert cs.get_bool("MONITOR_ENABLED") is False


def test_user_choice_survives_restart_with_env_still_set(db):
    cc.seed_monitor_from_env({"MONITOR": "yes"})
    cs.set_setting("MONITOR_ENABLED", False)
    cc.seed_monitor_from_env({"MONITOR": "yes"})
    assert cs.get_bool("MONITOR_ENABLED") is False


def test_failed_write_does_not_set_the_flag(db):
    with patch("core.settings.set_setting", return_value=False):
        assert cc.seed_monitor_from_env({"MONITOR": "yes"}) is False
    assert not get_user_preference(cc.PREF_MONITOR_ENV_MIGRATED, default=False)
    # Retried on the next start.
    assert cc.seed_monitor_from_env({"MONITOR": "yes"}) is True


def test_database_not_ready_does_nothing():
    with patch.object(cc, "_db_ready", return_value=False):
        assert cc.seed_monitor_from_env({"MONITOR": "yes"}) is False


class TestPeekSetting:
    def test_reads_the_value(self, db):
        cs.set_setting("MONITOR_ENABLED", True)
        assert cs.peek_setting("MONITOR_ENABLED") is True

    def test_unreadable_store_is_none_not_the_default(self, db):
        cs.invalidate()
        with patch.object(cs, "_load_snapshot", return_value=None):
            assert cs.peek_setting("MONITOR_ENABLED") is None
            assert cs.get_setting("MONITOR_ENABLED") is False
