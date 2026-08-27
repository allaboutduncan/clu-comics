"""Tests for routes/series.py -- series management endpoints."""
import io
import json
import os
import pytest
from unittest.mock import patch, MagicMock


class TestSeriesSearch:

    def test_empty_query(self, client):
        resp = client.get("/api/series/search?q=")
        assert resp.status_code == 400

    def test_no_metron_creds(self, client, app):
        # Config already has empty METRON_USERNAME/PASSWORD from conftest
        resp = client.get("/api/series/search?q=batman")
        assert resp.status_code == 400

    @patch("routes.series.metron")
    def test_search_success(self, mock_metron, client, app):
        app.config["METRON_USERNAME"] = "user"
        app.config["METRON_PASSWORD"] = "pass"

        mock_series = MagicMock()
        mock_series.id = 100
        mock_series.display_name = "Batman"
        mock_series.name = "Batman"
        mock_series.volume = 2020
        mock_series.year_began = 2020
        mock_series.issue_count = 50
        mock_series.status = "Ongoing"
        mock_series.publisher = MagicMock(name="DC Comics")
        mock_series.publisher.name = "DC Comics"

        mock_api = MagicMock()
        mock_api.series_list.return_value = [mock_series]
        mock_metron.get_flask_api.return_value = mock_api
        mock_metron.is_connection_error.return_value = False

        mock_app = MagicMock()
        mock_app.generate_series_slug.return_value = "batman-v2020-100"
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.get("/api/series/search?q=batman")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["count"] == 1
        # Not subscribed (no mapped folder) -> flagged False for row highlighting
        assert data["series"][0]["subscribed"] is False

    @patch("core.database.get_mapped_series_ids")
    @patch("routes.series.metron")
    def test_search_flags_subscribed_series(
        self, mock_metron, mock_mapped_ids, client, app
    ):
        app.config["METRON_USERNAME"] = "user"
        app.config["METRON_PASSWORD"] = "pass"

        mock_series = MagicMock()
        mock_series.id = 100
        mock_series.display_name = "Batman"
        mock_series.name = "Batman"
        mock_series.volume = 2020
        mock_series.year_began = 2020
        mock_series.issue_count = 50
        mock_series.status = "Ongoing"
        mock_series.publisher = MagicMock()
        mock_series.publisher.name = "DC Comics"

        mock_api = MagicMock()
        mock_api.series_list.return_value = [mock_series]
        mock_metron.get_flask_api.return_value = mock_api
        mock_metron.is_connection_error.return_value = False

        # Series id 100 has a mapped folder -> subscribed
        mock_mapped_ids.return_value = {100}

        mock_app = MagicMock()
        mock_app.generate_series_slug.return_value = "batman-v2020-100"
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.get("/api/series/search?q=batman")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["series"][0]["subscribed"] is True

    @patch("routes.series.metron")
    def test_search_with_publisher_id(self, mock_metron, client, app):
        app.config["METRON_USERNAME"] = "user"
        app.config["METRON_PASSWORD"] = "pass"

        mock_api = MagicMock()
        mock_api.series_list.return_value = []
        mock_metron.get_flask_api.return_value = mock_api
        mock_metron.is_connection_error.return_value = False

        mock_app = MagicMock()
        mock_app.generate_series_slug.return_value = "x"
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.get("/api/series/search?q=spider-man&publisher_id=20")

        assert resp.status_code == 200
        mock_api.series_list.assert_called_once_with(
            {"name": "spider-man", "publisher_id": 20}
        )

    @patch("routes.series.metron")
    def test_search_ignores_non_numeric_publisher_id(self, mock_metron, client, app):
        app.config["METRON_USERNAME"] = "user"
        app.config["METRON_PASSWORD"] = "pass"

        mock_api = MagicMock()
        mock_api.series_list.return_value = []
        mock_metron.get_flask_api.return_value = mock_api
        mock_metron.is_connection_error.return_value = False

        mock_app = MagicMock()
        mock_app.generate_series_slug.return_value = "x"
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.get("/api/series/search?q=batman&publisher_id=abc")

        assert resp.status_code == 200
        mock_api.series_list.assert_called_once_with({"name": "batman"})


class TestMapSeries:

    @patch("core.database.save_publisher")
    @patch("core.database.save_series_mapping", return_value=True)
    def test_map_success(self, mock_save, mock_pub, client):
        resp = client.post("/api/series/100/map", json={
            "mapped_path": "/data/DC/Batman",
            "series": {
                "id": 100, "name": "Batman",
                "publisher": {"id": 10, "name": "DC Comics"},
            },
        })
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_no_data(self, client):
        resp = client.post("/api/series/100/map",
                           content_type="application/json",
                           data="{}")
        assert resp.status_code == 400

    def test_missing_fields(self, client):
        resp = client.post("/api/series/100/map", json={"mapped_path": "/x"})
        assert resp.status_code == 400

    @patch("routes.series.metron")
    @patch("core.database.save_publisher")
    @patch("core.database.save_series_mapping", return_value=True)
    def test_map_creates_cvinfo_and_series_json(
        self, mock_save, mock_pub, mock_metron, client, tmp_path
    ):
        # Real metron.create_cvinfo_file so the file is actually written
        import models.metron as metron_mod
        mock_metron.create_cvinfo_file.side_effect = metron_mod.create_cvinfo_file
        mock_metron.get_flask_api.return_value = None

        resp = client.post("/api/series/100/map", json={
            "mapped_path": str(tmp_path),
            "series": {
                "id": 100, "name": "Batman", "cv_id": 167796,
                "year_began": 2016,
                "publisher": {"id": 10, "name": "DC Comics"},
            },
        })
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

        cvinfo = tmp_path / "cvinfo"
        assert cvinfo.is_file()
        contents = cvinfo.read_text(encoding="utf-8")
        assert "4050-167796" in contents
        assert "series_id: 100" in contents
        assert "publisher_name: DC Comics" in contents
        assert "start_year: 2016" in contents

        assert (tmp_path / "series.json").is_file()


class TestGetSeriesMapping:

    @patch("core.database.get_series_mapping", return_value="/data/DC/Batman")
    def test_get_mapping(self, mock_get, client):
        resp = client.get("/api/series/100/mapping")
        assert resp.status_code == 200
        assert resp.get_json()["mapped_path"] == "/data/DC/Batman"

    @patch("core.database.get_series_mapping", return_value=None)
    def test_no_mapping(self, mock_get, client):
        resp = client.get("/api/series/100/mapping")
        assert resp.get_json()["mapped_path"] is None


