"""Routes behind the Status page's Scheduled Runs tab (routes/download_runs.py)."""

import csv
import io
import json

import pytest

from core import download_runs as dr
from core.auth import required_role_for_request
from core.database import create_user


@pytest.fixture
def run(app):
    """A finished GetComics run: one downloaded, one failed, one not found."""
    rec = dr.start_run(dr.KIND_GETCOMICS)
    rec.item("Batman", "1", dr.OUTCOME_QUEUED, series_id=1, source="mapped_series",
             provider="pixeldrain", download_id="ok", detail="Batman 001.cbz")
    rec.item("Batman", "2", dr.OUTCOME_QUEUED, series_id=1, source="mapped_series",
             provider="pixeldrain", download_id="bad")
    rec.item("Batman", "3", dr.OUTCOME_NO_MATCH, series_id=1, source="mapped_series",
             detail="4 result(s), none accepted")
    rec.count(dr.COUNT_OWNED, 12)
    rec.finish(dr.STATUS_OK)
    dr.set_download_outcome("ok", "complete")
    dr.set_download_outcome("bad", "failed", detail="All mirrors failed")
    return rec.run_id


class TestList:
    def test_lists_runs_with_tallies(self, client, run):
        resp = client.get("/api/download-runs")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        (got,) = data["runs"]
        assert got["id"] == run
        assert got["tally"]["downloaded"] == 1
        assert got["tally"]["failed"] == 1
        assert got["tally"]["not_found"] == 1
        assert got["tally"]["skipped"] == 12
        assert got["started_at_display"]

    def test_empty_history_is_an_empty_list(self, client):
        data = client.get("/api/download-runs").get_json()
        assert data == {"success": True, "runs": []}

    def test_filters_by_kind(self, client, run):
        data = client.get("/api/download-runs?kind=weekly_packs").get_json()
        assert data["runs"] == []

    def test_rejects_unknown_kind(self, client):
        assert client.get("/api/download-runs?kind=nope").status_code == 400

    def test_rejects_bad_limit(self, client):
        assert client.get("/api/download-runs?limit=lots").status_code == 400

    def test_read_failure_is_500_not_empty(self, client, monkeypatch):
        monkeypatch.setattr(dr, "list_runs", lambda **kw: None)
        assert client.get("/api/download-runs").status_code == 500


class TestDetail:
    def test_returns_items(self, client, run):
        data = client.get(f"/api/download-runs/{run}").get_json()
        assert [i["issue_number"] for i in data["run"]["items"]] == ["1", "2", "3"]

    def test_still_missing_filter(self, client, run):
        data = client.get(f"/api/download-runs/{run}?filter=missing").get_json()
        assert [i["issue_number"] for i in data["run"]["items"]] == ["2", "3"]
        assert data["run"]["items"][0]["detail"] == "All mirrors failed"

    def test_unknown_filter_is_400(self, client, run):
        assert client.get(f"/api/download-runs/{run}?filter=bogus").status_code == 400

    def test_unknown_run_is_404(self, client):
        assert client.get("/api/download-runs/424242").status_code == 404


class TestExport:
    def test_json_attachment(self, client, run):
        resp = client.get(f"/api/download-runs/{run}/export?format=json")
        assert resp.status_code == 200
        assert resp.mimetype == "application/json"
        assert "attachment" in resp.headers["Content-Disposition"]
        body = json.loads(resp.data)
        assert len(body["items"]) == 3

    def test_csv_attachment(self, client, run):
        resp = client.get(f"/api/download-runs/{run}/export?format=csv&filter=missing")
        assert resp.status_code == 200
        assert resp.mimetype == "text/csv"
        assert f"run-{run}.csv" in resp.headers["Content-Disposition"]
        rows = list(csv.DictReader(io.StringIO(resp.get_data(as_text=True))))
        assert [r["issue_number"] for r in rows] == ["2", "3"]
        assert rows[0]["download_status"] == "failed"

    def test_unknown_format_is_400(self, client, run):
        assert client.get(f"/api/download-runs/{run}/export?format=xml").status_code == 400

    def test_unknown_run_is_404(self, client):
        assert client.get("/api/download-runs/424242/export").status_code == 404


class TestAccess:
    @pytest.mark.parametrize("path", [
        "/api/download-runs",
        "/api/download-runs/1",
        "/api/download-runs/1/export",
    ])
    def test_clerk_level(self, app, path):
        """The same level as the /status page that shows them."""
        with app.test_request_context(path):
            assert required_role_for_request() == "clerk"

    def test_reader_is_denied(self, client, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("owner", password="ownerpass", role="owner")
        create_user("reader", password="readerpass", role="reader")
        client.post("/login", data={"username": "reader", "password": "readerpass"})
        assert client.get("/api/download-runs").status_code == 403


class TestStatusTemplate:
    """/status is served by api.py, which tests cannot import; render the
    template directly so a broken url_for or Jinja error is caught here."""

    def test_renders_both_tabs(self, app):
        from flask import render_template

        with app.test_request_context("/status"):
            html = render_template("status.html")
        assert 'id="runs-tab"' in html
        assert 'id="downloads-table"' in html
        assert "/api/download-runs" in html
