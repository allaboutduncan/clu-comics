"""Tests for core.config -- the preference-backed compatibility view and Flask config."""
import os
from unittest.mock import patch

import pytest

import core.settings as cs


@pytest.fixture
def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    with patch("core.database.get_db_path", return_value=db_path):
        from core.database import init_db
        init_db()
        cs.invalidate()
        yield db_path
        cs.invalidate()


class TestCompatibilityView:
    """api.py and a long tail of callers still read config.get("SETTINGS", KEY)."""

    def test_load_config_never_creates_an_ini(self, tmp_path, db):
        config_file = str(tmp_path / "config.ini")
        with patch("core.config.CONFIG_FILE", config_file):
            from core.config import load_config
            load_config()
        assert not os.path.exists(config_file)

    def test_section_exists(self, db):
        from core.config import config
        assert config.has_section("SETTINGS")
        assert "SETTINGS" in config

    def test_defaults_come_from_the_schema(self, db):
        from core.config import config
        assert config.get("SETTINGS", "AUTOCONVERT") == "False"
        assert config.getboolean("SETTINGS", "AUTO_RENAME_MONITOR") is True
        assert config.getint("SETTINGS", "TRASH_MAX_SIZE_MB") == 1024
        assert config.get("SETTINGS", "CACHE_DIR") == "/cache"

    def test_reads_stored_values(self, db):
        from core.config import config
        cs.set_setting("AUTOCONVERT", True)
        cs.set_setting("IGNORED_EXTENSIONS", ".a,.b")
        assert config.getboolean("SETTINGS", "AUTOCONVERT") is True
        assert config.get("SETTINGS", "IGNORED_EXTENSIONS") == ".a,.b"
        assert config["SETTINGS"]["IGNORED_EXTENSIONS"] == ".a,.b"
        assert config["SETTINGS"].get("NOT_A_SETTING", "x") == "x"

    def test_unknown_key_uses_fallback(self, db):
        from core.config import config
        assert config.get("SETTINGS", "NOPE", fallback="z") == "z"
        assert config.getboolean("SETTINGS", "NOPE", fallback=True) is True
        assert config.getint("SETTINGS", "NOPE", fallback=7) == 7
        assert not config.has_option("SETTINGS", "NOPE")

    def test_watch_and_target_come_from_prefs(self, db):
        from core.config import config
        from core.database import set_user_preference
        set_user_preference("watch", "/w")
        assert config.get("SETTINGS", "WATCH") == "/w"
        assert config.get("SETTINGS", "TARGET") == "/downloads/processed"

    def test_writes_are_refused(self, db):
        """The view is read-only: a stray write must not silently diverge."""
        from core.config import config
        config.set("SETTINGS", "AUTOCONVERT", "True")
        config["SETTINGS"]["AUTOCONVERT"] = "True"
        assert config.getboolean("SETTINGS", "AUTOCONVERT") is False


class TestLoadFlaskConfig:

    def test_app_config_reflects_stored_settings(self, db):
        from flask import Flask
        from core.config import load_flask_config

        cs.set_setting("AUTOCONVERT", True)
        cs.set_setting("TRASH_MAX_SIZE_MB", 50)
        cs.set_setting("HEADERS", "X-Test: 1")
        app = Flask(__name__)
        load_flask_config(app)
        assert app.config["AUTOCONVERT"] is True
        assert app.config["TRASH_MAX_SIZE_MB"] == 50
        assert app.config["HEADERS"] == "X-Test: 1"

    def test_a_save_is_visible_on_the_next_load(self, db):
        """load_flask_config runs after every save, so it must not serve a stale cache."""
        from flask import Flask
        from core.config import load_flask_config

        app = Flask(__name__)
        load_flask_config(app)
        assert app.config["XML_YEAR"] is False
        from core.database import set_user_preference
        set_user_preference("xml_year", True)  # as another process would
        load_flask_config(app)
        assert app.config["XML_YEAR"] is True


class TestLoadFlaskConfigSecretKey:
    """load_flask_config must not rotate an already-established secret key.

    It runs again on every settings save (provider credentials, file
    processing, download/API, system perf). Minting a fresh key there
    invalidates every session cookie signed with the old one, so saving a
    setting silently logs everyone out -- and because the session isn't
    modified during that request, Flask sends no replacement cookie.
    """

    @staticmethod
    def _app():
        from flask import Flask
        return Flask(__name__)

    def test_preserves_existing_secret_key(self, db, monkeypatch):
        from core.config import load_flask_config

        monkeypatch.delenv("SECRET_KEY", raising=False)
        app = self._app()
        app.secret_key = "stable-persisted-key"
        load_flask_config(app)
        assert app.secret_key == "stable-persisted-key"

    def test_env_secret_key_still_wins(self, db, monkeypatch):
        from core.config import load_flask_config

        monkeypatch.setenv("SECRET_KEY", "from-env")
        app = self._app()
        app.secret_key = "stale-key"
        load_flask_config(app)
        assert app.secret_key == "from-env"

    def test_mints_provisional_key_when_none_set(self, db, monkeypatch):
        """First boot, before app.py upgrades it to the persisted one."""
        from core.config import load_flask_config

        monkeypatch.delenv("SECRET_KEY", raising=False)
        app = self._app()
        app.secret_key = None
        load_flask_config(app)
        assert app.secret_key

    def test_repeated_calls_do_not_rotate(self, db, monkeypatch):
        """The actual regression: a settings save must not log everyone out."""
        from core.config import load_flask_config

        monkeypatch.delenv("SECRET_KEY", raising=False)
        app = self._app()
        app.secret_key = None
        load_flask_config(app)
        first = app.secret_key
        load_flask_config(app)
        assert first == app.secret_key
