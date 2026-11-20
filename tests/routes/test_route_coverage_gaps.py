"""Blueprint routes that no test named.

A scan of every route rule against the test suite found nine in routes/ that
nothing exercised. tests/unit/test_every_route_has_a_test.py keeps that list
empty from here on; these are the tests that emptied it.
"""
import os
from unittest.mock import MagicMock, patch

import pytest


# ── /api/bulk-metadata/jobs ────────────────────────────────────────────────

class TestBulkMetadataJobs:
    def test_no_jobs_is_an_empty_list(self, client):
        assert client.get("/api/bulk-metadata/jobs").get_json() == {"success": True, "jobs": []}

    def test_paging_is_passed_through(self, client):
        with patch("routes.bulk_metadata.list_bulk_jobs", return_value=[{"id": "j1"}]) as listing:
            body = client.get("/api/bulk-metadata/jobs?limit=5&offset=10").get_json()
        assert body["jobs"] == [{"id": "j1"}]
        listing.assert_called_once_with(limit=5, offset=10)

    def test_default_page(self, client):
        with patch("routes.bulk_metadata.list_bulk_jobs", return_value=[]) as listing:
            client.get("/api/bulk-metadata/jobs")
        listing.assert_called_once_with(limit=50, offset=0)


# ── /api/bulk-metadata/has-subfolders ──────────────────────────────────────

class TestHasSubfolders:
    URL = "/api/bulk-metadata/has-subfolders"

    def test_path_is_required(self, client):
        resp = client.get(self.URL)
        assert resp.status_code == 400
        assert resp.get_json() == {"success": False, "error": "path is required"}

    def test_a_path_outside_every_library_is_403(self, client, tmp_path_library, tmp_path_factory):
        outside = tmp_path_factory.mktemp("elsewhere")
        resp = client.get(self.URL, query_string={"path": str(outside)})
        assert resp.status_code == 403

    def test_a_file_is_404(self, client, tmp_path_library, tmp_path):
        comic = tmp_path / "Batman 001.cbz"
        comic.write_bytes(b"x")
        assert client.get(self.URL, query_string={"path": str(comic)}).status_code == 404

    def test_a_folder_of_comics_has_none(self, client, tmp_path_library, tmp_path):
        series = tmp_path / "Batman"
        series.mkdir()
        (series / "Batman 001.cbz").write_bytes(b"x")
        body = client.get(self.URL, query_string={"path": str(series)}).get_json()
        assert body == {"success": True, "has_subfolders": False}

    def test_a_publisher_folder_has_some(self, client, tmp_path_library, tmp_path):
        (tmp_path / "DC" / "Batman").mkdir(parents=True)
        body = client.get(self.URL, query_string={"path": str(tmp_path / "DC")}).get_json()
        assert body == {"success": True, "has_subfolders": True}


# ── /api/scan-directory ────────────────────────────────────────────────────

