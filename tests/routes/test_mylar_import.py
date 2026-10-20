"""Mylar3 importer routes (``routes/mylar_import.py``)."""

import io
import os
import sqlite3
import time
from unittest.mock import patch

import pytest

from models import mylar_import


@pytest.fixture
def env(app, tmp_path):
    """Patch the cache dir and the library roots onto tmp folders."""
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    library = tmp_path / "library"
    library.mkdir()
    with patch.object(mylar_import, "get_cache_dir", return_value=str(cache)), \
            patch.object(mylar_import, "get_library_roots", return_value=[str(library)]):
        yield {"cache": str(cache), "library": str(library)}


def mylar_db_bytes(tmp_path):
    path = tmp_path / "src_mylar.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE comics (ComicID TEXT, ComicName TEXT, ComicYear TEXT, "
                 "ComicLocation TEXT, Status TEXT)")
    conn.execute("INSERT INTO comics VALUES ('123', 'Batman', '2016', '/comics/Batman', 'Active')")
    conn.execute("CREATE TABLE issues (ComicID TEXT, Issue_Number TEXT, Status TEXT)")
    conn.commit()
    conn.close()
    return path.read_bytes()


INI = b"[General]\ndestination_dir = /comics\n[CV]\ncomicvine_api = secretkey\n"


def test_routes_are_owner_only():
    """The import writes client passwords and API keys."""
    from core.auth import _OWNER_PREFIXES

    for url in ("/import/mylar", "/api/import/mylar/load", "/api/import/mylar/apply",
                "/api/import/mylar/preview", "/api/import/mylar/status",
                "/api/import/mylar/drop-folder"):
        assert url.startswith(_OWNER_PREFIXES)


class TestPage:
    def test_renders(self, client, env):
        resp = client.get("/import/mylar")
        assert resp.status_code == 200
        assert b"Import from Mylar3" in resp.data
        assert b"mi-back-to-setup" not in resp.data

    def test_opened_from_the_guided_setup_offers_the_way_back(self, client, env):
        resp = client.get("/import/mylar?from=setup")
        assert b'id="mi-back-to-setup"' in resp.data
        assert b'href="/setup"' in resp.data


class TestLoad:
    def test_upload_returns_preview_without_secrets(self, client, env, tmp_path):
        resp = client.post("/api/import/mylar/load", data={
            "mylar_db": (io.BytesIO(mylar_db_bytes(tmp_path)), "mylar.db"),
            "config_ini": (io.BytesIO(INI), "config.ini"),
        }, content_type="multipart/form-data")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["mapping"][0]["mylar_root"] == "/comics"
        assert data["mapping"][0]["clu_root"] == env["library"]
        (row,) = data["series"]
        assert row["state"] == "will_create"
        assert row["path"] == os.path.join(env["library"], "Batman")
        assert data["settings"]["comicvine"] == {"state": "new"}
        assert b"secretkey" not in resp.data
        mylar_import.discard_import(data["import_id"])

    def test_nothing_uploaded(self, client, env):
        resp = client.post("/api/import/mylar/load", data={}, content_type="multipart/form-data")
        assert resp.status_code == 400

    def test_bad_database_is_rejected(self, client, env):
        resp = client.post("/api/import/mylar/load", data={
            "mylar_db": (io.BytesIO(b"nope" * 100), "mylar.db"),
        }, content_type="multipart/form-data")
        assert resp.status_code == 400
        assert "SQLite" in resp.get_json()["error"]

    def test_size_cap(self, client, env):
        with patch("routes.mylar_import.MAX_INI_BYTES", 10):
            resp = client.post("/api/import/mylar/load", data={
                "config_ini": (io.BytesIO(INI), "config.ini"),
            }, content_type="multipart/form-data")
        assert resp.status_code == 413
        staging = os.path.join(env["cache"], "mylar_import", ".staging")
        assert not os.listdir(staging)

    def test_drop_folder(self, client, env):
        drop = os.path.join(env["cache"], "mylar_import")
        os.makedirs(drop)
        with open(os.path.join(drop, "config.ini"), "wb") as f:
            f.write(INI)

        status = client.get("/api/import/mylar/drop-folder").get_json()
        assert status["config_ini"] is True and status["mylar_db"] is False

        resp = client.post("/api/import/mylar/load", json={"source": "drop-folder"})
        assert resp.status_code == 200
        assert resp.get_json()["has_config"] is True
        assert os.path.isfile(os.path.join(drop, "config.ini"))  # the user's copy stays
        mylar_import.discard_import(resp.get_json()["import_id"])

    def test_empty_drop_folder(self, client, env):
        resp = client.post("/api/import/mylar/load", json={"source": "drop-folder"})
        assert resp.status_code == 400


