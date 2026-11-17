"""core.reading_metadata_backfill: the Insights page's ComicInfo backfill.

It used to run in the request on one connection, so the first UPDATE opened a
write transaction that was held across every remaining archive read. These
tests pin that no transaction is open while files are read, that writes land
in batches, and that only one run can go at a time.
"""
import os
import sqlite3
import time
from unittest.mock import patch

import pytest

import core.reading_metadata_backfill as bf

COMICINFO = (
    "<ComicInfo><Writer>Ron Lim [3258], Ron Lim</Writer>"
    "<Penciller>Jim Starlin</Penciller><Characters>Thanos</Characters>"
    "<Publisher>Marvel [31]</Publisher></ComicInfo>"
)


@pytest.fixture(autouse=True)
def _reset_state():
    with bf._last_result_lock:
        bf._last_result = None
    yield
    deadline = time.time() + 10
    while bf.is_running() and time.time() < deadline:
        time.sleep(0.02)
    with bf._last_result_lock:
        bf._last_result = None


def _add_read(conn, path, user_id=1):
    conn.execute(
        "INSERT INTO issues_read (user_id, issue_path) VALUES (?, ?)",
        (user_id, path),
    )
    conn.commit()


def _row(conn, path, user_id=1):
    return conn.execute(
        "SELECT writer, penciller, characters, publisher FROM issues_read "
        "WHERE issue_path = ? AND user_id = ?",
        (path, user_id),
    ).fetchone()


class TestRunBackfill:
    def test_fills_and_normalises_metadata(self, db_connection, create_cbz):
        path = str(create_cbz("Silver Surfer 001.cbz", comicinfo_xml=COMICINFO))
        _add_read(db_connection, path)

        result = bf.run_backfill()

        assert result["updated"] == 1 and result["skipped"] == 0
        assert result["error"] is None
        assert tuple(_row(db_connection, path)) == (
            "Ron Lim", "Jim Starlin", "Thanos", "Marvel")

    def test_skip_reasons(self, db_connection, create_cbz, tmp_path):
        no_info = str(create_cbz("bare.cbz"))
        cbr = tmp_path / "x.cbr"
        cbr.write_bytes(b"Rar!")
        _add_read(db_connection, no_info)
        _add_read(db_connection, str(cbr))
        _add_read(db_connection, str(tmp_path / "gone.cbz"))

        result = bf.run_backfill()

        reasons = {i["file"]: i["reason"] for i in result["skipped_issues"]}
        assert reasons == {
            "bare.cbz": "No ComicInfo.xml",
            "x.cbr": "Not a CBZ/ZIP file",
            "gone.cbz": "File not found",
        }
        assert result["updated"] == 0 and result["skipped"] == 3

    def test_one_read_per_path_updates_every_users_row(self, db_connection, create_cbz):
        path = str(create_cbz("Shared 001.cbz", comicinfo_xml=COMICINFO))
        _add_read(db_connection, path, user_id=1)
        _add_read(db_connection, path, user_id=2)

        from core.comicinfo import read_comicinfo_from_zip as real
        calls = []

        def _counting(p):
            calls.append(p)
            return real(p)

        with patch("core.comicinfo.read_comicinfo_from_zip", side_effect=_counting):
            result = bf.run_backfill()

        assert calls == [path]
        assert result["total"] == 1
        assert _row(db_connection, path, 1)[0] == "Ron Lim"
        assert _row(db_connection, path, 2)[0] == "Ron Lim"

    def test_another_connection_can_write_while_files_are_read(
            self, db_connection, db_path, create_cbz):
        """The regression: a write transaction held across archive reads.

        Every archive read opens its own connection with NO busy timeout and
        commits a write. With the old code the second read would fail at once
        with "database is locked", because the first UPDATE's transaction was
        still open.
        """
        paths = [str(create_cbz(f"Issue {n:03d}.cbz", comicinfo_xml=COMICINFO))
                 for n in range(1, 6)]
        for p in paths:
            _add_read(db_connection, p)

        from core.comicinfo import read_comicinfo_from_zip as real
        writes = []

        def _read_and_write(p):
            other = sqlite3.connect(db_path, timeout=0)
            try:
                other.execute(
                    "INSERT INTO stats_cache (key, value) VALUES (?, ?)",
                    (f"probe:{os.path.basename(p)}", "x"),
                )
                other.commit()
                writes.append(p)
            finally:
                other.close()
            return real(p)

        with patch.object(bf, "BATCH_SIZE", 2), \
                patch("core.comicinfo.read_comicinfo_from_zip", side_effect=_read_and_write):
            result = bf.run_backfill()

        assert writes == sorted(paths)
        assert result["updated"] == 5 and result["error"] is None
        for p in paths:
            assert _row(db_connection, p)[0] == "Ron Lim"

    def test_writes_in_batches(self, db_connection, create_cbz):
        paths = [str(create_cbz(f"B {n:03d}.cbz", comicinfo_xml=COMICINFO))
                 for n in range(1, 6)]
        for p in paths:
            _add_read(db_connection, p)

        sizes = []
        real = bf._write_batch

        def _spy(batch):
            sizes.append(len(batch))
            return real(batch)

        with patch.object(bf, "BATCH_SIZE", 2), patch.object(bf, "_write_batch", _spy):
            bf.run_backfill()

        assert [s for s in sizes if s] == [2, 2, 1]

    def test_failed_write_is_reported_not_counted(self, db_connection, create_cbz):
        path = str(create_cbz("W 001.cbz", comicinfo_xml=COMICINFO))
        _add_read(db_connection, path)

        with patch.object(bf, "_write_batch", return_value=False):
            result = bf.run_backfill()

        assert result["updated"] == 0
        assert result["error"]
        assert result["skipped_issues"] == [
            {"file": "W 001.cbz", "reason": "Database write failed"}]

    def test_clears_reading_caches_of_every_user_with_rows(self, db_connection, create_cbz):
        path = str(create_cbz("C 001.cbz", comicinfo_xml=COMICINFO))
        _add_read(db_connection, path, user_id=1)
        _add_read(db_connection, path, user_id=2)

        with patch("core.database.clear_stats_cache_prefix") as clear:
            bf.run_backfill(user_id=1)

        assert sorted(c.args[0] for c in clear.call_args_list) == ["u1:", "u2:"]

    def test_skip_report_is_capped(self, db_connection, tmp_path):
        for n in range(5):
            _add_read(db_connection, str(tmp_path / f"gone{n}.cbz"))

        with patch.object(bf, "MAX_REPORTED_SKIPS", 2):
            result = bf.run_backfill()

        assert result["skipped"] == 5
        assert len(result["skipped_issues"]) == 2
        assert result["skipped_issues_truncated"] is True

    def test_per_item_lines_are_debug_and_one_info_summary(self, db_connection, tmp_path):
        for n in range(3):
            _add_read(db_connection, str(tmp_path / f"gone{n}.cbz"))

        with patch.object(bf, "app_logger") as log:
            bf.run_backfill()

        assert log.info.call_count == 1
        assert log.debug.call_count == 3
        log.warning.assert_not_called()


