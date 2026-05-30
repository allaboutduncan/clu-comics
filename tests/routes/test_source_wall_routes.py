"""Tests for routes/source_wall.py -- Source Wall metadata table editor."""
import json
import pytest
from unittest.mock import patch, MagicMock


class TestSourceWallPage:

    @patch("routes.source_wall.config")
    def test_page_loads(self, mock_config, client):
        mock_config.get.return_value = ""
        resp = client.get("/source-wall")
        assert resp.status_code == 200


class TestSourceWallFiles:

    @patch("routes.source_wall.get_source_wall_files")
    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_list_files_returns_metadata(self, mock_valid, mock_files, client):
        mock_files.return_value = (
            [{"id": 1, "name": "SubDir", "path": "/data/SubDir", "type": "directory"}],
            [{"id": 2, "name": "Issue 001.cbz", "path": "/data/Issue 001.cbz",
              "type": "file", "ci_series": "Batman", "ci_volume": "2020"}],
        )

        resp = client.get("/api/source-wall/files?path=/data")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["directories"]) == 1
        assert len(data["files"]) == 1
        assert data["files"][0]["ci_series"] == "Batman"

    def test_missing_path_returns_403(self, client):
        resp = client.get("/api/source-wall/files")
        assert resp.status_code == 403

    @patch("routes.source_wall.is_valid_library_path", return_value=False)
    def test_invalid_path_returns_403(self, mock_valid, client):
        resp = client.get("/api/source-wall/files?path=/etc/passwd")
        assert resp.status_code == 403


