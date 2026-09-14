"""Tests for config.py -- configuration loading and defaults."""
import pytest
import os
from unittest.mock import patch


class TestLoadConfig:

    def test_creates_config_file_if_missing(self, tmp_path):
        config_file = str(tmp_path / "config.ini")
        with patch("core.config.CONFIG_FILE", config_file), \
             patch("core.config.CONFIG_DIR", str(tmp_path)):
            from core.config import load_config, config
            load_config()
            assert os.path.exists(config_file)

    def test_settings_section_exists_after_load(self, tmp_path):
        config_file = str(tmp_path / "config.ini")
        with patch("core.config.CONFIG_FILE", config_file), \
             patch("core.config.CONFIG_DIR", str(tmp_path)):
            from core.config import load_config, config
            load_config()
            assert config.has_section("SETTINGS")

    def test_default_values_set(self, tmp_path):
        config_file = str(tmp_path / "config.ini")
        with patch("core.config.CONFIG_FILE", config_file), \
             patch("core.config.CONFIG_DIR", str(tmp_path)):
            from core.config import load_config, config
            load_config()
            assert config.get("SETTINGS", "AUTOCONVERT") == "False"
            assert config.get("SETTINGS", "CACHE_DIR") == "/cache"

    def test_preserves_existing_values(self, tmp_path):
        config_file = tmp_path / "config.ini"
        config_file.write_text(
            "[SETTINGS]\nAUTOCONVERT=True\nBOOTSTRAP_THEME=darkly\n"
        )
        with patch("core.config.CONFIG_FILE", str(config_file)), \
             patch("core.config.CONFIG_DIR", str(tmp_path)):
            from core.config import load_config, config
            load_config()
            assert config.get("SETTINGS", "AUTOCONVERT") == "True"
            assert config.get("SETTINGS", "BOOTSTRAP_THEME") == "darkly"

    def test_adds_missing_keys_to_existing_config(self, tmp_path):
        config_file = tmp_path / "config.ini"
        # Only write a few keys
        config_file.write_text("[SETTINGS]\nAUTOCONVERT=True\n")
        with patch("core.config.CONFIG_FILE", str(config_file)), \
             patch("core.config.CONFIG_DIR", str(tmp_path)):
            from core.config import load_config, config
            load_config()
            # Existing key preserved
            assert config.get("SETTINGS", "AUTOCONVERT") == "True"
            # Missing keys should get defaults
            assert config.has_option("SETTINGS", "CACHE_DIR")

    def test_case_sensitive_keys(self, tmp_path):
        config_file = str(tmp_path / "config.ini")
        with patch("core.config.CONFIG_FILE", config_file), \
             patch("core.config.CONFIG_DIR", str(tmp_path)):
            from core.config import load_config, config
            load_config()
            # Keys should be case-preserved (optionxform = str)
            assert config.has_option("SETTINGS", "AUTOCONVERT")
            assert config.has_option("SETTINGS", "CACHE_DIR")


class TestLoadFlaskConfigSecretKey:
    """load_flask_config must not rotate an already-established secret key.

    It runs again on every settings save (provider credentials, file
    processing, download/API, system perf, the main config POST). Minting a
    fresh key there invalidates every session cookie signed with the old one,
    so saving a setting silently logs everyone out -- and because the session
    isn't modified during that request, Flask sends no replacement cookie.
    """

    @staticmethod
    def _app():
        from flask import Flask
        return Flask(__name__)

    def test_preserves_existing_secret_key(self, tmp_path, monkeypatch):
        from core.config import load_flask_config

        monkeypatch.delenv("SECRET_KEY", raising=False)
        app = self._app()
        app.secret_key = "stable-persisted-key"

        with patch("core.config.CONFIG_FILE", str(tmp_path / "config.ini")),              patch("core.config.CONFIG_DIR", str(tmp_path)):
            load_flask_config(app)

        assert app.secret_key == "stable-persisted-key"

    def test_env_secret_key_still_wins(self, tmp_path, monkeypatch):
        from core.config import load_flask_config

        monkeypatch.setenv("SECRET_KEY", "from-env")
        app = self._app()
        app.secret_key = "stale-key"

        with patch("core.config.CONFIG_FILE", str(tmp_path / "config.ini")),              patch("core.config.CONFIG_DIR", str(tmp_path)):
            load_flask_config(app)

        assert app.secret_key == "from-env"

    def test_mints_provisional_key_when_none_set(self, tmp_path, monkeypatch):
        """First boot, before app.py upgrades it to the persisted one."""
        from core.config import load_flask_config

        monkeypatch.delenv("SECRET_KEY", raising=False)
        app = self._app()
        app.secret_key = None

        with patch("core.config.CONFIG_FILE", str(tmp_path / "config.ini")),              patch("core.config.CONFIG_DIR", str(tmp_path)):
            load_flask_config(app)

        assert app.secret_key

    def test_repeated_calls_do_not_rotate(self, tmp_path, monkeypatch):
        """The actual regression: a settings save must not log everyone out."""
        from core.config import load_flask_config

        monkeypatch.delenv("SECRET_KEY", raising=False)
        app = self._app()
        app.secret_key = None

        with patch("core.config.CONFIG_FILE", str(tmp_path / "config.ini")),              patch("core.config.CONFIG_DIR", str(tmp_path)):
            load_flask_config(app)
            first = app.secret_key
            load_flask_config(app)
            second = app.secret_key

        assert first == second