class TestScanDirectory:
    URL = "/api/scan-directory"

    def _indexed(self, root):
        from core.database import get_db_connection
        conn = get_db_connection()
        try:
            rows = conn.execute(
                "SELECT path, type FROM file_index WHERE path = ? OR path LIKE ?",
                (root, root + os.sep + "%"),
            ).fetchall()
        finally:
            conn.close()
        return {os.path.relpath(r["path"], root).replace(os.sep, "/"): r["type"] for r in rows}

    def test_path_is_required(self, client):
        assert client.post(self.URL, json={}).status_code == 400

    def test_a_path_outside_every_library_is_403(self, client, tmp_path_library, tmp_path_factory):
        outside = tmp_path_factory.mktemp("elsewhere")
        assert client.post(self.URL, json={"path": str(outside)}).status_code == 403

    def test_a_missing_folder_is_404(self, client, tmp_path_library, tmp_path):
        assert client.post(self.URL, json={"path": str(tmp_path / "gone")}).status_code == 404

    def test_a_file_is_400(self, client, tmp_path_library, tmp_path):
        comic = tmp_path / "Batman 001.cbz"
        comic.write_bytes(b"x")
        assert client.post(self.URL, json={"path": str(comic)}).status_code == 400

    def test_it_indexes_comics_and_skips_everything_else(self, client, tmp_path_library, tmp_path):
        series = tmp_path / "Batman"
        (series / "Annuals").mkdir(parents=True)
        (series / ".hidden").mkdir()
        (series / "_staging").mkdir()
        for name in ("Batman 001.cbz", "Batman 002.cbr", "missing.txt",      # indexed
                     "folder.png", "series.json", "cvinfo", "._Batman 001.cbz"):   # not
            (series / name).write_bytes(b"x")
        (series / "Annuals" / "Batman Annual 001.cbz").write_bytes(b"x")
        (series / ".hidden" / "secret.cbz").write_bytes(b"x")
        (series / "_staging" / "partial.cbz").write_bytes(b"x")

        body = client.post(self.URL, json={"path": str(series)}).get_json()

        assert body["success"] is True
        assert (body["directories"], body["files"]) == (2, 4)
        assert self._indexed(str(series)) == {
            ".": "directory",
            "Annuals": "directory",
            "Batman 001.cbz": "file",
            "Batman 002.cbr": "file",
            "missing.txt": "file",
            "Annuals/Batman Annual 001.cbz": "file",
        }

    def test_a_rescan_drops_rows_for_files_that_are_gone(self, client, tmp_path_library, tmp_path):
        series = tmp_path / "Batman"
        series.mkdir()
        (series / "Batman 001.cbz").write_bytes(b"x")
        (series / "Batman 002.cbz").write_bytes(b"x")
        client.post(self.URL, json={"path": str(series)})
        (series / "Batman 002.cbz").unlink()

        client.post(self.URL, json={"path": str(series)})

        assert sorted(self._indexed(str(series))) == [".", "Batman 001.cbz"]

    def test_a_rescan_does_not_clear_reading_list_matches(self, client, tmp_path_library, tmp_path):
        """A re-scan deletes and re-adds the subtree; it is not a deletion, so
        it must not go through forget_deleted_path (path-references.md)."""
        series = tmp_path / "Batman"
        series.mkdir()
        (series / "Batman 001.cbz").write_bytes(b"x")
        with patch("core.database.clear_path_references") as cleared:
            client.post(self.URL, json={"path": str(series)})
        cleared.assert_not_called()


# ── /api/check-missing-files ───────────────────────────────────────────────

class TestCheckMissingFiles:
    URL = "/api/check-missing-files"

    def _run(self, client, folder, report):
        def write_report(path):
            if report is not None:
                with open(os.path.join(path, "missing.txt"), "w") as fh:
                    fh.write(report)

        with patch("missing.check_missing_issues", side_effect=write_report) as check:
            resp = client.post(self.URL, json={"folder_path": str(folder)})
        return resp, check

    def test_folder_path_is_required(self, client):
        assert client.post(self.URL, json={}).status_code == 400

    def test_a_missing_folder_is_400(self, client, tmp_path_library, tmp_path):
        resp, check = self._run(client, tmp_path / "gone", None)
        assert resp.status_code == 400
        check.assert_not_called()

    def test_nothing_missing(self, client, tmp_path_library, tmp_path):
        series = tmp_path / "Batman"
        series.mkdir()
        resp, check = self._run(client, series, None)
        body = resp.get_json()
        assert body["success"] is True and body["missing_count"] == 0
        assert body["summary"] == "No missing issues found."
        check.assert_called_once_with(str(series))

    def test_listed_issues_are_counted(self, client, tmp_path_library, tmp_path):
        series = tmp_path / "Batman"
        series.mkdir()
        resp, _ = self._run(client, series, "Batman 003.cbz\nBatman 004.cbr\n\nheader line\n")
        body = resp.get_json()
        assert body["missing_count"] == 2
        assert body["summary"] == "Found 2 missing issue(s) in Batman."
        assert body["folder_name"] == "Batman"
        assert body["missing_file"] == os.path.join(str(series), "missing.txt")
        assert body["relative_missing_file"] == os.path.join("Batman", "missing.txt")

    def test_a_condensed_range_adds_its_total(self, client, tmp_path_library, tmp_path):
        series = tmp_path / "Batman"
        series.mkdir()
        resp, _ = self._run(client, series, "Batman 003.cbz\nBatman 010-050 [Total missing: 41]\n")
        assert resp.get_json()["missing_count"] == 42

    def test_a_failed_check_is_a_500(self, client, tmp_path_library, tmp_path):
        series = tmp_path / "Batman"
        series.mkdir()
        with patch("missing.check_missing_issues", side_effect=RuntimeError("boom")):
            resp = client.post(self.URL, json={"folder_path": str(series)})
        assert resp.status_code == 500
        assert resp.get_json() == {"error": "boom"}


