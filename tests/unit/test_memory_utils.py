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


class TestSwapCountsTowardsUsage:
    """#842: a worker with 0.3 GB resident and 3.99 GB swapped out never
    crossed the 1000 MB threshold, because only RSS was read."""

    def test_usage_is_resident_plus_swapped(self, monkeypatch):
        from types import SimpleNamespace
        from core import memory_utils

        monitor = MemoryMonitor(threshold_mb=1500, cleanup_threshold_mb=1000)
        monitor.process = SimpleNamespace(
            memory_info=lambda: SimpleNamespace(rss=300 * 1024 * 1024))
        monkeypatch.setattr(memory_utils, "_swapped_bytes", lambda: 3990 * 1024 * 1024)

        assert monitor.get_memory_usage() == 4290
        assert monitor.should_cleanup()

    def test_vmswap_is_read_from_proc_status(self, monkeypatch, tmp_path):
        import builtins
        from core import memory_utils

        status = tmp_path / "status"
        status.write_text("Name:\tgunicorn\nVmRSS:\t  300000 kB\nVmSwap:\t  2048 kB\n")
        real_open = builtins.open
        monkeypatch.setattr(
            builtins, "open",
            lambda path, *a, **k: real_open(status if path == "/proc/self/status" else path, *a, **k))

        assert memory_utils._swapped_bytes() == 2048 * 1024

    def test_no_proc_means_no_swap_not_an_error(self, monkeypatch):
        import builtins
        from core import memory_utils

        def refuse(path, *a, **k):
            raise FileNotFoundError(path)

        monkeypatch.setattr(builtins, "open", refuse)
        assert memory_utils._swapped_bytes() == 0


class TestCleanupReturnsHeapToTheOs:
    def test_force_cleanup_trims_after_collecting(self, monkeypatch):
        from core import memory_utils

        order = []
        monkeypatch.setattr(memory_utils.gc, "collect", lambda: order.append("gc") or 0)
        monkeypatch.setattr(memory_utils, "release_free_heap", lambda: order.append("trim"))
        monitor = MemoryMonitor()
        monkeypatch.setattr(monitor, "get_memory_usage", lambda: 1200.0)

        monitor.force_cleanup()
        assert order == ["gc", "trim"]

    def test_trim_never_raises(self):
        from core import memory_utils

        assert memory_utils.release_free_heap() in (True, False)


def test_image_caps_malloc_arenas():
    import pathlib

    dockerfile = pathlib.Path(__file__).resolve().parents[2] / "Dockerfile"
    assert "MALLOC_ARENA_MAX=2" in dockerfile.read_text(encoding="utf-8")
