"""The automatic download run reports (core/download_runs) against a real schema.

Each bound on the tables' growth has a test, because "keep everything" is the
easiest simplification to make and the one that turns a report into the largest
table in the database.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from core import download_runs as dr


@pytest.fixture
def store(db_connection, db_path):
    """Point the module's lazy db_conn at the test database."""
    with patch("core.database.get_db_path", return_value=db_path):
        yield db_connection


def _item(run, n, outcome=dr.OUTCOME_NO_MATCH, download_id=None, **kw):
    run.item("Batman", str(n), outcome, series_id=1, source="mapped_series",
             download_id=download_id, **kw)


class TestRecording:
    def test_a_run_and_its_items_round_trip(self, store):
        run = dr.start_run(dr.KIND_GETCOMICS)
        _item(run, 1, dr.OUTCOME_QUEUED, download_id="d1", detail="Batman 001.cbz")
        _item(run, 2, dr.OUTCOME_NO_RESULTS)
        run.count(dr.COUNT_OWNED, 40)
        run.finish(dr.STATUS_OK)

        got = dr.get_run(run.run_id)
        assert got["status"] == dr.STATUS_OK
        assert got["finished_at"]
        assert [i["issue_number"] for i in got["items"]] == ["1", "2"]
        assert got["items"][0]["download_status"] == "queued"
        assert got["items"][1]["download_status"] is None
        assert got["counts"][dr.COUNT_OWNED] == 40
        assert got["tally"]["searched"] == 2
        assert got["tally"]["queued"] == 1
        assert got["tally"]["not_found"] == 1
        assert got["tally"]["skipped"] == 40

    def test_skips_are_counted_never_listed(self, store):
        """The bulk of a sweep is already-owned issues; they must not be rows."""
        run = dr.start_run(dr.KIND_GETCOMICS)
        for _ in range(500):
            run.count(dr.COUNT_OWNED)
        run.finish()
        assert dr.get_run(run.run_id)["items"] == []
        rows = store.execute("SELECT COUNT(*) FROM download_run_items").fetchone()[0]
        assert rows == 0

    def test_items_past_the_cap_are_only_counted(self, store):
        run = dr.start_run(dr.KIND_GETCOMICS)
        with patch.object(dr, "RUN_ITEM_CAP", 5):
            for n in range(8):
                _item(run, n)
        run.finish()
        got = dr.get_run(run.run_id)
        assert len(got["items"]) == 5
        assert got["items_truncated"] is True
        # The counters still cover every searched issue.
        assert got["tally"]["searched"] == 8

    def test_items_are_flushed_in_batches_before_finish(self, store):
        """A crash mid-sweep keeps what was already flushed."""
        run = dr.start_run(dr.KIND_GETCOMICS)
        with patch.object(dr, "FLUSH_EVERY", 3):
            for n in range(4):
                _item(run, n)
        rows = store.execute("SELECT COUNT(*) FROM download_run_items").fetchone()[0]
        assert rows == 3

    def test_explicit_flush_writes_before_a_worker_can_settle(self, store):
        run = dr.start_run(dr.KIND_GETCOMICS)
        _item(run, 1, dr.OUTCOME_QUEUED, download_id="d1")
        run.flush()
        dr.set_download_outcome("d1", "complete")
        run.finish()
        assert dr.get_run(run.run_id)["items"][0]["download_status"] == "complete"

    def test_finish_twice_is_harmless(self, store):
        run = dr.start_run(dr.KIND_GETCOMICS)
        run.finish(dr.STATUS_OK)
        run.finish(dr.STATUS_ERROR, "late")
        assert dr.get_run(run.run_id)["status"] == dr.STATUS_OK

    def test_scope_is_written(self, store):
        run = dr.start_run(dr.KIND_GETCOMICS, dr.TRIGGER_SERIES)
        run.set_scope("Batman")
        run.finish()
        got = dr.get_run(run.run_id)
        assert got["scope"] == "Batman"
        assert got["trigger"] == dr.TRIGGER_SERIES

    def test_long_detail_is_clipped(self, store):
        run = dr.start_run(dr.KIND_GETCOMICS)
        _item(run, 1, detail="x" * 5000)
        run.finish()
        assert len(dr.get_run(run.run_id)["items"][0]["detail"]) == dr.DETAIL_MAX


