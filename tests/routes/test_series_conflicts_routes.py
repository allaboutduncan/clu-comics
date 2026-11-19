"""Routes for Library Health's series folder conflicts (routes/library_health.py)."""

import os
from unittest.mock import patch

import pytest


@pytest.fixture
def conflict(tmp_path_library, tmp_path):
    from core import series_conflicts as sc

    folder = tmp_path / "Image" / "Savage Dragon"
    folder.mkdir(parents=True)
    (folder / "SD 001.cbz").write_bytes(b"PK")
    mapped = tmp_path / "Image Comics" / "Savage Dragon"
    mapped.mkdir(parents=True)
    sc.record_scan([{
        "folder": str(folder), "metron_id": 101, "series_name": "Savage Dragon",
        "year": 1993, "publisher_name": "Image", "status": "Ongoing", "cv_id": None,
        "source": "series.json:metron_id", "conflict_with": str(mapped),
    }], [tmp_path_library])
    row = sc.list_conflicts()[0]
    row["mapped"] = str(mapped)
    return row


class TestPage:
    def test_page_renders(self, client):
        resp = client.get("/library-health/series")
        assert resp.status_code == 200
        assert b"Series Folder Conflicts" in resp.data
        assert b"library_series_conflicts.js" in resp.data

    def test_library_health_links_to_it(self, client):
        resp = client.get("/library-health")
        assert b"/library-health/series" in resp.data

    def test_summary_carries_the_count(self, client, conflict):
        data = client.get("/api/problem-files/health/summary").get_json()
        assert data["series_conflicts"] == 1


class TestList:
    def test_lists_conflicts(self, client, conflict):
        data = client.get("/api/problem-files/series-conflicts").get_json()
        assert data["success"] is True
        assert data["open"] == 1
        row = data["conflicts"][0]
        assert row["folder"] == conflict["folder"]
        assert row["reachable"] is True
        assert "files_sig" not in row and "ignored_sig" not in row

    def test_read_failure_is_500_not_empty(self, client):
        with patch("core.series_conflicts.list_conflicts", return_value=None):
            resp = client.get("/api/problem-files/series-conflicts")
        assert resp.status_code == 500


class TestIgnore:
    def test_ignore_and_show_ignored(self, client, conflict):
        resp = client.post("/api/problem-files/series-conflicts/ignore", json={"id": conflict["id"]})
        assert resp.status_code == 200
        assert client.get("/api/problem-files/series-conflicts").get_json()["conflicts"] == []
        rows = client.get("/api/problem-files/series-conflicts?include_ignored=1").get_json()["conflicts"]
        assert rows[0]["ignored"] is True

    def test_unignore(self, client, conflict):
        client.post("/api/problem-files/series-conflicts/ignore", json={"id": conflict["id"]})
        client.post("/api/problem-files/series-conflicts/ignore",
                    json={"id": conflict["id"], "ignored": False})
        assert client.get("/api/problem-files/series-conflicts").get_json()["open"] == 1

    @pytest.mark.parametrize("body", [{}, {"id": "x"}, {"id": -1}])
    def test_bad_id_is_400(self, client, body):
        assert client.post("/api/problem-files/series-conflicts/ignore", json=body).status_code == 400

    def test_unknown_id_is_404(self, client, db_connection):
        resp = client.post("/api/problem-files/series-conflicts/ignore", json={"id": 999})
        assert resp.status_code == 404


class TestRemove:
    def test_refuses_a_folder_that_is_still_there(self, client, conflict):
        resp = client.post("/api/problem-files/series-conflicts/remove", json={"id": conflict["id"]})
        assert resp.status_code == 409

    def test_removes_a_gone_folder(self, client, conflict):
        for name in os.listdir(conflict["folder"]):
            os.remove(os.path.join(conflict["folder"], name))
        os.rmdir(conflict["folder"])
        resp = client.post("/api/problem-files/series-conflicts/remove", json={"id": conflict["id"]})
        assert resp.status_code == 200
        assert client.get("/api/problem-files/series-conflicts").get_json()["conflicts"] == []


