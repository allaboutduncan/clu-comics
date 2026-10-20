"""Guided Setup lifecycle prefs (``core/setup_state.py``) and the init_db stamp.

The redirect itself is covered in ``tests/routes/test_setup.py``.
"""
import sqlite3
from unittest.mock import patch

import pytest


def _stored_state(db_path):
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value, category FROM user_preferences WHERE key = 'guided_setup_state'"
        ).fetchone()
    finally:
        conn.close()
    return row


@pytest.fixture
def init(db_path):
    from core import settings
    from core.database import init_db, wait_for_background_analyze

    def run():
        with patch("core.database.get_db_path", return_value=db_path):
            assert init_db()
            wait_for_background_analyze()
        settings.invalidate()

    return run


class TestInitDbStamp:
    """init_db decides fresh-vs-upgraded once, from the tables it found."""

    def test_fresh_database_is_none(self, db_path, init):
        init()
        assert _stored_state(db_path) == ('"none"', "onboarding")

    def test_existing_database_is_upgraded(self, db_path, init):
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE some_older_table (x TEXT)")
        conn.commit()
        conn.close()
        init()
        assert _stored_state(db_path)[0] == '"upgraded"'

    def test_never_restamped(self, db_path, init):
        init()
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE user_preferences SET value = '\"completed\"' WHERE key = 'guided_setup_state'")
        conn.commit()
        conn.close()
        init()
        assert _stored_state(db_path)[0] == '"completed"'

    def test_pre_wizard_install_on_restart_is_upgraded(self, db_path, init):
        """A database from before this release has tables but no pref."""
        init()
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM user_preferences WHERE key = 'guided_setup_state'")
        conn.commit()
        conn.close()
        init()
        assert _stored_state(db_path)[0] == '"upgraded"'

    def test_readable_through_settings(self, db_path, init):
        from core.setup_state import get_setup_state

        init()
        with patch("core.database.get_db_path", return_value=db_path):
            assert get_setup_state() == {"state": "none", "last_step": "", "path": []}


class TestSetupState:
    def test_schema_entries(self):
        from core.settings import SETTINGS_SCHEMA

        for key, pref in [("GUIDED_SETUP_STATE", "guided_setup_state"),
                          ("GUIDED_SETUP_LAST_STEP", "guided_setup_last_step"),
                          ("GUIDED_SETUP_PATH", "guided_setup_path")]:
            assert SETTINGS_SCHEMA[key].pref == pref
            assert SETTINGS_SCHEMA[key].category == "onboarding"

    def test_unset_state_is_undecided_not_none(self):
        """An unreadable store must not look like a fresh install."""
        from core.settings import SETTINGS_SCHEMA
        from core.setup_state import STATE_NONE

        assert SETTINGS_SCHEMA["GUIDED_SETUP_STATE"].default != STATE_NONE

    def test_claim_first_run_once(self, db_connection):
        from core.setup_state import claim_first_run, get_setup_state

        assert get_setup_state()["state"] == "none"
        assert claim_first_run() is True
        assert get_setup_state()["state"] == "in_progress"
        assert claim_first_run() is False

    def test_claim_with_unreadable_store(self):
        from core.setup_state import claim_first_run

        with patch("core.settings._load_snapshot", return_value=None):
            assert claim_first_run() is False

    def test_path_round_trip(self, db_connection):
        from core.setup_state import get_setup_state, set_setup_state

        assert set_setup_state(path=["repair", "metadata"], last_step="libraries")
        state = get_setup_state()
        assert state["path"] == ["repair", "metadata"]
        assert state["last_step"] == "libraries"

    @pytest.mark.parametrize("value,ok", [
        ("libraries", True), ("use_case-3", True), ("a", True),
        ("", False), ("Libraries", False), ("-x", False), ("a b", False),
        ("a,b", False), ("x" * 65, False), (None, False), (3, False),
    ])
    def test_tokens(self, value, ok):
        from core.setup_state import is_valid_token

        assert is_valid_token(value) is ok