class TestNeverRaises:
    def test_start_run_without_a_database_is_a_null_recorder(self):
        with patch("core.database.get_db_connection", return_value=None):
            run = dr.start_run(dr.KIND_GETCOMICS)
        assert run is dr.NULL_RECORDER
        run.count("x")
        _item(run, 1)
        run.flush()
        run.finish()

    def test_a_failing_write_is_swallowed(self, store):
        run = dr.start_run(dr.KIND_GETCOMICS)
        _item(run, 1)
        with patch("core.database.get_db_connection", side_effect=RuntimeError("locked")):
            run.finish()
            dr.set_download_outcome("d1", "failed")

    def test_null_recorder_has_the_recorder_interface(self):
        """The sweep calls these unconditionally on a dry run."""
        public = {n for n in vars(dr.RunRecorder) if not n.startswith("_")}
        assert public <= set(dir(dr.NULL_RECORDER))


class TestRetention:
    def _runs(self, conn, kind=dr.KIND_GETCOMICS):
        return [r[0] for r in conn.execute(
            "SELECT id FROM download_runs WHERE kind = ? ORDER BY id", (kind,)
        ).fetchall()]

    def test_keeps_only_the_newest_runs_per_kind(self, store):
        with patch.object(dr, "_keep_runs", return_value=3):
            ids = []
            for n in range(5):
                run = dr.start_run(dr.KIND_GETCOMICS)
                _item(run, n)
                run.finish()
                ids.append(run.run_id)
        # Pruning runs before the insert, so at most keep + 1 survive.
        assert self._runs(store) == ids[-4:]
        orphan = store.execute(
            "SELECT COUNT(*) FROM download_run_items WHERE run_id NOT IN "
            "(SELECT id FROM download_runs)"
        ).fetchone()[0]
        assert orphan == 0

    def test_one_kind_never_prunes_another(self, store):
        weekly = dr.start_run(dr.KIND_WEEKLY_PACKS)
        weekly.finish()
        with patch.object(dr, "_keep_runs", return_value=1):
            for _ in range(4):
                dr.start_run(dr.KIND_GETCOMICS).finish()
        assert self._runs(store, dr.KIND_WEEKLY_PACKS) == [weekly.run_id]

    def test_drops_runs_older_than_the_age_limit(self, store):
        old = dr.start_run(dr.KIND_GETCOMICS)
        _item(old, 1)
        old.finish()
        stale = (datetime.now(timezone.utc) - timedelta(days=dr.MAX_AGE_DAYS + 1))
        store.execute(
            "UPDATE download_runs SET started_at = ? WHERE id = ?",
            (stale.strftime("%Y-%m-%d %H:%M:%S"), old.run_id),
        )
        store.commit()
        new = dr.start_run(dr.KIND_GETCOMICS)
        assert self._runs(store) == [new.run_id]
        assert store.execute("SELECT COUNT(*) FROM download_run_items").fetchone()[0] == 0

    def test_keep_setting_is_read_per_call(self, store):
        with patch("core.settings.get_int", return_value=7):
            assert dr._keep_runs() == 7
        with patch("core.settings.get_int", return_value=0):
            assert dr._keep_runs() == dr.DEFAULT_KEEP


