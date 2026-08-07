"""Tests for core.memory_utils threshold configuration.

The high-memory warning re-fires on every poll (60s) for as long as RSS stays
above the threshold, so a deployment whose normal working set is legitimately
larger than the default would fill its logs with a warning it can do nothing
about. The thresholds have to be tunable.
"""
import pytest

from core.memory_utils import MemoryMonitor, _threshold_from_env


class TestThresholdFromEnv:

    def test_returns_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("MEMORY_THRESHOLD_MB", raising=False)
        assert _threshold_from_env("MEMORY_THRESHOLD_MB", 1500) == 1500

    def test_reads_the_environment(self, monkeypatch):
        monkeypatch.setenv("MEMORY_THRESHOLD_MB", "4096")
        assert _threshold_from_env("MEMORY_THRESHOLD_MB", 1500) == 4096

    def test_garbage_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("MEMORY_THRESHOLD_MB", "lots")
        assert _threshold_from_env("MEMORY_THRESHOLD_MB", 1500) == 1500

    def test_non_positive_falls_back_to_default(self, monkeypatch):
        """A zero threshold would warn on every single poll forever."""
        monkeypatch.setenv("MEMORY_THRESHOLD_MB", "0")
        assert _threshold_from_env("MEMORY_THRESHOLD_MB", 1500) == 1500
        monkeypatch.setenv("MEMORY_THRESHOLD_MB", "-5")
        assert _threshold_from_env("MEMORY_THRESHOLD_MB", 1500) == 1500


class TestMemoryMonitorThresholds:

    def test_defaults_preserved(self, monkeypatch):
        monkeypatch.delenv("MEMORY_THRESHOLD_MB", raising=False)
        monkeypatch.delenv("MEMORY_CLEANUP_THRESHOLD_MB", raising=False)

        monitor = MemoryMonitor()

        assert monitor.threshold_mb == 1500
        assert monitor.cleanup_threshold_mb == 1000

    def test_env_overrides_both(self, monkeypatch):
        monkeypatch.setenv("MEMORY_THRESHOLD_MB", "3000")
        monkeypatch.setenv("MEMORY_CLEANUP_THRESHOLD_MB", "2500")

        monitor = MemoryMonitor()

        assert monitor.threshold_mb == 3000
        assert monitor.cleanup_threshold_mb == 2500

    def test_explicit_arguments_win_over_env(self, monkeypatch):
        monkeypatch.setenv("MEMORY_THRESHOLD_MB", "3000")

        monitor = MemoryMonitor(threshold_mb=800, cleanup_threshold_mb=400)

        assert monitor.threshold_mb == 800
        assert monitor.cleanup_threshold_mb == 400
