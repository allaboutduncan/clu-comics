"""Notification hooks on the in-process download path (api.py).

api.py cannot be imported in tests — it starts worker threads and builds a
cloudscraper session at import time — so this covers the two halves that can be
reached without it:

* ``download_notification_body`` lives in core.download_utils for exactly that
  reason, and is tested directly.
* The ordering guarantee that a *cancelled* download never notifies is a
  property of where the call sits in ``process_download``, so it is asserted
  against the parsed AST rather than by running the function.
"""

import ast
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
API_PATH = os.path.join(PROJECT_ROOT, "api.py")


class TestDownloadNotificationBody:
    def test_uses_the_destination_filename(self):
        from core.download_utils import download_notification_body

        body = download_notification_body("Batman 001.cbz")
        assert body.startswith("Batman 001.cbz")

    def test_includes_the_provider(self):
        from core.download_utils import download_notification_body

        body = download_notification_body("Batman 001.cbz", provider="Pixeldrain")
        assert "Source: Pixeldrain" in body

    def test_includes_the_error(self):
        from core.download_utils import download_notification_body

        body = download_notification_body("Batman 001.cbz", error="404 Not Found")
        assert "Error: 404 Not Found" in body

    def test_falls_back_to_the_resolved_path(self):
        """Browser-extension grabs carry no dest_filename."""
        from core.download_utils import download_notification_body

        body = download_notification_body(
            None, file_path=os.path.join("downloads", "temp", "Batman 001.cbz")
        )
        assert body.startswith("Batman 001.cbz")

    def test_falls_back_to_unknown_file(self):
        from core.download_utils import download_notification_body

        assert download_notification_body(None) == "Unknown file"

    def test_one_field_per_line(self):
        from core.download_utils import download_notification_body

        body = download_notification_body("a.cbz", provider="MEGA", error="boom")
        assert body.splitlines() == ["a.cbz", "Source: MEGA", "Error: boom"]

    def test_reports_the_automatic_retries_that_were_spent(self):
        """Distinguishes "a mirror hiccuped" from "CLU tried for twenty minutes
        and this one is dead" — which is the point of holding the push back."""
        from core.download_utils import download_notification_body

        body = download_notification_body("a.cbz", error="boom", attempts=3)
        assert body.splitlines()[-1] == "Gave up after 3 automatic retries"

    def test_a_single_retry_reads_naturally(self):
        from core.download_utils import download_notification_body

        body = download_notification_body("a.cbz", attempts=1)
        assert body.splitlines()[-1] == "Gave up after 1 automatic retry"

    def test_no_retry_line_when_none_were_spent(self):
        """A Cloudflare failure is reported immediately, with no retries."""
        from core.download_utils import download_notification_body

        assert download_notification_body("a.cbz", attempts=0) == "a.cbz"
        assert download_notification_body("a.cbz") == "a.cbz"

    def test_a_junk_attempt_count_is_ignored(self):
        from core.download_utils import download_notification_body

        assert download_notification_body("a.cbz", attempts=None) == "a.cbz"


def _process_download_body():
    """Top-level statements of api.process_download, without importing api."""
    with open(API_PATH, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "process_download":
            return node.body
    pytest.fail("process_download not found in api.py")


def _calls(node):
    """Every called function name reachable under ``node``."""
    names = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            names.append(child.func.id)
    return names


class TestFailureNotifyIsBehindTheCancelGuard:
    """A download the user cancelled must never report itself as failed.

    Aborting a transfer is how a cancel surfaces from most providers, so the
    failure path is reached for cancels too. ``set_error_status`` already
    downgrades those to 'cancelled'; the notification has to sit *after* the
    early return that follows it, or every cancel produces a "Download failed"
    push.
    """

    def test_guard_precedes_the_notification(self):
        body = _process_download_body()

        guard_idx = notify_idx = None
        for idx, stmt in enumerate(body):
            if (
                isinstance(stmt, ast.If)
                and "is_cancel_requested" in _calls(stmt.test)
                and any(isinstance(s, ast.Return) for s in stmt.body)
            ):
                guard_idx = idx
            if guard_idx is not None and "notify_async" in _calls(stmt):
                notify_idx = idx
                break

        assert guard_idx is not None, "cancel guard not found in process_download"
        assert notify_idx is not None, "failure notification not found after the guard"
        assert guard_idx < notify_idx

    def test_the_failure_notification_is_the_failed_event(self):
        body = _process_download_body()

        for stmt in reversed(body):
            if "notify_async" in _calls(stmt):
                names = [n.id for n in ast.walk(stmt) if isinstance(n, ast.Name)]
                assert "EVENT_DOWNLOAD_FAILED" in names
                return
        pytest.fail("no notify_async call at the top level of process_download")


def _top_level_index(body, predicate):
    """Index of the first top-level statement matching ``predicate``."""
    for idx, stmt in enumerate(body):
        if predicate(stmt):
            return idx
    return None


class TestAutoRetryDefersTheFailureNotification:
    """A transient failure must not push, and a cancel must not auto-retry.

    Both are properties of *where* ``_schedule_auto_retry`` sits in the terminal
    failure block, so they are asserted against the parsed AST for the same
    reason the cancel guard above is: api.py cannot be imported in tests.

    The required order is cancel guard -> auto-retry -> notification. Move the
    retry above the guard and every cancelled download gets re-queued; move it
    below the notification and a user gets a "Download failed" push each time a
    mirror hiccups, twenty minutes before CLU has actually given up.
    """

    def test_the_cancel_guard_precedes_the_auto_retry(self):
        body = _process_download_body()

        guard_idx = _top_level_index(
            body,
            lambda s: (
                isinstance(s, ast.If)
                and "is_cancel_requested" in _calls(s.test)
                and any(isinstance(x, ast.Return) for x in s.body)
            ),
        )
        retry_idx = _top_level_index(
            body, lambda s: "_schedule_auto_retry" in _calls(s)
        )

        assert guard_idx is not None, "cancel guard not found in process_download"
        assert retry_idx is not None, "_schedule_auto_retry not found in process_download"
        assert guard_idx < retry_idx

    def test_the_auto_retry_precedes_the_failure_notification(self):
        body = _process_download_body()

        retry_idx = _top_level_index(
            body, lambda s: "_schedule_auto_retry" in _calls(s)
        )
        notify_idx = _top_level_index(
            body,
            lambda s: "notify_async" in _calls(s) and "EVENT_DOWNLOAD_FAILED" in [
                n.id for n in ast.walk(s) if isinstance(n, ast.Name)
            ],
        )

        assert retry_idx is not None, "_schedule_auto_retry not found in process_download"
        assert notify_idx is not None, "failure notification not found"
        assert retry_idx < notify_idx

    def test_a_scheduled_retry_returns_before_notifying(self):
        """Scheduling has to short-circuit the rest of the block, or the push
        (and the weekly-pack 'failed' write) happen anyway."""
        body = _process_download_body()

        for stmt in body:
            if "_schedule_auto_retry" not in _calls(stmt):
                continue
            assert isinstance(stmt, ast.If), (
                "_schedule_auto_retry must gate an early return"
            )
            assert any(isinstance(x, ast.Return) for x in stmt.body)
            return
        pytest.fail("_schedule_auto_retry not found in process_download")