class TestSubscribeSeries:
    """The subscribe route must sanitize the user-supplied path before
    os.makedirs so filesystem-hostile characters never reach disk."""

    @patch("models.series_json.write_series_json")
    @patch("models.getcomics.prepopulate_series_index")
    @patch("core.database.save_series_mapping", return_value=True)
    @patch("routes.series.metron")
    @patch("routes.series.get_series_by_id",
            return_value={"id": 100, "name": "Batman"})
    @patch("routes.series.os.makedirs")
    def test_subscribe_sanitizes_path(
        self, mock_makedirs, mock_get, mock_metron, mock_save,
        mock_prepop, mock_write, client,
    ):
        mock_metron.create_cvinfo_file.return_value = True
        mock_metron.get_flask_api.return_value = None

        resp = client.post("/api/series/100/subscribe", json={
            "path": '/data/DC Comics/Bat:man & Robin? <v2>/v2016',
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True

        # Baseline chars stripped, ':' -> ' -', separators preserved.
        expected = "/data/DC Comics/Bat -man Robin v2/v2016"
        assert data["path"] == expected
        made = mock_makedirs.call_args[0][0]
        assert made == expected
        for ch in '\\*?"<>|&$;':
            assert ch not in made

    @patch("routes.series.os.makedirs")
    def test_subscribe_rejects_empty_after_sanitize(self, mock_makedirs, client):
        # A path made up entirely of illegal chars collapses to empty -> 400.
        resp = client.post("/api/series/100/subscribe", json={"path": '/<>|?*/'})
        assert resp.status_code == 400
        mock_makedirs.assert_not_called()


class TestDeleteSeriesMapping:

    @patch("core.database.remove_series_mapping", return_value=True)
    def test_delete_success(self, mock_rm, client):
        resp = client.delete("/api/series/100/mapping")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    @patch("core.database.remove_series_mapping", return_value=False)
    def test_delete_failure(self, mock_rm, client):
        resp = client.delete("/api/series/100/mapping")
        assert resp.status_code == 500


class TestDeleteIssueFile:
    """POST /api/series/<id>/issue/<num>/delete-file — trash it, want it again."""

    @pytest.fixture
    def series_dir(self, tmp_path):
        d = tmp_path / "data" / "Batman"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def test_missing_path(self, client):
        resp = client.post("/api/series/100/issue/1/delete-file", json={})
        assert resp.status_code == 400

    @patch("core.database.get_series_mapping", return_value=None)
    def test_series_not_mapped(self, mock_map, client):
        resp = client.post("/api/series/100/issue/1/delete-file",
                             json={"path": "Batman 001.cbz"})
        assert resp.status_code == 404

    @patch("helpers.trash.move_to_trash")
    def test_rejects_path_outside_mapped_folder(self, mock_trash, client,
                                                series_dir, tmp_path):
        outside = tmp_path / "data" / "secret.cbz"
        outside.write_bytes(b"x")

        with patch("core.database.get_series_mapping", return_value=str(series_dir)):
            resp = client.post("/api/series/100/issue/1/delete-file",
                                 json={"path": str(outside)})

        assert resp.status_code == 403
        mock_trash.assert_not_called()
        assert outside.exists()

    def test_missing_file(self, client, series_dir):
        with patch("core.database.get_series_mapping", return_value=str(series_dir)):
            resp = client.post("/api/series/100/issue/1/delete-file",
                                 json={"path": "Batman 001.cbz"})
        assert resp.status_code == 404

    def test_rejects_directory(self, client, series_dir):
        (series_dir / "extras").mkdir()
        with patch("core.database.get_series_mapping", return_value=str(series_dir)):
            resp = client.post("/api/series/100/issue/1/delete-file",
                                 json={"path": "extras"})
        assert resp.status_code == 400

    @patch("helpers.collection.rebuild_wanted_for_series", return_value=3)
    @patch("core.database.clear_manual_status", return_value=True)
    def test_delete_marks_issue_wanted(self, mock_clear, mock_rebuild,
                                       client, series_dir):
        target = series_dir / "Batman 001 (2020).cbz"
        target.write_bytes(b"cbz")

        def _fake_trash(path):
            os.remove(path)
            return {"trashed": True, "path": path}

        with patch("core.database.get_series_mapping", return_value=str(series_dir)), \
                patch("helpers.trash.move_to_trash", side_effect=_fake_trash):
            resp = client.post("/api/series/100/issue/1/delete-file",
                                 json={"path": str(target).replace("\\", "/")})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["trashed"] is True
        assert data["wanted_count"] == 3
        assert not target.exists()

        # A stale "owned" mark would keep the issue off the wanted list.
        mock_clear.assert_called_once_with(100, "1")
        mock_rebuild.assert_called_once_with(100)


class TestManualStatus:

    @patch("core.database.get_manual_status_for_series", return_value={"1": {"status": "owned"}})
    def test_get_manual_status(self, mock_get, client):
        resp = client.get("/api/series/100/manual-status")
        data = resp.get_json()
        assert data["success"] is True
        assert "1" in data["manual_status"]

    @patch("core.database.set_manual_status", return_value=True)
    def test_set_status(self, mock_set, client):
        resp = client.post("/api/series/100/issue/1/manual-status",
                           json={"status": "owned", "notes": "hardcover"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_invalid_status(self, client):
        resp = client.post("/api/series/100/issue/1/manual-status",
                           json={"status": "invalid"})
        assert resp.status_code == 400

    @patch("core.database.clear_manual_status", return_value=True)
    def test_delete_status(self, mock_clear, client):
        resp = client.delete("/api/series/100/issue/1/manual-status")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True


class TestBulkManualStatus:

    @patch("core.database.bulk_set_manual_status", return_value=3)
    def test_bulk_set(self, mock_bulk, client):
        resp = client.post("/api/series/100/bulk-manual-status", json={
            "issue_numbers": ["1", "2", "3"],
            "status": "owned",
        })
        assert resp.status_code == 200
        assert resp.get_json()["count"] == 3

    def test_empty_issues(self, client):
        resp = client.post("/api/series/100/bulk-manual-status", json={
            "issue_numbers": [],
            "status": "owned",
        })
        assert resp.status_code == 400

    @patch("core.database.bulk_clear_manual_status", return_value=2)
    def test_bulk_delete(self, mock_clear, client):
        resp = client.delete("/api/series/100/bulk-manual-status", json={
            "issue_numbers": ["1", "2"],
        })
        assert resp.status_code == 200
        assert resp.get_json()["count"] == 2


class TestWantedApi:

    @patch("routes.series.get_wanted_issues", return_value=[
        {"issue_id": 1, "series_name": "Batman"},
    ])
    def test_get_wanted(self, mock_wanted, client):
        resp = client.get("/api/wanted")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["count"] == 1


class TestRefreshWanted:

    @patch("routes.series.app_state")
    def test_refresh_started(self, mock_state, client):
        mock_state.wanted_refresh_in_progress = False
        mock_app = MagicMock()
        with patch.dict("sys.modules", {"app": mock_app}):
            resp = client.post("/api/refresh-wanted")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    @patch("routes.series.app_state")
    def test_already_refreshing(self, mock_state, client):
        mock_state.wanted_refresh_in_progress = True
        resp = client.post("/api/refresh-wanted")
        assert resp.status_code == 200
        assert "already" in resp.get_json()["message"].lower()


class TestWantedStatus:

    @patch("routes.series.app_state")
    @patch("core.database.get_wanted_cache_age", return_value="5 minutes")
    @patch("core.database.get_cached_wanted_issues", return_value=[{"id": 1}])
    def test_wanted_status(self, mock_cached, mock_age, mock_state, client):
        mock_state.wanted_refresh_in_progress = False
        resp = client.get("/api/wanted-status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["count"] == 1
        assert data["refreshing"] is False


class TestLibrariesApi:

    @patch("core.database.get_libraries", return_value=[
        {"id": 1, "name": "Comics", "path": "/data/comics", "enabled": True},
    ])
    def test_get_libraries(self, mock_libs, client):
        resp = client.get("/api/libraries")
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["libraries"]) == 1

    @patch("core.database.add_library", return_value=1)
    def test_add_library(self, mock_add, client, tmp_path):
        lib_path = str(tmp_path / "comics")
        import os
        os.makedirs(lib_path)

        mock_app = MagicMock()
        with patch.dict("sys.modules", {"app": mock_app}), \
             patch("core.database.sync_file_index_incremental"), \
             patch("core.database.invalidate_browse_cache"):
            resp = client.post("/api/libraries", json={
                "name": "Comics", "path": lib_path,
            })
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_add_library_missing_name(self, client):
        with patch.dict("sys.modules", {"app": MagicMock()}):
            resp = client.post("/api/libraries", json={"path": "/tmp"})
        assert resp.status_code == 400

    @patch("core.database.get_library_by_id", return_value={"id": 1, "name": "Old"})
    @patch("core.database.update_library", return_value=True)
    def test_update_library(self, mock_update, mock_get, client):
        resp = client.put("/api/libraries/1", json={"name": "New Name"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    @patch("core.database.get_library_by_id", return_value=None)
    def test_update_nonexistent(self, mock_get, client):
        resp = client.put("/api/libraries/999", json={"name": "X"})
        assert resp.status_code == 404

    @patch("core.database.get_library_by_id", return_value={"id": 1, "name": "Comics"})
    @patch("core.database.delete_library", return_value=True)
    def test_delete_library(self, mock_del, mock_get, client):
        resp = client.delete("/api/libraries/1")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True


class TestPublishersApi:

    @patch("core.database.get_all_publishers", return_value=[
        {"id": 10, "name": "DC Comics"},
    ])
    def test_get_publishers(self, mock_get, client):
        resp = client.get("/api/publishers")
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["publishers"]) == 1

    @patch("core.database.get_db_connection")
    @patch("core.database.save_publisher", return_value=True)
    def test_add_publisher(self, mock_save, mock_conn, client):
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = [None]
        mock_db = MagicMock()
        mock_db.cursor.return_value = mock_cursor
        mock_conn.return_value = mock_db

        resp = client.post("/api/publishers", json={"name": "Test Pub"})
        assert resp.status_code == 200

    def test_add_publisher_no_name(self, client):
        resp = client.post("/api/publishers", json={})
        assert resp.status_code == 400

    @patch("core.database.delete_publisher", return_value=True)
    def test_delete_publisher(self, mock_del, client):
        resp = client.delete("/api/publishers/10")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    @patch("core.database.delete_publisher", return_value=True)
    def test_delete_negative_publisher(self, mock_del, client):
        resp = client.delete("/api/publishers/-1")
        assert resp.status_code == 200

    @patch("routes.series.metron")
    @patch("core.database.save_publisher", return_value=True)
    def test_sync_publishers_from_metron(self, mock_save, mock_metron, client, app):
        app.config["METRON_USERNAME"] = "user"
        app.config["METRON_PASSWORD"] = "pass"

        pub1 = MagicMock()
        pub1.id = 1
        pub1.name = "DC Comics"
        pub2 = MagicMock()
        pub2.id = 2
        pub2.name = "Marvel"

        mock_api = MagicMock()
        mock_api.publishers_list.return_value = [pub1, pub2]
        mock_metron.get_flask_api.return_value = mock_api
        mock_metron.is_connection_error.return_value = False

        resp = client.post("/api/publishers/sync")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["count"] == 2
        assert mock_save.call_count == 2

    @patch("routes.series.metron")
    def test_sync_publishers_no_metron(self, mock_metron, client):
        mock_metron.get_flask_api.return_value = None
        resp = client.post("/api/publishers/sync")
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False


class TestSeriesSubscription:

    @patch("core.database.set_series_subscription", return_value=True)
    def test_toggle_subscription_enable(self, mock_set, client):
        resp = client.post("/api/series/100/subscription",
                           json={"enabled": True})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        mock_set.assert_called_once_with(100, True)

    @patch("core.database.set_series_subscription", return_value=True)
    def test_toggle_subscription_disable(self, mock_set, client):
        resp = client.post("/api/series/100/subscription",
                           json={"enabled": False})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        mock_set.assert_called_once_with(100, False)


class TestSeriesMonitored:

    @patch("core.database.set_series_monitored", return_value=True)
    def test_toggle_monitored_enable(self, mock_set, client):
        resp = client.post("/api/series/100/monitored", json={"enabled": True})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        mock_set.assert_called_once_with(100, True)

    @patch("core.database.set_series_monitored", return_value=True)
    def test_toggle_monitored_disable(self, mock_set, client):
        resp = client.post("/api/series/100/monitored", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        mock_set.assert_called_once_with(100, False)


class TestSeriesBulkMonitored:

    @patch("core.database.set_series_monitored_bulk", return_value=3)
    def test_bulk_enable(self, mock_set, client):
        resp = client.post(
            "/api/series/bulk-monitored",
            json={"series_ids": [1, 2, 3], "enabled": True},
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["updated"] == 3
        mock_set.assert_called_once_with([1, 2, 3], True)

    @patch("core.database.set_series_monitored_bulk", return_value=2)
    def test_bulk_disable_coerces_ids(self, mock_set, client):
        resp = client.post(
            "/api/series/bulk-monitored",
            json={"series_ids": ["4", "5", "bad"], "enabled": False},
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        # Non-int ids are dropped, the rest coerced to ints.
        mock_set.assert_called_once_with([4, 5], False)

    @patch("core.database.set_series_monitored_bulk", return_value=0)
    def test_bulk_empty(self, mock_set, client):
        resp = client.post(
            "/api/series/bulk-monitored", json={"series_ids": [], "enabled": True}
        )
        assert resp.status_code == 200
        assert resp.get_json()["updated"] == 0
        mock_set.assert_called_once_with([], True)


class TestSeriesPageSearchYear:
    """Same on the series page: the row and its search button carry the
    issue's publication year so both GetComics and Usenet can narrow."""

    def test_row_and_button_carry_issue_year(self, app, client_with_data):
        app.jinja_env.globals["generate_series_slug"] = (
            lambda name, sid, volume=None: f"{sid}-slug"
        )
        resp = client_with_data.get("/series/batman-v2020-100")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        # Seeded issues have store_date 2020-mm-10.
        assert 'data-issue-year="2020"' in html
        assert "searchGetComics(this.dataset.series, this.dataset.issue, this.dataset.year)" in html


class _MetronSeries:
    """Stand-in for the mokkari Series model the route gets back from Metron."""

    def __init__(self, **fields):
        self.__dict__.update(fields)

    def model_dump(self, mode=None):
        return dict(self.__dict__)


class TestSeriesPageForceRefresh:
    """The page's Refresh button (?refresh=1) has to show what Metron holds now.

    Skipping CLU's own cache isn't enough: mokkari answers api.series() from its
    own SQLite response cache, which never expires on read, so without a purge
    the refetch replays the old body and an edited Summary never appears.
    """

    @staticmethod
    def _register_slug_global(app):
        app.jinja_env.globals["generate_series_slug"] = (
            lambda name, sid, volume=None: f"{sid}-slug"
        )

    @patch("routes.series.metron")
    def test_purges_response_cache_before_refetching(
        self, mock_metron, app, client_with_data
    ):
        self._register_slug_global(app)
        app.config["METRON_USERNAME"] = "user"
        app.config["METRON_PASSWORD"] = "pass"

        calls = []
        mock_api = MagicMock()

        def _series(series_id):
            calls.append("fetch")
            return _MetronSeries(
                id=series_id,
                name="Batman",
                desc="Summary edited on Metron",
                volume=2020,
                status="Ongoing",
                issue_count=3,
                year_began=2020,
                year_end=None,
            )

        mock_api.series.side_effect = _series
        mock_metron.get_flask_api.return_value = mock_api
        mock_metron.is_connection_error.return_value = False
        mock_metron.get_all_issues_for_series.return_value = []
        mock_metron.purge_series_cache.side_effect = lambda *a, **k: calls.append("purge")

        resp = client_with_data.get("/series/batman-v2020-100?refresh=1")

        assert resp.status_code == 200
        mock_metron.purge_series_cache.assert_called_once_with(100, api=mock_api)
        # Purging after the fetch would be useless.
        assert calls == ["purge", "fetch"]
        assert "Summary edited on Metron" in resp.get_data(as_text=True)

    @patch("routes.series.metron")
    def test_normal_load_keeps_the_cache(self, mock_metron, app, client_with_data):
        """Without ?refresh=1 the page serves the DB copy and touches neither
        Metron nor its cache."""
        self._register_slug_global(app)

        resp = client_with_data.get("/series/batman-v2020-100")

        assert resp.status_code == 200
        mock_metron.purge_series_cache.assert_not_called()


class TestSyncSeriesDescription:

    @patch("routes.series.metron")
    def test_sync_purges_cache_and_updates_changed_desc(
        self, mock_metron, client_with_data
    ):
        from core.database import get_series_by_id

        mock_api = MagicMock()
        mock_api.series.return_value = {"id": 100, "desc": "Rewritten summary"}
        mock_metron.get_flask_api.return_value = mock_api
        mock_metron.get_all_issues_for_series.return_value = []

        resp = client_with_data.post("/api/sync/series/100")

        assert resp.status_code == 200
        mock_metron.purge_series_cache.assert_called_once_with(100, api=mock_api)
        # The seeded row already had a description; a changed one must still land.
        assert get_series_by_id(100)["desc"] == "Rewritten summary"


class TestWantedPage:
    """The /wanted page seeds each search with the issue's own year — without
    it "Iron Man 8" matches every volume that ever had an #8."""

    @staticmethod
    def _register_slug_global(app):
        app.jinja_env.globals["generate_series_slug"] = (
            lambda name, sid, volume=None: f"{sid}-slug"
        )

    def _seed_wanted(self, store_date="2026-01-14", cover_date="2026-03-01"):
        from core.database import save_wanted_issues_for_series
        save_wanted_issues_for_series(100, "Iron Man", 2020, [{
            "id": 5001, "number": "8", "name": "Chapter Eight",
            "store_date": store_date, "cover_date": cover_date, "image": None,
        }])

    def test_search_button_carries_issue_year(self, app, client_with_data):
        self._register_slug_global(app)
        self._seed_wanted()

        html = client_with_data.get("/wanted").get_data(as_text=True)
        assert 'data-year="2026"' in html
        assert "searchGetComics(this.dataset.series, this.dataset.issue, this.dataset.year)" in html

    def test_falls_back_to_cover_date(self, app, client_with_data):
        self._register_slug_global(app)
        self._seed_wanted(store_date=None, cover_date="2019-05-01")

        html = client_with_data.get("/wanted").get_data(as_text=True)
        assert 'data-year="2019"' in html

    def test_no_dates_sends_no_year(self, app, client_with_data):
        self._register_slug_global(app)
        self._seed_wanted(store_date=None, cover_date=None)

        html = client_with_data.get("/wanted").get_data(as_text=True)
        # Better to search without a year than to guess the wrong one.
        assert 'data-year=""' in html


class TestPullListPage:
    """The /pull-list page must render with per-series collection status
    coloring and the status filter/legend controls."""

    @staticmethod
    def _register_slug_global(app):
        # app.py registers this Jinja global; the test harness doesn't.
        app.jinja_env.globals["generate_series_slug"] = (
            lambda name, sid, volume=None: f"{sid}-slug"
        )

    def test_renders_with_status_controls(self, app, client_with_data):
        self._register_slug_global(app)
        resp = client_with_data.get("/pull-list")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        # Legend/filter chips and per-row color hooks are present.
        assert 'id="statusFilter"' in html
        assert "data-status-color=" in html
        # Multi-select controls for the bulk Monitored toggle.
        assert 'id="selectAll"' in html
        assert "row-select" in html
        assert "data-series-id=" in html
        assert 'id="bulkToolbar"' in html

    def test_unmonitored_series_marked(self, app, client_with_data):
        self._register_slug_global(app)
        from core.database import set_series_monitored
        set_series_monitored(100, False)

        resp = client_with_data.get("/pull-list")
        html = resp.get_data(as_text=True)
        # A non-monitored series is greyed and labelled.
        assert 'data-status-color="unmonitored"' in html
        assert "Not monitored" in html


def _post_import(client, payload, *, raw=None, filename="pull-list.json"):
    """Helper: POST a JSON payload (or raw bytes) to the import endpoint."""
    if raw is None:
        raw = json.dumps(payload).encode("utf-8")
    return client.post(
        "/api/pull-list/import",
        data={"file": (io.BytesIO(raw), filename)},
        content_type="multipart/form-data",
    )


class TestPullListExport:

    def test_returns_attachment_json(self, client_with_data):
        resp = client_with_data.get("/api/pull-list/export")
        assert resp.status_code == 200
        assert resp.mimetype == "application/json"
        disp = resp.headers.get("Content-Disposition", "")
        assert disp.startswith("attachment;")
        assert "pull-list-" in disp

        body = json.loads(resp.get_data(as_text=True))
        assert body["version"] == 1
        assert body["series_count"] == 2
        ids = {s["id"] for s in body["series"]}
        assert ids == {100, 200}

    def test_excludes_unmapped(self, client_with_data):
        from tests.factories.db_factories import create_series
        from core.database import get_db_connection
        create_series(series_id=300, name="Unmapped", volume=2021,
                      publisher_id=10)
        conn = get_db_connection()
        conn.execute("UPDATE series SET mapped_path = NULL WHERE id = ?", (300,))
        conn.commit()
        conn.close()

        body = json.loads(
            client_with_data.get("/api/pull-list/export").get_data(as_text=True)
        )
        ids = {s["id"] for s in body["series"]}
        assert 300 not in ids
        assert body["series_count"] == 2

    def test_omits_runtime_fields(self, client_with_data):
        body = json.loads(
            client_with_data.get("/api/pull-list/export").get_data(as_text=True)
        )
        for entry in body["series"]:
            assert "cover_image" not in entry
            assert "last_synced_at" not in entry
            assert "created_at" not in entry
            assert "updated_at" not in entry
            assert "issue_count" not in entry
            assert "desc" not in entry

    def test_includes_publisher_name(self, client_with_data):
        body = json.loads(
            client_with_data.get("/api/pull-list/export").get_data(as_text=True)
        )
        batman = next(s for s in body["series"] if s["id"] == 100)
        assert batman["publisher_name"] == "DC Comics"
        assert batman["mapped_path"] == "/data/DC Comics/Batman"


class TestPullListImport:

    def test_rejects_missing_file(self, client_with_data):
        resp = client_with_data.post("/api/pull-list/import")
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False

    def test_rejects_malformed_json(self, client_with_data):
        resp = _post_import(client_with_data, None, raw=b"{not json")
        assert resp.status_code == 400
        assert "Invalid JSON" in resp.get_json()["error"]

    def test_rejects_wrong_version(self, client_with_data):
        resp = _post_import(client_with_data, {"version": 99, "series": []})
        assert resp.status_code == 400
        assert "Unsupported" in resp.get_json()["error"]

    def test_rejects_missing_series_array(self, client_with_data):
        resp = _post_import(client_with_data, {"version": 1})
        assert resp.status_code == 400
        assert "series" in resp.get_json()["error"]

    def test_imports_new_series(self, client_with_data):
        from core.database import get_series_by_id
        payload = {
            "version": 1,
            "series": [{
                "id": 999,
                "name": "New Series",
                "volume": 2024,
                "volume_year": 2024,
                "status": "Ongoing",
                "publisher_id": 10,
                "publisher_name": "DC Comics",
                "mapped_path": "/data/DC Comics/New Series",
            }],
        }
        resp = _post_import(client_with_data, payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["imported_new"] == 1
        assert data["updated_existing"] == 0
        assert data["errors"] == []

        row = get_series_by_id(999)
        assert row is not None
        assert row["mapped_path"] == "/data/DC Comics/New Series"
        assert row["name"] == "New Series"
        assert row["publisher_id"] == 10

    def test_existing_updates_mapped_path_only(self, client_with_data):
        from core.database import get_series_by_id
        payload = {
            "version": 1,
            "series": [{
                "id": 100,
                "name": "CLOBBERED",
                "status": "Cancelled",
                "mapped_path": "/data/DC Comics/Batman (relocated)",
                "publisher_id": 10,
                "publisher_name": "DC Comics",
            }],
        }
        resp = _post_import(client_with_data, payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["imported_new"] == 0
        assert data["updated_existing"] == 1

        row = get_series_by_id(100)
        assert row["mapped_path"] == "/data/DC Comics/Batman (relocated)"
        # Synced metadata must be preserved on update
        assert row["name"] == "Batman"
        assert row["status"] != "Cancelled"

    def test_upserts_unknown_publisher(self, client_with_data):
        from core.database import get_series_by_id, get_db_connection
        payload = {
            "version": 1,
            "series": [{
                "id": 888,
                "name": "Indie Comic",
                "volume": 2024,
                "publisher_name": "Brand New Pub",
                "mapped_path": "/data/Indie/Indie Comic",
            }],
        }
        resp = _post_import(client_with_data, payload)
        assert resp.status_code == 200
        assert resp.get_json()["imported_new"] == 1

        row = get_series_by_id(888)
        assert row is not None
        assert row["publisher_id"] is not None

        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "SELECT name FROM publishers WHERE id = ?", (row["publisher_id"],)
        )
        pub_row = c.fetchone()
        conn.close()
        assert pub_row is not None
        assert pub_row["name"] == "Brand New Pub"

    def test_per_row_error_isolated(self, client_with_data):
        from core.database import get_series_by_id
        payload = {
            "version": 1,
            "series": [
                {"name": "no-id"},                       # invalid: missing id
                {"id": 777, "name": "Good", "mapped_path": "/data/g"},
            ],
        }
        resp = _post_import(client_with_data, payload)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["imported_new"] == 1
        assert len(data["errors"]) == 1
        assert get_series_by_id(777) is not None


class TestSearchAliases:
    """GET/PUT /api/series/<id>/search-aliases."""

    @patch("models.getcomics.get_sitemap_subseries_aliases", return_value=["2000ad"])
    @patch("models.getcomics.get_series_aliases", return_value="2000ad")
    @patch("routes.series.get_series_by_id", return_value={"name": "2000 AD"})
    def test_get_returns_success_and_aliases(self, mock_series, mock_aliases, mock_cands, client):
        resp = client.get("/api/series/100/search-aliases")
        assert resp.status_code == 200
        data = resp.get_json()
        # The missing `success` flag was why the UI stayed stuck on "Loading..."
        assert data["success"] is True
        assert data["current_aliases"] == "2000ad"
        assert data["candidate_aliases"] == ["2000ad"]

    @patch("routes.series.get_series_by_id", return_value=None)
    def test_get_series_not_found(self, mock_series, client):
        resp = client.get("/api/series/999/search-aliases")
        assert resp.status_code == 404
        assert resp.get_json()["success"] is False

    @patch("models.getcomics.update_series_aliases", return_value=2)
    @patch("routes.series.get_series_by_id", return_value={"name": "2000 AD"})
    def test_put_saves_aliases(self, mock_series, mock_update, client):
        resp = client.put("/api/series/100/search-aliases",
                           json={"aliases": "2000ad"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["entries_updated"] == 2
        mock_update.assert_called_once_with("2000 AD", "2000ad")


class TestGetSeriesAliasesPersistence:
    """models.getcomics.get_series_aliases falls back to the alias table."""

    def test_falls_back_to_alias_table_when_no_scrape_row(self, app):
        from core.database import get_db_connection
        from models.getcomics import (
            update_series_aliases,
            get_series_aliases,
        )
        # No getcomics_urls row exists for this series, so update touches 0
        # scrape-index entries -- but the alias must still persist.
        updated = update_series_aliases("2000 AD", "2000ad")
        assert updated == 0
        # Reading back must surface the alias via the canonical alias table.
        assert "2000ad" in get_series_aliases("2000 AD")


class TestSyncComicVineSeries:
    """POST /api/sync/series/<id> for a ComicVine-sourced id. It used to require
    a ComicVine API key, so a user running only the local ComicVine dump could
    not sync at all."""

    def _cv_series_id(self):
        from helpers.comicvine_ids import make_comicvine_series_id
        return make_comicvine_series_id(18705)

    def test_syncs_from_the_local_db_without_an_api_key(self, client):
        cv_issues = [
            {"id": 1, "cv_id": 500, "number": "1", "name": "One",
             "cover_date": "2016-06-01", "store_date": None,
             "image": None, "resource_url": None},
        ]
        with patch("models.comicvine_source._local_available", return_value=True), \
             patch("models.comicvine_source._api_key", return_value=None), \
             patch("models.comicvine_sqlite.get_all_issues_for_volume",
                   return_value=cv_issues), \
             patch("routes.series.get_series_by_id", return_value={"id": 1, "name": "Saga"}), \
             patch("routes.series.delete_issues_for_series"), \
             patch("routes.series.save_issues_bulk"), \
             patch("routes.series.update_series_sync_time"), \
             patch("core.database.clear_wanted_cache_for_series"):
            resp = client.post(f"/api/sync/series/{self._cv_series_id()}")

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["issue_count"] == 1

    def test_errors_only_when_no_comicvine_source_at_all(self, client):
        with patch("models.comicvine_source._local_available", return_value=False), \
             patch("models.comicvine_source._api_key", return_value=None):
            resp = client.post(f"/api/sync/series/{self._cv_series_id()}")

        assert resp.status_code == 500
        assert "ComicVine not configured" in resp.get_json()["error"]


class TestPullListAddFolder:
    """POST /api/pull-list/add-folder -- Scan Library for a single folder,
    driven by the folder dropdowns in the File Manager and collection grid."""

    ENDPOINT = "/api/pull-list/add-folder"

    def test_missing_folder_is_rejected(self, client):
        resp = client.post(self.ENDPOINT, json={})
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False

    def test_applied_folder_reports_the_series(self, client):
        with patch("models.library_automap.add_folder_to_pull_list",
                   return_value={"status": "applied", "series_id": 555,
                                 "series_name": "Batman",
                                 "source": "series.json:metron_id"}) as add:
            resp = client.post(self.ENDPOINT, json={"folder": "/data/DC/Batman"})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["status"] == "applied"
        assert data["series_name"] == "Batman"
        assert add.call_args[0][0] == "/data/DC/Batman"

    def test_manual_pick_passes_series_id_and_fallback(self, client):
        with patch("models.library_automap.add_folder_to_pull_list",
                   return_value={"status": "applied", "series_id": 42,
                                 "series_name": "Chosen"}) as add:
            resp = client.post(self.ENDPOINT, json={
                "folder": "/data/DC/Batman", "series_id": 42,
                "series_name": "Chosen", "publisher_name": "DC", "year": 2011,
            })

        assert resp.status_code == 200
        kwargs = add.call_args[1]
        assert kwargs["series_id"] == 42
        assert kwargs["fallback"]["series_name"] == "Chosen"
        assert kwargs["fallback"]["publisher_name"] == "DC"
        assert kwargs["fallback"]["year"] == 2011

    def test_needs_match_is_not_an_error(self, client):
        # A folder with no sidecar is a prompt to pick the series, not a failure.
        with patch("models.library_automap.add_folder_to_pull_list",
                   return_value={"status": "needs_match",
                                 "suggested_name": "Some Series",
                                 "reason": "No series.json or cvinfo file in this folder"}):
            resp = client.post(self.ENDPOINT, json={"folder": "/data/DC/Some Series"})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["status"] == "needs_match"
        assert data["suggested_name"] == "Some Series"

    def test_failed_status_carries_an_error(self, client):
        with patch("models.library_automap.add_folder_to_pull_list",
                   return_value={"status": "failed",
                                 "message": "Folder is not inside a configured library"}):
            resp = client.post(self.ENDPOINT, json={"folder": "/downloads/Batman"})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is False
        assert "not inside a configured library" in data["error"]

    def test_unexpected_error_is_a_500(self, client):
        with patch("models.library_automap.add_folder_to_pull_list",
                   side_effect=RuntimeError("boom")):
            resp = client.post(self.ENDPOINT, json={"folder": "/data/DC/Batman"})

        assert resp.status_code == 500
        assert resp.get_json()["success"] is False



class TestReleasesPublisherFilter:
    """
    Weekly Releases publisher filter.

    Metron's issue-list payload carries the series but no publisher, so the page
    groups releases through the metron_series_publishers cache and fills the
    misses in a bounded background pass.
    """

    ENDPOINT = "/api/releases/publishers"

    @pytest.fixture(autouse=True)
    def _reset_warm_state(self, db_connection):
        from routes import series as series_routes

        series_routes._publisher_warm_active.clear()
        series_routes._publisher_warm_state.clear()
        yield
        series_routes._publisher_warm_active.clear()
        series_routes._publisher_warm_state.clear()

    @staticmethod
    def _issue(issue_id, series_id):
        issue = MagicMock()
        issue.id = issue_id
        issue.number = "1"
        issue.cover_date = "2024-01-10"
        issue.image = "http://example.test/cover.jpg"
        issue.series = MagicMock()
        issue.series.id = series_id
        issue.series.name = f"Series {series_id}"
        issue.series.volume = 1
        return issue

    def _releases_context(self, client, app, releases):
        """Render /releases with Metron stubbed, returning the template context."""
        from datetime import datetime

        app.config["METRON_USERNAME"] = "user"
        app.config["METRON_PASSWORD"] = "pass"

        captured = {}

        def fake_render(template, **context):
            captured.update(context)
            captured["_template"] = template
            return "ok"

        with patch("routes.series.metron") as mock_metron, \
                patch("routes.series.render_template", side_effect=fake_render):
            mock_metron.get_flask_api.return_value = MagicMock()
            mock_metron.calculate_comic_week.return_value = (
                datetime(2024, 1, 7),
                datetime(2024, 1, 13),
            )
            mock_metron.get_releases.return_value = releases
            resp = client.get("/releases?date=2024-01-10")

        assert resp.status_code == 200
        return captured

    # -- page context -----------------------------------------------------
    def test_page_maps_series_to_cached_publishers(self, client, app):
        from core.database import save_series_publishers

        save_series_publishers({11: "Marvel", 22: "DC Comics"})

        ctx = self._releases_context(
            client, app, [self._issue(1, 11), self._issue(2, 22)]
        )

        assert ctx["publisher_map"] == {11: "Marvel", 22: "DC Comics"}
        # Everything resolved -> no background work, no client polling.
        assert ctx["publisher_pending"] is False
        assert ctx["query_after"] == "2024-01-07"
        assert ctx["query_before"] == "2024-01-13"

    def test_page_starts_warm_pass_for_unknown_series(self, client, app):
        from core.database import save_series_publishers

        save_series_publishers({11: "Marvel"})

        with patch("routes.series._start_publisher_warm",
                   return_value=True) as mock_warm:
            ctx = self._releases_context(
                client, app, [self._issue(1, 11), self._issue(2, 99)]
            )

        assert ctx["publisher_pending"] is True
        args = mock_warm.call_args[0]
        assert args[1] == "2024-01-07" and args[2] == "2024-01-13"
        assert list(args[3]) == [99]  # only the unresolved series

    def test_page_survives_metron_being_unconfigured(self, client, app):
        # The template always reads publisher_map/publisher_pending, so the
        # error branch has to supply them too.
        resp = client.get("/releases")
        assert resp.status_code == 200

    def _render_releases_page(self, client, app):
        """Render /releases with two issues (one publisher known) and return the HTML."""
        from datetime import datetime
        from core.database import save_series_publishers

        app.config["METRON_USERNAME"] = "user"
        app.config["METRON_PASSWORD"] = "pass"
        app.jinja_env.globals["generate_series_slug"] = lambda *a, **k: "slug-1"
        save_series_publishers({11: "Marvel"})

        with patch("routes.series.metron") as mock_metron, \
                patch("routes.series._start_publisher_warm", return_value=True):
            mock_metron.get_flask_api.return_value = MagicMock()
            mock_metron.calculate_comic_week.return_value = (
                datetime(2024, 1, 7), datetime(2024, 1, 13),
            )
            mock_metron.get_releases.return_value = [
                self._issue(1, 11), self._issue(2, 99),
            ]
            resp = client.get("/releases?date=2024-01-10")

        assert resp.status_code == 200
        return resp.get_data(as_text=True)

    def test_page_renders_the_filter_and_tags_every_card(self, client, app):
        # The pills are built client-side from these two things: the serialized
        # map and a data-series-id on each card. If either stops rendering, the
        # filter silently disappears.
        html = self._render_releases_page(client, app)

        assert 'id="publisher-filter"' in html
        # Both views carry the tag -- the filter hides cards and rows alike.
        assert html.count("release-card stagger-load") == 2
        assert html.count('class="release-row"') == 2
        assert html.count("data-series-id=") == 4
        # JSON keys are strings, matching the dataset lookup in the template.
        assert 'const publisherMap = {"11": "Marvel"}' in html
        assert "const pendingWarm = true" in html

    def test_page_renders_both_views_with_a_toggle(self, client, app):
        # Cards and the table are rendered together and swapped client-side, so
        # the table needs no pagination and the toggle no round trip.
        html = self._render_releases_page(client, app)

        assert 'id="view-toggle"' in html
        assert 'id="releases-cards"' in html
        assert 'id="releases-table"' in html
        # Table starts hidden: cards are the default until localStorage says otherwise.
        assert 'class="card border-0 shadow-sm d-none" id="releases-table"' in html
        assert "const VIEW_KEY = 'releasesViewMode'" in html

        # Covers belong to the cards; the table stays compact and text-only.
        cards = html[html.index('id="releases-cards"'):html.index('id="releases-table"')]
        table = html[html.index('id="releases-table"'):html.index("<style>")]
        assert cards.count("<img") == 2  # one per card
        assert "<img" not in table

    def test_table_view_sorts_on_publisher_title_and_issue(self, client, app):
        html = self._render_releases_page(client, app)

        for key in ("publisher", "title", "issue"):
            assert f'sortable" data-sort="{key}"' in html
        # Publisher is resolved client-side, so each row ships an empty cell
        # that syncPublisherCells() fills as the map arrives.
        assert html.count('class="publisher-cell text-muted"') == 2
        assert html.count('data-title="series 11"') == 1
        assert html.count('data-issue="1"') == 2

    # -- warm pass --------------------------------------------------------
    def test_warm_pass_bulk_resolves_by_known_publisher(self, db_connection):
        # One publisher-filtered call resolves every series that publisher
        # shipped in the window -- the whole point of the bulk phase.
        from core.database import get_series_publishers, save_series_publishers
        from routes import series as series_routes

        save_series_publishers({1: "Marvel"})
        unknown = list(range(100, 130))  # > the per-series cap, so bulk runs

        def fake_get_releases(api, date_after, date_before, publisher_name=None):
            if publisher_name == "Marvel":
                return [self._issue(i, sid) for i, sid in enumerate(unknown[:25])]
            return []

        with patch("routes.series.metron") as mock_metron:
            mock_metron.get_releases.side_effect = fake_get_releases
            mock_metron.get_series_details.return_value = {"publisher_name": "Image"}
            series_routes._warm_release_publishers(
                MagicMock(), "2024-01-07", "2024-01-13", unknown, "k"
            )

        cached = get_series_publishers(unknown)
        assert cached[100] == "Marvel"
        assert cached[124] == "Marvel"
        # The bulk phase missed the tail; per-series lookups picked it up.
        assert cached[125] == "Image"

    def test_warm_pass_uses_series_lookup_for_small_tail(self, db_connection):
        from core.database import get_series_publishers
        from routes import series as series_routes

        with patch("routes.series.metron") as mock_metron:
            mock_metron.get_series_details.return_value = {"publisher_name": "Boom"}
            series_routes._warm_release_publishers(
                MagicMock(), "2024-01-07", "2024-01-13", [55], "k"
            )
            # A 1-series tail is not worth a bulk sweep.
            mock_metron.get_releases.assert_not_called()

        assert get_series_publishers([55]) == {55: "Boom"}

    def test_warm_pass_that_learns_nothing_stops_retrying(self, db_connection):
        from routes import series as series_routes

        with patch("routes.series.metron") as mock_metron:
            mock_metron.get_series_details.return_value = None
            series_routes._warm_release_publishers(
                MagicMock(), "2024-01-07", "2024-01-13", [55], "2024-01-07|2024-01-13"
            )

        assert series_routes._publisher_warm_state["2024-01-07|2024-01-13"]["exhausted"]
        # Exhausted window -> no further passes are started.
        with patch("routes.series.threading.Thread") as mock_thread:
            started = series_routes._start_publisher_warm(
                MagicMock(), "2024-01-07", "2024-01-13", [55]
            )
        assert started is False
        mock_thread.assert_not_called()

    def test_warm_pass_is_not_started_twice_for_a_window(self, db_connection):
        from routes import series as series_routes

        with patch("routes.series.threading.Thread") as mock_thread:
            first = series_routes._start_publisher_warm(
                MagicMock(), "2024-01-07", "2024-01-13", [55]
            )
            second = series_routes._start_publisher_warm(
                MagicMock(), "2024-01-07", "2024-01-13", [55]
            )

        assert first is True and second is True
        assert mock_thread.call_count == 1

    def test_warm_pass_is_capped_per_window(self, db_connection):
        from routes import series as series_routes

        with patch("routes.series.threading.Thread"):
            for _ in range(series_routes._PUBLISHER_WARM_MAX_PASSES):
                series_routes._start_publisher_warm(
                    MagicMock(), "2024-01-07", "2024-01-13", [55]
                )
                series_routes._publisher_warm_active.clear()

            assert series_routes._start_publisher_warm(
                MagicMock(), "2024-01-07", "2024-01-13", [55]
            ) is False

    # -- poll endpoint ----------------------------------------------------
    def test_api_returns_cached_publishers(self, client):
        from core.database import save_series_publishers

        save_series_publishers({11: "Marvel"})

        resp = client.post(self.ENDPOINT, json={"series_ids": [11]})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["publishers"] == {"11": "Marvel"}
        assert data["unknown"] == 0
        assert data["pending"] is False

    def test_api_rearms_the_warm_pass_for_unknown_series(self, client):
        with patch("routes.series._start_publisher_warm",
                   return_value=True) as mock_warm:
            resp = client.post(self.ENDPOINT, json={
                "series_ids": [77],
                "date_after": "2024-01-07",
                "date_before": "2024-01-13",
            })

        data = resp.get_json()
        assert data["unknown"] == 1
        assert data["pending"] is True
        assert mock_warm.call_args[0][1] == "2024-01-07"

    def test_api_rejects_a_bad_date(self, client):
        resp = client.post(self.ENDPOINT, json={
            "series_ids": [1], "date_after": "not-a-date",
        })
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False

    def test_api_ignores_unusable_series_ids(self, client):
        resp = client.post(self.ENDPOINT, json={"series_ids": ["abc", None, "12"]})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["unknown"] == 1  # only the coercible "12" counted

    def test_api_handles_an_empty_body(self, client):
        resp = client.post(self.ENDPOINT, json={})
        assert resp.status_code == 200
        assert resp.get_json()["publishers"] == {}
