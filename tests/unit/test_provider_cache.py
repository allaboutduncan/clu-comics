"""Tests for helpers.provider_cache -- writable cache locations for provider clients.

Simyan and Mokkari both default their SQLite files to ``$HOME/.cache``, which the
container's non-root user can neither reach nor create (issue #396). These tests
pin the resolution order and the fallback that keeps a provider working when the
preferred location is unwritable.
"""
import os

import pytest

from helpers.provider_cache import provider_cache_dir


class TestProviderCacheDir:

    def test_honours_xdg_cache_home(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

        result = provider_cache_dir("simyan")

        assert result == str(tmp_path / "simyan")

    def test_creates_the_directory(self, tmp_path, monkeypatch):
        """Neither Simyan nor Mokkari creates parent dirs itself."""
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

        result = provider_cache_dir("mokkari")

        assert os.path.isdir(result)

    def test_leaf_name_separates_providers(self, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))

        assert provider_cache_dir("simyan") != provider_cache_dir("mokkari")

    def test_falls_back_to_tempdir_when_unwritable(self, monkeypatch):
        """An unwritable config dir must not take the provider down with it."""
        import helpers.provider_cache as pc

        real_makedirs = os.makedirs

        def fake_makedirs(path, *args, **kwargs):
            if "clu-mokkari" not in str(path):
                raise OSError(13, "Permission denied")
            return real_makedirs(path, *args, **kwargs)

        monkeypatch.setenv("XDG_CACHE_HOME", "/nonexistent-unwritable")
        monkeypatch.setattr(pc.os, "makedirs", fake_makedirs)

        result = provider_cache_dir("mokkari")

        assert "clu-mokkari" in result
        assert os.path.isdir(result)

    def test_falls_back_to_config_dir_without_xdg(self, tmp_path, monkeypatch):
        import core.config

        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setattr(core.config, "CONFIG_DIR", str(tmp_path))

        result = provider_cache_dir("simyan")

        assert result == str(tmp_path / ".cache" / "simyan")
