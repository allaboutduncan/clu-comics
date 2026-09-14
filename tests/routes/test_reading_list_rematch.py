"""Tests for POST /api/reading-lists/<id>/rematch and the Metron import
field mapping.

Two things are covered here, and the second is why the first is needed.

The re-match route exists because a reading list stores the path it matched,
so tightening the matcher corrects nothing already imported. Sync only ever
covered GitHub sources, which left a Metron list with no route to a fix at all.

The matching loop itself now lives in ``core.reading_list_match`` -- the nightly
GetComics sweep re-matches tracked lists before deciding what is still wanted,
and the two callers must not drift -- so the auto-match writer is patched there,
not on ``routes.reading_lists``.

The Metron import tests call ``process_metron_import`` directly. Every other
test in tests/routes/test_reading_list_metron.py patches ``threading.Thread``,
so the worker never runs and the Metron -> match_file field mapping had no
coverage whatsoever -- which is how the volume/issue year confusion shipped.
"""
from unittest.mock import patch, MagicMock, ANY

import pytest


# ── /rematch ────────────────────────────────────────────────────────────────

class TestRematchRoute:

    @patch("routes.reading_lists.get_reading_list", return_value=None)
    def test_unknown_list_is_404(self, mock_get, client):
        resp = client.post("/api/reading-lists/999/rematch")
        assert resp.status_code == 404
        assert resp.get_json()["success"] is False

    @patch("routes.reading_lists.threading.Thread")
    @patch("routes.reading_lists.uuid.uuid4", return_value="rematch-task-1")
    @patch("routes.reading_lists.get_reading_list")
    def test_queues_a_background_job(self, mock_get, mock_uuid, mock_thread, client):
        mock_get.return_value = {"id": 1, "name": "Test", "entries": [{"id": 10}]}
        resp = client.post("/api/reading-lists/1/rematch")
        data = resp.get_json()
        assert data["success"] is True
        assert data["background"] is True
        assert data["task_id"] == "rematch-task-1"
        # Off-request: walking the library outlasts gunicorn's 120s timeout.
        mock_thread.assert_called_once()


class TestRematchWorker:

    def _entries(self):
        return [
            {"id": 1, "series": "Batman", "issue_number": "14",
             "volume": None, "year": 2025, "issue_year": 2026,
             "metron_id": None,
             "matched_file_path": "/data/DC/Batman/Batman 014 (1942).cbz",
             "manual_override_path": None},
            {"id": 2, "series": "Batman", "issue_number": "15",
             "volume": None, "year": 2025, "issue_year": 2026,
             "metron_id": None,
             "matched_file_path": None,
             "manual_override_path": "/data/DC/hand picked.cbz"},
        ]

    @patch("core.reading_list_match.set_reading_list_entry_auto_match")
    @patch("routes.reading_lists.get_reading_list")
    def test_clears_a_match_the_matcher_now_rejects(self, mock_get, mock_set):
        """The whole point: an entry whose stored match is no longer believed
        must go back to unmatched, not keep a path we would not pick today."""
        from routes.reading_lists import process_rematch, import_tasks
        mock_get.return_value = {"id": 1, "name": "Test", "entries": self._entries()}

        with patch("models.cbl.CBLLoader.match_file", return_value=None):
            import_tasks["t1"] = {}
            process_rematch("t1", 1, "{series_name} {issue_number}")

        mock_set.assert_called_once_with(1, None)
        assert import_tasks["t1"]["status"] == "complete"

    @patch("core.reading_list_match.set_reading_list_entry_auto_match")
    @patch("routes.reading_lists.get_reading_list")
    def test_never_touches_a_manual_override(self, mock_get, mock_set):
        from routes.reading_lists import process_rematch, import_tasks
        mock_get.return_value = {"id": 1, "name": "Test", "entries": self._entries()}

        with patch("models.cbl.CBLLoader.match_file", return_value=None):
            import_tasks["t2"] = {}
            process_rematch("t2", 1, "{series_name} {issue_number}")

        # Entry 2 is hand-mapped; only entry 1 may be written.
        written = [call.args[0] for call in mock_set.call_args_list]
        assert written == [1]
        assert "1 manual kept" in import_tasks["t2"]["message"]

    @patch("core.reading_list_match.set_reading_list_entry_auto_match")
    @patch("routes.reading_lists.get_reading_list")
    def test_writes_a_corrected_match(self, mock_get, mock_set):
        from routes.reading_lists import process_rematch, import_tasks
        mock_get.return_value = {"id": 1, "name": "Test", "entries": self._entries()}
        good = "/data/DC/Batman (2025)/Batman 014 (2026).cbz"

        with patch("models.cbl.CBLLoader.match_file", return_value=good):
            import_tasks["t3"] = {}
            process_rematch("t3", 1, "{series_name} {issue_number}")

        mock_set.assert_called_once_with(1, good)

    @patch("core.reading_list_match.set_reading_list_entry_auto_match")
    @patch("routes.reading_lists.get_reading_list")
    def test_unchanged_match_is_not_rewritten(self, mock_get, mock_set):
        from routes.reading_lists import process_rematch, import_tasks
        entries = self._entries()
        same = entries[0]["matched_file_path"]
        mock_get.return_value = {"id": 1, "name": "Test", "entries": entries}

        with patch("models.cbl.CBLLoader.match_file", return_value=same):
            import_tasks["t4"] = {}
            process_rematch("t4", 1, "{series_name} {issue_number}")

        mock_set.assert_not_called()

    @patch("core.reading_list_match.set_reading_list_entry_auto_match")
    @patch("routes.reading_lists.get_reading_list")
    def test_passes_the_stored_year_hints_to_the_matcher(self, mock_get, mock_set):
        from routes.reading_lists import process_rematch, import_tasks
        mock_get.return_value = {"id": 1, "name": "Test", "entries": self._entries()[:1]}

        with patch("models.cbl.CBLLoader.match_file", return_value=None) as match:
            import_tasks["t5"] = {}
            process_rematch("t5", 1, "{series_name} {issue_number}")

        kwargs = match.call_args.kwargs
        assert kwargs["volume_year"] == 2025
        assert 2026 in kwargs["issue_years"]

    @patch("routes.reading_lists.get_reading_list", return_value=None)
    def test_missing_list_is_reported_not_raised(self, mock_get):
        from routes.reading_lists import process_rematch, import_tasks
        import_tasks["t6"] = {}
        process_rematch("t6", 999, "{series_name} {issue_number}")
        assert import_tasks["t6"]["status"] == "error"


