"""Route tests for the Database settings page (/api/database/*)."""
import os
import threading
import zipfile
from unittest.mock import patch

import pytest


def _make_fake_backup(backup_dir, filename, contents=b"fake-db-bytes"):
    os.makedirs(backup_dir, exist_ok=True)
    backup_path = os.path.join(backup_dir, filename)
    with zipfile.ZipFile(backup_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("comic_utils.db", contents)
    return backup_path


def _corrupt_db(db_path):
    """Overwrite a SQLite DB (and drop its sidecars) with garbage so integrity
    checks fail with a DatabaseError. Caller must close open connections first
    (Windows can't replace a file with an open handle)."""
    for suffix in ("-wal", "-shm"):
        side = db_path + suffix
        if os.path.exists(side):
            try:
                os.remove(side)
            except OSError:
                # Windows may briefly hold the WAL sidecar after close; a garbage
                # main-file header alone is enough to trip the integrity check.
                pass
    with open(db_path, "wb") as f:
        f.write(b"this is not a sqlite database" * 64)


def _corrupt_db_but_readable(db_path, rows=4000):
    """Replace the DB with one that is malformed but still partly readable.

    _corrupt_db writes pure junk, which salvage can do nothing with. Salvage is
    for the realistic case: a valid header, an intact schema and one damaged
    page in the middle.
    """
    import sqlite3

    for suffix in ("-wal", "-shm"):
        side = db_path + suffix
        if os.path.exists(side):
            try:
                os.remove(side)
            except OSError:
                pass
    os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA page_size=4096")
    conn.execute("CREATE TABLE file_index (id INTEGER PRIMARY KEY, path TEXT)")
    conn.executemany(
        "INSERT INTO file_index (path) VALUES (?)",
        [(f"/data/x/{i}-" + "y" * 60,) for i in range(rows)],
    )
    conn.commit()
    conn.close()
    pages = os.path.getsize(db_path) // 4096
    with open(db_path, "r+b") as f:
        f.seek((pages // 2) * 4096)
        f.write(b"\x99" * 4096)


class TestDatabaseStats:
    def test_returns_expected_shape(self, client):
        resp = client.get("/api/database/stats")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        stats = data["stats"]
        for key in ("db_path", "db_size", "wal_size", "shm_size", "tables",
                    "total_rows", "integrity"):
            assert key in stats
        assert isinstance(stats["tables"], list)

    def test_reports_healthy_integrity(self, client):
        resp = client.get("/api/database/stats")
        stats = resp.get_json()["stats"]
        assert stats["integrity"]["ok"] is True
        assert stats["integrity"]["error"] is None

    def test_reports_corruption(self, client, db_path, db_connection):
        # Close the fixture connection so the DB file can be overwritten.
        db_connection.close()
        _corrupt_db(db_path)

        resp = client.get("/api/database/stats")
        assert resp.status_code == 200
        stats = resp.get_json()["stats"]
        assert stats["integrity"]["ok"] is False
        assert stats["integrity"]["error"]

    def test_lists_known_tables(self, client):
        resp = client.get("/api/database/stats")
        data = resp.get_json()
        names = [t["name"] for t in data["stats"]["tables"]]
        # init_db creates these — they should be present after the fixture.
        assert "file_index" in names
        assert "thumbnail_jobs" in names

    def test_no_backups_yet(self, client):
        resp = client.get("/api/database/stats")
        data = resp.get_json()
        assert data["last_backup"] is None
        assert data["backup_count"] == 0


class TestDatabaseBackupsList:
    def test_empty_list_initially(self, client):
        resp = client.get("/api/database/backups")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["backups"] == []

    def test_lists_only_well_named_backups(self, client, db_path):
        backup_dir = os.path.dirname(db_path)
        _make_fake_backup(backup_dir, "comic_utils_backup_20260101_120000.zip")
        _make_fake_backup(backup_dir, "comic_utils_backup_20260102_120000.zip")
        # Junk file that should be ignored
        with open(os.path.join(backup_dir, "random.zip"), "wb") as f:
            f.write(b"x")

        resp = client.get("/api/database/backups")
        data = resp.get_json()
        names = [b["filename"] for b in data["backups"]]
        assert names == [
            "comic_utils_backup_20260102_120000.zip",
            "comic_utils_backup_20260101_120000.zip",
        ]
        assert "random.zip" not in names


class TestDatabaseBackup:
    def test_force_creates_backup(self, client, db_path):
        resp = client.post("/api/database/backup")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["filename"].startswith("comic_utils_backup_")
        assert data["filename"].endswith(".zip")
        # File actually exists on disk
        backup_path = os.path.join(os.path.dirname(db_path), data["filename"])
        assert os.path.exists(backup_path)


class TestDatabaseRestore:
    def test_missing_filename_returns_400(self, client):
        resp = client.post("/api/database/restore", json={})
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False

    def test_invalid_filename_returns_400(self, client):
        # Path traversal / wrong format
        for bad in ("../../etc/passwd", "comic_utils_backup_xx.zip", "evil.zip"):
            resp = client.post("/api/database/restore", json={"filename": bad})
            assert resp.status_code == 400, f"expected 400 for {bad}"
            assert resp.get_json()["success"] is False

    def test_unknown_filename_returns_404(self, client):
        resp = client.post(
            "/api/database/restore",
            json={"filename": "comic_utils_backup_19990101_000000.zip"},
        )
        assert resp.status_code == 404

    def test_restore_round_trip(self, client, db_path, db_connection):
        # Create a real backup first.
        b_resp = client.post("/api/database/backup")
        backup_filename = b_resp.get_json()["filename"]

        # Close the fixture's open connection so Windows can replace the DB file.
        db_connection.close()

        # Now restore from it. Should succeed and produce a pre-restore safety backup.
        r_resp = client.post(
            "/api/database/restore", json={"filename": backup_filename}
        )
        assert r_resp.status_code == 200
        data = r_resp.get_json()
        assert data["success"] is True
        assert data["pre_restore_backup"]
        assert data["pre_restore_backup"].startswith("comic_utils_backup_")

        # The DB file still exists and has the expected schema (sanity).
        assert os.path.exists(db_path)


class TestDatabaseBackupDelete:
    def test_invalid_filename_returns_400(self, client):
        resp = client.delete("/api/database/backups/evil.zip")
        assert resp.status_code == 400

    def test_path_traversal_rejected(self, client):
        # Werkzeug normalises ../../etc out before this hits us, but a name like
        # "comic_utils_backup_../etc/passwd" still doesn't match the regex.
        resp = client.delete("/api/database/backups/comic_utils_backup_xxxxxxxx_xxxxxx.zip")
        assert resp.status_code == 400

    def test_unknown_filename_returns_404(self, client):
        resp = client.delete(
            "/api/database/backups/comic_utils_backup_19990101_000000.zip"
        )
        assert resp.status_code == 404

    def test_delete_round_trip(self, client, db_path):
        # Create a real backup first.
        backup_filename = client.post("/api/database/backup").get_json()["filename"]
        backup_path = os.path.join(os.path.dirname(db_path), backup_filename)
        assert os.path.exists(backup_path)

        resp = client.delete(f"/api/database/backups/{backup_filename}")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert not os.path.exists(backup_path)

        # Subsequent listing reflects the delete.
        listed = client.get("/api/database/backups").get_json()["backups"]
        assert backup_filename not in [b["filename"] for b in listed]


class TestDatabaseBackupDownload:
    def test_invalid_filename_returns_400(self, client):
        resp = client.get("/api/database/backups/evil.zip/download")
        assert resp.status_code == 400

    def test_unknown_filename_returns_404(self, client):
        resp = client.get(
            "/api/database/backups/comic_utils_backup_19990101_000000.zip/download"
        )
        assert resp.status_code == 404

    def test_download_serves_zip_bytes(self, client, db_path):
        backup_filename = client.post("/api/database/backup").get_json()["filename"]
        backup_path = os.path.join(os.path.dirname(db_path), backup_filename)
        with open(backup_path, "rb") as f:
            expected_first_bytes = f.read(4)

        resp = client.get(f"/api/database/backups/{backup_filename}/download")
        assert resp.status_code == 200
        assert resp.mimetype == "application/zip"
        assert resp.headers.get("Content-Disposition", "").startswith("attachment")
        assert backup_filename in resp.headers.get("Content-Disposition", "")
        # ZIP files start with "PK\x03\x04"
        assert resp.data[:4] == expected_first_bytes
        assert resp.data[:2] == b"PK"


class TestCheckIntegrity:
    def test_healthy_db_passes(self, db_path, db_connection):
        from core.database import check_integrity

        ok, msg = check_integrity(db_path)
        assert ok is True
        assert msg == "ok"

    def test_missing_db_is_treated_as_ok(self, tmp_path):
        from core.database import check_integrity

        ok, msg = check_integrity(str(tmp_path / "does_not_exist.db"))
        assert ok is True
        assert msg == "ok"

    def test_corrupt_db_detected(self, db_path, db_connection):
        from core.database import check_integrity

        db_connection.close()
        _corrupt_db(db_path)

        ok, msg = check_integrity(db_path)
        assert ok is False
        assert msg  # non-empty SQLite error text


class TestWaitForBackgroundAnalyze:
    """init_db() leaves a thread writing to the DB after it returns.

    Anything that replaces the file or inspects it for corruption has to be
    able to wait: a commit from that thread landing after the file is
    overwritten flushes its cached pages back and undoes the overwrite.
    """

    def test_no_thread_is_already_quiet(self):
        from core import database

        with patch.object(database, "_analyze_thread", None):
            assert database.wait_for_background_analyze() is True

    def test_waits_for_a_running_thread(self):
        from core import database

        started = threading.Event()
        release = threading.Event()

        def _work():
            started.set()
            release.wait(5)

        thread = threading.Thread(target=_work, daemon=True)
        thread.start()
        started.wait(5)
        try:
            with patch.object(database, "_analyze_thread", thread):
                # Still working -- reported as not settled rather than hanging.
                assert database.wait_for_background_analyze(timeout=0.1) is False
                release.set()
                assert database.wait_for_background_analyze(timeout=5) is True
        finally:
            release.set()
            thread.join(5)

    def test_init_db_leaves_nothing_running(self, db_path):
        """The fixture already waits, so a caller that just ran init_db()
        should find the DB quiet."""
        from core import database

        with patch("core.database.get_db_path", return_value=db_path):
            assert database.init_db() is True
            assert database.wait_for_background_analyze(timeout=10) is True


class TestBackupIntegrityGuard:
    def test_corrupt_db_does_not_evict_good_backups(self, db_path, db_connection):
        from core.database import backup_database, list_backups

        # Create a healthy rolling backup while the DB is valid.
        good = backup_database(max_backups=3, force=True)
        assert good and good.startswith("comic_utils_backup_")
        assert good in [b["filename"] for b in list_backups()]

        # Corrupt the DB, then attempt another backup.
        db_connection.close()
        _corrupt_db(db_path)
        result = backup_database(max_backups=3, force=True)

        # No new rotating backup was created; the good one survives.
        listed = [b["filename"] for b in list_backups()]
        assert listed == [good]
        # A corrupt DB must never be saved under the restorable backup name.
        if isinstance(result, str):
            assert result.startswith("comic_utils_corrupt_")
            assert result not in listed  # quarantine is not offered for restore


class TestRestoreIntegrityGuard:
    def test_restore_refuses_corrupt_backup(self, db_path, db_connection):
        import sqlite3
        from core.database import restore_database

        backup_dir = os.path.dirname(db_path)
        # Fake backup whose comic_utils.db member is not a valid database.
        _make_fake_backup(backup_dir, "comic_utils_backup_20260101_120000.zip")

        # Restore must abort rather than swap in a corrupt DB.
        with pytest.raises(RuntimeError):
            restore_database("comic_utils_backup_20260101_120000.zip")

        # The live DB is untouched and still valid.
        conn = sqlite3.connect(db_path, timeout=5)
        try:
            rows = conn.execute("PRAGMA quick_check").fetchall()
            assert rows == [("ok",)]
        finally:
            conn.close()


class TestBackupReusesAKnownIntegrityResult:
    """``PRAGMA quick_check`` is a full scan of the database file, and startup
    ran one a few lines before it called backup_database -- so every boot paid
    for two. ``known_integrity`` lets the caller hand its result over.

    The parameter narrows what the backup does; it must never widen it. A caller
    that does not pass one (the manual backup button, restore_database's
    pre-restore snapshot) still gets the check.
    """

    def test_a_supplied_result_skips_the_second_check(self, db_path, db_connection):
        from core.database import backup_database

        with patch("core.database.check_integrity") as checked:
            result = backup_database(max_backups=3, force=True, known_integrity=True)

        assert checked.call_count == 0, "backup_database ran a redundant quick_check"
        assert result and result.startswith("comic_utils_backup_")

    def test_a_supplied_failure_still_quarantines(self, db_path, db_connection):
        """The corruption guard is the reason the check is there at all. Handing
        in the answer must not let a bad DB rotate away good backups."""
        from core.database import backup_database, list_backups

        good = backup_database(max_backups=3, force=True)
        assert good and good.startswith("comic_utils_backup_")

        with patch("core.database.check_integrity") as checked:
            result = backup_database(max_backups=3, force=True, known_integrity=False)

        assert checked.call_count == 0
        listed = [b["filename"] for b in list_backups()]
        assert listed == [good], "a DB reported corrupt rotated the good backup away"
        if isinstance(result, str):
            assert result.startswith("comic_utils_corrupt_")

    def test_the_check_still_runs_when_nothing_is_supplied(self, db_path, db_connection):
        from core.database import backup_database

        with patch("core.database.check_integrity", return_value=(True, "ok")) as checked:
            backup_database(max_backups=3, force=True)

        assert checked.call_count == 1


# ---------------------------------------------------------------------------
# Maintenance, health and salvage endpoints
# ---------------------------------------------------------------------------


def _wait_for_op(client, op_id, tries=200):
    """Poll the stashed result of a backgrounded maintenance operation."""
    import time

    for _ in range(tries):
        resp = client.get(f"/api/database/operation/{op_id}")
        data = resp.get_json()
        if not data.get("pending"):
            return data.get("result") or {}
        time.sleep(0.05)
    raise AssertionError(f"operation {op_id} never finished")


class TestDatabaseHealth:
    def test_shape(self, client):
        resp = client.get("/api/database/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        for key in ("last_known_integrity", "errors", "error_summary",
                    "wal_size", "db_size"):
            assert key in data

    def test_reports_recorded_errors(self, client):
        import sqlite3

        import core.db_health as db_health

        db_health.clear_db_errors()
        try:
            db_health.note_db_error(
                sqlite3.DatabaseError("database disk image is malformed"),
                "test_context",
            )
            data = client.get("/api/database/health").get_json()
            assert data["error_summary"]["corruption"] == 1
            assert data["errors"][0]["context"] == "test_context"
            assert data["last_known_integrity"]["ok"] is False
        finally:
            db_health.clear_db_errors()
            import core.app_state as app_state

            app_state.set_db_integrity(True, None)


class TestStatsExtras:
    def test_reports_storage_and_pages(self, client):
        stats = client.get("/api/database/stats").get_json()["stats"]
        assert "storage" in stats and "pages" in stats
        assert "last_known_integrity" in stats
        pages = stats["pages"]
        assert pages["page_count"] and pages["page_size"]
        assert pages["reclaimable_bytes"] is not None

    def test_quarantine_list_is_present(self, client):
        data = client.get("/api/database/stats").get_json()
        assert isinstance(data["quarantine"], list)


class TestIntegrityEndpoint:
    def test_quick_check_passes(self, client):
        data = client.post("/api/database/integrity", json={"full": False}).get_json()
        assert data["success"] is True
        assert data["ok"] is True
        assert data["full"] is False

    def test_quick_check_reports_corruption(self, client, db_path, db_connection):
        db_connection.close()
        _corrupt_db(db_path)
        data = client.post("/api/database/integrity", json={"full": False}).get_json()
        assert data["ok"] is False
        assert data["message"]

    def test_full_check_is_backgrounded(self, client):
        """integrity_check on a large DB outlasts gunicorn's 120s timeout, so
        it must return an op id rather than block the request."""
        data = client.post("/api/database/integrity", json={"full": True}).get_json()
        assert data["full"] is True
        assert data["op_id"]
        result = _wait_for_op(client, data["op_id"])
        assert result["ok"] is True


class TestCheckpointEndpoint:
    def test_checkpoints(self, client):
        data = client.post("/api/database/checkpoint").get_json()
        assert data["success"] is True
        assert data["busy"] in (0, 1)
        assert data["wal_size_after"] is not None


class TestOptimizeEndpoint:
    def test_optimizes(self, client):
        data = client.post("/api/database/optimize").get_json()
        assert data["success"] is True
        assert data["analyzed"] is True


class TestCompactEndpoint:
    def test_refuses_a_corrupt_database(self, client, db_path, db_connection):
        """VACUUM rewrites every page; never do that to a damaged file."""
        db_connection.close()
        _corrupt_db(db_path)
        data = client.post("/api/database/compact").get_json()
        assert data["op_id"]
        result = _wait_for_op(client, data["op_id"])
        assert result["success"] is False
        assert "integrity" in result["error"].lower()


class TestSalvageEndpoints:
    def test_no_candidate_initially(self, client):
        data = client.get("/api/database/salvage").get_json()
        assert data["success"] is True
        assert data["candidate"] is None

    def test_apply_requires_a_token(self, client):
        resp = client.post("/api/database/salvage/apply", json={})
        assert resp.status_code == 400

    def test_apply_without_a_candidate_is_404(self, client):
        resp = client.post("/api/database/salvage/apply", json={"token": "x"})
        assert resp.status_code == 404

    def test_salvage_round_trip(self, client, db_path, db_connection):
        import core.db_repair as db_repair

        db_connection.close()
        _corrupt_db_but_readable(db_path)
        try:
            start = client.post("/api/database/salvage").get_json()
            assert start["op_id"]
            result = _wait_for_op(client, start["op_id"])
            assert result["success"] is True, result.get("error")

            listed = client.get("/api/database/salvage").get_json()
            assert listed["candidate"] is not None
            candidate = listed["candidate"]
            assert candidate["integrity_ok"] is True
            assert candidate["diff"]["tables"]

            # A stale token must be refused -- it guards the window between
            # reading the diff and pressing the button.
            stale = client.post(
                "/api/database/salvage/apply", json={"token": "wrong"}
            )
            assert stale.status_code == 409

            applied = client.post(
                "/api/database/salvage/apply",
                json={"token": candidate["token"]},
            )
            assert applied.status_code == 200
            body = applied.get_json()
            assert body["success"] is True
            assert body["requires_restart"] is True
        finally:
            db_repair.discard_candidate()

    def test_discard(self, client):
        resp = client.delete("/api/database/salvage")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True


class TestQuarantineDownload:
    def test_rejects_a_bad_filename(self, client):
        """The quarantine pattern is its own traversal guard; it must never be
        widened into _BACKUP_FILENAME_RE, which also decides what can be
        restored."""
        resp = client.get("/api/database/quarantine/evil.zip/download")
        assert resp.status_code == 400

    def test_missing_snapshot_is_404(self, client):
        resp = client.get(
            "/api/database/quarantine/comic_utils_corrupt_20200101_000000.zip/download"
        )
        assert resp.status_code == 404
