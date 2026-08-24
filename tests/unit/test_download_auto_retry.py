"""Automatic retry policy for failed downloads (core.download_utils).

A download that fails on every mirror is usually the victim of something
transient, so CLU parks it and puts the same task back on the queue up to
``MAX_AUTO_RETRIES`` times before calling it dead. The decision logic lives in
core.download_utils rather than api.py precisely so it can be tested here --
api.py starts worker threads and a cloudscraper session at import time and
cannot be imported by the suite at all. api.py keeps only the threading.Timer
wiring on top of these functions.
"""

import ast
import os

import pytest

from core.download_utils import (
    ACTIVE_STATUSES,
    AUTO_RETRY_DELAYS,
    CANCELLABLE_STATUSES,
    MAX_AUTO_RETRIES,
    RETRYABLE_STATUSES,
    RETRY_PENDING,
    _LIVE_TO_HISTORY_STATUS,
    auto_retry_delay,
    begin_retry_wait,
    build_status_snapshot,
    count_active_downloads,
    reset_for_retry,
    retry_seconds_remaining,
    should_auto_retry,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
API_PATH = os.path.join(PROJECT_ROOT, "api.py")


@pytest.fixture
def progress():
    """A minimal stand-in for api.download_progress with one failed download."""
    return {
        "dl-1": {
            "url": "https://example.test/comic.cbz",
            "status": "error",
            "progress": 63,
            "bytes_total": 1000,
            "bytes_downloaded": 630,
            "error": "Connection reset",
            "provider": "GetComics",
        }
    }


class TestBackoffSchedule:
    def test_three_retries_spaced_one_five_and_fifteen_minutes(self):
        assert AUTO_RETRY_DELAYS == (60, 300, 900)

    def test_max_retries_matches_the_schedule_length(self):
        """The tuple is the real bound; the constant only names it."""
        assert MAX_AUTO_RETRIES == len(AUTO_RETRY_DELAYS)

    @pytest.mark.parametrize("attempt,expected", [(0, 60), (1, 300), (2, 900)])
    def test_delay_for_each_attempt(self, attempt, expected):
        assert auto_retry_delay(attempt) == expected

    def test_out_of_range_attempt_clamps_instead_of_raising(self):
        """Shortening the schedule must not IndexError on a download worker."""
        assert auto_retry_delay(99) == AUTO_RETRY_DELAYS[-1]
        assert auto_retry_delay(-3) == AUTO_RETRY_DELAYS[0]

    def test_junk_attempt_falls_back_to_the_first_delay(self):
        assert auto_retry_delay(None) == AUTO_RETRY_DELAYS[0]
        assert auto_retry_delay("soon") == AUTO_RETRY_DELAYS[0]


class TestShouldAutoRetry:
    def test_a_fresh_failure_retries(self, progress):
        assert should_auto_retry(progress, "dl-1", 0) is True

    @pytest.mark.parametrize("attempt", range(MAX_AUTO_RETRIES))
    def test_every_attempt_inside_the_budget_retries(self, progress, attempt):
        assert should_auto_retry(progress, "dl-1", attempt) is True

    def test_the_budget_is_capped(self, progress):
        assert should_auto_retry(progress, "dl-1", MAX_AUTO_RETRIES) is False
        assert should_auto_retry(progress, "dl-1", MAX_AUTO_RETRIES + 5) is False

    def test_a_cancelled_download_never_retries(self, progress):
        """Aborting the transfer is how a cancel surfaces from most providers,
        so the failure path runs for cancels too."""
        progress["dl-1"]["cancelled"] = True
        assert should_auto_retry(progress, "dl-1", 0) is False

    def test_a_dismissed_download_never_retries(self, progress):
        """The entry can be cleared while the download is still running."""
        assert should_auto_retry(progress, "dl-1", 0) is True
        del progress["dl-1"]
        assert should_auto_retry(progress, "dl-1", 0) is False

    def test_a_cloudflare_failure_never_retries(self, progress):
        """manual_url is only set on the Cloudflare-challenge branch. No
        automated client passes those, so retrying only delays the manual link
        the user actually needs by the full backoff window."""
        progress["dl-1"]["manual_url"] = "https://getcomics.test/post"
        assert should_auto_retry(progress, "dl-1", 0) is False

    def test_tolerates_a_non_dict_progress(self):
        assert should_auto_retry(None, "dl-1", 0) is False

    def test_junk_attempt_does_not_raise(self, progress):
        assert should_auto_retry(progress, "dl-1", "two") is False


class TestBeginRetryWait:
    def test_parks_the_download_in_its_own_status(self, progress):
        begin_retry_wait(progress, "dl-1", 0, 60)
        assert progress["dl-1"]["status"] == RETRY_PENDING

    def test_the_pending_status_is_not_error(self, progress):
        """'Clear Failed Downloads' filters on 'error' -- a download that is
        about to run again must not be swept away by it."""
        begin_retry_wait(progress, "dl-1", 0, 60)
        assert progress["dl-1"]["status"] != "error"

    def test_records_the_attempt_number_one_based(self, progress):
        begin_retry_wait(progress, "dl-1", 1, 300)
        assert progress["dl-1"]["retry_count"] == 2

    def test_records_when_the_retry_is_due(self, progress):
        begin_retry_wait(progress, "dl-1", 0, 60)
        assert 55 <= retry_seconds_remaining(progress["dl-1"]) <= 60

    def test_keeps_the_error_for_the_tooltip(self, progress):
        begin_retry_wait(progress, "dl-1", 0, 60, error="HTTP 503")
        assert progress["dl-1"]["error"] == "HTTP 503"

    def test_leaves_the_existing_error_alone_when_none_is_given(self, progress):
        begin_retry_wait(progress, "dl-1", 0, 60)
        assert progress["dl-1"]["error"] == "Connection reset"

    def test_a_missing_entry_is_a_no_op(self, progress):
        begin_retry_wait(progress, "gone", 0, 60)
        assert "gone" not in progress


class TestResetForRetry:
    def test_requeues_as_queued_with_counters_zeroed(self, progress):
        reset_for_retry(progress, "dl-1")
        entry = progress["dl-1"]
        assert entry["status"] == "queued"
        assert entry["progress"] == 0
        assert entry["bytes_total"] == 0
        assert entry["bytes_downloaded"] == 0
        assert entry["error"] is None
        assert entry["cancelled"] is False
        assert entry["provider"] is None
        assert entry["retry_at"] is None

    def test_clears_the_cloudflare_marker(self, progress):
        """A stale manual_url would veto every future auto-retry of this
        download, because should_auto_retry treats it as unrecoverable."""
        progress["dl-1"]["manual_url"] = "https://getcomics.test/post"
        reset_for_retry(progress, "dl-1")
        assert progress["dl-1"]["manual_url"] is None
        assert should_auto_retry(progress, "dl-1", 0) is True

    def test_keeps_the_url_so_the_download_can_run_again(self, progress):
        reset_for_retry(progress, "dl-1")
        assert progress["dl-1"]["url"] == "https://example.test/comic.cbz"

    def test_a_missing_entry_is_a_no_op(self, progress):
        reset_for_retry(progress, "gone")
        assert "gone" not in progress


class TestRetrySecondsRemaining:
    def test_counts_down_to_the_due_time(self):
        import time

        assert retry_seconds_remaining({"retry_at": time.time() + 90}) == 90

    def test_never_goes_negative(self):
        import time

        assert retry_seconds_remaining({"retry_at": time.time() - 500}) == 0

    def test_accepts_an_explicit_now(self):
        assert retry_seconds_remaining({"retry_at": 1_000_060}, now=1_000_000) == 60

    def test_tolerates_a_missing_or_junk_due_time(self):
        assert retry_seconds_remaining({}) == 0
        assert retry_seconds_remaining({"retry_at": None}) == 0
        assert retry_seconds_remaining({"retry_at": "soon"}) == 0
        assert retry_seconds_remaining(None) == 0


class TestWeeklyPackStatusMapping:
    def test_a_pending_retry_still_reads_as_downloading(self):
        """Mapping it to 'failed' would make is_weekly_pack_downloaded() stop
        counting the row, so the scheduler would queue a second download of the
        same pack alongside the pending retry."""
        assert _LIVE_TO_HISTORY_STATUS[RETRY_PENDING] == "downloading"


class TestStatusSets:
    def test_a_pending_retry_counts_as_active(self):
        """The nav badge must not go quiet while a download is still coming."""
        assert RETRY_PENDING in ACTIVE_STATUSES

    def test_a_pending_retry_can_be_retried_by_hand(self):
        """Nobody should have to sit through a fifteen-minute backoff."""
        assert RETRY_PENDING in RETRYABLE_STATUSES

    def test_a_pending_retry_can_be_cancelled(self):
        assert RETRY_PENDING in CANCELLABLE_STATUSES

    def test_a_failed_download_is_retryable_but_not_active(self):
        assert "error" in RETRYABLE_STATUSES
        assert "error" not in ACTIVE_STATUSES

    def test_settled_downloads_are_neither(self):
        for status in ("complete", "cancelled"):
            assert status not in ACTIVE_STATUSES
            assert status not in RETRYABLE_STATUSES


class TestCountActiveDownloads:
    def test_counts_queued_running_and_pending_retries(self):
        progress = {
            "a": {"status": "queued"},
            "b": {"status": "in_progress"},
            "c": {"status": RETRY_PENDING},
            "d": {"status": "complete"},
            "e": {"status": "error"},
            "f": {"status": "cancelled"},
        }
        assert count_active_downloads(progress) == 3

    def test_empty_and_junk_inputs(self):
        assert count_active_downloads({}) == 0
        assert count_active_downloads(None) == 0
        assert count_active_downloads({"a": "not a dict"}) == 0


class TestBuildStatusSnapshot:
    def test_decorates_a_pending_retry_with_a_countdown(self):
        progress = {"dl-1": {"status": RETRY_PENDING, "retry_count": 2,
                             "retry_at": 1_000_120}}

        snapshot = build_status_snapshot(progress, now=1_000_000)

        assert snapshot["dl-1"]["retry_in"] == 120
        assert snapshot["dl-1"]["retry_max"] == MAX_AUTO_RETRIES

    def test_the_countdown_does_not_leak_back_into_live_state(self):
        """Only the response is decorated — the progress dict is the source of
        truth for every other reader (weekly packs, the wanted sweep)."""
        progress = {"dl-1": {"status": RETRY_PENDING, "retry_at": 1_000_120}}

        build_status_snapshot(progress, now=1_000_000)

        assert "retry_in" not in progress["dl-1"]
        assert "retry_max" not in progress["dl-1"]

    def test_other_downloads_pass_through_untouched(self):
        entry = {"status": "in_progress", "progress": 42}
        progress = {"dl-1": entry}

        snapshot = build_status_snapshot(progress)

        assert snapshot["dl-1"] is entry

    def test_a_lapsed_countdown_reads_as_zero_not_negative(self):
        progress = {"dl-1": {"status": RETRY_PENDING, "retry_at": 1_000_000}}

        snapshot = build_status_snapshot(progress, now=1_000_090)

        assert snapshot["dl-1"]["retry_in"] == 0

    def test_empty_and_junk_inputs(self):
        assert build_status_snapshot({}) == {}
        assert build_status_snapshot(None) == {}


def _api_function(name):
    """Top-level statements of an api.py function, without importing api."""
    with open(API_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    pytest.fail(f"{name} not found in api.py")


def _names_used(node):
    """Every identifier referenced anywhere under ``node``."""
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


class TestApiRoutesUseTheSharedPolicy:
    """api.py's download routes must delegate, not reimplement.

    api.py starts worker threads and a cloudscraper session at import time, so
    the suite can never exercise these routes directly. Keeping the real logic
    in core.download_utils (tested above) and asserting structurally that the
    routes call it is what makes that coverage mean anything — a route that
    inlines its own status list would silently drop 'retry_pending'.
    """

    def test_status_endpoint_delegates_to_build_status_snapshot(self):
        assert "build_status_snapshot" in _names_used(
            _api_function("download_status_all")
        )

    def test_summary_endpoint_delegates_to_count_active_downloads(self):
        assert "count_active_downloads" in _names_used(
            _api_function("download_summary")
        )

    def test_cancel_endpoint_uses_the_shared_status_set(self):
        assert "CANCELLABLE_STATUSES" in _names_used(_api_function("cancel_download"))

    def test_retry_endpoint_uses_the_shared_status_set(self):
        assert "RETRYABLE_STATUSES" in _names_used(_api_function("retry_download"))

    def test_retry_endpoint_restores_the_full_retry_budget(self):
        """A hand-driven retry is a fresh start; an exhausted counter left in
        place would veto every future automatic retry of this download."""
        source = ast.unparse(_api_function("retry_download"))
        assert "'retry_count'] = 0" in source

    def test_retry_endpoint_shares_the_field_reset(self):
        assert "reset_for_retry" in _names_used(_api_function("retry_download"))
