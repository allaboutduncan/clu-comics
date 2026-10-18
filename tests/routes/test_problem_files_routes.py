"""Routes for the Problem Files page.

Patch targets follow the module they live in: `routes/problem_files.py` imports
everything inside its views, so the symbols are patched at their real home
(`core.problem_files.*`), not on the blueprint.
"""

from unittest.mock import patch

import pytest


def _row(path="/data/Comics/Bad.cbz", source="thumbnail", **kw):
    row = {
        "id": 1,
        "path": path,
        "source": source,
        "error_class": "BadZipFile",
        "error_message": "Bad CRC-32 for file 'p0.jpg'",
        "first_seen": "2026-09-14 12:54:58",
        "last_seen": "2026-09-14 12:54:58",
        "occurrences": 1,
        "file_mtime": 1.0,
        "dismissed_at": None,
        "filename": "Bad.cbz",
        "folder": "/data/Comics",
        "source_label": "Thumbnail",
        "dismissed": False,
        "reachable": True,
        "can_retry": True,
        "classification": {
            "cause": "A page inside the archive is physically damaged.",
            "advice": "Replace the file.",
            "action": "replace",
            "repairable": False,
            "healthy_file": False,
        },
    }
    row.update(kw)
    return row


class TestPage:
    @patch("core.problem_files.count_problems", return_value={"open": 0, "dismissed": 0, "by_source": {}})
    @patch("core.problem_files.list_problems", return_value=[])
    def test_page_renders(self, _list, _counts, client):
        resp = client.get("/problem-files")
        assert resp.status_code == 200
        assert b"Problem Files" in resp.data

    @patch("core.problem_files.count_problems", return_value={"open": 0, "dismissed": 0, "by_source": {}})
    @patch("core.problem_files.list_problems", return_value=[])
    def test_page_states_it_does_not_scan(self, _list, _counts, client):
        """"Report only" is a promise; an empty page must not read as "your
        library is clean". With the health scan opt-in, the page still says an
        empty list means nothing has *failed*."""
        resp = client.get("/problem-files")
        assert b"nothing has failed" in resp.data


class TestList:
    @patch("core.problem_files.count_problems", return_value={"open": 1, "dismissed": 0, "by_source": {"thumbnail": 1}})
    @patch("core.problem_files.list_problems", return_value=[_row()])
    def test_shape(self, _list, _counts, client):
        resp = client.get("/api/problem-files")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["problems"][0]["filename"] == "Bad.cbz"
        assert data["counts"]["open"] == 1

    @patch("core.problem_files.count_problems", return_value={})
    @patch("core.problem_files.list_problems", return_value=[])
    def test_filters_are_passed_through(self, mock_list, _counts, client):
        client.get("/api/problem-files?source=rebuild&q=Wizard&include_dismissed=1")
        kwargs = mock_list.call_args.kwargs
        assert kwargs["source"] == "rebuild"
        assert kwargs["query"] == "Wizard"
        assert kwargs["include_dismissed"] is True

    def test_unknown_source_is_rejected(self, client):
        resp = client.get("/api/problem-files?source=nonsense")
        assert resp.status_code == 400

    def test_bad_paging_is_rejected(self, client):
        resp = client.get("/api/problem-files?limit=abc")
        assert resp.status_code == 400


