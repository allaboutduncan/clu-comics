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
        library is clean"."""
        resp = client.get("/problem-files")
        assert b"does not scan" in resp.data


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
