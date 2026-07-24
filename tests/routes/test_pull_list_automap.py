"""Tests for the Pull List auto-map routes in routes/series.py."""
from unittest.mock import patch


class TestScanRoute:
    def test_scan_starts_job(self, client):
        with patch("models.library_automap.start_scan_job", return_value="abc123") as start:
            resp = client.post("/api/pull-list/scan")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["op_id"] == "abc123"
        start.assert_called_once()


class TestScanStatusRoute:
    def test_unknown_job_returns_404(self, client):
        with patch("models.library_automap.get_scan_job", return_value=None):
            resp = client.get("/api/pull-list/scan/status?op_id=nope")
        assert resp.status_code == 404
        assert resp.get_json()["success"] is False

    def test_running_job(self, client):
        job = {"status": "running", "current": 3, "total": 10, "detail": "Batman"}
        with patch("models.library_automap.get_scan_job", return_value=job):
            resp = client.get("/api/pull-list/scan/status?op_id=x")
        data = resp.get_json()
        assert data["status"] == "running"
        assert data["current"] == 3
        assert "result" not in data

    def test_done_job_returns_result(self, client):
        job = {
            "status": "done", "current": 10, "total": 10, "detail": "",
            "result": {"applied": 4, "review": [], "skipped": [], "errors": [],
                       "total_candidates": 4},
        }
        with patch("models.library_automap.get_scan_job", return_value=job):
            resp = client.get("/api/pull-list/scan/status?op_id=x")
        data = resp.get_json()
        assert data["status"] == "done"
        assert data["result"]["applied"] == 4

    def test_done_job_passes_through_errors(self, client):
        # Errored folders (issue #436) ride along in the done result so the UI
        # can report them without failing the whole scan.
        job = {
            "status": "done", "current": 4, "total": 4, "detail": "",
            "result": {
                "applied": 3, "review": [], "skipped": [], "total_candidates": 4,
                "errors": [{"folder": "/data/Broken", "reason": "corrupt sidecar"}],
            },
        }
        with patch("models.library_automap.get_scan_job", return_value=job):
            resp = client.get("/api/pull-list/scan/status?op_id=x")
        data = resp.get_json()
        assert data["status"] == "done"
        assert data["result"]["errors"][0]["folder"] == "/data/Broken"

    def test_error_job_returns_error(self, client):
        job = {"status": "error", "current": 0, "total": 0, "detail": "", "error": "boom"}
        with patch("models.library_automap.get_scan_job", return_value=job):
            resp = client.get("/api/pull-list/scan/status?op_id=x")
        data = resp.get_json()
        assert data["status"] == "error"
        assert data["error"] == "boom"


class TestApplyRoute:
    def test_no_items_is_400(self, client):
        resp = client.post("/api/pull-list/apply", json={"items": []})
        assert resp.status_code == 400

    def test_invalid_items_is_400(self, client):
        # items present but none have both folder and metron_id
        resp = client.post("/api/pull-list/apply", json={"items": [{"folder": "/x"}]})
        assert resp.status_code == 400

    def test_valid_items_applied(self, client):
        fake = {"applied": 1, "failed": []}
        with patch("models.library_automap.apply_and_sync", return_value=fake) as apply:
            resp = client.post(
                "/api/pull-list/apply",
                json={"items": [{"folder": "/data/Batman", "metron_id": 555}]},
            )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["applied"] == 1
        # Only cleaned fields are forwarded
        forwarded = apply.call_args[0][0]
        assert forwarded[0]["folder"] == "/data/Batman"
        assert forwarded[0]["metron_id"] == 555
