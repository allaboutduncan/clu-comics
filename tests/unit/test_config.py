"""Tests for core.config -- the leftover config.ini warning and Flask config."""
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


class TestLeftoverIni:
    """config.ini is not read any more; one left behind is reported, once."""

    @pytest.fixture(autouse=True)
    def _fresh(self, tmp_path, monkeypatch):
        import core.config as cc
        monkeypatch.setattr(cc, "CONFIG_DIR", str(tmp_path))
        monkeypatch.setattr(cc, "_leftover_ini_reported", False)
        self.cc = cc
        self.dir = tmp_path

    def test_no_file_no_warning_and_none_created(self):
        with patch.object(self.cc.app_logger, "warning") as warn:
            self.cc.load_config()
        warn.assert_not_called()
        assert not (self.dir / "config.ini").exists()

    def test_unimported_ini_is_reported_once(self):
        ini = self.dir / "config.ini"
        ini.write_text("[SETTINGS]\nAUTOCONVERT = True\n")
        with patch.object(self.cc.app_logger, "warning") as warn:
            self.cc.load_config()
            self.cc.load_config()
        assert warn.call_count == 1
        message = warn.call_args[0][0]
        assert str(ini) in message and "7.3" in message

    def test_the_file_is_neither_read_nor_touched(self, db):
        ini = self.dir / "config.ini"
        body = "[SETTINGS]\nAUTOCONVERT = True\n"
        ini.write_text(body)
        self.cc.load_config()
        assert ini.read_text() == body
        assert cs.get_bool("AUTOCONVERT") is False

    def test_an_imported_ini_is_silent(self):
        (self.dir / "config.ini.migrated").write_text("[SETTINGS]\n")
        with patch.object(self.cc.app_logger, "warning") as warn:
            self.cc.load_config()
        warn.assert_not_called()

    def test_the_view_and_the_importer_are_gone(self):
        for name in ("config", "CONFIG_FILE", "migrate_ini_to_user_preferences",
                     "seed_monitor_from_env"):
            assert not hasattr(self.cc, name), name


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
