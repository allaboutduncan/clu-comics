"""Routes for the library health card, badge, scan and duplicate review.

Patch targets follow the module they live in: ``routes/library_health.py``
imports everything inside its views, so symbols are patched at their real home
(``core.library_health.*``).
"""

import time
from unittest.mock import patch

import pytest

from core import library_health as lh


@pytest.fixture(autouse=True)
def _fresh_settings():
    from core.settings import invalidate

    invalidate()
    yield
    invalidate()


def _group(gid="abc123", kind="exact"):
    return {
        "id": gid, "key": "exact:x", "kind": kind, "dismissed": False,
        "reason": "Identical files (same content hash)",
        "members": [
            {"path": "/data/A/a.cbz", "name": "a.cbz", "folder": "/data/A", "size": 10,
             "modified_at": 1.0, "series": None, "number": None, "year": None,
             "has_comicinfo": True, "provider": None, "hash": "h", "status": "ok"},
            {"path": "/data/A/a (1).cbz", "name": "a (1).cbz", "folder": "/data/A", "size": 10,
             "modified_at": 1.0, "series": None, "number": None, "year": None,
             "has_comicinfo": False, "provider": None, "hash": "h", "status": "ok"},
        ],
        "keep_suggestion": "/data/A/a.cbz",
        "wasted_bytes": 10,
    }


class TestOwnerOnly:
    @pytest.mark.parametrize("url", [
        "/library-health/duplicates",
        "/problem-files/duplicates",
        "/api/problem-files/health/summary",
        "/api/problem-files/health/badge",
        "/api/problem-files/health/scan",
        "/api/problem-files/health/scan/stop",
        "/api/problem-files/health/settings",
        "/api/problem-files/duplicates",
        "/api/problem-files/duplicates/resolve",
        "/api/problem-files/duplicates/dismiss",
    ])
    def test_every_route_is_under_an_owner_prefix(self, url):
        """The scan decides whether CLU reads every comic in the library and
        the resolve route trashes files, so none of this may fall through to
        the Reader/Clerk defaults."""
        from core.auth import _OWNER_PREFIXES

        assert url.startswith(_OWNER_PREFIXES)


class TestSummaryAndBadge:
    def test_summary_shape(self, client):
        data = client.get("/api/problem-files/health/summary").get_json()
        assert data["success"] is True
        for key in ("enabled", "coverage", "score", "level", "duplicates", "last_run_at"):
            assert key in data
        assert data["enabled"] is False  # off by default: it reads every comic

    def test_badge_is_hidden_until_a_scan_has_run(self, client):
        assert client.get("/api/problem-files/health/badge").get_json()["visible"] is False
        lh.save_state({"last_run_at": time.time()})
        data = client.get("/api/problem-files/health/badge").get_json()
        assert data["visible"] is True
        assert data["level"] == "green" and data["score"] == 100

    def test_badge_carries_the_duplicate_count_for_the_menu(self, client):
        lh.save_state({"last_run_at": time.time(), "dup_exact_groups": 2,
                       "dup_likely_groups": 3})
        data = client.get("/api/problem-files/health/badge").get_json()
        assert data["duplicate_groups"] == 5

    def test_badge_counts_nothing_once_a_scan_has_stored_coverage(self, client):
        """It is polled from every open tab; coverage is two whole-table counts."""
        lh.save_state({"last_run_at": time.time(),
                       "coverage": {"total": 10, "checked": 4, "pct": 40}})
        with patch("core.library_health.coverage", side_effect=AssertionError("counted")):
            data = client.get("/api/problem-files/health/badge").get_json()
        assert data["coverage_pct"] == 40


