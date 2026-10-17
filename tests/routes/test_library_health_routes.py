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
        with patch("core.library_health.dismiss_group", return_value=(True, None)) as dismiss:
            resp = client.post("/api/problem-files/duplicates/dismiss",
                               json={"id": "g", "dismissed": False})
        assert resp.status_code == 200
        dismiss.assert_called_once_with("g", dismissed=False)

    def test_dismiss_unknown_group_is_a_400(self, client):
        with patch("core.library_health.dismiss_group", return_value=(False, "gone")):
            resp = client.post("/api/problem-files/duplicates/dismiss", json={"id": "g"})
        assert resp.status_code == 400


class TestInfoModalIsAvailable:
    """Both pages let the user open the ComicInfo viewer on a file to verify it."""

    def test_duplicates_page_ships_the_viewer(self, client):
        body = client.get("/problem-files/duplicates").data
        assert b'id="cbzInfoModal"' in body
        assert b"clu-cbz-info.js" in body

    def test_problem_files_page_ships_the_viewer(self, client):
        with patch("core.problem_files.count_problems", return_value={}), \
             patch("core.problem_files.list_problems", return_value=[]):
            body = client.get("/problem-files").data
        assert b'id="cbzInfoModal"' in body
        assert b"clu-cbz-info.js" in body

    @pytest.mark.parametrize("script,needle", [
        ("problem_files.js", "case 'info':"),
        ("library_duplicates.js", "'info'"),
    ])
    def test_scripts_open_the_viewer_through_the_shared_helper(self, script, needle):
        """Wired structurally: a button with no handler fails silently."""
        import io
        import os

        path = os.path.join(os.path.dirname(__file__), "..", "..", "static", "js", script)
        src = io.open(path, encoding="utf-8").read()
        assert needle in src
        assert "CLU.showCBZInfo(" in src
        # Edits rewrite the archive, so the list is re-read when the modal closes.
        assert "onMetadataSaved" in src and "hidden.bs.modal" in src
