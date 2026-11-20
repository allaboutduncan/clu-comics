"""WATCH/TARGET are read from user_preferences only."""
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


def test_get_watch_dir_and_get_target_dir_return_user_preference(fresh_db):
    set_user_preference("watch", "/x/watch", category="file_processing")
    set_user_preference("target", "/x/target", category="file_processing")
    assert cc.get_watch_dir() == "/x/watch"
    assert cc.get_target_dir() == "/x/target"


def test_helpers_return_empty_when_unset(fresh_db):
    assert cc.get_watch_dir() == ""
    assert cc.get_target_dir() == ""