class TestSearchContext:
    """Rows carry the series/issue/year a source search needs.

    Parsed server-side through cbz_ops.rename.parse_comic_filename -- the same
    parser the rest of the app renames and matches with -- so "find a
    replacement" cannot drift from it.
    """

    @patch("core.problem_files.count_problems", return_value={})
    @patch("core.problem_files.list_problems",
           return_value=[_row(path="/data/Comics/Wizard Magazine 151 (2004).cbz",
                              filename="Wizard Magazine 151 (2004).cbz")])
    def test_series_issue_and_year_are_parsed(self, _list, _counts, client):
        resp = client.get("/api/problem-files")
        search = resp.get_json()["problems"][0]["search"]
        assert search["series"] == "Wizard Magazine"
        assert search["issue"] == "151"
        assert search["year"] == 2004
        assert search["query"] == "Wizard Magazine 151"

    @patch("core.problem_files.count_problems", return_value={})
    @patch("core.problem_files.list_problems",
           return_value=[_row(path="/data/Comics/qqqq.cbz", filename="qqqq.cbz")])
    def test_an_unparseable_name_still_gets_a_query(self, _list, _counts, client):
        """A search box the user can edit beats no button at all."""
        resp = client.get("/api/problem-files")
        search = resp.get_json()["problems"][0]["search"]
        assert search["query"]

    @patch("core.problem_files.count_problems", return_value={})
    @patch("core.problem_files.list_problems",
           return_value=[_row(filename="Bad.cbz")])
    def test_a_parser_failure_does_not_break_the_listing(
        self, _list, _counts, client
    ):
        """The page must still render if the parser raises."""
        with patch("cbz_ops.rename.parse_comic_filename", side_effect=RuntimeError("boom")):
            resp = client.get("/api/problem-files")
        assert resp.status_code == 200
        assert resp.get_json()["problems"][0]["search"]["query"]


