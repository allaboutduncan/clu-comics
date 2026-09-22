"""The /status page's Clear buttons have to cover every store behind the table.

Three independent stores feed that one table: api.py's ``download_progress``
(the in-process HTTP downloads), ``models.usenet.usenet_downloads`` and
``models.dcpp.dcpp_downloads``. ``/clear_downloads`` only ever knew about the
first, so a finished Usenet or DC++ row survived the button -- DC++ had to be
dismissed a row at a time, and Usenet had no dismiss control at all. Every row
left behind still read "Complete" or "Complete (import pending)", which made
the button look broken.

Text assertions, like test_status_page_cloudflare.py: the wiring lives in
status.html's own JavaScript and there is no Python to exercise.
"""

from pathlib import Path

import pytest

STATUS_HTML = Path(__file__).resolve().parents[2] / "templates" / "status.html"


@pytest.fixture(scope="module")
def status_source():
    return STATUS_HTML.read_text(encoding="utf-8")


class TestClearButtonsCoverClientStores:
    def test_both_buttons_go_through_one_helper(self, status_source):
        assert "function clearDownloads(" in status_source
        assert "clearDownloads('/clear_downloads', 'completed'" in status_source
        assert "clearDownloads('/clear_failed_downloads', 'failed'" in status_source

    def test_helper_also_clears_the_managed_clients(self, status_source):
        assert "/api/download-clients/downloads/clear" in status_source

    def test_bucket_is_sent_as_json(self, status_source):
        # The route rejects an unknown bucket with a 400, so the two names
        # here are a contract with core.download_utils.CLIENT_CLEAR_BUCKETS.
        assert "JSON.stringify({ bucket })" in status_source

    def test_one_store_failing_does_not_hide_the_other(self, status_source):
        # Promise.all over two already-caught promises: an all-or-nothing
        # chain would report a total failure while rows had in fact gone.
        assert "Promise.all([direct, clients])" in status_source
        assert "Some downloads could not be cleared" in status_source

    def test_confirmation_names_the_import_pending_case(self, status_source):
        # Clearing one of these drops CLU's only record that the file finished
        # at the client but never reached WATCH, so the modal says so.
        assert 'Complete (import pending)' in status_source
        assert 'marked "Complete (import pending)". Continue?' in status_source


class TestBucketNamesMatchTheServer:
    def test_buckets_are_defined_once(self):
        from core.download_utils import (
            CLIENT_CLEAR_BUCKETS,
            CLIENT_COMPLETED_STATUSES,
            CLIENT_FAILED_STATUSES,
        )

        assert set(CLIENT_CLEAR_BUCKETS) == {"completed", "failed"}
        assert CLIENT_CLEAR_BUCKETS["completed"] is CLIENT_COMPLETED_STATUSES
        assert CLIENT_CLEAR_BUCKETS["failed"] is CLIENT_FAILED_STATUSES

    def test_import_pending_clears_as_completed_not_failed(self):
        from core.download_utils import (
            CLIENT_COMPLETED_STATUSES,
            CLIENT_FAILED_STATUSES,
        )

        # The status the page renders as "Complete (import pending)".
        assert "complete_no_move" in CLIENT_COMPLETED_STATUSES
        assert "complete_no_move" not in CLIENT_FAILED_STATUSES
        assert CLIENT_COMPLETED_STATUSES.isdisjoint(CLIENT_FAILED_STATUSES)