class TestSwitch:
    def test_maps_the_folder_and_lists_the_old_one(self, client, conflict):
        fake = {"applied": 1, "failed": [], "applied_ids": [101]}
        with patch("models.library_automap.apply_and_sync", return_value=fake) as apply:
            resp = client.post("/api/problem-files/series-conflicts/switch", json={"id": conflict["id"]})
        assert resp.status_code == 200
        item = apply.call_args[0][0][0]
        assert item["folder"] == conflict["folder"]
        assert item["metron_id"] == 101
        rows = client.get("/api/problem-files/series-conflicts").get_json()["conflicts"]
        assert rows[0]["folder"] == conflict["mapped"]

    def test_failed_apply_is_500_and_keeps_the_row(self, client, conflict):
        fake = {"applied": 0, "failed": [{"folder": "x", "error": "Traceback secrets"}], "applied_ids": []}
        with patch("models.library_automap.apply_and_sync", return_value=fake):
            resp = client.post("/api/problem-files/series-conflicts/switch", json={"id": conflict["id"]})
        assert resp.status_code == 500
        assert "secrets" not in resp.get_json()["error"]
        rows = client.get("/api/problem-files/series-conflicts").get_json()["conflicts"]
        assert rows[0]["folder"] == conflict["folder"]

    def test_folder_outside_a_library_is_refused(self, client, conflict):
        with patch("helpers.library.is_valid_library_path", return_value=False), \
                patch("models.library_automap.apply_and_sync") as apply:
            resp = client.post("/api/problem-files/series-conflicts/switch", json={"id": conflict["id"]})
        assert resp.status_code == 400
        apply.assert_not_called()

    def test_unknown_id_is_404(self, client, db_connection):
        resp = client.post("/api/problem-files/series-conflicts/switch", json={"id": 999})
        assert resp.status_code == 404


def _plan(client, conflict, **body):
    url = f"/api/problem-files/series-conflicts/{conflict['id']}/plan"
    return client.post(url, json=body) if body else client.get(url)


class TestPlan:
    def test_lists_both_folders(self, client, conflict):
        resp = _plan(client, conflict)
        assert resp.status_code == 200
        data = resp.get_json()
        assert [m["name"] for m in data["plan"]["only_here"]] == ["SD 001.cbz"]
        assert data["summary"] == {"move": 1, "trash": 0, "held": 0}
        assert data["plan"]["signature"]

    def test_keeper_choice_changes_the_actions_not_the_signature(self, client, conflict):
        (__import__("pathlib").Path(conflict["mapped"]) / "SD 001.cbz").write_bytes(b"PK" + b"x" * 50)
        default = _plan(client, conflict).get_json()["plan"]
        key = default["pairs"][0]["key"]
        assert default["pairs"][0]["keep"] == "mapped"
        chosen = _plan(client, conflict, mode="merge", keep={key: "here"}).get_json()["plan"]
        assert [a["op"] for a in chosen["actions"]] == ["replace"]
        assert chosen["signature"] == default["signature"]

    def test_unknown_id_is_404(self, client, db_connection):
        assert client.get("/api/problem-files/series-conflicts/999/plan").status_code == 404

    def test_outside_a_library_is_400(self, client, conflict):
        with patch("helpers.library.is_valid_library_path", return_value=False):
            assert _plan(client, conflict).status_code == 400

    def test_bad_keep_is_400(self, client, conflict):
        assert _plan(client, conflict, keep=["x"]).status_code == 400