class TestOutcomes:
    def test_outcome_lands_on_the_item(self, store):
        run = dr.start_run(dr.KIND_GETCOMICS)
        _item(run, 1, dr.OUTCOME_QUEUED, download_id="d1")
        run.finish()
        dr.set_download_outcome("d1", "failed", detail="All mirrors failed")
        item = dr.get_run(run.run_id)["items"][0]
        assert item["download_status"] == "failed"
        assert item["detail"] == "All mirrors failed"

    def test_outcome_without_detail_keeps_the_detail(self, store):
        run = dr.start_run(dr.KIND_GETCOMICS)
        _item(run, 1, dr.OUTCOME_QUEUED, download_id="d1", detail="Batman 001.cbz")
        run.finish()
        dr.set_download_outcome("d1", "complete")
        item = dr.get_run(run.run_id)["items"][0]
        assert item["detail"] == "Batman 001.cbz"

    def test_an_unknown_download_is_a_no_op(self, store):
        """Every manual grab settles through the same hook."""
        dr.set_download_outcome("not-from-a-run", "complete")
        dr.set_download_outcome(None, "complete")


class TestReconcile:
    def test_running_runs_and_pending_downloads_are_closed(self, store):
        run = dr.start_run(dr.KIND_GETCOMICS)
        _item(run, 1, dr.OUTCOME_QUEUED, download_id="d1", provider="getcomics")
        _item(run, 2, dr.OUTCOME_QUEUED, download_id="d2", provider="usenet")
        _item(run, 3, dr.OUTCOME_QUEUED, download_id="d3", provider="dcpp")
        run.flush()  # the process "dies" here, never calling finish()

        assert dr.reconcile_interrupted_runs() == 1
        got = dr.get_run(run.run_id)
        assert got["status"] == dr.STATUS_ERROR
        assert "Interrupted" in got["error"]
        statuses = {i["provider"]: i["download_status"] for i in got["items"]}
        assert statuses["getcomics"] == "interrupted"
        assert statuses["usenet"] == "interrupted"
        # DC++ jobs are persisted and re-adopted at startup.
        assert statuses["dcpp"] == "queued"

    def test_settled_downloads_are_left_alone(self, store):
        run = dr.start_run(dr.KIND_GETCOMICS)
        _item(run, 1, dr.OUTCOME_QUEUED, download_id="d1")
        run.finish()
        dr.set_download_outcome("d1", "complete")
        dr.reconcile_interrupted_runs()
        assert dr.get_run(run.run_id)["items"][0]["download_status"] == "complete"


class TestReadSide:
    @pytest.fixture
    def run(self, store):
        run = dr.start_run(dr.KIND_GETCOMICS)
        _item(run, 1, dr.OUTCOME_QUEUED, download_id="ok")
        _item(run, 2, dr.OUTCOME_QUEUED, download_id="bad")
        _item(run, 3, dr.OUTCOME_QUEUED, download_id="pending")
        _item(run, 4, dr.OUTCOME_NO_MATCH)
        run.finish()
        dr.set_download_outcome("ok", "complete")
        dr.set_download_outcome("bad", "failed")
        return run

    @pytest.mark.parametrize("flt, issues", [
        ("all", ["1", "2", "3", "4"]),
        ("queued", ["1", "2", "3"]),
        ("downloaded", ["1"]),
        ("failed", ["2"]),
        ("not_found", ["4"]),
        # A download still in flight is not missing yet.
        ("missing", ["2", "4"]),
    ])
    def test_filters(self, run, flt, issues):
        got = dr.get_run(run.run_id, flt)
        assert [i["issue_number"] for i in got["items"]] == issues

    def test_unknown_filter_raises(self, run):
        with pytest.raises(ValueError):
            dr.get_run(run.run_id, "everything; DROP TABLE x")

    def test_unknown_run_is_none(self, store):
        assert dr.get_run(999999) is None

    def test_list_is_newest_first_and_filterable(self, store):
        a = dr.start_run(dr.KIND_GETCOMICS)
        a.finish()
        b = dr.start_run(dr.KIND_WEEKLY_PACKS)
        b.finish()
        assert [r["id"] for r in dr.list_runs()] == [b.run_id, a.run_id]
        assert [r["id"] for r in dr.list_runs(kind=dr.KIND_GETCOMICS)] == [a.run_id]

    def test_list_read_failure_is_none_not_empty(self):
        with patch("core.database.get_db_connection", side_effect=RuntimeError("boom")):
            assert dr.list_runs() is None