class TestScan:
    def test_starts_in_the_background_and_releases_the_claim(self, client):
        with patch("core.library_health.run_scan", return_value={}) as run:
            resp = client.post("/api/problem-files/health/scan")
            assert resp.status_code == 200
            body = resp.get_json()
            assert body["success"] is True and body["op_id"]
            deadline = time.time() + 5
            while lh.scan_running() and time.time() < deadline:
                time.sleep(0.02)
        assert lh.scan_running() is False
        assert run.call_args.kwargs["run_cheap"] is True

    def test_second_scan_is_refused_with_409(self, client):
        assert lh.claim_scan()
        try:
            resp = client.post("/api/problem-files/health/scan")
            assert resp.status_code == 409
            assert resp.get_json()["success"] is False
        finally:
            lh.release_scan()

    def test_stop_is_a_409_when_nothing_is_running(self, client):
        resp = client.post("/api/problem-files/health/scan/stop")
        assert resp.status_code == 409
        assert resp.get_json()["success"] is False

    def test_stop_flags_the_running_scan(self, client):
        assert lh.claim_scan()
        try:
            resp = client.post("/api/problem-files/health/scan/stop")
            assert resp.status_code == 200 and resp.get_json()["success"] is True
            assert lh.stop_requested() is True
            summary = client.get("/api/problem-files/health/summary").get_json()
            assert summary["running"] is True and summary["stopping"] is True
        finally:
            lh.release_scan()
        # The next scan is not born stopped.
        assert lh.claim_scan()
        try:
            assert lh.stop_requested() is False
        finally:
            lh.release_scan()

    def test_the_page_tells_a_finished_scan_from_one_a_restart_killed(self):
        """The operations registry is in memory. A missing operation used to
        fall into the success branch, so a scan the restart killed was
        announced as complete."""
        src = TestInfoModalIsAvailable._js("problem_files.js")
        follow = src[src.index("function followHealthScan"):src.index("function stopHealthScan")]
        assert "last_run_at !== before" in follow
        assert "was interrupted" in follow
        assert "/api/problem-files/health/scan/stop" in src
        assert 'id="pf-health-stop"' in open(
            "templates/problem_files.html", encoding="utf-8").read()

    def test_a_crashing_scan_still_releases_the_claim(self, client):
        with patch("core.library_health.run_scan", side_effect=RuntimeError("boom")):
            assert client.post("/api/problem-files/health/scan").status_code == 200
            deadline = time.time() + 5
            while lh.scan_running() and time.time() < deadline:
                time.sleep(0.02)
        assert lh.scan_running() is False


class TestSettings:
    def test_defaults_then_roundtrip(self, client):
        data = client.get("/api/problem-files/health/settings").get_json()
        assert data["enabled"] is False and data["batch"] == 200

        resp = client.post("/api/problem-files/health/settings",
                           json={"enabled": True, "interval_hours": 12, "batch": 500})
        data = resp.get_json()
        assert data["enabled"] is True
        assert data["interval_hours"] == 12 and data["batch"] == 500

    @pytest.mark.parametrize("body", [
        {"interval_hours": 0}, {"batch": 999999}, {"recheck_days": 1}, {"batch": "lots"},
    ])
    def test_bad_values_are_rejected_and_not_stored(self, client, body):
        assert client.post("/api/problem-files/health/settings", json=body).status_code == 400
        assert client.get("/api/problem-files/health/settings").get_json()["batch"] == 200


