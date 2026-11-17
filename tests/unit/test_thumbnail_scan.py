"""core.thumbnail_scan: the startup thumbnail scan, against a real database.

The scan used to open a write transaction with a directory's first upsert and
hold it while it stat'd every file in that directory and submitted the jobs,
committing only at the end of the directory. On a slow NAS that held the WAL
write lock across all of that I/O, so every other writer waited out its 30s
busy_timeout and failed with "database is locked" -- including the scan's own
jobs, whose terminal write queued behind the directory's commit.
"""

import os
import sqlite3

import pytest

from core import thumbnail_scan
from core.thumbnail_scan import needs_thumbnail, scan_library_thumbnails


def _comic(folder, name, mtime=None):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return str(path)


def _seed(conn, path, status, file_mtime):
    conn.execute(
        "INSERT INTO thumbnail_jobs (path, status, file_mtime) VALUES (?, ?, ?)",
        (path, status, file_mtime),
    )
    conn.commit()


def _row(conn, path):
    return conn.execute(
        "SELECT status, file_mtime FROM thumbnail_jobs WHERE path = ?", (path,)
    ).fetchone()


def _write_lock_is_free(db_path):
    """Can another connection take the write lock right now, without waiting?"""
    other = sqlite3.connect(db_path, timeout=0)
    try:
        other.execute("PRAGMA busy_timeout=0")
        other.execute("BEGIN IMMEDIATE")
        other.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        other.close()


class _Submitter:
    def __init__(self, on_submit=None):
        self.paths = []
        self.on_submit = on_submit

    def __call__(self, path):
        if self.on_submit:
            self.on_submit(path)
        self.paths.append(path)
        return True


def _scan(library, submit, **kw):
    kw.setdefault("in_flight", lambda p: False)
    return scan_library_thumbnails([str(library)], submit, **kw)


class TestNoTransactionAcrossFileIO:

    def test_another_writer_gets_the_lock_during_the_stat_phase(
        self, db_connection, db_path, tmp_path
    ):
        library = tmp_path / "lib"
        for i in range(5):
            _comic(library / "Batman", f"Batman {i:03d}.cbz")

        verdicts = []

        def stat(path):
            verdicts.append(_write_lock_is_free(db_path))
            return os.stat(path)

        # batch_size=2: the stats of chunks 2 and 3 come after chunk 1's write.
        result = _scan(library, _Submitter(), stat=stat, batch_size=2)

        assert result["queued"] == 5
        assert len(verdicts) == 5
        assert all(verdicts), "the scan held the write lock while stat'ing files"

    def test_jobs_are_submitted_only_after_their_rows_are_committed(
        self, db_connection, db_path, tmp_path
    ):
        library = tmp_path / "lib"
        paths = [_comic(library / "X-Men", f"X-Men {i:03d}.cbz") for i in range(3)]
        seen = {}

        def on_submit(path):
            # A fresh connection sees the committed row, and the lock is free:
            # a fast worker's terminal write would not queue behind the scan.
            other = sqlite3.connect(db_path, timeout=0)
            try:
                seen[path] = other.execute(
                    "SELECT status, file_mtime FROM thumbnail_jobs WHERE path = ?",
                    (path,),
                ).fetchone()
            finally:
                other.close()
            assert _write_lock_is_free(db_path)

        submitter = _Submitter(on_submit)
        _scan(library, submitter)

        assert sorted(submitter.paths) == sorted(paths)
        for path in paths:
            status, mtime = seen[path]
            assert status == "processing"
            assert mtime == pytest.approx(os.stat(path).st_mtime)

    def test_a_terminal_write_after_submit_is_not_overwritten(
        self, db_connection, tmp_path
    ):
        """A worker that finishes before the scan moves on keeps its verdict."""
        from core.thumbnail_cache import set_job_status

        library = tmp_path / "lib"
        path = _comic(library / "Saga", "Saga 001.cbz")

        _scan(library, _Submitter(lambda p: set_job_status(p, "completed")))

        assert _row(db_connection, path)["status"] == "completed"

    def test_a_large_directory_is_written_in_bounded_batches(
        self, db_connection, tmp_path, monkeypatch
    ):
        library = tmp_path / "lib"
        folder = library / "Big"
        folder.mkdir(parents=True)
        for i in range(450):
            (folder / f"Big {i:04d}.cbz").write_bytes(b"x")

        sizes = []
        real = thumbnail_scan.mark_processing_batch

        def spy(rows):
            rows = list(rows)
            sizes.append(len(rows))
            return real(rows)

        monkeypatch.setattr(thumbnail_scan, "mark_processing_batch", spy)
        submitter = _Submitter()
        result = _scan(library, submitter, batch_size=200)

        assert sizes == [200, 200, 50]
        assert result["queued"] == 450
        assert result["batches"] == 3
        count = db_connection.execute(
            "SELECT COUNT(*) FROM thumbnail_jobs WHERE status = 'processing' "
            "AND file_mtime IS NOT NULL"
        ).fetchone()[0]
        assert count == 450