# ── Metron import field mapping ─────────────────────────────────────────────

class TestMetronImportFieldMapping:
    """Closes the gap that let the bug ship: these run the worker for real."""

    def _items(self, cover_date="2026-12-01", store_date="2026-10-07"):
        return [{
            "order": 1,
            "issue": {
                "id": 98765,
                "number": "14",
                "cover_date": cover_date,
                "store_date": store_date,
                # volume is an ORDINAL, year_began the volume start year.
                "series": {"id": 1, "name": "Batman", "volume": 4,
                           "year_began": 2025},
            },
        }]

    def _run_import(self, added, items=None):
        from routes.reading_lists import process_metron_import, import_tasks
        with patch("routes.reading_lists.fetch_reading_list_detail",
                   return_value={"name": "Bad Seeds"}), \
             patch("routes.reading_lists.fetch_reading_list_items",
                   return_value=items if items is not None else self._items()), \
             patch("routes.reading_lists.create_reading_list", return_value=7), \
             patch("routes.reading_lists.update_reading_list_description"), \
             patch("routes.reading_lists.update_reading_list_source_version"), \
             patch("routes.reading_lists.add_reading_list_entry",
                   side_effect=lambda lid, data: added.append(data) or 1), \
             patch("models.cbl.CBLLoader.prefetch_metron_ids"), \
             patch("models.cbl.CBLLoader.match_file", return_value=None) as match:
            import_tasks["m1"] = {}
            process_metron_import("m1", MagicMock(), 42,
                                  "{series_name} {issue_number}")
        return match

    def test_issue_and_volume_year_are_kept_apart(self):
        added = []
        match = self._run_import(added)
        kwargs = match.call_args.kwargs
        assert kwargs["issue_years"] == {2026}
        assert kwargs["volume_year"] == 2025

    def test_store_and_cover_year_are_both_accepted_when_they_differ(self):
        """An issue shipping in November under a January cover date is
        honestly labelled either way, so a file carrying either year must
        match. Taking only one of the two would reject the other."""
        added = []
        match = self._run_import(
            added,
            items=self._items(cover_date="2026-01-01", store_date="2025-11-19"),
        )
        assert match.call_args.kwargs["issue_years"] == {2025, 2026}

    def test_ordinal_volume_never_reaches_a_year_argument(self):
        added = []
        match = self._run_import(added)
        kwargs = match.call_args.kwargs
        # series.volume is 4; it must not be mistaken for a year anywhere.
        assert kwargs["volume_year"] != 4
        assert 4 not in kwargs["issue_years"]

    def test_metron_issue_id_is_passed_and_stored(self):
        added = []
        match = self._run_import(added)
        assert match.call_args.kwargs["metron_id"] == 98765
        assert added[0]["metron_id"] == 98765

    def test_entry_records_both_years_separately(self):
        added = []
        self._run_import(added)
        entry = added[0]
        assert entry["year"] == 2025          # the series/volume start year
        assert entry["issue_year"] == 2026    # when this issue came out
        assert entry["series"] == "Batman"
        assert entry["issue_number"] == "14"