class TestDuplicates:
    def test_page_renders(self, client):
        resp = client.get("/library-health/duplicates")
        assert resp.status_code == 200
        assert b"Duplicate Files" in resp.data

    def test_old_url_redirects(self, client):
        resp = client.get("/problem-files/duplicates")
        assert resp.status_code == 301
        assert resp.headers["Location"].endswith("/library-health/duplicates")

    def test_page_carries_the_trash_settings(self, client, app):
        app.config["TRASH_ENABLED"] = True
        app.config["TRASH_MAX_SIZE_MB"] = 50
        body = client.get("/library-health/duplicates").data
        assert b'data-trash-enabled="1"' in body
        assert b'data-trash-max-bytes="52428800"' in body

    def test_page_reports_the_trash_disabled(self, client, app):
        app.config["TRASH_ENABLED"] = False
        body = client.get("/library-health/duplicates").data
        assert b'data-trash-enabled="0"' in body

    def test_confirmation_is_skipped_only_when_the_trash_can_hold_the_copies(self):
        src = TestInfoModalIsAvailable._js("library_duplicates.js")
        assert "function canSkipConfirm(" in src
        assert "data-trash-enabled" in src and "data-trash-max-bytes" in src
        # Unknown sizes and an over-limit total both fall back to the modal.
        assert "if (!(size > 0)) return false;" in src
        assert "total <= max" in src
        assert "showModal('dup-confirm-modal')" in src

    def test_library_health_page_has_the_health_card(self, client):
        with patch("core.problem_files.count_problems", return_value={}), \
             patch("core.problem_files.list_problems", return_value=[]):
            body = client.get("/library-health").data
        assert b"pf-health" in body
        assert b'data-value="integrity"' in body
        assert b'data-value="orphan"' in body

    def test_list_shape_and_totals(self, client):
        found = {"exact": [_group()], "likely": [_group("def456", "likely")]}
        with patch("core.library_health.find_duplicates", return_value=found):
            data = client.get("/api/problem-files/duplicates").get_json()
        assert data["success"] is True
        assert data["total_groups"] == 2 and data["truncated"] is False
        assert data["wasted_bytes"] == 10  # only exact duplicates count as reclaimable

    def test_include_dismissed_is_passed_through(self, client):
        with patch("core.library_health.find_duplicates",
                   return_value={"exact": [], "likely": []}) as find:
            client.get("/api/problem-files/duplicates?include_dismissed=1")
        assert find.call_args.kwargs["include_dismissed"] is True

    def test_list_is_paged_exact_first_and_every_group_is_reachable(self, client):
        found = {
            "exact": [_group(f"e{i}") for i in range(2)],
            "likely": [_group(f"l{i}", "likely") for i in range(4)],
        }
        seen = []
        with patch("core.library_health.find_duplicates", return_value=found):
            data = client.get("/api/problem-files/duplicates?limit=3").get_json()
            assert [g["id"] for g in data["exact"]] == ["e0", "e1"]
            assert [g["id"] for g in data["likely"]] == ["l0"]
            assert data["total_groups"] == 6 and data["truncated"] is True
            assert data["total_exact"] == 2 and data["total_likely"] == 4
            assert data["next_offset"] == 3
            # Totals describe the whole list, not the page.
            assert data["wasted_bytes"] == 20
            offset = 0
            while offset is not None:
                page = client.get(
                    f"/api/problem-files/duplicates?limit=3&offset={offset}").get_json()
                seen += [g["id"] for g in page["exact"] + page["likely"]]
                offset = page["next_offset"]
        assert seen == ["e0", "e1", "l0", "l1", "l2", "l3"]

    def test_bad_paging_is_a_400(self, client):
        assert client.get("/api/problem-files/duplicates?offset=x").status_code == 400

    def test_a_failed_read_is_an_error_not_an_empty_list(self, client):
        """An empty list has to mean "no duplicates". The page said "Nothing to
        review" over a database it had just failed to read."""
        with patch("core.library_health.find_duplicates",
                   side_effect=RuntimeError("database disk image is malformed")) as find:
            resp = client.get("/api/problem-files/duplicates")
        assert find.call_args.kwargs["strict"] is True
        assert resp.status_code == 500
        body = resp.get_json()
        assert body["success"] is False and "malformed" not in body["error"]

    def test_page_shows_a_load_failure_and_pages_the_list(self):
        src = TestInfoModalIsAvailable._js("library_duplicates.js")
        assert "could not be loaded" in src
        assert "function loadMore" in src and "'?offset='" in src
        assert 'id="dup-more"' in open(
            "templates/library_duplicates.html", encoding="utf-8").read()

    @pytest.mark.parametrize("body", [
        {}, {"id": "x"}, {"id": "x", "keep": "/a"}, {"id": "x", "keep": "/a", "trash": "nope"},
    ])
    def test_resolve_needs_all_fields(self, client, body):
        assert client.post("/api/problem-files/duplicates/resolve", json=body).status_code == 400

    def test_resolve_passes_the_choice_through(self, client):
        with patch("core.library_health.resolve_group",
                   return_value={"success": True, "results": []}) as resolve:
            resp = client.post("/api/problem-files/duplicates/resolve",
                               json={"id": "g", "keep": "/a", "trash": ["/b"]})
        assert resp.status_code == 200
        resolve.assert_called_once_with("g", "/a", ["/b"])

    def test_resolve_validation_error_is_a_400(self, client):
        with patch("core.library_health.resolve_group",
                   return_value={"success": False, "error": "Some files are not in this group"}):
            resp = client.post("/api/problem-files/duplicates/resolve",
                               json={"id": "g", "keep": "/a", "trash": ["/b"]})
        assert resp.status_code == 400

    def test_partial_failure_is_a_207(self, client):
        with patch("core.library_health.resolve_group", return_value={
                "success": False, "results": [{"path": "/b", "success": False, "error": "x"}]}):
            resp = client.post("/api/problem-files/duplicates/resolve",
                               json={"id": "g", "keep": "/a", "trash": ["/b"]})
        assert resp.status_code == 207

    def test_dismiss(self, client):
        assert client.post("/api/problem-files/duplicates/dismiss", json={}).status_code == 400
        ok = {"success": True, "gone": False}
        with patch("core.library_health.dismiss_group", return_value=ok) as dismiss:
            resp = client.post("/api/problem-files/duplicates/dismiss",
                               json={"id": "g", "dismissed": False})
        assert resp.status_code == 200
        dismiss.assert_called_once_with("g", dismissed=False, until_scan=False, seen_paths=None)

    def test_dismiss_until_next_scan_passes_the_members_seen(self, client):
        ok = {"success": True, "gone": False}
        with patch("core.library_health.dismiss_group", return_value=ok) as dismiss:
            resp = client.post("/api/problem-files/duplicates/dismiss",
                               json={"id": "g", "until_scan": True, "members": ["/data/a.cbz"]})
        assert resp.status_code == 200
        dismiss.assert_called_once_with("g", dismissed=True, until_scan=True,
                                        seen_paths=["/data/a.cbz"])

    def test_dismissing_a_group_fixed_elsewhere_is_a_200(self, client):
        with patch("core.library_health.dismiss_group",
                   return_value={"success": True, "gone": True}):
            resp = client.post("/api/problem-files/duplicates/dismiss", json={"id": "g"})
        assert resp.status_code == 200
        assert resp.get_json()["gone"] is True

    def test_dismiss_with_a_new_copy_is_a_409(self, client):
        with patch("core.library_health.dismiss_group",
                   return_value={"success": False, "changed": True, "error": "x"}):
            resp = client.post("/api/problem-files/duplicates/dismiss", json={"id": "g"})
        assert resp.status_code == 409
        assert resp.get_json()["changed"] is True

    def test_dismiss_failure_is_a_400(self, client):
        with patch("core.library_health.dismiss_group",
                   return_value={"success": False, "error": "Database unavailable"}):
            resp = client.post("/api/problem-files/duplicates/dismiss", json={"id": "g"})
        assert resp.status_code == 400
        assert resp.get_json() == {"success": False, "error": "Could not update this group"}

    def test_dismiss_exception_is_logged_not_returned(self, client):
        """CodeQL py/stack-trace-exposure: exception text must not reach the client."""
        boom = RuntimeError("no such table: duplicate_dismissals at /config/comic_utils.db")
        with patch("core.library_health.dismiss_group", side_effect=boom), \
             patch("helpers.responses.app_logger") as log:
            resp = client.post("/api/problem-files/duplicates/dismiss", json={"id": "g"})
        assert resp.status_code == 500
        assert resp.get_json() == {"success": False, "error": "Could not update this group"}
        assert "comic_utils.db" not in resp.get_data(as_text=True)
        assert log.error.call_args.kwargs["exc_info"] is boom

    def test_dismiss_rejects_malformed_members(self, client):
        resp = client.post("/api/problem-files/duplicates/dismiss",
                           json={"id": "g", "members": "/data/a.cbz"})
        assert resp.status_code == 400


