"""Tests for routes/downloads.py -- download management endpoints."""
import pytest
from unittest.mock import patch, MagicMock


# The five titles getcomics.org returns for "Teenage Mutant Ninja Turtles 3
# 2024", in the order the site returns them. Four of the five are spin-offs
# that carry the parent series name, an en dash, a subtitle and the same issue
# number and year -- indistinguishable to a substring heuristic, which is why
# the correct result used to rank fourth.
TMNT_TITLES = [
    "Teenage Mutant Ninja Turtles – Saturday Morning Adventures Vol. 3",
    "Teenage Mutant Ninja Turtles – Nightwatcher #3 (2024)",
    "Teenage Mutant Ninja Turtles #3 (2024)",
    "Teenage Mutant Ninja Turtles – The Last Ronin II – Re-Evolution #3 (2024)",
    "Teenage Mutant Ninja Turtles – Black, White, & Green #3 (2024)",
]
TMNT_RESULTS = [
    {"title": t, "link": f"https://getcomics.org/{i}", "image": ""}
    for i, t in enumerate(TMNT_TITLES)
]
TMNT_CORRECT = "Teenage Mutant Ninja Turtles #3 (2024)"
TMNT_QUERY = (
    "/api/getcomics/search?q=Teenage+Mutant+Ninja+Turtles+3+2024"
    "&series=Teenage+Mutant+Ninja+Turtles&issue=3&issue_year=2024"
)


class TestGetcomicsSearch:

    @patch("models.getcomics.search_getcomics", return_value=[
        {"title": "Batman #1", "url": "https://getcomics.org/batman-1"},
    ])
    def test_search(self, mock_search, client):
        resp = client.get("/api/getcomics/search?q=batman")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["results"]) == 1

    def test_empty_query(self, client):
        resp = client.get("/api/getcomics/search?q=")
        assert resp.status_code == 400

    @patch("models.getcomics.search_getcomics", side_effect=Exception("error"))
    def test_search_error(self, mock_search, client):
        resp = client.get("/api/getcomics/search?q=batman")
        assert resp.status_code == 500