class TestEnumeratesWhatTheIndexHolds:

    def test_hidden_directories_and_files_are_skipped(self, db_connection, tmp_path):
        library = tmp_path / "lib"
        keep = _comic(library / "Series", "Series 001.cbz")
        _comic(library / ".hidden", "Hidden 001.cbz")
        _comic(library / "_staging", "Staged 001.cbz")
        _comic(library / "Series", "._Series 001.cbz")
        _comic(library / "Series", "_Series 002.cbz")
        (library / "Series" / "notes.txt").write_text("x")

        submitter = _Submitter()
        _scan(library, submitter)

        assert submitter.paths == [keep]
        rows = {r[0] for r in db_connection.execute("SELECT path FROM thumbnail_jobs")}
        assert rows == {keep}

    def test_rows_the_old_walk_left_are_pruned(self, db_connection, tmp_path):
        library = tmp_path / "lib"
        library.mkdir()
        junk = str(library / "._Old 001.cbz")
        _seed(db_connection, junk, "error", 1.0)

        _scan(library, _Submitter())

        assert _row(db_connection, junk) is None


class TestWhatGetsQueued:

    def test_an_up_to_date_completed_row_is_not_requeued(self, db_connection, tmp_path):
        library = tmp_path / "lib"
        path = _comic(library / "S", "S 001.cbz", mtime=1_700_000_000)
        _seed(db_connection, path, "completed", 1_700_000_000.0)

        submitter = _Submitter()
        result = _scan(library, submitter)

        assert submitter.paths == []
        assert result["skipped"] == 1
        assert _row(db_connection, path)["status"] == "completed"

    def test_a_rewritten_completed_file_is_requeued(self, db_connection, tmp_path):
        library = tmp_path / "lib"
        path = _comic(library / "S", "S 001.cbz", mtime=1_700_000_100)
        _seed(db_connection, path, "completed", 1_700_000_000.0)

        submitter = _Submitter()
        _scan(library, submitter)

        assert submitter.paths == [path]
        row = _row(db_connection, path)
        assert row["status"] == "processing"
        assert row["file_mtime"] == pytest.approx(1_700_000_100)

    def test_an_error_row_is_retried(self, db_connection, tmp_path):
        library = tmp_path / "lib"
        path = _comic(library / "S", "S 001.cbz", mtime=1_700_000_000)
        _seed(db_connection, path, "error", 1_700_000_000.0)

        submitter = _Submitter()
        _scan(library, submitter)

        assert submitter.paths == [path]

    def test_a_null_mtime_row_is_requeued_once_and_stamped(
        self, db_connection, tmp_path
    ):
        """NULL means "migrated, mtime unknown": retry once; the row then
        carries a real mtime and settles."""
        library = tmp_path / "lib"
        path = _comic(library / "S", "S 001.cbz", mtime=1_700_000_000)
        _seed(db_connection, path, "completed", None)

        submitter = _Submitter()
        _scan(library, submitter)
        assert submitter.paths == [path]
        assert _row(db_connection, path)["file_mtime"] == pytest.approx(1_700_000_000)

        # The job completes; the next restart leaves it alone.
        db_connection.execute(
            "UPDATE thumbnail_jobs SET status = 'completed' WHERE path = ?", (path,)
        )
        db_connection.commit()
        again = _Submitter()
        _scan(library, again)
        assert again.paths == []

    def test_a_skipped_row_is_permanent(self, db_connection, tmp_path):
        library = tmp_path / "lib"
        path = _comic(library / "S", "S 001.pdf", mtime=1_700_000_000)
        _seed(db_connection, path, "skipped", 1_700_000_000.0)

        submitter = _Submitter()
        _scan(library, submitter)

        assert submitter.paths == []
        assert _row(db_connection, path)["status"] == "skipped"

    def test_an_orphaned_processing_row_is_requeued(self, db_connection, tmp_path):
        library = tmp_path / "lib"
        path = _comic(library / "S", "S 001.cbz", mtime=1_700_000_000)
        _seed(db_connection, path, "processing", 1_700_000_000.0)

        submitter = _Submitter()
        _scan(library, submitter, in_flight=lambda p: False)

        assert submitter.paths == [path]

    def test_an_in_flight_job_is_left_alone(self, db_connection, tmp_path):
        library = tmp_path / "lib"
        path = _comic(library / "S", "S 001.cbz", mtime=1_700_000_000)
        _seed(db_connection, path, "error", 1_700_000_000.0)

        submitter = _Submitter()
        _scan(library, submitter, in_flight=lambda p: True)

        assert submitter.paths == []
        assert _row(db_connection, path)["status"] == "error"

    def test_new_rows_carry_the_files_mtime(self, db_connection, tmp_path):
        library = tmp_path / "lib"
        path = _comic(library / "S", "S 001.cbr", mtime=1_650_000_000)

        _scan(library, _Submitter())

        row = _row(db_connection, path)
        assert row["status"] == "processing"
        assert row["file_mtime"] == pytest.approx(1_650_000_000)