class TestRenameMember:
    """POST /api/problem-files/duplicates/rename (#691)."""

    URL = "/api/problem-files/duplicates/rename"
    BODY = {"id": "g", "path": "/data/A/a (1).cbz", "name": "a annual.cbz"}
    NEW = "/data/A/a annual.cbz"

    def _post(self, client, body=None, *, valid=None, renamed=({"success": True}, 200),
              exists=False):
        valid = valid if valid is not None else {"new_path": self.NEW}
        with patch("core.library_health.validate_member_rename", return_value=valid) as v, \
             patch("routes.files.rename_path", return_value=renamed) as r, \
             patch("core.library_health.follow_member_rename") as f, \
             patch("core.library_health.group_exists", return_value=exists):
            resp = client.post(self.URL, json=self.BODY if body is None else body)
        return resp, v, r, f

    def test_is_owner_only(self):
        from core.auth import _OWNER_PREFIXES

        assert self.URL.startswith(_OWNER_PREFIXES)

    @pytest.mark.parametrize("body", [
        {}, {"id": "g", "path": "/a"}, {"id": "g", "name": "x.cbz"},
        {"id": "g", "path": "/a", "name": ""}, {"id": "g", "path": ["/a"], "name": "x.cbz"},
    ])
    def test_needs_id_path_and_name(self, client, body):
        resp, v, r, _ = self._post(client, body)
        assert resp.status_code == 400
        v.assert_not_called()
        r.assert_not_called()

    def test_success_renames_follows_and_reports_the_group_gone(self, client):
        resp, v, r, f = self._post(client)
        assert resp.status_code == 200
        assert resp.get_json() == {"success": True, "new_path": self.NEW,
                                   "new_name": "a annual.cbz", "group_gone": True}
        v.assert_called_once_with("g", self.BODY["path"], "a annual.cbz")
        r.assert_called_once_with(self.BODY["path"], self.NEW)
        f.assert_called_once_with(self.BODY["path"], self.NEW)

    def test_a_group_that_is_still_there_is_reported(self, client):
        resp, *_ = self._post(client, exists=True)
        assert resp.get_json()["group_gone"] is False

    def test_an_unreadable_group_is_not_reported_gone(self, client):
        resp, *_ = self._post(client, exists=None)
        assert resp.get_json()["group_gone"] is False

    def test_a_refused_name_never_reaches_the_disk(self, client):
        resp, v, r, f = self._post(client, valid={"error": "A file name cannot contain: /"})
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "A file name cannot contain: /"
        r.assert_not_called()
        f.assert_not_called()

    def test_a_taken_name_is_a_409(self, client):
        resp, _, _, f = self._post(client, renamed=({"error": "Destination already exists"}, 400))
        assert resp.status_code == 409
        assert resp.get_json()["error"] == "Destination already exists"
        f.assert_not_called()

    @pytest.mark.parametrize("status", [403, 404])
    def test_refusals_pass_through(self, client, status):
        resp, *_ = self._post(client, renamed=({"error": "nope"}, status))
        assert resp.status_code == status

    def test_a_crash_does_not_leak_its_text(self, client):
        resp, *_ = self._post(client, renamed=({"error": "EIO at /config/comic_utils.db"}, 500))
        assert resp.status_code == 500
        assert "comic_utils.db" not in resp.get_data(as_text=True)

    def test_really_renames_through_the_shared_body(self, client, tmp_path):
        """No patch on rename_path: the /rename body moves the file and the index."""
        from unittest.mock import MagicMock

        old = tmp_path / "Wonder Woman 005 (1996).cbz"
        old.write_bytes(b"data")
        new = tmp_path / "Wonder Woman Annual 005 (1996).cbz"
        fake_app = MagicMock()
        with patch.dict("sys.modules", {"app": fake_app}), \
             patch("routes.files.is_critical_path", return_value=False), \
             patch("core.library_health.validate_member_rename",
                   return_value={"new_path": str(new)}), \
             patch("core.library_health.follow_member_rename") as follow, \
             patch("core.library_health.group_exists", return_value=False):
            resp = client.post(self.URL, json={"id": "g", "path": str(old), "name": new.name})
        assert resp.status_code == 200
        assert new.exists() and not old.exists()
        fake_app.update_index_on_move.assert_called_once_with(str(old), str(new))
        follow.assert_called_once_with(str(old), str(new))