class TestMerge:
    def _post(self, client, conflict, **extra):
        sig = _plan(client, conflict).get_json()["plan"]["signature"]
        body = {"id": conflict["id"], "mode": "merge", "signature": sig}
        body.update(extra)
        return client.post("/api/problem-files/series-conflicts/merge", json=body)

    def test_queues_one_file_job(self, client, conflict):
        with patch("core.job_queue.file_jobs.submit", return_value="op-1") as submit:
            resp = self._post(client, conflict)
        assert resp.status_code == 200
        assert resp.get_json()["op_id"] == "op-1"
        assert submit.call_args[0][0] == "series_merge"

    def test_changed_folder_is_409_and_queues_nothing(self, client, conflict):
        sig = _plan(client, conflict).get_json()["plan"]["signature"]
        (__import__("pathlib").Path(conflict["folder"]) / "SD 002.cbz").write_bytes(b"PK")
        with patch("core.job_queue.file_jobs.submit") as submit:
            resp = client.post("/api/problem-files/series-conflicts/merge",
                               json={"id": conflict["id"], "signature": sig})
        assert resp.status_code == 409
        submit.assert_not_called()

    def test_missing_signature_is_400(self, client, conflict):
        resp = client.post("/api/problem-files/series-conflicts/merge", json={"id": conflict["id"]})
        assert resp.status_code == 400

    def test_grant_refusal_queues_nothing(self, client, conflict):
        sig = _plan(client, conflict).get_json()["plan"]["signature"]
        with patch("core.path_access.grant_refusal", return_value=("No access", 403)), \
                patch("core.job_queue.file_jobs.submit") as submit:
            resp = client.post("/api/problem-files/series-conflicts/merge",
                               json={"id": conflict["id"], "signature": sig})
        assert resp.status_code == 403
        submit.assert_not_called()

    def test_drop_with_nothing_to_drop_is_400(self, client, conflict):
        with patch("core.job_queue.file_jobs.submit") as submit:
            resp = self._post(client, conflict, mode="drop")
        assert resp.status_code == 400
        submit.assert_not_called()

    def test_the_job_merges_end_to_end(self, client, conflict):
        """Run the queued body inline: the comic lands in the mapped folder, the row goes."""
        from core import series_conflicts as sc

        captured = {}
        with patch("core.job_queue.file_jobs.submit",
                   side_effect=lambda *a, **k: captured.setdefault("fn", a[3]) and "op-2"), \
                patch("helpers.collection.reconcile_wanted_for_series"), \
                patch("helpers.trash.move_to_trash",
                      side_effect=lambda p: __import__("shutil").rmtree(p) or {"trashed": False}):
            assert self._post(client, conflict).status_code == 200
            captured["fn"]("op-2")
        assert os.listdir(conflict["mapped"]) == ["SD 001.cbz"]
        assert not os.path.exists(conflict["folder"])
        assert sc.get_conflict(conflict["id"]) is None

    def test_the_job_trashes_on_a_worker_thread(self, app, client, conflict, tmp_path):
        """The real trash, on a thread with no app context of its own.

        ``move_to_trash`` reads app.config; run bare on the queue's worker it
        raised "Working outside of application context" for every file, so a
        Drop trashed nothing and the conflict stayed listed.
        """
        import threading

        from core import series_conflicts as sc

        trash = tmp_path / "trash-bin"
        trash.mkdir()
        app.config.update(TRASH_ENABLED=True, TRASH_DIR=str(trash), TRASH_MAX_SIZE_MB=100)
        dup = __import__("pathlib").Path(conflict["mapped"]) / "SD 001.cbz"
        dup.write_bytes(b"PK")

        captured = {}
        with patch("core.job_queue.file_jobs.submit",
                   side_effect=lambda *a, **k: captured.setdefault("fn", a[3]) and "op-3"), \
                patch("helpers.collection.reconcile_wanted_for_series"):
            assert self._post(client, conflict, mode="drop").status_code == 200
            worker = threading.Thread(target=captured["fn"], args=("op-3",))
            worker.start()
            worker.join(30)
        assert "SD 001.cbz" in os.listdir(trash)
        assert not os.path.exists(os.path.join(conflict["folder"], "SD 001.cbz"))
        assert sc.get_conflict(conflict["id"]) is None


@pytest.fixture
def group(tmp_path_library, tmp_path):
    """Two series in /Image, both mapped under /Image Comics."""
    from core import series_conflicts as sc

    review = []
    for sid, name in ((201, "Kaya"), (202, "Feral")):
        here = tmp_path / "Image" / name
        here.mkdir(parents=True)
        (here / f"{name} 001.cbz").write_bytes(b"PK")
        mapped = tmp_path / "Image Comics" / name
        mapped.mkdir(parents=True)
        review.append({"folder": str(here), "metron_id": sid, "series_name": name,
                       "conflict_with": str(mapped)})
    sc.record_scan(review, [tmp_path_library])
    return sorted(r["id"] for r in sc.list_conflicts())