class TestDatabaseFailures:

    def test_a_busy_database_costs_only_its_chunk(
        self, db_connection, tmp_path, monkeypatch
    ):
        library = tmp_path / "lib"
        for i in range(4):
            _comic(library / "S", f"S {i:03d}.cbz")

        real = thumbnail_scan.mark_processing_batch
        calls = []

        def flaky(rows):
            rows = list(rows)
            calls.append(rows)
            if len(calls) == 1:
                raise sqlite3.OperationalError("database is locked")
            return real(rows)

        monkeypatch.setattr(thumbnail_scan, "mark_processing_batch", flaky)
        submitter = _Submitter()
        result = _scan(library, submitter, batch_size=2)

        failed = {p for p, _ in calls[0]}
        assert result["db_failed"] == 2
        assert result["queued"] == 2
        assert not failed & set(submitter.paths), (
            "a job was submitted although its row was never written"
        )

    def test_corruption_is_raised_for_the_caller_to_latch(
        self, db_connection, tmp_path, monkeypatch
    ):
        library = tmp_path / "lib"
        _comic(library / "S", "S 001.cbz")

        def broken(paths):
            list(paths)
            raise sqlite3.DatabaseError("database disk image is malformed")

        monkeypatch.setattr(thumbnail_scan, "get_job_rows", broken)
        submitter = _Submitter()
        with pytest.raises(sqlite3.DatabaseError):
            _scan(library, submitter)
        assert submitter.paths == []

    def test_an_unreadable_file_is_counted_not_fatal(self, db_connection, tmp_path):
        library = tmp_path / "lib"
        good = _comic(library / "S", "S 001.cbz")
        bad = _comic(library / "S", "S 002.cbz")

        def stat(path):
            if path == bad:
                raise PermissionError("denied")
            return os.stat(path)

        submitter = _Submitter()
        result = _scan(library, submitter, stat=stat)

        assert submitter.paths == [good]
        assert result["stat_errors"] == 1

    def test_a_missing_library_root_is_skipped(self, db_connection, tmp_path):
        result = scan_library_thumbnails(
            [str(tmp_path / "nope")], _Submitter(), in_flight=lambda p: False
        )
        assert result["queued"] == 0


class TestDecision:
    """needs_thumbnail on its own, for the cases above at a glance."""

    @pytest.mark.parametrize(
        "existing, mtime, in_flight, expected",
        [
            (None, 10.0, False, True),
            (("completed", 10.0), 10.0, False, False),
            (("completed", 10.0), 11.0, False, True),
            (("completed", None), 10.0, False, True),
            (("error", 10.0), 10.0, False, True),
            (("skipped", 10.0), 10.0, False, False),
            (("processing", 10.0), 10.0, False, True),
            (("processing", 10.0), 10.0, True, False),
            (None, 10.0, True, False),
        ],
    )
    def test_table(self, existing, mtime, in_flight, expected):
        assert needs_thumbnail(existing, mtime, in_flight) is expected
