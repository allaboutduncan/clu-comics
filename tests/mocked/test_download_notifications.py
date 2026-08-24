"""Notification hooks on the Usenet and DC++ terminal-status choke points.

Downloads settle in three independent places; these cover the two client-backed
pollers. The in-process HTTP path is covered by tests/unit/test_api_notify.py.
"""

from unittest.mock import MagicMock, patch

import pytest

import models.dcpp as dc
import models.usenet as un


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Isolate module job dicts and keep DC++ ledger writes off a real DB."""
    un.usenet_downloads.clear()
    dc.dcpp_downloads.clear()
    monkeypatch.setattr(dc, "_persist_new", MagicMock())
    monkeypatch.setattr(dc, "_persist_update", MagicMock())
    monkeypatch.setattr(dc, "_persist_delete", MagicMock(return_value=True))
    yield
    un.usenet_downloads.clear()
    dc.dcpp_downloads.clear()


@pytest.fixture
def notify():
    """Patch notify_async where the hooks import it: core.notifications."""
    with patch("core.notifications.notify_async") as mock:
        yield mock


def _events(mock):
    """(event_id, title) for each dispatched notification."""
    return [(call.args[0], call.args[1]) for call in mock.call_args_list]


class TestUsenetNotifications:

    def test_complete_notifies_once(self, notify):
        from core.notifications import EVENT_DOWNLOAD_COMPLETE

        un.usenet_downloads["d1"] = {"filename": "Batman 001.cbz", "status": "downloading"}
        un._set_status("d1", "complete", percent=100)

        assert _events(notify) == [(EVENT_DOWNLOAD_COMPLETE, "Download complete")]
        assert "Batman 001.cbz" in notify.call_args.args[2]
        assert "Usenet" in notify.call_args.args[2]

    def test_failed_notifies_with_the_error(self, notify):
        from core.notifications import EVENT_DOWNLOAD_FAILED

        un.usenet_downloads["d1"] = {"filename": "Batman 001.cbz", "status": "downloading"}
        un._set_status("d1", "failed", error="Download failed at client")

        assert _events(notify) == [(EVENT_DOWNLOAD_FAILED, "Download failed")]
        assert "Download failed at client" in notify.call_args.args[2]

    def test_complete_no_move_says_it_was_not_imported(self, notify):
        from core.notifications import EVENT_DOWNLOAD_COMPLETE

        un.usenet_downloads["d1"] = {"filename": "Batman 001.cbz", "status": "downloading"}
        un._set_status("d1", "complete_no_move", percent=100)

        assert _events(notify) == [(EVENT_DOWNLOAD_COMPLETE, "Download complete")]
        assert "not been imported" in notify.call_args.args[2]

    def test_untracked_job_still_notifies_without_a_filename(self, notify):
        """A dropped job must not crash the poller mid-loop."""
        un._set_status("gone", "complete", percent=100)

        assert len(notify.call_args_list) == 1
        assert "Unknown file" in notify.call_args.args[2]

    def test_notification_failure_never_reaches_the_poller(self):
        """_poll_loop has no per-round guard: a raise here kills the thread."""
        with patch(
            "core.notifications.notify_async", side_effect=RuntimeError("boom")
        ):
            un.usenet_downloads["d1"] = {"filename": "x.cbz", "status": "downloading"}
            un._set_status("d1", "complete", percent=100)  # must not raise

        assert un.usenet_downloads["d1"]["status"] == "complete"


class TestDcppNotifications:

    def test_complete_notifies_once(self, notify):
        from core.notifications import EVENT_DOWNLOAD_COMPLETE

        dc.dcpp_downloads["d1"] = {
            "filename": "Batman 001.cbz", "status": "downloading", "percent": 50,
        }
        dc._set_status("d1", "complete", percent=100)

        assert _events(notify) == [(EVENT_DOWNLOAD_COMPLETE, "Download complete")]
        assert "Batman 001.cbz" in notify.call_args.args[2]
        assert "DC++" in notify.call_args.args[2]

    def test_failed_notifies_with_the_error(self, notify):
        from core.notifications import EVENT_DOWNLOAD_FAILED

        dc.dcpp_downloads["d1"] = {
            "filename": "Batman 001.cbz", "status": "downloading", "percent": 50,
        }
        dc._set_status("d1", "failed", error="Download failed at client")

        assert _events(notify) == [(EVENT_DOWNLOAD_FAILED, "Download failed")]
        assert "Download failed at client" in notify.call_args.args[2]

    def test_complete_no_move_says_it_was_not_imported(self, notify):
        dc.dcpp_downloads["d1"] = {
            "filename": "Batman 001.cbz", "status": "downloading", "percent": 50,
        }
        dc._set_status("d1", "complete_no_move", percent=100)

        assert "not been imported" in notify.call_args.args[2]

    def test_untracked_job_does_not_notify(self, notify):
        """DC++ returns early for an unknown id, before the hook."""
        dc._set_status("gone", "complete", percent=100)
        notify.assert_not_called()

    def test_notification_failure_never_reaches_the_poller(self):
        with patch(
            "core.notifications.notify_async", side_effect=RuntimeError("boom")
        ):
            dc.dcpp_downloads["d1"] = {
                "filename": "x.cbz", "status": "downloading", "percent": 50,
            }
            dc._set_status("d1", "complete", percent=100)  # must not raise

        assert dc.dcpp_downloads["d1"]["status"] == "complete"

    def test_ledger_delete_still_runs_on_completion(self, notify):
        dc.dcpp_downloads["d1"] = {
            "filename": "Batman 001.cbz", "status": "downloading", "percent": 50,
        }
        dc._set_status("d1", "complete", percent=100)

        dc._persist_delete.assert_called_once_with("d1")