# ── /crop-freeform ─────────────────────────────────────────────────────────

class TestCropFreeform:
    URL = "/crop-freeform"
    BOX = {"x": 10, "y": 20, "width": 300, "height": 400}

    @pytest.mark.parametrize("drop", ["target", "x", "y", "width", "height"])
    def test_every_field_is_required(self, client, tmp_path_library, tmp_path, drop):
        body = {"target": str(tmp_path / "p.jpg"), **self.BOX}
        del body[drop]
        with patch("routes.files.cropFreeForm") as crop:
            resp = client.post(self.URL, json=body)
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False
        crop.assert_not_called()

    def test_zero_is_a_coordinate_not_a_missing_one(self, client, tmp_path_library, tmp_path):
        target = str(tmp_path / "p.jpg")
        with patch("routes.files.cropFreeForm", return_value=(target, target + ".bak")) as crop, \
                patch("routes.files.get_image_data_url", return_value="data:,"):
            resp = client.post(self.URL, json={"target": target, **self.BOX, "x": 0, "y": 0})
        assert resp.status_code == 200
        crop.assert_called_once_with(target, 0, 0, 300, 400)

    def test_returns_the_new_image_and_its_backup(self, client, tmp_path_library, tmp_path):
        target = str(tmp_path / "p.jpg")
        with patch("routes.files.cropFreeForm", return_value=(target, target + ".bak")), \
                patch("routes.files.get_image_data_url", side_effect=lambda p: "data:" + os.path.basename(p)):
            body = client.post(self.URL, json={"target": target, **self.BOX}).get_json()
        assert body["success"] is True
        assert body["newImagePath"] == target and body["backupImagePath"] == target + ".bak"
        assert body["newImageData"] == "data:p.jpg" and body["backupImageData"] == "data:p.jpg.bak"

    def test_a_failed_crop_is_a_500(self, client, tmp_path_library, tmp_path):
        with patch("routes.files.cropFreeForm", side_effect=OSError("cannot open")):
            resp = client.post(self.URL, json={"target": str(tmp_path / "p.jpg"), **self.BOX})
        assert resp.status_code == 500
        assert resp.get_json() == {"success": False, "error": "cannot open"}

    def test_a_refused_path_is_never_cropped(self, client, tmp_path):
        with patch("routes.files.file_op_refusal", return_value=("Access denied", 403)), \
                patch("routes.files.cropFreeForm") as crop:
            resp = client.post(self.URL, json={"target": str(tmp_path / "p.jpg"), **self.BOX})
        assert resp.status_code == 403
        crop.assert_not_called()


# ── /api/problem-files/refresh-series-json-all/result ──────────────────────

class TestRefreshAllResult:
    URL = "/api/problem-files/refresh-series-json-all/result"

    def test_null_when_no_run_has_finished(self, client):
        with patch("core.series_json_check.last_bulk_result", return_value=None):
            assert client.get(self.URL).get_json() == {"success": True, "result": None}

    def test_returns_the_last_report(self, client):
        report = {"total": 3, "corrected": 2, "failed": [{"folder": "/data/X", "reason": "no description"}]}
        with patch("core.series_json_check.last_bulk_result", return_value=report):
            assert client.get(self.URL).get_json() == {"success": True, "result": report}


# ── /api/series/<id>/check-collection ──────────────────────────────────────

