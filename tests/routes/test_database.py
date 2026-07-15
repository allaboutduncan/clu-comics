"""Route tests for the Database settings page (/api/database/*)."""
import os
import zipfile

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