class TestSingleRun:
    def test_second_start_is_refused_while_running(self, db_connection):
        assert bf._run_lock.acquire(blocking=False)
        try:
            assert bf.is_running()
            assert bf.start_backfill(user_id=1) is None
        finally:
            bf._run_lock.release()

    def test_lock_released_after_run_and_result_kept(self, db_connection, create_cbz):
        import core.app_state as app_state

        path = str(create_cbz("R 001.cbz", comicinfo_xml=COMICINFO))
        _add_read(db_connection, path)

        op_id = bf.start_backfill(user_id=1)
        assert op_id
        deadline = time.time() + 10
        while bf.is_running() and time.time() < deadline:
            time.sleep(0.02)

        assert not bf.is_running()
        assert app_state.get_operation(op_id)["status"] == "completed"
        assert bf.last_result(user_id=1)["updated"] == 1
        # The report names files from the history; only its starter gets it.
        assert bf.last_result(user_id=2) is None

    def test_lock_released_when_run_crashes(self, db_connection):
        import core.app_state as app_state

        with patch.object(bf, "run_backfill", side_effect=RuntimeError("boom")):
            op_id = bf.start_backfill(user_id=1)
            deadline = time.time() + 10
            while bf.is_running() and time.time() < deadline:
                time.sleep(0.02)

        assert not bf.is_running()
        assert app_state.get_operation(op_id)["status"] == "error"


def test_app_py_no_longer_serves_the_route():
    """routes/reading.py owns it; a second copy in app.py would drift (and
    app.py cannot be imported in tests, so it would be untested)."""
    app_src = os.path.join(os.path.dirname(__file__), "..", "..", "app.py")
    with open(app_src, encoding="utf-8") as f:
        assert "/api/backfill-reading-metadata" not in f.read()