class TestCheckCollection:
    def _url(self, series_id=7, **query):
        return f"/api/series/{series_id}/check-collection", query

    def test_an_unmapped_series_is_404(self, client):
        url, _ = self._url()
        with patch("core.database.get_series_mapping", return_value=None):
            resp = client.get(url)
        assert resp.status_code == 404
        assert resp.get_json() == {"error": "Series not mapped"}

    def test_a_mapped_folder_that_is_gone_is_404(self, client, tmp_path):
        url, _ = self._url()
        with patch("core.database.get_series_mapping", return_value=str(tmp_path / "gone")):
            resp = client.get(url)
        assert resp.status_code == 404
        assert resp.get_json() == {"error": "Mapped directory not found"}

    def _check(self, client, tmp_path, status, manual, refresh=False):
        issues = [{"number": str(n)} for n in (1, 2, 3, 4)]
        url, _ = self._url()
        with patch("core.database.get_series_mapping", return_value=str(tmp_path)), \
                patch("core.database.get_manual_status_for_series", return_value=manual), \
                patch("core.database.invalidate_collection_status_for_series") as invalidate, \
                patch("routes.series.get_series_by_id", return_value={"id": 7, "name": "Batman"}), \
                patch("routes.series.get_issues_for_series", return_value=issues), \
                patch("routes.series.match_issues_to_collection", return_value=status) as match:
            resp = client.get(url, query_string={"refresh": "true"} if refresh else {})
        return resp, match, invalidate

    def test_counts_found_manual_and_missing(self, client, tmp_path):
        status = {"1": {"found": True}, "2": {"found": True}, "3": {"found": False}, "4": {"found": False}}
        manual = {"3": "owned"}   # marked by hand, no file
        resp, match, invalidate = self._check(client, tmp_path, status, manual)
        body = resp.get_json()
        assert body["success"] is True
        assert (body["found_count"], body["manual_count"], body["missing_count"], body["total_count"]) == (2, 1, 1, 4)
        assert match.call_args.kwargs == {"use_cache": True}
        invalidate.assert_not_called()

    def test_a_manual_mark_on_a_found_issue_is_not_counted_twice(self, client, tmp_path):
        status = {"1": {"found": True}, "2": {"found": False}, "3": {"found": False}, "4": {"found": False}}
        resp, _, _ = self._check(client, tmp_path, status, {"1": "owned"})
        assert resp.get_json()["missing_count"] == 3

    def test_refresh_drops_the_cache_and_rescans(self, client, tmp_path):
        resp, match, invalidate = self._check(client, tmp_path, {}, {}, refresh=True)
        assert resp.status_code == 200
        invalidate.assert_called_once_with(7)
        assert match.call_args.kwargs == {"use_cache": False}

    def test_no_cache_and_no_metron_is_a_500(self, client, tmp_path):
        url, _ = self._url()
        with patch("core.database.get_series_mapping", return_value=str(tmp_path)), \
                patch("routes.series.get_series_by_id", return_value=None), \
                patch("routes.series.get_issues_for_series", return_value=[]), \
                patch("routes.series.metron") as metron:
            metron.get_flask_api.return_value = None
            resp = client.get(url)
        assert resp.status_code == 500
        assert "no cached data" in resp.get_json()["error"]


# ── /api/sync/all ──────────────────────────────────────────────────────────