class TestInfoModalIsAvailable:
    """Both pages let the user open the ComicInfo viewer on a file to verify it."""

    def test_duplicates_page_ships_the_compare_viewer(self, client):
        """The duplicates page compares copies read-only; it no longer ships the editor."""
        body = client.get("/library-health/duplicates").data
        assert b'id="cbzDupeModal"' in body
        assert b"clu-cbz-dupe.js" in body
        assert b'id="cbzInfoModal"' not in body
        assert b"clu-cbz-info.js" not in body

    def test_problem_files_page_ships_the_viewer(self, client):
        with patch("core.problem_files.count_problems", return_value={}), \
             patch("core.problem_files.list_problems", return_value=[]):
            body = client.get("/library-health").data
        assert b'id="cbzInfoModal"' in body
        assert b"clu-cbz-info.js" in body

    @staticmethod
    def _js(script):
        import io
        import os

        path = os.path.join(os.path.dirname(__file__), "..", "..", "static", "js", script)
        return io.open(path, encoding="utf-8").read()

    def test_problem_files_opens_the_editor_through_the_shared_helper(self):
        """Wired structurally: a button with no handler fails silently."""
        src = self._js("problem_files.js")
        assert "case 'info':" in src
        assert "CLU.showCBZInfo(" in src
        # Edits rewrite the archive, so the list is re-read when the modal closes.
        assert "onMetadataSaved" in src and "hidden.bs.modal" in src

    def test_empty_folder_delete_skips_the_confirmation(self):
        """Its leftovers are recreatable and the server re-checks emptiness,
        so the row deletes at once; files still go through the modal."""
        src = self._js("problem_files.js")
        body = src.split("function confirmDelete(row)", 1)[1].split("function doDelete", 1)[0]
        bypass, modal = body.split("pending = row;", 1)
        assert "row.source === 'empty-folder'" in bypass
        assert "doDelete(row)" in bypass
        assert "showModal('pf-delete-modal')" in modal

    def test_deleted_row_turns_red_then_fades(self):
        src = self._js("problem_files.js")
        body = src.split("function doDelete(row)", 1)[1].split("function bind()", 1)[0]
        request = body.index("postJson('/api/problem-files/delete'")
        assert body.index("classList.add('table-danger')") < request
        # A refused delete puts the row back the way it was.
        assert "classList.remove('table-danger')" in body
        assert "fadeDeleted(tr).then(refresh)" in body

    def test_duplicates_opens_the_compare_viewer(self):
        src = self._js("library_duplicates.js")
        assert "=== 'info'" in src and "=== 'compare'" in src
        assert "CLU.showCBZDupe(" in src
        assert "CLU.showCBZInfo(" not in src
        # "Keep this one" hands back to the page's own confirmation, after the
        # viewer has closed so the two modals never stack.
        assert "onKeep" in src and "confirmResolve(gi)" in src
        assert "hidden.bs.modal" in src

    def test_duplicates_renames_only_likely_groups(self):
        """An exact group is identical bytes, so a rename cannot resolve it."""
        src = self._js("library_duplicates.js")
        open_info = src.split("function openInfo(gi, mi)", 1)[1].split("function renameMember", 1)[0]
        assert "if (g.kind === 'likely')" in open_info
        assert "opts.onRename" in open_info
        rename = src.split("function renameMember(gi, mi, name)", 1)[1].split("function keepFromModal", 1)[0]
        assert "postJson('/api/problem-files/duplicates/rename'" in rename
        assert "path: m.path" in rename  # from state, never the DOM
        assert "d.group_gone" in rename and "removeCard(gi)" in rename


