"""Tests for config.py -- configuration loading and defaults."""
import pytest
import os
from unittest.mock import patch


class TestLoadConfig:

    def test_creates_config_file_if_missing(self, tmp_path):
        config_file = str(tmp_path / "config.ini")
        with patch("config.CONFIG_FILE", config_file), \
             patch("config.CONFIG_DIR", str(tmp_path)):
            from config import load_config, config
            load_config()
            assert os.path.exists(config_file)

    def test_settings_section_exists_after_load(self, tmp_path):
        config_file = str(tmp_path / "config.ini")
        with patch("config.CONFIG_FILE", config_file), \
             patch("config.CONFIG_DIR", str(tmp_path)):
            from config import load_config, config
            load_config()
            assert config.has_section("SETTINGS")

    def test_default_values_set(self, tmp_path):
        config_file = str(tmp_path / "config.ini")
        with patch("config.CONFIG_FILE", config_file), \
             patch("config.CONFIG_DIR", str(tmp_path)):
            from config import load_config, config
            load_config()
            assert config.get("SETTINGS", "AUTOCONVERT") == "False"
            assert config.get("SETTINGS", "CACHE_DIR") == "/cache"

    def test_preserves_existing_values(self, tmp_path):
        config_file = tmp_path / "config.ini"
        config_file.write_text(
            "[SETTINGS]\nAUTOCONVERT=True\nBOOTSTRAP_THEME=darkly\n"
        )
        with patch("config.CONFIG_FILE", str(config_file)), \
             patch("config.CONFIG_DIR", str(tmp_path)):
            from config import load_config, config
            load_config()
            assert config.get("SETTINGS", "AUTOCONVERT") == "True"
            assert config.get("SETTINGS", "BOOTSTRAP_THEME") == "darkly"

    def test_adds_missing_keys_to_existing_config(self, tmp_path):
        config_file = tmp_path / "config.ini"
        # Only write a few keys
        config_file.write_text("[SETTINGS]\nAUTOCONVERT=True\n")
        with patch("config.CONFIG_FILE", str(config_file)), \
             patch("config.CONFIG_DIR", str(tmp_path)):
            from config import load_config, config
            load_config()
            # Existing key preserved
            assert config.get("SETTINGS", "AUTOCONVERT") == "True"
            # Missing keys should get defaults
            assert config.has_option("SETTINGS", "CACHE_DIR")

    def test_case_sensitive_keys(self, tmp_path):
        config_file = str(tmp_path / "config.ini")
        with patch("config.CONFIG_FILE", config_file), \
             patch("config.CONFIG_DIR", str(tmp_path)):
            from config import load_config, config
            load_config()
            # Keys should be case-preserved (optionxform = str)
            assert config.has_option("SETTINGS", "AUTOCONVERT")
            assert config.has_option("SETTINGS", "CACHE_DIR")