class TestGroupsList:
    def test_list_carries_the_group(self, client, group):
        data = client.get("/api/problem-files/series-conflicts").get_json()
        assert len(data["groups"]) == 1
        assert sorted(data["groups"][0]["ids"]) == group
        assert data["groups"][0]["parent"].endswith("/Image")


class TestBulkPlan:
    def test_dry_run_totals(self, client, group):
        data = client.post("/api/problem-files/series-conflicts/bulk-plan", json={"ids": group}).get_json()
        assert data["success"] is True
        assert data["totals"] == {"series": 2, "move": 2, "trash": 0, "held": 0}
        assert all(it["ok"] and it["signature"] for it in data["items"])

    def test_an_unknown_id_is_reported_not_fatal(self, client, group):
        data = client.post("/api/problem-files/series-conflicts/bulk-plan",
                           json={"ids": group + [999]}).get_json()
        assert data["totals"]["series"] == 2
        assert [it for it in data["items"] if it["id"] == 999][0]["ok"] is False

    @pytest.mark.parametrize("body", [{}, {"ids": []}, {"ids": ["x"]}])
    def test_missing_ids_is_400(self, client, body):
        assert client.post("/api/problem-files/series-conflicts/bulk-plan", json=body).status_code == 400

    def test_too_many_is_400(self, client):
        resp = client.post("/api/problem-files/series-conflicts/bulk-plan",
                           json={"ids": list(range(1, 200))})
        assert resp.status_code == 400


class TestBulkMerge:
    def _items(self, client, ids):
        data = client.post("/api/problem-files/series-conflicts/bulk-plan", json={"ids": ids}).get_json()
        return [{"id": it["id"], "signature": it["signature"]} for it in data["items"] if it["ok"]]

    def test_queues_one_job(self, client, group):
        items = self._items(client, group)
        with patch("core.job_queue.file_jobs.submit", return_value="op-b") as submit:
            resp = client.post("/api/problem-files/series-conflicts/bulk-merge", json={"items": items})
        assert resp.status_code == 200
        assert resp.get_json()["series"] == 2
        submit.assert_called_once()

    def test_a_changed_folder_refuses_everything(self, client, group, tmp_path):
        items = self._items(client, group)
        (tmp_path / "Image" / "Kaya" / "Kaya 002.cbz").write_bytes(b"PK")
        with patch("core.job_queue.file_jobs.submit") as submit:
            resp = client.post("/api/problem-files/series-conflicts/bulk-merge", json={"items": items})
        assert resp.status_code == 409
        submit.assert_not_called()

    def test_grant_refusal_queues_nothing(self, client, group):
        items = self._items(client, group)
        with patch("core.path_access.grant_refusal", return_value=("No access", 403)), \
                patch("core.job_queue.file_jobs.submit") as submit:
            resp = client.post("/api/problem-files/series-conflicts/bulk-merge", json={"items": items})
        assert resp.status_code == 403
        submit.assert_not_called()

    @pytest.mark.parametrize("items", [None, [], [{"id": 1}], ["x"]])
    def test_bad_items_is_400(self, client, items):
        resp = client.post("/api/problem-files/series-conflicts/bulk-merge", json={"items": items})
        assert resp.status_code == 400

    def test_the_job_merges_every_series_on_a_worker_thread(self, app, client, group, tmp_path):
        import threading

        from core import series_conflicts as sc

        items = self._items(client, group)
        captured = {}
        with patch("core.job_queue.file_jobs.submit",
                   side_effect=lambda *a, **k: captured.setdefault("fn", a[3]) and "op-c"), \
                patch("helpers.collection.reconcile_wanted_for_series"), \
                patch("helpers.trash.move_to_trash",
                      side_effect=lambda p: __import__("shutil").rmtree(p) or {"trashed": False}):
            assert client.post("/api/problem-files/series-conflicts/bulk-merge",
                               json={"items": items}).status_code == 200
            worker = threading.Thread(target=captured["fn"], args=("op-c",))
            worker.start()
            worker.join(30)
        assert os.listdir(tmp_path / "Image Comics" / "Kaya") == ["Kaya 001.cbz"]
        assert os.listdir(tmp_path / "Image Comics" / "Feral") == ["Feral 001.cbz"]
        assert sc.list_conflicts() == []