class TestCompareViewer:
    """static/js/clu-cbz-dupe.js, asserted structurally (no JS test runner here)."""

    @staticmethod
    def _src():
        import io
        import os

        path = os.path.join(os.path.dirname(__file__), "..", "..", "static", "js", "clu-cbz-dupe.js")
        return io.open(path, encoding="utf-8").read()

    def test_is_read_only(self):
        src = self._src()
        for needle in ("/cbz-update-comicinfo", "/cbz-clear-comicinfo", "contenteditable", "method: 'POST'"):
            assert needle not in src

    def test_shows_only_the_sections_that_tell_copies_apart(self):
        src = self._src()
        for kept in ("Basic Information", "Publication Details", "File Information", "First Files"):
            assert kept in src
        for dropped in ("Additional Information", "Creative Team", "Content Details"):
            assert dropped not in src

    def test_escapes_and_never_puts_a_path_in_an_attribute(self):
        src = self._src()
        assert "CLU.escapeHtml" in src
        assert "data-path" not in src
        assert 'data-col="' in src

    def test_rename_is_offered_only_through_the_page_callback(self):
        """The viewer draws the editor; the request is the page's (onRename),
        which is why test_is_read_only still holds."""
        src = self._src()
        assert "data-dupe-act=\"rename\"" in src
        assert "if (_onRename)" in src
        assert "_onRename(ci, name)" in src
        # Escape in the name box cancels the edit, not the whole modal.
        editor = src.split("function _openRename(ci)", 1)[1].split("function _closeRename", 1)[0]
        assert "e.key === 'Escape'" in editor and "e.stopPropagation()" in editor
        # The typed name is set as a value, never spliced into markup.
        assert "input.value = parts[0]" in editor

    def test_keyboard_listener_is_removed_on_close(self):
        src = self._src()
        assert "addEventListener('keydown', _handleKeydown)" in src
        assert "removeEventListener('keydown', _handleKeydown)" in src
        assert "hidden.bs.modal" in src

    def test_partial_matches_the_dom_contract(self):
        import io
        import os

        path = os.path.join(os.path.dirname(__file__), "..", "..", "templates", "partials",
                            "modal_cbz_dupe.html")
        html = io.open(path, encoding="utf-8").read()
        src = self._src()
        for el_id in ("cbzDupeModal", "cbzDupeModalLabel", "cbzDupeContent"):
            assert f'id="{el_id}"' in html
            assert f"'{el_id}'" in src


