"""Tests for weekly-pack history reconciliation (issue #467).

``weekly_packs_history.status`` is only a mirror of the in-memory
``api.download_progress``. Restarts, cancellations and cleared entries used to
freeze rows at 'queued'/'downloading' forever — which also blocked re-download,
because ``is_weekly_pack_downloaded()`` counts those as done.
"""
import pytest
from unittest.mock import patch

from core.download_utils import (
    apply_live_weekly_pack_status,
    reconcile_weekly_pack_history,
    _live_download_progress,
)


def _seed(pack_date, publisher, status, download_id=None):
    from core.database import log_weekly_pack_download

    log_weekly_pack_download(
        pack_date, publisher, "JPG", "https://example.com/pack.zip", status,
        download_id=download_id,
    )


def _status(pack_date, publisher="DC"):
    from core.database import get_weekly_packs_history

    row = next(
        h for h in get_weekly_packs_history(limit=100)
        if h["pack_date"] == pack_date and h["publisher"] == publisher
    )
    return row["status"]


class TestLiveStatusMapping:

    @pytest.mark.parametrize("live_status,expected", [
        ("in_progress", "downloading"),
        ("complete", "completed"),
        ("error", "failed"),
        ("cancelled", "cancelled"),
    ])
    def test_maps_live_state_onto_row(self, db_connection, live_status, expected):
        _seed("2026.01.07", "DC", "queued", download_id="dl-1")

        changed = reconcile_weekly_pack_history(
            progress={"dl-1": {"status": live_status}}
        )

        assert changed == 1
        assert _status("2026.01.07") == expected

    def test_still_queued_is_left_alone(self, db_connection):
        _seed("2026.01.07", "DC", "queued", download_id="dl-1")

        assert reconcile_weekly_pack_history(progress={"dl-1": {"status": "queued"}}) == 0
        assert _status("2026.01.07") == "queued"

    def test_already_downloading_is_not_rewritten(self, db_connection):
        """The steady state must be write-free — this runs on every UI poll."""
        _seed("2026.01.07", "DC", "downloading", download_id="dl-1")

        assert reconcile_weekly_pack_history(
            progress={"dl-1": {"status": "in_progress"}}
        ) == 0

    def test_unknown_live_status_is_left_alone(self, db_connection):
        _seed("2026.01.07", "DC", "queued", download_id="dl-1")

        assert reconcile_weekly_pack_history(progress={"dl-1": {"status": "???"}}) == 0
        assert _status("2026.01.07") == "queued"


class TestOrphanedRows:

    def test_missing_live_entry_becomes_interrupted(self, db_connection):
        """Restart, /clear_downloads or /dismiss_download drops the entry."""
        _seed("2026.01.07", "DC", "downloading", download_id="dl-gone")

        assert reconcile_weekly_pack_history(progress={}) == 1
        assert _status("2026.01.07") == "interrupted"

    def test_legacy_row_without_id_heals_once_stale(self, db_connection):
        """Rows written before download_id tracking existed."""
        _seed("2026.01.07", "DC", "queued")

        assert reconcile_weekly_pack_history(progress={}, stale_after_seconds=0) == 1
        assert _status("2026.01.07") == "interrupted"

    def test_fresh_row_without_id_is_protected(self, db_connection):
        """api.py's status writes carry no download_id, so a young id-less row
        may belong to a download that is actually running."""
        _seed("2026.01.07", "DC", "downloading")

        assert reconcile_weekly_pack_history(progress={}) == 0
        assert _status("2026.01.07") == "downloading"

    def test_interrupted_row_is_eligible_for_redownload(self, db_connection):
        from core.database import is_weekly_pack_downloaded

        _seed("2026.01.07", "DC", "downloading", download_id="dl-gone")
        reconcile_weekly_pack_history(progress={})

        assert is_weekly_pack_downloaded("2026.01.07", "DC", "JPG") is False


class TestNoOpPaths:

    def test_no_stale_rows_means_no_progress_lookup(self, db_connection):
        _seed("2026.01.07", "DC", "completed", download_id="dl-1")

        with patch("core.download_utils._live_download_progress") as mock_live:
            assert reconcile_weekly_pack_history() == 0
        mock_live.assert_not_called()

    def test_no_progress_source_writes_nothing(self, db_connection):
        """The monitor process (which never imports api) must not clobber rows."""
        _seed("2026.01.07", "DC", "downloading", download_id="dl-1")

        with patch("core.download_utils._live_download_progress", return_value=None):
            assert reconcile_weekly_pack_history() == 0
        assert _status("2026.01.07") == "downloading"

    def test_non_dict_progress_source_is_rejected(self):
        from unittest.mock import MagicMock

        with patch.dict("sys.modules", {"api": MagicMock()}):
            assert _live_download_progress() is None

    def test_missing_api_module_is_safe(self):
        import sys

        saved = sys.modules.pop("api", None)
        try:
            assert _live_download_progress() is None
        finally:
            if saved is not None:
                sys.modules["api"] = saved


class TestApplyLiveStatus:

    def test_overlays_status_and_progress(self):
        history = [{"pack_date": "2026.01.07", "status": "queued", "download_id": "dl-1"}]

        apply_live_weekly_pack_status(
            history, progress={"dl-1": {"status": "in_progress", "progress": 42}}
        )

        assert history[0]["status"] == "downloading"
        assert history[0]["progress"] == 42

    def test_rows_without_live_entry_are_untouched(self):
        history = [{"pack_date": "2026.01.07", "status": "completed", "download_id": None}]

        apply_live_weekly_pack_status(history, progress={})

        assert history[0]["status"] == "completed"
        assert "progress" not in history[0]
