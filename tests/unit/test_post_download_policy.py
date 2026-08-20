"""
Unit tests for the post-download ownership policy in core/download_utils.py.

WATCH has two writers -- api.py's download workers (threads in the Gunicorn
process) and monitor.py (a separate OS process) -- and no lock between them.
These tests pin down which of the two finishes a completed download, which is
what stops the pair from both converting it, both moving it into TARGET, and
littering TARGET with the extraction pages in between.

The policy lives here rather than in api.py because api.py cannot be imported by
the suite at all (tests/routes/conftest.py substitutes a MagicMock for it).
"""
import pytest

from core.download_utils import (
    CONVERT_IN_PLACE,
    DEFER_TO_MONITOR,
    FULL_POST_PROCESS,
    monitor_claims,
    monitor_enabled,
    parse_ignored_extensions,
    post_download_action,
    watch_drain_timeout_seconds,
)

# The shipped default (core/config.py). Note .rar is on it -- that is the whole
# reason CONVERT_IN_PLACE exists.
DEFAULT_IGNORED = ".crdownload,.torrent,.tmp,.mega,.rar,.bak,.zip"


class TestMonitorEnabled:
    """Mirrors app.py's `os.environ.get("MONITOR","").strip().lower() == "yes"`."""

    @pytest.mark.parametrize("value", ["yes", "YES", "Yes", " yes "])
    def test_truthy_values(self, value):
        assert monitor_enabled({"MONITOR": value}) is True

    @pytest.mark.parametrize("value", ["no", "", "  ", "true", "1"])
    def test_falsy_values(self, value):
        assert monitor_enabled({"MONITOR": value}) is False

    def test_missing_key(self):
        assert monitor_enabled({}) is False

    def test_reads_real_environ_by_default(self, monkeypatch):
        monkeypatch.setenv("MONITOR", "yes")
        assert monitor_enabled() is True
        monkeypatch.setenv("MONITOR", "no")
        assert monitor_enabled() is False


class TestParseIgnoredExtensions:
    def test_normalizes_case_spacing_and_missing_dot(self):
        assert parse_ignored_extensions(".crdownload, .RAR ,zip") == {
            ".crdownload", ".rar", ".zip",
        }

    @pytest.mark.parametrize("value", ["", None, ",,  ,"])
    def test_empty_inputs(self, value):
        assert parse_ignored_extensions(value) == set()


class TestMonitorClaims:
    """Must stay in lockstep with monitor.py's _handle_file_if_complete."""

    @pytest.mark.parametrize("name", ["Batman 001.cbz", "Batman 001.cbr"])
    def test_claims_comics_not_on_the_ignore_list(self, name):
        assert monitor_claims(name, DEFAULT_IGNORED) is True

    def test_does_not_claim_rar(self):
        # .rar ships on IGNORED_EXTENSIONS, so the monitor never imports one.
        assert monitor_claims("Batman 001.rar", DEFAULT_IGNORED) is False

    def test_zip_depends_on_auto_unpack(self):
        assert monitor_claims("pack.zip", DEFAULT_IGNORED, auto_unpack=False) is False
        assert monitor_claims("pack.zip", DEFAULT_IGNORED, auto_unpack=True) is True

    def test_extensionless_is_not_claimed(self):
        assert monitor_claims("no_extension_here", DEFAULT_IGNORED) is False

    def test_empty_path(self):
        assert monitor_claims("", DEFAULT_IGNORED) is False


class TestPostDownloadAction:
    def _act(self, name, monitor_running, ignored=DEFAULT_IGNORED, auto_unpack=False):
        return post_download_action(
            name,
            monitor_running=monitor_running,
            ignored_extensions=ignored,
            auto_unpack=auto_unpack,
        )

    @pytest.mark.parametrize("name", ["a.cbr", "a.rar", "a.cbz", "a.zip"])
    def test_no_monitor_means_api_does_everything(self, name):
        # Nothing else drains WATCH in this configuration, so api.py must keep
        # its convert -> move -> rename behaviour unchanged.
        assert self._act(name, monitor_running=False) == FULL_POST_PROCESS

    @pytest.mark.parametrize("name", ["a.cbr", "a.cbz"])
    def test_monitor_claims_it_so_api_defers(self, name):
        assert self._act(name, monitor_running=True) == DEFER_TO_MONITOR

    def test_rar_is_converted_in_place_not_deferred(self):
        # Deferring a .rar would strand it in WATCH forever: the monitor's
        # ignore list means it will never pick one up.
        assert self._act("a.rar", monitor_running=True) == CONVERT_IN_PLACE

    def test_zip_defers_regardless_of_auto_unpack(self):
        assert self._act("a.zip", monitor_running=True, auto_unpack=False) == DEFER_TO_MONITOR
        assert self._act("a.zip", monitor_running=True, auto_unpack=True) == DEFER_TO_MONITOR

    def test_user_ignoring_cbr_falls_back_to_converting_in_place(self):
        assert self._act("a.cbr", monitor_running=True,
                         ignored=".cbr,.rar") == CONVERT_IN_PLACE

    def test_unconvertible_ignored_file_is_left_alone(self):
        assert self._act("notes.bak", monitor_running=True) == DEFER_TO_MONITOR

    @pytest.mark.parametrize("value", [None, ""])
    def test_missing_path_defers(self, value):
        assert self._act(value, monitor_running=True) == DEFER_TO_MONITOR
        assert self._act(value, monitor_running=False) == DEFER_TO_MONITOR


class TestWatchDrainTimeout:
    def test_covers_two_reconciliation_sweeps_plus_headroom(self):
        assert watch_drain_timeout_seconds(5) == 720
        assert watch_drain_timeout_seconds(15) == 1920

    def test_capped_at_an_hour(self):
        # A long sweep interval must not keep a poller thread alive per download.
        assert watch_drain_timeout_seconds(30) == 3600
        assert watch_drain_timeout_seconds(600) == 3600

    @pytest.mark.parametrize("value", [0, 1, -5])
    def test_never_below_the_historical_five_minutes(self, value):
        assert watch_drain_timeout_seconds(value) == 300

    @pytest.mark.parametrize("value", [None, "abc"])
    def test_bad_input_falls_back_to_the_default_interval(self, value):
        assert watch_drain_timeout_seconds(value) == 720
