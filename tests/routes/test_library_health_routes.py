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
        "/problem-files/duplicates",
        "/api/problem-files/health/summary",
        "/api/problem-files/health/badge",
        "/api/problem-files/health/scan",
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
        resp = client.get("/problem-files/duplicates")
        assert resp.status_code == 200
        assert b"Duplicate Files" in resp.data

    def test_problem_files_page_has_the_health_card(self, client):
        with patch("core.problem_files.count_problems", return_value={}), \
             patch("core.problem_files.list_problems", return_value=[]):
            body = client.get("/problem-files").data
        assert b"pf-health" in body
        assert b'data-source="integrity"' in body
        assert b'data-source="orphan"' in body

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

    def test_list_is_capped_exact_first(self, client):
        with patch("core.library_health.MAX_DUPLICATE_GROUPS", 3):
            found = {
                "exact": [_group(f"e{i}") for i in range(2)],
                "likely": [_group(f"l{i}", "likely") for i in range(4)],
            }
            with patch("core.library_health.find_duplicates", return_value=found):
                data = client.get("/api/problem-files/duplicates").get_json()
        assert [g["id"] for g in data["exact"]] == ["e0", "e1"]
        assert len(data["likely"]) == 1
        assert data["total_groups"] == 6 and data["truncated"] is True

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


class TestInfoModalIsAvailable:
    """Both pages let the user open the ComicInfo viewer on a file to verify it."""

    def test_duplicates_page_ships_the_compare_viewer(self, client):
        """The duplicates page compares copies read-only; it no longer ships the editor."""
        body = client.get("/problem-files/duplicates").data
        assert b'id="cbzDupeModal"' in body
        assert b"clu-cbz-dupe.js" in body
        assert b'id="cbzInfoModal"' not in body
        assert b"clu-cbz-info.js" not in body

    def test_problem_files_page_ships_the_viewer(self, client):
        with patch("core.problem_files.count_problems", return_value={}), \
             patch("core.problem_files.list_problems", return_value=[]):
            body = client.get("/problem-files").data
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

    def test_duplicates_opens_the_compare_viewer(self):
        src = self._js("library_duplicates.js")
        assert "=== 'info'" in src and "=== 'compare'" in src
        assert "CLU.showCBZDupe(" in src
        assert "CLU.showCBZInfo(" not in src
        # "Keep this one" hands back to the page's own confirmation, after the
        # viewer has closed so the two modals never stack.
        assert "onKeep" in src and "confirmResolve(gi)" in src
        assert "hidden.bs.modal" in src


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