class TestPreviewAndApply:
    def _load(self, client, tmp_path):
        resp = client.post("/api/import/mylar/load", data={
            "mylar_db": (io.BytesIO(mylar_db_bytes(tmp_path)), "mylar.db"),
        }, content_type="multipart/form-data")
        return resp.get_json()["import_id"]

    def test_preview_with_edited_mapping(self, client, env, tmp_path):
        import_id = self._load(client, tmp_path)
        other = os.path.join(env["library"], "Sub")
        resp = client.post("/api/import/mylar/preview", json={
            "import_id": import_id,
            "mapping": [{"mylar_root": "/comics", "clu_root": other}],
        })
        assert resp.status_code == 200
        assert resp.get_json()["series"][0]["path"] == os.path.join(other, "Batman")
        mylar_import.discard_import(import_id)

    def test_preview_unknown_import(self, client, env):
        resp = client.post("/api/import/mylar/preview", json={"import_id": "nope"})
        assert resp.status_code == 404

    def _wait(self, client, op_id):
        deadline = time.time() + 10
        job = None
        while time.time() < deadline:
            job = client.get(f"/api/import/mylar/status?op_id={op_id}").get_json()["job"]
            if job["status"] != "running":
                break
            time.sleep(0.05)
        return job

    def test_apply_runs_the_job(self, client, env, tmp_path):
        import_id = self._load(client, tmp_path)
        with patch.object(mylar_import, "import_series",
                          return_value={"mapped": [{"cv_id": 123, "name": "Batman"}],
                                        "folders_created": 1}) as run:
            resp = client.post("/api/import/mylar/apply", json={
                "import_id": import_id, "series": [123], "settings": {},
            })
            assert resp.status_code == 200
            job = self._wait(client, resp.get_json()["op_id"])

        assert job["status"] == "done", job
        assert job["result"]["mapped_total"] == 1
        rows = run.call_args[0][1]
        assert [r["cv_id"] for r in rows] == [123]
        assert mylar_import.get_import(import_id) is None  # discarded after apply

        # The outcome outlives the job, for the Guided Setup's status.
        last = mylar_import.read_last_run()
        assert last["status"] == "done"
        assert last["mapped"] == 1 and last["deferred"] == 0
        assert last["finished_at"] > 0

    def test_a_failed_apply_records_the_error(self, client, env, tmp_path):
        import_id = self._load(client, tmp_path)
        with patch.object(mylar_import, "import_series", side_effect=RuntimeError("disk full")):
            resp = client.post("/api/import/mylar/apply", json={
                "import_id": import_id, "series": [123], "settings": {},
            })
            job = self._wait(client, resp.get_json()["op_id"])
        assert job["status"] == "error"
        last = mylar_import.read_last_run()
        assert last["status"] == "error" and last["error"] == "disk full"

    def test_a_failed_record_does_not_fail_the_import(self, client, env, tmp_path):
        import_id = self._load(client, tmp_path)
        with patch.object(mylar_import, "import_series", return_value={"mapped": []}), \
             patch("core.settings.set_setting", side_effect=RuntimeError("locked")):
            resp = client.post("/api/import/mylar/apply", json={
                "import_id": import_id, "series": [123], "settings": {},
            })
            job = self._wait(client, resp.get_json()["op_id"])
        assert job["status"] == "done", job

    def test_apply_unknown_import(self, client, env):
        resp = client.post("/api/import/mylar/apply", json={"import_id": "nope"})
        assert resp.status_code == 404

    def test_apply_refuses_a_second_run(self, client, env, tmp_path):
        import_id = self._load(client, tmp_path)
        with patch.dict(mylar_import._jobs, {"x": {"status": "running"}}):
            resp = client.post("/api/import/mylar/apply", json={"import_id": import_id})
        assert resp.status_code == 409
        mylar_import.discard_import(import_id)

    def test_status_without_op_id_returns_the_latest_job(self, client, env):
        job = {"id": "abc", "status": "running", "started_at": time.time(),
               "current": 3, "total": 9, "detail": "Saga"}
        with patch.dict(mylar_import._jobs, {"abc": job}, clear=True):
            data = client.get("/api/import/mylar/status").get_json()
            page = client.get("/import/mylar").data
        assert data["job"]["id"] == "abc"
        assert b'data-latest-job="abc"' in page

    def test_status_without_op_id_and_no_job(self, client, env):
        with patch.dict(mylar_import._jobs, clear=True):
            data = client.get("/api/import/mylar/status").get_json()
        assert data == {"success": True, "job": None}

    def test_status_unknown(self, client, env):
        assert client.get("/api/import/mylar/status?op_id=nope").status_code == 404