class TestRetry:
    @patch("core.problem_files.get_problem", return_value=None)
    @patch("core.problem_files.retry_problem", return_value=(True, "Thumbnail rebuilt"))
    def test_a_fixed_file_reports_fixed(self, mock_retry, _get, client):
        resp = client.post(
            "/api/problem-files/retry",
            json={"path": "/data/Comics/Bad.cbz", "source": "thumbnail"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["fixed"] is True
        mock_retry.assert_called_once_with("/data/Comics/Bad.cbz", "thumbnail")

    @patch("core.problem_files.get_problem", return_value=_row(occurrences=2))
    @patch("core.problem_files.retry_problem", return_value=(False, "Still failing"))
    def test_a_still_broken_file_returns_the_current_row(self, _retry, _get, client):
        """The row's state is the answer, never a guess from the return value:
        the retried operation is what records or clears it."""
        resp = client.post(
            "/api/problem-files/retry",
            json={"path": "/data/Comics/Bad.cbz", "source": "thumbnail"},
        )
        data = resp.get_json()
        assert data["fixed"] is False
        assert data["problem"]["occurrences"] == 2

    def test_missing_path_is_rejected(self, client):
        resp = client.post("/api/problem-files/retry", json={"source": "thumbnail"})
        assert resp.status_code == 400

    def test_missing_source_is_rejected(self, client):
        resp = client.post("/api/problem-files/retry", json={"path": "/data/x.cbz"})
        assert resp.status_code == 400

    def test_unknown_source_is_rejected(self, client):
        resp = client.post(
            "/api/problem-files/retry", json={"path": "/data/x.cbz", "source": "wat"}
        )
        assert resp.status_code == 400


class TestDismiss:
    @patch("core.problem_files.set_dismissed", return_value=True)
    def test_dismiss(self, mock_set, client):
        resp = client.post(
            "/api/problem-files/dismiss",
            json={"path": "/data/x.cbz", "source": "thumbnail"},
        )
        assert resp.status_code == 200
        assert mock_set.call_args.kwargs["dismissed"] is True

    @patch("core.problem_files.set_dismissed", return_value=True)
    def test_undismiss(self, mock_set, client):
        client.post(
            "/api/problem-files/dismiss",
            json={"path": "/data/x.cbz", "source": "thumbnail", "dismissed": False},
        )
        assert mock_set.call_args.kwargs["dismissed"] is False

    @patch("core.problem_files.set_dismissed", return_value=False)
    def test_missing_entry_is_a_404(self, _set, client):
        resp = client.post(
            "/api/problem-files/dismiss",
            json={"path": "/data/x.cbz", "source": "thumbnail"},
        )
        assert resp.status_code == 404


class TestRemove:
    @patch("core.problem_files.clear_problem", return_value=2)
    def test_remove_without_source_clears_every_entry(self, mock_clear, client):
        resp = client.post("/api/problem-files/remove", json={"path": "/data/x.cbz"})
        assert resp.status_code == 200
        assert resp.get_json()["removed"] == 2
        mock_clear.assert_called_once_with("/data/x.cbz", None)


class TestDelete:
    @patch("core.problem_files.has_problem", return_value=True)
    @patch("core.problem_files.clear_problem", return_value=1)
    @patch("helpers.trash.move_to_trash", return_value={"trashed": True, "path": "/trash/x"})
    @patch("helpers.library.is_critical_path", return_value=False)
    @patch("os.path.exists", return_value=True)
    def test_delete_trashes_and_clears(
        self, _exists, _critical, mock_trash, mock_clear, _has, client
    ):
        resp = client.post("/api/problem-files/delete", json={"path": "/data/x.cbz"})
        assert resp.status_code == 200
        assert resp.get_json()["trashed"] is True
        mock_trash.assert_called_once_with("/data/x.cbz")
        mock_clear.assert_called_once_with("/data/x.cbz")

    @patch("core.problem_files.has_problem", return_value=True)
    @patch("helpers.library.is_critical_path", return_value=True)
    @patch("os.path.exists", return_value=True)
    def test_a_protected_path_is_refused(self, _exists, _critical, _has, client):
        resp = client.post("/api/problem-files/delete", json={"path": "/data"})
        assert resp.status_code == 403

    @patch("core.problem_files.has_problem", return_value=True)
    @patch("core.problem_files.clear_problem", return_value=1)
    @patch("os.path.exists", return_value=False)
    def test_an_already_missing_file_just_clears_the_entry(
        self, _exists, mock_clear, _has, client
    ):
        resp = client.post("/api/problem-files/delete", json={"path": "/data/gone.cbz"})
        assert resp.status_code == 200
        assert resp.get_json()["missing"] is True
        mock_clear.assert_called_once_with("/data/gone.cbz")

    def test_missing_path_is_rejected(self, client):
        resp = client.post("/api/problem-files/delete", json={})
        assert resp.status_code == 400

    @patch("core.problem_files.has_problem", return_value=False)
    def test_an_unlisted_path_cannot_be_deleted(self, _has, client):
        """is_critical_path guards WATCH/TARGET/trash but not /config or
        /cache, where the database lives. This endpoint must only ever act on
        rows the page is displaying."""
        resp = client.post("/api/problem-files/delete", json={"path": "/config"})
        assert resp.status_code == 404


class TestReplacements:
    """Claiming, applying and dismissing a replacement download."""

    @patch("core.problem_replacements.claim_replacement", return_value=True)
    def test_claim_passes_the_parsed_context_through(self, mock_claim, client):
        resp = client.post("/api/problem-files/replace", json={
            "path": "/data/DC/Tales of the Unexpected 008 (2007).cbz",
            "series": "Tales of the Unexpected",
            "issue": "8",
            "query": "Tales of the Unexpected 8",
            "download_source": "getcomics",
        })
        assert resp.status_code == 200
        kwargs = mock_claim.call_args.kwargs
        assert kwargs["series"] == "Tales of the Unexpected"
        assert kwargs["issue"] == "8"
        assert mock_claim.call_args.args[0].endswith("008 (2007).cbz")

    def test_claim_requires_a_path(self, client):
        resp = client.post("/api/problem-files/replace", json={"series": "X"})
        assert resp.status_code == 400

    @patch("core.problem_replacements.claim_replacement", return_value=False)
    def test_a_failed_claim_is_reported(self, _claim, client):
        resp = client.post("/api/problem-files/replace", json={"path": "/data/x.cbz"})
        assert resp.status_code == 500

    @patch("core.problem_replacements.list_replacements", return_value=[])
    @patch("core.problem_replacements.apply_pending_for_app",
           return_value=[{"target_path": "/data/x.cbz", "status": "applied",
                          "filename": "x.cbz"}])
    def test_apply_returns_what_changed(self, _apply, _list, client):
        resp = client.post("/api/problem-files/replacements/apply")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["changed"][0]["status"] == "applied"

    @patch("core.problem_replacements.acknowledge", return_value=True)
    def test_ack_dismisses(self, mock_ack, client):
        resp = client.post("/api/problem-files/replacements/ack",
                           json={"path": "/data/x.cbz"})
        assert resp.status_code == 200
        assert resp.get_json()["acknowledged"] is True
        mock_ack.assert_called_once_with("/data/x.cbz")

    @patch("core.problem_replacements.cancel", return_value=True)
    def test_ack_with_cancel_drops_the_record(self, mock_cancel, client):
        resp = client.post("/api/problem-files/replacements/ack",
                           json={"path": "/data/x.cbz", "cancel": True})
        assert resp.get_json()["cancelled"] is True
        mock_cancel.assert_called_once_with("/data/x.cbz")

    @patch("core.problem_files.count_problems", return_value={})
    @patch("core.problem_replacements.list_replacements",
           return_value=[{"target_path": "/data/Comics/Bad.cbz", "status": "pending",
                          "filename": "Bad.cbz"}])
    @patch("core.problem_files.list_problems", return_value=[_row()])
    def test_the_listing_carries_replacement_state(
        self, _list, _reps, _counts, client
    ):
        """The row needs it for its badge, and the top level needs it for the
        banner -- an applied swap deletes the row, so the banner is the only
        place the result can still be reported."""
        resp = client.get("/api/problem-files")
        data = resp.get_json()
        assert data["problems"][0]["replacement"]["status"] == "pending"
        assert data["replacements"][0]["filename"] == "Bad.cbz"


class TestReadFailureIsReported:
    @patch("core.problem_files.count_problems", return_value={"open": 56})
    @patch("core.problem_files.list_problems", return_value=None)
    def test_a_read_failure_is_a_500_not_an_empty_page(self, _list, _counts, client):
        """The page must never render "Nothing has failed" over a database it
        could not read."""
        resp = client.get("/api/problem-files")
        assert resp.status_code == 500
        assert resp.get_json()["success"] is False

    @patch("core.problem_replacements.list_replacements", return_value=[])
    @patch("core.problem_files.count_problems",
           return_value={"open": 0, "dismissed": 0, "by_source": {}})
    @patch("core.problem_files.list_problems", return_value=[])
    def test_a_genuinely_empty_list_is_still_a_success(
        self, _list, _counts, _reps, client
    ):
        resp = client.get("/api/problem-files")
        assert resp.status_code == 200
        assert resp.get_json()["problems"] == []


class TestSeriesJsonScan:
    def test_starts_a_background_scan(self, client):
        import threading

        from routes import problem_files as pf

        ran = threading.Event()

        def fake_scan(op_id=None):
            ran.set()
            return {}

        with patch("core.series_json_check.scan_library", side_effect=fake_scan):
            resp = client.post("/api/problem-files/scan-series-json")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["success"] is True and data["op_id"]
            assert ran.wait(5)
        # The lock is released once the worker finishes.
        for _ in range(100):
            if not pf._scan_lock.locked():
                break
            threading.Event().wait(0.05)
        assert not pf._scan_lock.locked()

    def test_second_scan_while_running_is_409(self, client):
        from routes import problem_files as pf

        assert pf._scan_lock.acquire(blocking=False)
        try:
            resp = client.post("/api/problem-files/scan-series-json")
            assert resp.status_code == 409
        finally:
            pf._scan_lock.release()

    def test_series_json_is_a_valid_source_filter(self, client):
        with patch("core.problem_files.list_problems", return_value=[]), \
             patch("core.problem_files.count_problems",
                   return_value={"open": 0, "dismissed": 0, "by_source": {}}):
            resp = client.get("/api/problem-files?source=series-json")
        assert resp.status_code == 200

    def test_delete_refuses_a_series_json_row(self, client):
        with patch("core.problem_files.has_problem", return_value=True), \
             patch("helpers.trash.move_to_trash") as trash:
            resp = client.post("/api/problem-files/delete",
                               json={"path": "/data/Batman/series.json"})
        assert resp.status_code == 400
        trash.assert_not_called()


class TestRefreshAllSeriesJson:
    """Bulk Refresh for series.json rows (core.series_json_check.refresh_all)."""

    URL = "/api/problem-files/refresh-series-json-all"

    def test_it_is_owner_only_by_prefix(self):
        from core.auth import _OWNER_PREFIXES

        assert self.URL.startswith(_OWNER_PREFIXES)
        assert (self.URL + "/result").startswith(_OWNER_PREFIXES)

    def test_nothing_to_refresh_is_a_400(self, client):
        with patch("core.series_json_check.count_open_series_json", return_value=0):
            resp = client.post(self.URL)
        assert resp.status_code == 400

    def test_unknown_provider_is_a_400(self, client):
        resp = client.post(self.URL, json={"provider": "nonsense"})
        assert resp.status_code == 400

    def test_busy_scan_is_a_409_and_nothing_starts(self, client):
        from routes import problem_files as pf

        assert pf._scan_lock.acquire(blocking=False)
        try:
            with patch("core.series_json_check.count_open_series_json", return_value=3), \
                 patch("core.series_json_check.refresh_all") as run:
                resp = client.post(self.URL)
                run.assert_not_called()
        finally:
            pf._scan_lock.release()
        assert resp.status_code == 409

    def test_starts_in_the_background_and_releases_the_lock(self, client):
        import time as _time

        from routes import problem_files as pf

        with patch("core.series_json_check.count_open_series_json", return_value=3), \
             patch("core.series_json_check.refresh_all", return_value={}) as run:
            resp = client.post(self.URL, json={"provider": "metron"})
            assert resp.status_code == 200 and resp.get_json()["op_id"]
            deadline = _time.time() + 5
            while pf._scan_lock.locked() and _time.time() < deadline:
                _time.sleep(0.02)
        assert not pf._scan_lock.locked()
        assert run.call_args.kwargs["provider"] == "metron"

    def test_a_crashing_run_still_releases_the_lock(self, client):
        import time as _time

        from routes import problem_files as pf

        with patch("core.series_json_check.count_open_series_json", return_value=1), \
             patch("core.series_json_check.refresh_all", side_effect=RuntimeError("boom")):
            assert client.post(self.URL).status_code == 200
            deadline = _time.time() + 5
            while pf._scan_lock.locked() and _time.time() < deadline:
                _time.sleep(0.02)
        assert not pf._scan_lock.locked()

    def test_result_endpoint(self, client):
        report = {"total": 2, "fixed": 1, "failed": [{"folder": "/d/A", "path": "/d/A/series.json", "reason": "x"}]}
        with patch("core.series_json_check.last_bulk_result", return_value=report):
            data = client.get(self.URL + "/result").get_json()
        assert data["success"] is True and data["result"]["fixed"] == 1
        with patch("core.series_json_check.last_bulk_result", return_value=None):
            assert client.get(self.URL + "/result").get_json()["result"] is None

    def test_page_has_the_button_the_panel_and_the_confirmation(self, client):
        with patch("core.problem_files.count_problems", return_value={}), \
             patch("core.problem_files.list_problems", return_value=[]):
            body = client.get("/problem-files").data
        assert b'id="pf-refresh-sj-all"' in body
        assert b'id="pf-sj-result"' in body
        assert b'id="pf-sj-all-modal"' in body

    def test_the_background_run_has_an_application_context(self, client):
        """The provider lookups read current_app. A bare thread has none, so each
        raised "Working outside of application context", the lookup swallowed it,
        and every folder reported "No provider returned data" while the same
        refresh from a request worked."""
        import time as _time

        from flask import has_app_context

        from routes import problem_files as pf

        seen = {}

        def fake(op_id, provider="auto"):
            seen["ctx"] = has_app_context()
            return {}

        with patch("core.series_json_check.count_open_series_json", return_value=1), \
             patch("core.series_json_check.refresh_all", side_effect=fake):
            assert client.post(self.URL).status_code == 200
            deadline = _time.time() + 5
            while pf._scan_lock.locked() and _time.time() < deadline:
                _time.sleep(0.02)
        assert seen.get("ctx") is True


class TestEmptyFolders:
    def test_empty_folder_is_a_valid_source_filter(self, client):
        with patch("core.problem_files.list_problems", return_value=[]) as mock_list, \
             patch("core.problem_files.count_problems",
                   return_value={"open": 0, "dismissed": 0, "by_source": {}}):
            resp = client.get("/api/problem-files?source=empty-folder")
        assert resp.status_code == 200
        assert mock_list.call_args.kwargs["source"] == "empty-folder"

    def test_a_folder_row_gets_no_search_context(self, client):
        row = _row(path="/data/Comics/Empty", source="empty-folder",
                   filename="Empty", error_class="EmptyFolder")
        with patch("core.problem_files.list_problems", return_value=[row]), \
             patch("core.problem_files.count_problems",
                   return_value={"open": 1, "dismissed": 0, "by_source": {}}):
            resp = client.get("/api/problem-files")
        assert "search" not in resp.get_json()["problems"][0]

    def test_page_has_the_filter_and_the_scan_button(self, client):
        with patch("core.problem_files.list_problems", return_value=[]), \
             patch("core.problem_files.count_problems",
                   return_value={"open": 0, "dismissed": 0, "by_source": {}}):
            resp = client.get("/problem-files")
        assert b'data-source="empty-folder"' in resp.data
        assert b'id="pf-scan-empty"' in resp.data

    def test_scan_starts_in_the_background(self, client):
        import threading

        ran = threading.Event()

        def fake_scan(op_id=None):
            ran.set()
            return {"flagged": 0}

        with patch("core.library_health.scan_empty_folders", side_effect=fake_scan):
            resp = client.post("/api/problem-files/scan-empty-folders")
            assert resp.status_code == 200
            assert resp.get_json()["op_id"]
            assert ran.wait(5)

    def test_second_scan_while_running_is_409(self, client):
        from core import library_health as lh

        assert lh._empty_scan_lock.acquire(blocking=False)
        try:
            resp = client.post("/api/problem-files/scan-empty-folders")
            assert resp.status_code == 409
        finally:
            lh._empty_scan_lock.release()


class TestDeleteFolder:
    """A folder is deleted only while it is a listed empty folder *and* still
    empty: the scan that listed it may be a day old."""

    def _patches(self, tmp_path, listed=True, outcome=("EmptyFolder", "No files")):
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(patch("core.problem_files.has_problem", return_value=True))
        stack.enter_context(patch("core.problem_files.get_problem",
                                  return_value={"path": "x"} if listed else None))
        stack.enter_context(patch("helpers.library.is_critical_path", return_value=False))
        stack.enter_context(patch("core.library_health.inspect_folder", return_value=outcome))
        self.clear = stack.enter_context(patch("core.problem_files.clear_problem", return_value=1))
        self.clear_under = stack.enter_context(
            patch("core.problem_files.clear_problems_under", return_value=1))
        self.trash = stack.enter_context(
            patch("helpers.trash.move_to_trash", return_value={"trashed": True}))
        return stack

    def test_a_still_empty_listed_folder_is_trashed_with_its_rows(self, client, tmp_path):
        folder = tmp_path / "Empty"
        folder.mkdir()
        with self._patches(tmp_path):
            resp = client.post("/api/problem-files/delete", json={"path": str(folder)})
            assert resp.status_code == 200
            self.trash.assert_called_once_with(str(folder))
            # A series.json row inside it must not linger as "unreachable".
            self.clear_under.assert_called_once_with(str(folder))

    def test_a_folder_that_filled_up_is_refused_and_dropped(self, client, tmp_path):
        folder = tmp_path / "Filled"
        folder.mkdir()
        with self._patches(tmp_path, outcome=None):
            resp = client.post("/api/problem-files/delete", json={"path": str(folder)})
            assert resp.status_code == 409
            self.trash.assert_not_called()
            self.clear.assert_called_once_with(str(folder), "empty-folder")

    def test_a_folder_listed_under_another_source_is_refused(self, client, tmp_path):
        folder = tmp_path / "Other"
        folder.mkdir()
        with self._patches(tmp_path, listed=False):
            resp = client.post("/api/problem-files/delete", json={"path": str(folder)})
            assert resp.status_code == 404
            self.trash.assert_not_called()