class TestNavigation:
    """Library Health and Duplicate Files live in the File Manager menu."""

    @pytest.fixture(scope="class")
    def header(self):
        with open("templates/header.html", encoding="utf-8") as f:
            return f.read()

    def _menu(self, header, marker):
        start = header.index(marker)
        return header[start:header.index("</ul>", start)]

    def test_file_manager_menu_holds_both_pages_for_the_owner_only(self, header):
        menu = self._menu(header, 'id="file-manager-dropdown"')
        files = menu.index("url_for('collection.files_page')")
        owner = menu.index("{% if _nav_is_owner %}", menu.index("dropdown-menu"))
        health = menu.index("href=\"{{ url_for('problem_files.page') }}\"")
        dups = menu.index("href=\"{{ url_for('library_health.duplicates_page') }}\"")
        end = menu.index("{% endif %}", dups)
        # File Manager itself is for clerks too; the two pages sit in the owner block.
        assert files < owner < health < dups < end
        assert 'id="health-badge"' in menu and 'id="duplicates-badge"' in menu

    def test_the_menu_is_inside_the_clerk_gate(self, header):
        at = header.index('id="file-manager-dropdown"')
        assert header.rindex("{% if _nav_is_clerk %}", 0, at) > header.rindex("{% endif %}", 0, at)

    def test_the_gear_menu_no_longer_links_either(self, header):
        gear = self._menu(header, 'bi bi-gear-fill')
        assert "problem_files.page" not in gear
        assert "library_health.duplicates_page" not in gear
        # ...and neither does the gear's own "active" list.
        toggle = header[header.rindex("<a ", 0, header.index("bi bi-gear-fill")):
                        header.index("bi bi-gear-fill")]
        assert "problem_files.page" not in toggle

    def test_each_page_is_linked_once(self, header):
        assert header.count("href=\"{{ url_for('problem_files.page') }}\"") == 1
        assert header.count("href=\"{{ url_for('library_health.duplicates_page') }}\"") == 1
        assert header.count('id="health-dot"') == 1

    @pytest.mark.parametrize("url", ["/files", "/library-health", "/library-health/duplicates"])
    def test_the_menu_is_marked_active_on_all_three_pages(self, client, url):
        html = client.get(url).get_data(as_text=True)
        start = html.index('id="file-manager-dropdown"')
        toggle = html[start:html.index("</a>", start)]
        assert "dropdown-toggle" in toggle and " active" in toggle
        gear = html[html.rindex("<a ", 0, html.index("bi bi-gear-fill")):
                    html.index("bi bi-gear-fill")]
        assert " active" not in gear