class TestGetcomicsSearchScoring:
    """The search modal ranks by the same scorer the auto-download uses."""

    @patch("routes.downloads.get_series_alias_list", return_value=[])
    @patch("models.getcomics.search_getcomics", return_value=TMNT_RESULTS)
    def test_correct_issue_is_ranked_first(self, mock_search, mock_alias, client):
        """The regression: getcomics returns the wanted issue fourth of five."""
        data = client.get(TMNT_QUERY).get_json()
        assert data["results"][0]["title"] == TMNT_CORRECT
        assert data["results"][0]["decision"] == "ACCEPT"

    @patch("routes.downloads.get_series_alias_list", return_value=[])
    @patch("models.getcomics.search_getcomics", return_value=TMNT_RESULTS)
    def test_only_the_parent_series_is_accepted(self, mock_search, mock_alias, client):
        """Every en-dash spin-off is a different series, not a variant."""
        results = client.get(TMNT_QUERY).get_json()["results"]
        accepted = [r for r in results if r["decision"] == "ACCEPT"]
        assert [r["title"] for r in accepted] == [TMNT_CORRECT]
        spin_offs = [r for r in results if r["title"] != TMNT_CORRECT]
        assert all(r["decision"] == "REJECT" for r in spin_offs)

    @patch("routes.downloads.get_series_alias_list", return_value=[])
    @patch("models.getcomics.search_getcomics", return_value=TMNT_RESULTS)
    def test_scores_are_attached_and_sorted_desc(self, mock_search, mock_alias, client):
        data = client.get(TMNT_QUERY).get_json()
        assert data["scored"] is True
        scores = [r["score"] for r in data["results"]]
        assert len(scores) == len(TMNT_TITLES)
        assert scores == sorted(scores, reverse=True)

    @patch("models.getcomics.search_getcomics", return_value=TMNT_RESULTS)
    def test_q_only_stays_unscored_and_unordered(self, mock_search, client):
        """A bare ``q`` keeps the pre-scoring response shape."""
        data = client.get("/api/getcomics/search?q=Teenage+Mutant+Ninja+Turtles+3+2024").get_json()
        assert data["scored"] is False
        assert [r["title"] for r in data["results"]] == TMNT_TITLES
        assert "score" not in data["results"][0]
        assert "decision" not in data["results"][0]

    @patch("routes.downloads.get_series_alias_list", return_value=[])
    @patch("models.getcomics.search_getcomics", return_value=TMNT_RESULTS)
    def test_query_that_diverges_from_series_is_unscored(self, mock_search, mock_alias, client):
        """A retyped query leaves the page context stale; don't score against it."""
        data = client.get(
            "/api/getcomics/search?q=Teenage+Mutant+Ninja+Turtles+3&series=Batman&issue=3"
        ).get_json()
        assert data["scored"] is False

    @patch("routes.downloads.get_series_alias_list", return_value=["TMNT"])
    @patch("models.getcomics.search_getcomics", return_value=TMNT_RESULTS)
    def test_alias_seeded_query_still_scores(self, mock_search, mock_alias, client):
        """series.html seeds the query with the alias, not the canonical name."""
        data = client.get(
            "/api/getcomics/search?q=TMNT+3+2024"
            "&series=Teenage+Mutant+Ninja+Turtles&issue=3&issue_year=2024"
        ).get_json()
        assert data["scored"] is True
        assert data["results"][0]["title"] == TMNT_CORRECT

    @pytest.mark.parametrize("raw,expected", [
        ("2024", 2024),
        ("2024-05-01", 2024),
        ("", None),
        ("not-a-year", None),
        ("1492", None),
    ])
    @patch("routes.downloads.get_series_alias_list", return_value=[])
    @patch("routes.downloads.score_getcomics_result", return_value=(95, False, True))
    @patch("models.getcomics.search_getcomics", return_value=TMNT_RESULTS[2:3])
    def test_issue_year_coercion(self, mock_search, mock_score, mock_alias,
                                 raw, expected, client):
        client.get(
            "/api/getcomics/search?q=Teenage+Mutant+Ninja+Turtles+3"
            f"&series=Teenage+Mutant+Ninja+Turtles&issue=3&issue_year={raw}"
        )
        assert mock_score.call_args.args[3] == expected