class TestSourceWallSavePending:

    @patch("core.app_state.register_operation", return_value="op-456")
    @patch("routes.source_wall.threading")
    @patch("routes.source_wall.update_file_index_ci_field", return_value=True)
    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_save_pending_multi_path_multi_field(self, mock_valid, mock_update,
                                                  mock_thread, mock_register, client):
        mock_thread.Thread.return_value = MagicMock()

        resp = client.post(
            "/api/source-wall/save-pending",
            data=json.dumps({
                "updates": {
                    "/data/a.cbz": {"ci_publisher": "Boom", "ci_year": "2023"},
                    "/data/b.cbz": {"ci_publisher": "Boom"},
                }
            }),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["op_id"] == "op-456"
        assert data["affected"] == 2
        assert data["edits"] == 3

        calls = {(c.args[0], c.args[1], c.args[2]) for c in mock_update.call_args_list}
        assert ("/data/a.cbz", "ci_publisher", "Boom") in calls
        assert ("/data/a.cbz", "ci_year", "2023") in calls
        assert ("/data/b.cbz", "ci_publisher", "Boom") in calls

        # One distinct path per worker item — guarantees no .tmpzip race.
        thread_kwargs = mock_thread.Thread.call_args.kwargs
        items = thread_kwargs["args"][0]
        paths_in_items = [item[0] for item in items]
        assert len(paths_in_items) == len(set(paths_in_items))

    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_save_pending_empty_updates_rejected(self, mock_valid, client):
        resp = client.post("/api/source-wall/save-pending",
                           data=json.dumps({"updates": {}}),
                           content_type="application/json")
        assert resp.status_code == 400

    @patch("routes.source_wall.is_valid_library_path", return_value=False)
    def test_save_pending_invalid_path_rejected(self, mock_valid, client):
        resp = client.post(
            "/api/source-wall/save-pending",
            data=json.dumps({"updates": {"/etc/passwd": {"ci_series": "x"}}}),
            content_type="application/json",
        )
        assert resp.status_code == 403

    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_save_pending_invalid_field_rejected(self, mock_valid, client):
        resp = client.post(
            "/api/source-wall/save-pending",
            data=json.dumps({"updates": {"/data/a.cbz": {"bad_field": "x"}}}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_save_pending_empty_fields_for_path_rejected(self, mock_valid, client):
        resp = client.post(
            "/api/source-wall/save-pending",
            data=json.dumps({"updates": {"/data/a.cbz": {}}}),
            content_type="application/json",
        )
        assert resp.status_code == 400


class TestSourceWallHasComicinfoSync:
    """`_bulk_sync_pending_to_cbz` must keep file_index.has_comicinfo aligned
    with on-disk state after writes. Without this the Missing XML view shows
    stale entries until the metadata scanner re-scans the folder."""

    def _make_cbz(self, path, with_comicinfo):
        import zipfile
        with zipfile.ZipFile(str(path), 'w') as zf:
            zf.writestr('page1.png', b'fake')
            if with_comicinfo:
                zf.writestr(
                    'ComicInfo.xml',
                    b'<?xml version="1.0"?><ComicInfo><Series>Old</Series></ComicInfo>',
                )
        return str(path)

    def test_flips_has_comicinfo_after_successful_write(self, db_connection, tmp_path):
        from core.database import (
            add_file_index_entry,
            set_has_comicinfo,
            get_db_connection,
        )
        from routes.source_wall import _bulk_sync_pending_to_cbz

        # File A: has ComicInfo on disk; file_index incorrectly reads 0.
        a = self._make_cbz(tmp_path / 'a.cbz', with_comicinfo=True)
        # File B: no ComicInfo on disk; the save creates a fresh one, so the
        # flag must flip to 1.
        b = self._make_cbz(tmp_path / 'b.cbz', with_comicinfo=False)

        for p in (a, b):
            add_file_index_entry(
                name=p.split('/')[-1].split('\\')[-1],
                path=p, entry_type='file', size=1, parent=str(tmp_path),
            )
            set_has_comicinfo(p, 0)

        # Real op id (no mock) — register so update_operation in the worker is a no-op-safe call.
        import core.app_state as app_state
        op_id = app_state.register_operation('source_wall_test', 'test', total=2)

        _bulk_sync_pending_to_cbz(
            items=[
                (a, {'Series': 'New Name'}),
                (b, {'Series': 'New Name'}),
            ],
            op_id=op_id,
        )

        conn = get_db_connection()
        a_row = conn.execute(
            "SELECT has_comicinfo FROM file_index WHERE path = ?", (a,)
        ).fetchone()
        b_row = conn.execute(
            "SELECT has_comicinfo FROM file_index WHERE path = ?", (b,)
        ).fetchone()
        conn.close()

        assert a_row is not None and a_row['has_comicinfo'] == 1, (
            "File with existing ComicInfo should now read has_comicinfo=1"
        )
        assert b_row is not None and b_row['has_comicinfo'] == 1, (
            "File without ComicInfo should flip to 1 after a fresh XML is created"
        )

    def test_creates_comicinfo_when_missing(self, db_connection, tmp_path):
        """Source Wall save on a CBZ with no ComicInfo writes a fresh one
        containing only the staged fields — no defaults injected."""
        import zipfile
        from core.database import (
            add_file_index_entry,
            set_has_comicinfo,
            get_db_connection,
        )
        from core.comicinfo import find_comicinfo_in_zip, read_comicinfo_from_zip
        from routes.source_wall import _bulk_sync_pending_to_cbz

        path = self._make_cbz(tmp_path / 'fresh.cbz', with_comicinfo=False)
        add_file_index_entry(
            name='fresh.cbz', path=path, entry_type='file', size=1,
            parent=str(tmp_path),
        )
        set_has_comicinfo(path, 0)

        import core.app_state as app_state
        op_id = app_state.register_operation('source_wall_test', 'test', total=1)

        _bulk_sync_pending_to_cbz(
            items=[(path, {'Series': 'X', 'Title': 'Y', 'Year': '2024'})],
            op_id=op_id,
        )

        with zipfile.ZipFile(path, 'r') as z:
            ci_path = find_comicinfo_in_zip(z)
            assert ci_path is not None, "ComicInfo.xml should now exist"
            assert "/" not in ci_path and "\\" not in ci_path, (
                "ComicInfo.xml must be at the archive root"
            )

        parsed = read_comicinfo_from_zip(path)
        assert parsed == {'Series': 'X', 'Title': 'Y', 'Year': '2024'}, (
            "Only staged fields should be written — no LanguageISO/Manga/Notes "
            "defaults from generate_comicinfo_xml"
        )

        conn = get_db_connection()
        row = conn.execute(
            "SELECT has_comicinfo FROM file_index WHERE path = ?", (path,)
        ).fetchone()
        conn.close()
        assert row is not None and row['has_comicinfo'] == 1


class TestSourceWallReconcileFromDb:
    """`/api/source-wall/reconcile-from-db` reads current ci_ values from
    file_index and queues a background CBZ rebuild — used to write XML from
    the database when on-disk ComicInfo.xml has drifted."""

    @patch("core.app_state.register_operation", return_value="op-789")
    @patch("routes.source_wall.threading")
    @patch("routes.source_wall.get_file_index_ci_for_paths")
    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_reconcile_happy_path(self, mock_valid, mock_get, mock_thread,
                                  mock_register, client):
        mock_thread.Thread.return_value = MagicMock()
        # Path A has mixed fields, B has one — empty values must be dropped.
        mock_get.return_value = {
            "/data/a.cbz": {
                "ci_series": "Batman", "ci_year": "2023", "ci_title": "",
                "ci_number": "1", "ci_count": "", "ci_volume": "",
                "ci_writer": "", "ci_penciller": "", "ci_inker": "",
                "ci_colorist": "", "ci_letterer": "", "ci_coverartist": "",
                "ci_publisher": "", "ci_genre": "", "ci_characters": "",
            },
            "/data/b.cbz": {
                "ci_series": "Flash", "ci_title": "", "ci_number": "",
                "ci_count": "", "ci_volume": "", "ci_year": "",
                "ci_writer": "", "ci_penciller": "", "ci_inker": "",
                "ci_colorist": "", "ci_letterer": "", "ci_coverartist": "",
                "ci_publisher": "", "ci_genre": "", "ci_characters": "",
            },
        }

        resp = client.post(
            "/api/source-wall/reconcile-from-db",
            data=json.dumps({"paths": ["/data/a.cbz", "/data/b.cbz"]}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["op_id"] == "op-789"
        assert data["affected"] == 2
        assert data["skipped"] == 0

        # Worker payload uses XML tag names, not ci_ names, and excludes empties.
        thread_kwargs = mock_thread.Thread.call_args.kwargs
        items = thread_kwargs["args"][0]
        items_by_path = {p: fields for p, fields in items}
        assert items_by_path["/data/a.cbz"] == {
            "Series": "Batman", "Year": "2023", "Number": "1",
        }
        assert items_by_path["/data/b.cbz"] == {"Series": "Flash"}

    @patch("routes.source_wall.get_file_index_ci_for_paths")
    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_reconcile_skips_all_empty(self, mock_valid, mock_get, client):
        empty = {f: "" for f in [
            "ci_title", "ci_series", "ci_number", "ci_count", "ci_volume",
            "ci_year", "ci_writer", "ci_penciller", "ci_inker", "ci_colorist",
            "ci_letterer", "ci_coverartist", "ci_publisher", "ci_genre",
            "ci_characters",
        ]}
        mock_get.return_value = {"/data/empty.cbz": empty}

        resp = client.post(
            "/api/source-wall/reconcile-from-db",
            data=json.dumps({"paths": ["/data/empty.cbz"]}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["success"] is False
        assert data["skipped"] == 1

    def test_reconcile_empty_paths_rejected(self, client):
        resp = client.post(
            "/api/source-wall/reconcile-from-db",
            data=json.dumps({"paths": []}),
            content_type="application/json",
        )
        assert resp.status_code == 400

    @patch("routes.source_wall.is_valid_library_path", return_value=False)
    def test_reconcile_invalid_path_rejected(self, mock_valid, client):
        resp = client.post(
            "/api/source-wall/reconcile-from-db",
            data=json.dumps({"paths": ["/etc/passwd"]}),
            content_type="application/json",
        )
        assert resp.status_code == 403

    @patch("routes.source_wall.get_file_index_ci_for_paths", return_value={})
    @patch("routes.source_wall.is_valid_library_path", return_value=True)
    def test_reconcile_missing_from_index_skipped(self, mock_valid, mock_get, client):
        # Path passes validation but isn't in file_index — silently skipped,
        # then the request fails 400 because there's nothing to write.
        resp = client.post(
            "/api/source-wall/reconcile-from-db",
            data=json.dumps({"paths": ["/data/ghost.cbz"]}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["skipped"] == 1


class TestSourceWallColumns:

    @patch("routes.source_wall.get_user_preference", return_value=["name", "ci_volume"])
    def test_get_defaults(self, mock_pref, client):
        resp = client.get("/api/source-wall/columns")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "name" in data["columns"]

    @patch("routes.source_wall.set_user_preference", return_value=True)
    def test_save_and_load_round_trip(self, mock_set, client):
        cols = ["name", "ci_series", "ci_writer", "ci_year"]
        resp = client.post("/api/source-wall/columns",
                           data=json.dumps({"columns": cols}),
                           content_type="application/json")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        mock_set.assert_called_once_with("source_wall_columns", cols, category="source_wall")


class TestSourceWallSuggest:

    @patch("routes.source_wall.get_distinct_ci_values",
           return_value=["Alan Moore", "Alan Grant"])
    def test_suggest_returns_values(self, mock_distinct, client):
        resp = client.get("/api/source-wall/suggest?field=ci_writer&q=Ala&path=/data/Comics")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert len(data["values"]) == 2
        assert "Alan Moore" in data["values"]
        mock_distinct.assert_called_once_with("ci_writer", "Ala", parent_path="/data/Comics", limit=20)

    def test_suggest_rejects_invalid_field(self, client):
        resp = client.get("/api/source-wall/suggest?field=bad_field&q=test")
        assert resp.status_code == 400

    def test_suggest_short_query_returns_empty(self, client):
        resp = client.get("/api/source-wall/suggest?field=ci_writer&q=Al")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["values"] == []