class TestSyncAll:
    URL = "/api/sync/all"

    def test_without_metron_it_is_a_500(self, client):
        with patch("routes.series.metron") as metron:
            metron.get_flask_api.return_value = None
            resp = client.post(self.URL)
        assert resp.status_code == 500

    def _api(self, by_id):
        """A Metron client stand-in whose ``using(...).series(id)`` answers from *by_id*."""
        api = MagicMock()

        def series(series_id):
            answer = by_id[series_id]
            if isinstance(answer, Exception):
                raise answer
            return answer

        api.using.return_value.series.side_effect = series
        return api

    def test_each_series_is_reported_on_its_own(self, client):
        api = self._api({1: {"id": 1}, 2: None, 3: RuntimeError("bad payload")})
        with patch("routes.series.metron") as metron, \
                patch("routes.series.get_series_needing_sync", return_value=[{"id": 1}, {"id": 2}, {"id": 3}]) as due, \
                patch("routes.series.delete_issues_for_series") as delete, \
                patch("routes.series.save_issues_bulk") as save, \
                patch("routes.series.update_series_sync_time") as stamp:
            metron.get_flask_api.return_value = api
            metron.get_all_issues_for_series.return_value = [{"number": "1"}, {"number": "2"}]
            metron.is_connection_error.return_value = False
            body = client.post(self.URL, json={"hours": 6}).get_json()

        due.assert_called_once_with(6)
        assert (body["success"], body["synced"], body["failed"]) == (True, 1, 2)
        assert body["results"] == [
            {"series_id": 1, "success": True, "issue_count": 2},
            {"series_id": 2, "success": False, "error": "Not found"},
            {"series_id": 3, "success": False, "error": "bad payload"},
        ]
        # Only the series that answered is rewritten.
        delete.assert_called_once_with(1)
        save.assert_called_once_with([{"number": "1"}, {"number": "2"}], 1)
        stamp.assert_called_once_with(1, 2)

    def test_the_default_window_is_a_day(self, client):
        with patch("routes.series.metron") as metron, \
                patch("routes.series.get_series_needing_sync", return_value=[]) as due:
            metron.get_flask_api.return_value = self._api({})
            body = client.post(self.URL).get_json()
        due.assert_called_once_with(24)
        assert (body["synced"], body["failed"], body["results"]) == (0, 0, [])

    def test_an_outage_is_named_as_one(self, client):
        api = self._api({1: ConnectionError("timed out")})
        with patch("routes.series.metron") as metron, \
                patch("routes.series.get_series_needing_sync", return_value=[{"id": 1}]):
            metron.get_flask_api.return_value = api
            metron.is_connection_error.return_value = True
            body = client.post(self.URL).get_json()
        assert body["results"] == [
            {"series_id": 1, "success": False, "error": "Metron is currently unavailable"}]


# ── /api/publishers/search ─────────────────────────────────────────────────

class TestPublisherSearch:
    URL = "/api/publishers/search"

    def _search(self, client, results, existing=()):
        api = MagicMock()
        api.using.return_value.publishers_list.return_value = results
        with patch("routes.series.metron") as metron, \
                patch("core.database.get_all_publishers", return_value=[{"id": i} for i in existing]):
            metron.get_flask_api.return_value = api
            metron.paused_reason.return_value = "Metron is paused: daily quota spent"
            resp = client.get(self.URL, query_string={"q": "dc"})
        return resp, api

    @pytest.mark.parametrize("q", ["", "   "])
    def test_a_query_is_required(self, client, q):
        resp = client.get(self.URL, query_string={"q": q})
        assert resp.status_code == 400
        assert resp.get_json() == {"success": False, "error": "Search query required"}

    def test_without_metron_it_is_a_400(self, client):
        with patch("routes.series.metron") as metron:
            metron.get_flask_api.return_value = None
            resp = client.get(self.URL, query_string={"q": "dc"})
        assert resp.status_code == 400

    def test_results_say_which_publishers_are_already_added(self, client):
        dc = MagicMock(id=10, image="https://img/dc.png")
        dc.name = "DC Comics"
        vertigo = MagicMock(id=11, image=None)
        vertigo.name = "Vertigo"
        resp, api = self._search(client, [dc, vertigo], existing=(10,))
        assert resp.get_json() == {"success": True, "publishers": [
            {"id": 10, "name": "DC Comics", "image": "https://img/dc.png", "exists": True},
            {"id": 11, "name": "Vertigo", "image": None, "exists": False},
        ]}
        api.using.return_value.publishers_list.assert_called_once_with({"name": "dc"})

    def test_no_matches_is_an_empty_list_not_an_error(self, client):
        resp, _ = self._search(client, [])
        assert resp.get_json() == {"success": True, "publishers": []}

    def test_a_paused_metron_is_a_503_with_the_reason(self, client):
        resp, _ = self._search(client, None)
        assert resp.status_code == 503
        assert resp.get_json() == {"success": False, "error": "Metron is paused: daily quota spent"}

    def test_an_outage_is_a_503(self, client):
        api = MagicMock()
        api.using.return_value.publishers_list.side_effect = ConnectionError("timed out")
        with patch("routes.series.metron") as metron:
            metron.get_flask_api.return_value = api
            metron.is_connection_error.return_value = True
            resp = client.get(self.URL, query_string={"q": "dc"})
        assert resp.status_code == 503
        assert "unavailable" in resp.get_json()["error"]