class TestGetcomicsDownload:

    def test_no_url(self, client):
        resp = client.post("/api/getcomics/download", json={})
        assert resp.status_code == 400

    @patch("api.download_queue")
    @patch("api.download_progress", {})
    @patch("models.getcomics.get_download_links", return_value={
        "pixeldrain": "https://pixeldrain.com/u/abc123",
    })
    @patch("core.config.config")
    def test_download_queued(self, mock_config, mock_links, mock_queue, client):
        mock_config.get.return_value = "pixeldrain,download_now,mega"
        resp = client.post("/api/getcomics/download",
                           json={"url": "https://getcomics.org/batman", "filename": "b.cbz"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "download_id" in data

    @patch("models.getcomics.get_download_links", return_value={})
    @patch("core.config.config")
    def test_no_download_link(self, mock_config, mock_links, client):
        mock_config.get.return_value = "pixeldrain"
        resp = client.post("/api/getcomics/download",
                           json={"url": "https://getcomics.org/x"})
        assert resp.status_code == 404


class TestGetcomicsDownloadStatus:
    """The UI polls this endpoint so it can surface a Cloudflare-blocked
    download with a manual link instead of silently failing."""

    def test_unknown_download_returns_404(self, client):
        resp = client.get("/api/getcomics/download-status/does-not-exist")
        assert resp.status_code == 404
        assert resp.get_json()["success"] is False

    @patch("api.download_progress", {
        "abc-123": {"status": "in_progress", "progress": 42, "error": None,
                    "manual_url": None, "filename": "b.cbz", "provider": "pixeldrain"},
    })
    def test_in_progress(self, client):
        resp = client.get("/api/getcomics/download-status/abc-123")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["status"] == "in_progress"
        assert data["progress"] == 42
        assert data["manual_url"] is None

    @patch("api.download_progress", {
        "cf-1": {
            "status": "error",
            "progress": -1,
            "error": "fs2.comicfiles.ru is protected by a Cloudflare challenge...",
            # The getcomics post page (where the user clicks download themselves),
            # NOT the resolved mirror URL or /dls/ link which 403 in a browser.
            "manual_url": "https://getcomics.org/comic/geiger-ground-zero-2",
            "filename": "Geiger.cbz",
            "provider": "getcomics",
        },
    })
    def test_error_surfaces_manual_url(self, client):
        resp = client.get("/api/getcomics/download-status/cf-1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["status"] == "error"
        assert data["manual_url"] == "https://getcomics.org/comic/geiger-ground-zero-2"


class TestSyncSchedule:

    @patch("core.database.get_sync_schedule", return_value=None)
    def test_get_schedule_default(self, mock_sched, client):
        resp = client.get("/api/get-sync-schedule")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["schedule"]["frequency"] == "disabled"

    @patch("core.database.get_sync_schedule", return_value={
        "frequency": "daily", "time": "03:00", "weekday": 0, "last_sync": None,
    })
    def test_get_schedule_configured(self, mock_sched, client):
        mock_app = MagicMock()
        mock_app.get_next_run_for_job.return_value = "2024-01-01 03:00"
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.get("/api/get-sync-schedule")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["schedule"]["frequency"] == "daily"

    @patch("core.database.save_sync_schedule", return_value=True)
    def test_save_schedule(self, mock_save, client):
        mock_app = MagicMock()
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.post("/api/save-sync-schedule",
                               json={"frequency": "daily", "time": "04:00"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_save_invalid_frequency(self, client):
        with patch.dict("sys.modules", {"app": MagicMock()}):
            resp = client.post("/api/save-sync-schedule",
                               json={"frequency": "hourly"})
        assert resp.status_code == 400


class TestGetcomicsSchedule:

    @patch("core.database.get_getcomics_schedule", return_value=None)
    def test_get_default(self, mock_sched, client):
        resp = client.get("/api/get-getcomics-schedule")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["schedule"]["frequency"] == "disabled"

    @patch("core.database.save_getcomics_schedule", return_value=True)
    def test_save_schedule(self, mock_save, client):
        mock_app = MagicMock()
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.post("/api/save-getcomics-schedule",
                               json={"frequency": "weekly", "time": "08:00",
                                     "weekday": 3})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_save_invalid_time(self, client):
        with patch.dict("sys.modules", {"app": MagicMock()}):
            resp = client.post("/api/save-getcomics-schedule",
                               json={"frequency": "daily", "time": "25:00"})
        assert resp.status_code == 400


class TestRunGetcomicsNow:

    def test_trigger_download(self, client):
        mock_app = MagicMock()
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.post("/api/run-getcomics-now")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True


class TestCheckSeriesMissing:
    """POST /api/series/<id>/check-missing — the series page's own
    "Check for Missing Issues" button. Same search pass as the scheduled sweep,
    narrowed to one series."""

    MAPPED = {"id": 100, "name": "Batman", "mapped_path": "/data/DC Comics/Batman"}

    def test_unknown_series_404(self, client):
        with patch("routes.downloads.get_series_by_id", return_value=None):
            resp = client.post("/api/series/999/check-missing")
        assert resp.status_code == 404
        assert resp.get_json()["success"] is False

    def test_unmapped_series_400(self, client):
        series = {"id": 100, "name": "Batman", "mapped_path": ""}
        with patch("routes.downloads.get_series_by_id", return_value=series):
            resp = client.post("/api/series/100/check-missing")
        assert resp.status_code == 400
        assert "Map this series" in resp.get_json()["error"]

    def test_missing_folder_400(self, client):
        """A vanished folder is reported, not silently searched to no effect."""
        with patch("routes.downloads.get_series_by_id", return_value=self.MAPPED),                 patch("routes.downloads.os.path.isdir", return_value=False):
            resp = client.post("/api/series/100/check-missing")
        assert resp.status_code == 400
        assert "not found" in resp.get_json()["error"]

    def test_starts_a_scoped_background_run(self, client):
        mock_app = MagicMock()
        with patch.dict("sys.modules", {"app": mock_app}),                 patch("routes.downloads.get_series_by_id", return_value=self.MAPPED),                 patch("routes.downloads.os.path.isdir", return_value=True),                 patch("routes.downloads.threading") as mock_threading:
            resp = client.post("/api/series/100/check-missing")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["op_id"]

        kwargs = mock_threading.Thread.call_args.kwargs
        # Scoped to this series, and reporting against the op the caller polls.
        assert kwargs["kwargs"] == {"only_series_id": 100, "op_id": data["op_id"]}
        assert kwargs["daemon"] is True
        mock_threading.Thread.return_value.start.assert_called_once()

    def test_registers_a_trackable_operation(self, client):
        import core.app_state as app_state

        with patch.dict("sys.modules", {"app": MagicMock()}),                 patch("routes.downloads.get_series_by_id", return_value=self.MAPPED),                 patch("routes.downloads.os.path.isdir", return_value=True),                 patch("routes.downloads.threading"):
            resp = client.post("/api/series/100/check-missing")

        op_id = resp.get_json()["op_id"]
        op = next(
            (o for o in app_state.get_active_operations() if o["id"] == op_id), None
        )
        assert op is not None
        assert op["op_type"] == "search"
        assert "Batman" in op["label"]
        app_state.complete_operation(op_id)


class TestOperationStatus:
    """GET /api/operation/<id> — the single-operation read the series page
    polls. Deliberately separate from /api/operations, which clears the
    notification queue base.html's poller owns."""

    def test_returns_a_running_operation(self, client):
        import core.app_state as app_state

        op_id = app_state.register_operation("search", "Checking Batman", total=3)
        app_state.update_operation(op_id, current=1, detail="Batman #2")
        try:
            resp = client.get(f"/api/operation/{op_id}")
        finally:
            app_state.complete_operation(op_id)

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["operation"]["status"] == "running"
        assert data["operation"]["detail"] == "Batman #2"

    def test_unknown_operation_is_null_not_an_error(self, client):
        """A finished op is pruned after a short TTL, so absent means done."""
        resp = client.get("/api/operation/does-not-exist")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["operation"] is None


class TestWeeklyPacksConfig:

    @patch("core.database.get_weekly_packs_config", return_value=None)
    def test_get_config_default(self, mock_config, client):
        resp = client.get("/api/get-weekly-packs-config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["config"]["enabled"] is False

    @patch("core.database.save_weekly_packs_config", return_value=True)
    def test_save_config(self, mock_save, client):
        mock_app = MagicMock()
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.post("/api/save-weekly-packs-config", json={
                "enabled": True,
                "format": "JPG",
                "publishers": ["DC", "Marvel"],
                "weekday": 2,
                "time": "10:00",
            })
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_invalid_format(self, client):
        with patch.dict("sys.modules", {"app": MagicMock()}):
            resp = client.post("/api/save-weekly-packs-config", json={
                "format": "PNG",
                "publishers": ["DC"],
            })
        assert resp.status_code == 400

    def test_invalid_publisher(self, client):
        with patch.dict("sys.modules", {"app": MagicMock()}):
            resp = client.post("/api/save-weekly-packs-config", json={
                "format": "JPG",
                "publishers": ["FakePublisher"],
            })
        assert resp.status_code == 400


class TestWeeklyPacksHistory:

    @patch("core.database.get_weekly_packs_history", return_value=[
        {"pack_date": "2024-01-01", "publisher": "DC", "status": "completed"},
    ])
    def test_get_history(self, mock_hist, client):
        resp = client.get("/api/weekly-packs-history")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["history"]) == 1

    @patch("core.database.get_weekly_packs_history", return_value=[
        {"pack_date": "2024-01-01", "publisher": "DC", "status": "queued",
         "download_id": "dl-1"},
    ])
    def test_live_status_overrides_stored_status(self, mock_hist, client):
        """A row stuck at 'queued' must report what the Status page shows."""
        import sys

        sys.modules["api"].download_progress = {"dl-1": {"status": "complete"}}
        try:
            resp = client.get("/api/weekly-packs-history")
        finally:
            sys.modules["api"].download_progress = {}

        assert resp.status_code == 200
        assert resp.get_json()["history"][0]["status"] == "completed"

    @patch("core.database.get_weekly_packs_history", return_value=[
        {"pack_date": "2024-01-01", "publisher": "DC", "status": "downloading",
         "download_id": "dl-1"},
    ])
    def test_in_flight_row_carries_progress(self, mock_hist, client):
        import sys

        sys.modules["api"].download_progress = {
            "dl-1": {"status": "in_progress", "progress": 37}
        }
        try:
            resp = client.get("/api/weekly-packs-history")
        finally:
            sys.modules["api"].download_progress = {}

        item = resp.get_json()["history"][0]
        assert item["status"] == "downloading"
        assert item["progress"] == 37

    @patch("core.download_utils.reconcile_weekly_pack_history",
           side_effect=Exception("db down"))
    @patch("core.database.get_weekly_packs_history", return_value=[])
    def test_reconcile_failure_does_not_break_the_page(self, mock_hist, mock_rec, client):
        resp = client.get("/api/weekly-packs-history")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True


class TestRunWeeklyPacksNow:

    def test_trigger(self, client):
        mock_app = MagicMock()
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.post("/api/run-weekly-packs-now")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True


class TestCheckWeeklyPackStatus:

    @patch("models.getcomics.find_first_actionable_weekly_pack")
    @patch("core.database.get_weekly_packs_config")
    def test_start_date_mode_reports_first_actionable(self, mock_config, mock_find, client):
        mock_config.return_value = {
            "start_date": "2026-01-01",
            "publishers": ["DC", "Marvel"],
            "format": "JPG",
        }
        mock_find.return_value = {
            "pack_date": "2026.01.14",
            "pack_url": "https://getcomics.org/other-comics/2026-01-14-weekly-pack/",
            "status": "available",
        }

        resp = client.get("/api/check-weekly-pack-status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["found"] is True
        assert data["pack_date"] == "2026.01.14"
        assert data["links_available"] is True
        assert data["start_date"] == "2026-01-01"
        # start_date drives the check, not the homepage.
        args = mock_find.call_args[0]
        assert args[0] == "2026-01-01"

    @patch("models.getcomics.find_first_actionable_weekly_pack", return_value=None)
    @patch("core.database.get_weekly_packs_config")
    def test_start_date_mode_all_downloaded(self, mock_config, mock_find, client):
        mock_config.return_value = {
            "start_date": "2026-01-01",
            "publishers": ["DC"],
            "format": "JPG",
        }

        resp = client.get("/api/check-weekly-pack-status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["found"] is False
        assert data["start_date"] == "2026-01-01"

    @patch("models.getcomics.check_weekly_pack_availability", return_value=True)
    @patch("models.getcomics.find_latest_weekly_pack_url",
           return_value=("https://getcomics.org/other-comics/2026-02-04-weekly-pack/", "2026.02.04"))
    @patch("core.database.get_weekly_packs_config", return_value={"start_date": None})
    def test_no_start_date_uses_homepage(self, mock_config, mock_latest, mock_avail, client):
        resp = client.get("/api/check-weekly-pack-status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["found"] is True
        assert data["pack_date"] == "2026.02.04"
        assert data["links_available"] is True
        assert "start_date" not in data
        mock_latest.assert_called_once()
