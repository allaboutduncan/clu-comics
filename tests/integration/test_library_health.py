"""The rolling library health scan against a real SQLite schema.

Each class names a rule in ``core/library_health.py`` that a later "tidy-up"
could plausibly break.
"""

import os
import time
import zipfile
from unittest.mock import patch

import pytest

from core import library_health as lh
from core.problem_files import (
    SOURCE_INTEGRITY,
    SOURCE_ORPHAN,
    get_problem,
    list_problems,
    record_problem,
)


@pytest.fixture
def store(db_connection):
    """db_connection already patches core.database.get_db_path for the test."""
    from core.settings import invalidate

    invalidate()
    yield db_connection
    invalidate()


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    return root


def _good_cbz(path, payload=b"x"):
    # A fixed entry timestamp: writestr() stamps "now" (2-second resolution),
    # so two "identical" archives written either side of a tick hashed
    # differently and the exact-duplicate tests failed under a loaded run.
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        for name in ("p001.jpg", "p002.jpg"):
            zf.writestr(zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0)), payload * 64)
    return str(path)


def _corrupt_cbz(path):
    """A stored (uncompressed) zip with one payload byte flipped -> bad CRC."""
    _good_cbz(path, payload=b"A")
    data = bytearray(open(path, "rb").read())
    idx = data.index(b"AAAA")
    data[idx] ^= 0xFF
    open(path, "wb").write(bytes(data))
    return str(path)


def _index(conn, path, **extra):
    st = os.stat(path)
    cols = {
        "name": os.path.basename(path),
        "path": path,
        "type": "file",
        "size": st.st_size,
        "parent": os.path.dirname(path),
        "modified_at": st.st_mtime,
    }
    cols.update(extra)
    conn.execute(
        f"INSERT OR REPLACE INTO file_index ({', '.join(cols)}) "
        f"VALUES ({', '.join('?' * len(cols))})",
        list(cols.values()),
    )
    conn.commit()


def _health(conn, path):
    return conn.execute(
        "SELECT * FROM file_health WHERE path = ?", (path,)
    ).fetchone()


@pytest.fixture(autouse=True)
def _reachable_library(library):
    with patch("helpers.library.get_library_roots", return_value=[str(library)]):
        yield


class TestInspectArchive:
    def test_good_cbz_is_ok(self, library):
        p = _good_cbz(library / "Good 001.cbz")
        assert lh.inspect_archive(p) == ("ok", None, None)

    def test_flipped_byte_is_a_crc_failure_the_page_can_explain(self, library):
        from core.problem_files import classify

        p = _corrupt_cbz(library / "Bad 001.cbz")
        status, cls, msg = lh.inspect_archive(p)
        assert status == "damaged"
        # The message has to be one classify() already knows, or the page falls
        # back to "CLU could not read this file".
        assert "replace" == classify(cls, msg)["action"]

    def test_not_a_zip_is_the_rebuildable_case(self, library):
        from core.problem_files import classify

        p = library / "Rar.cbz"
        p.write_bytes(b"Rar!\x1a\x07\x00 not a zip")
        status, cls, msg = lh.inspect_archive(str(p))
        assert status == "damaged"
        assert classify(cls, msg)["repairable"] is True

    def test_empty_file_is_damaged(self, library):
        p = library / "Empty.cbz"
        p.write_bytes(b"")
        assert lh.inspect_archive(str(p))[0] == "damaged"

    def test_cbz_without_pages_is_flagged_but_a_zip_is_not(self, library):
        cbz = library / "Text.cbz"
        zp = library / "Stuff.zip"
        for p in (cbz, zp):
            with zipfile.ZipFile(p, "w") as zf:
                zf.writestr("readme.txt", "hello")
        assert lh.inspect_archive(str(cbz))[0] == "damaged"
        # A .zip in a library may be anything; "no pages" would be a false alarm.
        assert lh.inspect_archive(str(zp))[0] == "ok"

    def test_cbr_is_hashed_and_passed(self, library):
        p = library / "Old 001.cbr"
        p.write_bytes(b"whatever")
        assert lh.inspect_archive(str(p)) == ("ok", None, None)


class TestSingleRead:
    """A small ZIP is read once; the verdict must not depend on which path ran."""

    def _samples(self, library):
        good = _good_cbz(library / "Good 001.cbz")
        bad = _corrupt_cbz(library / "Bad 001.cbz")
        rar = library / "Rar.cbz"
        rar.write_bytes(b"Rar!\x1a\x07\x00 not a zip")
        empty = library / "Empty.cbz"
        empty.write_bytes(b"")
        text = library / "Text.cbz"
        with zipfile.ZipFile(text, "w") as zf:
            zf.writestr("readme.txt", "hello")
        cbr = library / "Old 001.cbr"
        cbr.write_bytes(b"whatever")
        return [good, bad, str(rar), str(empty), str(text), str(cbr)]

    def test_both_paths_agree_on_digest_and_verdict(self, library):
        samples = self._samples(library)
        once = [lh.check_file(p) for p in samples]
        with patch.object(lh, "SINGLE_READ_MAX_BYTES", -1):
            twice = [lh.check_file(p) for p in samples]
        assert once == twice
        assert [r[1] for r in once] == ["ok", "damaged", "damaged", "damaged", "damaged", "ok"]
        assert once[0][0] == lh.hash_file(samples[0])

    def test_a_small_zip_is_opened_once(self, library):
        p = _good_cbz(library / "Good 001.cbz")
        import builtins

        real_open, opened = builtins.open, []

        def spy(file, *args, **kwargs):
            if file == p:
                opened.append(file)
            return real_open(file, *args, **kwargs)

        with patch("builtins.open", side_effect=spy):
            assert lh.check_file(p)[1] == "ok"
        assert len(opened) == 1

    def test_an_unreadable_file_still_raises_oserror(self, library):
        with pytest.raises(OSError):
            lh.check_file(str(library / "Nope 001.cbz"))


class TestRollingBatch:
    def test_records_damage_and_clears_health(self, store, library):
        good = _good_cbz(library / "Good 001.cbz")
        bad = _corrupt_cbz(library / "Bad 002.cbz")
        _index(store, good)
        _index(store, bad)

        stats = lh.run_batch(50, 90)

        assert stats["checked"] == 2 and stats["damaged"] == 1
        assert get_problem(bad, SOURCE_INTEGRITY) is not None
        assert get_problem(good, SOURCE_INTEGRITY) is None
        assert _health(store, good)["status"] == "ok"
        assert _health(store, good)["content_hash"]
        assert _health(store, bad)["status"] == "damaged"

    def test_second_run_has_nothing_to_do(self, store, library):
        """Resumable by derivation: no cursor, so nothing to lose in a restart."""
        _index(store, _good_cbz(library / "Good 001.cbz"))
        assert lh.run_batch(50, 90)["checked"] == 1
        assert lh.run_batch(50, 90)["checked"] == 0

    def test_batch_limit_rolls_across_runs(self, store, library):
        for i in range(5):
            _index(store, _good_cbz(library / f"Rolling {i:03d}.cbz", payload=bytes([65 + i])))
        assert lh.run_batch(2, 90)["checked"] == 2
        assert lh.run_batch(2, 90)["checked"] == 2
        assert lh.run_batch(2, 90)["checked"] == 1
        assert lh.run_batch(2, 90)["checked"] == 0

    def test_rewritten_file_is_checked_again(self, store, library):
        p = _good_cbz(library / "Swap 001.cbz")
        _index(store, p)
        lh.run_batch(50, 90)
        # A replacement lands at the same path: new bytes, new index mtime.
        _corrupt_cbz(library / "Swap 001.cbz")
        os.utime(p, (time.time() + 100, time.time() + 100))
        _index(store, p)
        stats = lh.run_batch(50, 90)
        assert stats["checked"] == 1 and stats["damaged"] == 1

    def test_old_results_expire_so_bit_rot_is_found(self, store, library):
        p = _good_cbz(library / "Quiet 001.cbz")
        _index(store, p)
        lh.run_batch(50, 90)
        store.execute("UPDATE file_health SET checked_at = ?", (time.time() - 100 * 86400,))
        store.commit()
        assert lh.run_batch(50, 90)["checked"] == 1

    def test_a_fixed_file_clears_its_row(self, store, library):
        p = _corrupt_cbz(library / "Fixed 001.cbz")
        _index(store, p)
        lh.run_batch(50, 90)
        assert get_problem(p, SOURCE_INTEGRITY) is not None
        _good_cbz(library / "Fixed 001.cbz")
        ok, _msg = lh.recheck_file(p)
        assert ok is True
        assert get_problem(p, SOURCE_INTEGRITY) is None

    def test_unmounted_library_is_not_walked_or_flagged(self, store, library, tmp_path):
        gone = tmp_path / "unmounted"
        p = str(gone / "Ghost 001.cbz")
        store.execute(
            "INSERT INTO file_index (name, path, type, size, parent, modified_at) "
            "VALUES (?, ?, 'file', 1, ?, 1.0)", ("Ghost 001.cbz", p, str(gone)),
        )
        store.commit()
        with patch("helpers.library.get_library_roots", return_value=[str(gone)]):
            stats = lh.run_batch(50, 90)
        assert stats["checked"] == 0
        assert list_problems() == []
        assert _health(store, p) is None

    def test_a_mount_going_away_records_nothing(self, store, library):
        """Ten unreadable files in a row is a mount, not ten damaged comics."""
        for i in range(12):
            _index(store, _good_cbz(library / f"Mount {i:03d}.cbz", payload=bytes([70 + i])))
        with patch.object(lh, "check_file", side_effect=OSError(5, "I/O error")):
            stats = lh.run_batch(50, 90)
        assert stats["checked"] == 0 and stats["damaged"] == 0
        assert list_problems() == []
        # The ten that tripped it are deferred (no hash, no ledger row) so they
        # stop heading the queue; the place is left out for the rest of the run.
        assert stats["deferred"] == 10
        assert stats["skipped"] == [str(library)]
        rows = store.execute("SELECT status, content_hash FROM file_health").fetchall()
        assert len(rows) == 10
        assert {r["status"] for r in rows} == {"deferred"}
        assert {r["content_hash"] for r in rows} == {None}

    def test_files_sharing_no_folder_or_library_abort_the_batch(self, store, library, tmp_path):
        """A run of failures with nothing in common is not routed around."""
        for i in range(12):
            folder = tmp_path / f"elsewhere{i}"
            folder.mkdir()
            _index(store, _good_cbz(folder / "X 001.cbz", payload=bytes([70 + i])))
        with patch.object(lh, "check_file", side_effect=OSError(5, "I/O error")):
            stats = lh.run_batch(50, 90)
        assert stats["aborted"] is True
        assert list_problems() == []
        assert store.execute("SELECT COUNT(*) FROM file_health").fetchone()[0] == 0

    def test_an_unreadable_folder_does_not_stop_the_rest_of_the_library(self, store, library):
        """A folder with the wrong permissions used to abort every batch after
        ten files, for good: its files were never stamped, so they headed the
        queue on every run and nothing else was ever checked."""
        bad = library / "Locked"
        good = library / "Fine"
        bad.mkdir()
        good.mkdir()
        locked = [
            _good_cbz(bad / f"Locked {i:03d}.cbz", payload=bytes([70 + i])) for i in range(14)
        ]
        fine = [
            _good_cbz(good / f"Fine {i:03d}.cbz", payload=bytes([100 + i])) for i in range(3)
        ]
        # The locked folder first in the queue, as an unchecked folder is.
        for p in locked + fine:
            _index(store, p)
        real = lh.check_file

        def check(path):
            if os.path.dirname(path) == str(bad):
                raise PermissionError(13, "Permission denied", path)
            return real(path)

        with patch.object(lh, "check_file", side_effect=check):
            stats = lh.run_batch(50, 90)
            assert stats["aborted"] is False
            assert stats["skipped"] == [str(bad)]
            assert stats["checked"] == 3 and stats["deferred"] == 10
            assert all(_health(store, p)["status"] == "ok" for p in fine)
            assert list_problems() == []

            # The next run does not go back to the ten it deferred: it takes
            # the four that were never tried, and nothing else is held up.
            tried = []

            def check_again(path):
                tried.append(path)
                return check(path)

            with patch.object(lh, "check_file", side_effect=check_again):
                again = lh.run_batch(50, 90)
        assert sorted(tried) == sorted(locked[10:])
        assert again["aborted"] is False

    def test_a_deferred_file_is_retried_after_the_backoff(self, store, library):
        p = _good_cbz(library / "Later 001.cbz")
        _index(store, p)
        st = os.stat(p)
        lh._store_health(store, p, None, st.st_size, st.st_mtime, lh.STATUS_DEFERRED)
        store.commit()
        assert lh.files_needing_check(10, 90) == []
        store.execute("UPDATE file_health SET checked_at = ?",
                      (time.time() - lh.DEFERRED_RETRY_SECONDS - 60,))
        store.commit()
        assert [f["path"] for f in lh.files_needing_check(10, 90)] == [p]
        # Not counted as checked while it is deferred.
        assert lh.coverage()["checked"] == 0

    def test_an_empty_library_root_is_an_unmounted_one(self, store, library, tmp_path):
        """An unmounted bind mount is still a directory, just an empty one."""
        asleep = tmp_path / "nas"
        asleep.mkdir()
        p = str(asleep / "Series" / "Ghost 001.cbz")
        store.execute(
            "INSERT INTO file_index (name, path, type, size, parent, modified_at) "
            "VALUES (?, ?, 'file', 1, ?, 1.0)", ("Ghost 001.cbz", p, os.path.dirname(p)),
        )
        store.commit()
        with patch("helpers.library.get_library_roots", return_value=[str(asleep)]):
            assert lh._unreachable_roots() == [str(asleep)]
            stats = lh.run_batch(50, 90)
        assert stats["checked"] == 0 and stats["missing"] == 0
        assert list_problems() == []
        # Still indexed: an unmounted library proves nothing about its files.
        assert store.execute("SELECT COUNT(*) FROM file_index").fetchone()[0] == 1
        assert _health(store, p) is None

    def test_damage_that_cannot_be_listed_is_not_stamped_as_checked(self, store, library):
        """Stamp after recording. With the ledger full (or the write failing)
        the file was stamped damaged anyway, and so skipped by every scan for
        90 days with its damage written down nowhere."""
        p = _corrupt_cbz(library / "Bad 001.cbz")
        _index(store, p)
        with patch.object(lh, "record_problem", return_value=False):
            stats = lh.run_batch(50, 90)
        assert stats["damaged"] == 0 and stats["deferred"] == 1
        assert _health(store, p)["status"] == lh.STATUS_DEFERRED
        # Tried again once the backoff has passed, and listed this time.
        store.execute("UPDATE file_health SET checked_at = 1.0")
        store.commit()
        assert lh.run_batch(50, 90)["damaged"] == 1
        assert get_problem(p, SOURCE_INTEGRITY) is not None

    def test_a_healthy_file_costs_no_ledger_call(self, store, library):
        _index(store, _good_cbz(library / "Fine 001.cbz"))
        with patch.object(lh, "clear_problem") as clear, \
                patch.object(lh, "record_problem") as record:
            assert lh.run_batch(50, 90)["checked"] == 1
        clear.assert_not_called()
        record.assert_not_called()

    def test_stamps_are_committed_even_when_the_batch_is_cut_short(self, store, library):
        for i in range(5):
            _index(store, _good_cbz(library / f"Run {i:03d}.cbz", payload=bytes([70 + i])))
        real = lh.check_file
        calls = []

        def check(path):
            calls.append(path)
            if len(calls) == 4:
                raise RuntimeError("boom")
            return real(path)

        with patch.object(lh, "check_file", side_effect=check):
            stats = lh.run_batch(50, 90)
        assert stats["aborted"] is True
        # A second connection sees them: they were committed, not just written.
        import sqlite3
        from core.database import get_db_path

        other = sqlite3.connect(get_db_path())
        try:
            assert other.execute("SELECT COUNT(*) FROM file_health").fetchone()[0] == 3
        finally:
            other.close()

    def test_a_stop_request_ends_the_batch_and_keeps_what_was_done(self, store, library):
        for i in range(4):
            _index(store, _good_cbz(library / f"Stop {i:03d}.cbz", payload=bytes([70 + i])))
        assert lh.claim_scan()
        try:
            real = lh.check_file

            def check(path):
                lh.request_stop()
                return real(path)

            with patch.object(lh, "check_file", side_effect=check):
                stats = lh.run_batch(50, 90)
        finally:
            lh.release_scan()
        assert stats["stopped"] is True and stats["checked"] == 1
        assert store.execute("SELECT COUNT(*) FROM file_health").fetchone()[0] == 1
        # The claim clears the flag, so the next scan is not born stopped.
        assert lh.claim_scan()
        try:
            assert lh.stop_requested() is False
        finally:
            lh.release_scan()

    def test_stop_is_refused_when_nothing_is_running(self):
        assert lh.request_stop() is False

    def test_a_file_gone_from_a_present_folder_stops_heading_the_queue(self, store, library):
        _good_cbz(library / "Keep 001.cbz")  # the library is mounted
        p = _good_cbz(library / "Gone 001.cbz")
        _index(store, p)
        os.remove(p)
        stats = lh.run_batch(50, 90)
        assert stats["missing"] == 1
        # No ledger row: the listing would prune it on sight (_is_definitely_gone).
        assert list_problems() == []
        assert _health(store, p)["status"] == "missing"
        assert lh.run_batch(50, 90)["missing"] == 0

    def test_a_deleted_folder_is_forgotten_not_counted_as_a_mount(self, store, library):
        """#693: a top-level folder deleted outside CLU. Every file in it raised
        FileNotFoundError with no parent, which read as a mount going away: the
        batch aborted on every run and nothing was ever stamped."""
        _good_cbz(library / "Keep 001.cbz")  # the library is mounted
        gone = library / "Fantographics" / "Series"
        paths = [str(gone / f"Ghost {i:03d}.cbz") for i in range(12)]
        for p in paths:
            store.execute(
                "INSERT INTO file_index (name, path, type, size, parent, modified_at) "
                "VALUES (?, ?, 'file', 1, ?, 1.0)", (os.path.basename(p), p, str(gone)),
            )
        store.commit()
        stats = lh.run_batch(50, 90)
        assert stats["aborted"] is False
        assert stats["missing"] == 12
        assert list_problems() == []
        left = store.execute(
            "SELECT COUNT(*) FROM file_index WHERE path LIKE ?", (str(gone) + "%",)
        ).fetchone()[0]
        assert left == 0

    def test_scan_drops_a_moved_folders_ghost_duplicates(self, store, library):
        """The old copy of a series moved outside CLU paired with the new one
        as a likely duplicate, in thousands of groups."""
        new_dir = library / "Combined" / "Love and Rockets"
        new_dir.mkdir(parents=True)
        p_new = _good_cbz(new_dir / "Love and Rockets 001 (1982).cbz")
        tags = {"ci_series": "Love and Rockets", "ci_number": "1",
                "ci_year": 1982, "ci_publisher": "Fantagraphics"}
        _index(store, p_new, **tags)
        # Stored the way the indexer stores them: forward slashes.
        old_dir = (library / "Fantographics" / "Love and Rockets").as_posix()
        p_old = old_dir + "/Love and Rockets 001 (1982).cbz"
        for d in ((library / "Fantographics").as_posix(), old_dir):
            store.execute(
                "INSERT INTO file_index (name, path, type, parent) VALUES (?, ?, 'directory', ?)",
                (os.path.basename(d), d, os.path.dirname(d)),
            )
        store.execute(
            "INSERT INTO file_index (name, path, type, size, parent, modified_at, "
            "ci_series, ci_number, ci_year, ci_publisher) "
            "VALUES (?, ?, 'file', 1, ?, 1.0, ?, ?, ?, ?)",
            (os.path.basename(p_old), p_old, old_dir, *tags.values()),
        )
        store.commit()
        assert lh.count_duplicates()[1] == 1

        lh.run_scan(run_cheap=False)

        assert lh.count_duplicates() == (0, 0)
        assert store.execute(
            "SELECT path FROM file_index WHERE path = ?", (p_new,)
        ).fetchone() is not None

    def test_prune_keeps_everything_while_the_index_is_empty(self, store, library):
        p = _good_cbz(library / "Keep 001.cbz")
        _index(store, p)
        lh.run_batch(50, 90)
        store.execute("DELETE FROM file_index")
        store.commit()
        assert lh.prune_health_rows() == 0  # a rebuild in progress, not a purge
        _index(store, _good_cbz(library / "Other 001.cbz", payload=b"Z"))
        assert lh.prune_health_rows() == 1  # now Keep 001 is provably gone


class TestOrphans:
    def test_cbr_beside_cbz_is_listed_and_clears_when_resolved(self, store, library):
        cbr = library / "Twin 001.cbr"
        cbr.write_bytes(b"rar")
        _index(store, str(cbr))
        cbz = _good_cbz(library / "Twin 001.cbz")

        stats = lh.scan_orphans()
        assert stats["pairs"] == 1
        assert get_problem(str(cbr), SOURCE_ORPHAN)["error_class"] == "CbrBesideCbz"

        os.remove(cbz)
        stats = lh.scan_orphans()
        assert stats["pairs"] == 0 and stats["cleared"] == 1
        assert get_problem(str(cbr), SOURCE_ORPHAN) is None

    def test_a_lone_cbr_is_not_an_orphan(self, store, library):
        cbr = library / "Solo 001.cbr"
        cbr.write_bytes(b"rar")
        _index(store, str(cbr))
        assert lh.scan_orphans()["pairs"] == 0

    def test_watch_and_target_are_never_walked(self, store, tmp_path, library):
        """A comic waiting in a staging folder is not a library problem. The
        "left in WATCH for over a day" check is gone, and nothing walks either
        folder."""
        watch = tmp_path / "watch"
        target = tmp_path / "processed"
        for folder in (watch, target):
            folder.mkdir()
            old = _good_cbz(folder / "Old 001.cbz")
            long_ago = time.time() - 3 * 86400
            os.utime(old, (long_ago, long_ago))
        with patch("core.config.get_target_dir", return_value=str(target)), \
                patch("core.config.get_watch_dir", return_value=str(watch)), \
                patch("os.walk", side_effect=AssertionError("walked a folder")):
            stats = lh.scan_orphans()
        assert stats == {"pairs": 0, "cleared": 0}
        assert list_problems() == []

    def test_a_cbr_beside_its_cbz_in_target_is_not_listed(self, store, tmp_path, library):
        target = tmp_path / "processed"
        target.mkdir()
        cbr = target / "Twin 001.cbr"
        cbr.write_bytes(b"rar")
        _index(store, str(cbr))
        _good_cbz(target / "Twin 001.cbz")
        with patch("core.config.get_target_dir", return_value=str(target)), \
                patch("core.config.get_watch_dir", return_value=""):
            assert lh.scan_orphans()["pairs"] == 0
        assert list_problems() == []

    def test_an_old_stray_row_is_cleared_by_a_recheck(self, store, tmp_path, library):
        watch = tmp_path / "watch"
        watch.mkdir()
        old = _good_cbz(watch / "Old 001.cbz")
        # Written before the check was removed (no staging folder configured,
        # so the ledger accepts it here).
        record_problem(old, SOURCE_ORPHAN, error_class="StrayInStaging",
                       error_message="Unfiled in WATCH for over a day")
        with patch("core.config.get_target_dir", return_value=""), \
                patch("core.config.get_watch_dir", return_value=str(watch)):
            ok, _ = lh.recheck_orphan(old)
        assert ok is True
        assert get_problem(old, SOURCE_ORPHAN) is None

    def test_recheck_orphan_clears_a_resolved_pair(self, store, library):
        cbr = library / "Twin 002.cbr"
        cbr.write_bytes(b"rar")
        _index(store, str(cbr))
        cbz = _good_cbz(library / "Twin 002.cbz")
        lh.scan_orphans()
        assert lh.recheck_orphan(str(cbr)) == (False, "A .cbz is still beside it")
        os.remove(cbz)
        ok, _ = lh.recheck_orphan(str(cbr))
        assert ok is True
        assert get_problem(str(cbr), SOURCE_ORPHAN) is None


class TestDuplicates:
    def _two_identical(self, store, library):
        a = _good_cbz(library / "Batman 001 (2016).cbz")
        b = _good_cbz(library / "Batman 001 (2016) (1).cbz")
        _index(store, a)
        _index(store, b)
        lh.run_batch(50, 90)
        return a, b

    def test_identical_bytes_are_an_exact_group(self, store, library):
        a, b = self._two_identical(store, library)
        found = lh.find_duplicates()
        assert len(found["exact"]) == 1
        assert {m["path"] for m in found["exact"][0]["members"]} == {a, b}
        assert found["exact"][0]["wasted_bytes"] > 0

    def test_an_unchecked_file_cannot_be_an_exact_duplicate(self, store, library):
        """An unchecked file has no hash, so it cannot be an exact duplicate."""
        a = _good_cbz(library / "A 001.cbz")
        b = _good_cbz(library / "B 001.cbz")
        _index(store, a)
        _index(store, b)
        assert lh.find_duplicates()["exact"] == []

    def test_a_stale_hash_is_not_trusted(self, store, library):
        a, b = self._two_identical(store, library)
        # b is rewritten but the index has not been re-scanned yet is fine; here
        # the index moves on, so the stored hash no longer applies.
        store.execute("UPDATE file_index SET modified_at = modified_at + 50 WHERE path = ?", (b,))
        store.commit()
        assert lh.find_duplicates()["exact"] == []

    def test_tagged_same_issue_is_a_likely_group(self, store, library):
        a = _good_cbz(library / "Batman 001 (2016).cbz", payload=b"A")
        b = _good_cbz(library / "Batman 001 (digital).cbz", payload=b"B")
        tag = {"ci_series": "Batman", "ci_number": "001", "ci_year": 2016, "ci_publisher": "DC"}
        _index(store, a, **tag)
        _index(store, b, **dict(tag, ci_number="1"))
        found = lh.find_duplicates()
        assert found["exact"] == []
        assert len(found["likely"]) == 1

    def test_different_year_or_negative_number_is_not_merged(self, store, library):
        a = _good_cbz(library / "Batman 001 (2011).cbz", payload=b"A")
        b = _good_cbz(library / "Batman 001 (2016).cbz", payload=b"B")
        c = _good_cbz(library / "Batman -001 (2011).cbz", payload=b"C")
        _index(store, a, ci_series="Batman", ci_number="1", ci_year=2011)
        _index(store, b, ci_series="Batman", ci_number="1", ci_year=2016)
        _index(store, c, ci_series="Batman", ci_number="-1", ci_year=2011)
        assert lh.find_duplicates()["likely"] == []

    def test_event_issues_tagged_as_one_series_are_not_duplicates(self, store, library):
        """Future Quest: the Special one-shots were all tagged as the parent
        series, number and year. The filenames say they are different comics."""
        event = library / "Future Quest"
        event.mkdir()
        tag = {"ci_series": "Future Quest", "ci_number": "1", "ci_year": 2016, "ci_publisher": "DC Comics"}
        for i, name in enumerate((
            "Future Quest 001 (2016).cbz",
            "Aquaman - Jabberjaw Special 001 (2018).cbz",
            "Green Lantern Space Ghost Special 001 (2017).cbz",
        )):
            _index(store, _good_cbz(event / name, payload=bytes([65 + i])), **tag)
        assert lh.find_duplicates()["likely"] == []

    def test_a_blank_tagged_year_is_not_a_wildcard(self, store, library):
        for i, name in enumerate(("Batman 001.cbz", "Batman 001 (digital).cbz")):
            _index(store, _good_cbz(library / name, payload=bytes([65 + i])),
                   ci_series="Batman", ci_number="1", ci_year=None)
        found = lh.find_duplicates()["likely"]
        # Not claimed by the tag rule; the same-folder copy-marker rule is a
        # different (filename-only) question and does not apply to "(digital)".
        assert found == []

    def test_issue_stem(self):
        assert lh.issue_stem("/x/Future Quest 001 (2016).cbz", "1") == "future quest"
        assert lh.issue_stem("/x/Aquaman - Jabberjaw Special 001 (2018).cbz", "1") == "aquaman jabberjaw special"
        assert lh.issue_stem("/x/Batman 001 (1).cbz", "1") == lh.issue_stem("/x/Batman 001 (digital).cbz", "001")
        assert lh.issue_stem("/x/Batman 001 - Year One.cbz", "1") == "batman"
        assert lh.issue_stem("/x/Odd Name.cbz", "5") == "odd name"  # no number in the name

    def test_variants_in_one_folder_are_not_copies(self, store, library):
        """Only a trailing (n) and a download's _n are copy markers; stripping
        every parenthetical would fold Variant A into Variant B."""
        _index(store, _good_cbz(library / "Batman 001 (Variant A).cbz", payload=b"A"))
        _index(store, _good_cbz(library / "Batman 001 (Variant B).cbz", payload=b"B"))
        assert lh.find_duplicates()["likely"] == []

    def test_copy_markers_are_one_issue(self, store, library):
        _index(store, _good_cbz(library / "Batman 001.cbz", payload=b"A"))
        _index(store, _good_cbz(library / "Batman 001 (1).cbz", payload=b"B"))
        _index(store, _good_cbz(library / "Batman 001_4.cbz", payload=b"C"))
        found = lh.find_duplicates()
        assert len(found["likely"]) == 1
        assert len(found["likely"][0]["members"]) == 3

    def test_exact_group_is_not_repeated_as_likely(self, store, library):
        a, b = self._two_identical(store, library)
        tag = {"ci_series": "Batman", "ci_number": "1", "ci_year": 2016}
        _index(store, a, **tag)
        _index(store, b, **tag)
        found = lh.find_duplicates()
        assert len(found["exact"]) == 1 and found["likely"] == []

    def test_keeper_prefers_undamaged_tagged_cbz(self, store, library):
        a = _good_cbz(library / "Same 001.cbz", payload=b"A")
        b = _corrupt_cbz(library / "Same 001 (1).cbz")
        _index(store, a, has_comicinfo=1)
        _index(store, b, has_comicinfo=1)
        lh.run_batch(50, 90)
        tag = {"ci_series": "Same", "ci_number": "1", "ci_year": 2016}
        _index(store, a, has_comicinfo=1, **tag)
        _index(store, b, has_comicinfo=1, **tag)
        group = lh.find_duplicates()["likely"][0]
        assert group["keep_suggestion"] == a

    def test_dismissal_holds_until_the_member_set_changes(self, store, library):
        a, b = self._two_identical(store, library)
        gid = lh.find_duplicates()["exact"][0]["id"]
        assert lh.dismiss_group(gid) == {"success": True, "gone": False}
        assert lh.find_duplicates()["exact"] == []
        assert lh.find_duplicates(include_dismissed=True)["exact"][0]["dismissed"] is True

        # A third copy of the same bytes re-opens the group.
        c = _good_cbz(library / "Batman 001 (2016) (2).cbz")
        _index(store, c)
        lh.run_batch(50, 90)
        assert len(lh.find_duplicates()["exact"]) == 1


    def test_restore_brings_a_dismissed_group_back(self, store, library):
        self._two_identical(store, library)
        gid = lh.find_duplicates()["exact"][0]["id"]
        lh.dismiss_group(gid)
        assert lh.find_duplicates()["exact"] == []
        lh.dismiss_group(gid, dismissed=False)
        assert len(lh.find_duplicates()["exact"]) == 1

    @staticmethod
    def _backdate_dismissals(store):
        # dismissed_at has one-second resolution; put it clearly before the scan.
        store.execute("UPDATE duplicate_dismissals SET dismissed_at = datetime('now', '-1 minute')")
        store.commit()

    def test_init_db_adds_until_scan_to_an_existing_table(self, store):
        """An install that already has dismissals keeps them, as 'not duplicates'."""
        from core.database import init_db

        store.execute("DROP TABLE duplicate_dismissals")
        store.execute(
            "CREATE TABLE duplicate_dismissals (group_key TEXT PRIMARY KEY, "
            "members_sig TEXT NOT NULL, dismissed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )
        store.execute("INSERT INTO duplicate_dismissals (group_key, members_sig) VALUES ('k', 's')")
        store.commit()
        init_db()
        assert lh._load_dismissals() == {"k": ("s", False)}

    def test_dismiss_until_scan_hides_the_group_and_says_so(self, store, library):
        self._two_identical(store, library)
        gid = lh.find_duplicates()["exact"][0]["id"]
        assert lh.dismiss_group(gid, until_scan=True) == {"success": True, "gone": False}
        assert lh.find_duplicates()["exact"] == []
        group = lh.find_duplicates(include_dismissed=True)["exact"][0]
        assert group["dismissed"] is True and group["dismiss_mode"] == "scan"

    def test_next_scan_brings_back_a_group_that_still_exists(self, store, library):
        self._two_identical(store, library)
        gid = lh.find_duplicates()["exact"][0]["id"]
        lh.dismiss_group(gid, until_scan=True)
        self._backdate_dismissals(store)
        lh.run_scan(run_cheap=False, batch=10)
        found = lh.find_duplicates()["exact"]
        assert len(found) == 1 and found[0]["dismissed"] is False
        assert lh.load_state()["dup_exact_groups"] == 1

    def test_a_group_resolved_elsewhere_stays_gone_after_the_scan(self, store, library):
        a, b = self._two_identical(store, library)
        gid = lh.find_duplicates()["exact"][0]["id"]
        lh.dismiss_group(gid, until_scan=True)
        # The user deletes one copy from the File Manager.
        os.remove(b)
        store.execute("DELETE FROM file_index WHERE path = ?", (b,))
        store.commit()
        self._backdate_dismissals(store)
        lh.run_scan(run_cheap=False, batch=10)
        assert lh.find_duplicates(include_dismissed=True)["exact"] == []

    def test_a_dismissal_made_after_the_scan_began_waits_for_the_next(self, store, library):
        self._two_identical(store, library)
        gid = lh.find_duplicates()["exact"][0]["id"]
        lh.dismiss_group(gid, until_scan=True)
        assert lh.expire_scan_dismissals(time.time() - 60) == 0
        assert lh.find_duplicates()["exact"] == []

    @pytest.mark.parametrize("until_scan", [True, False])
    def test_dismissing_a_group_already_fixed_elsewhere_succeeds(self, store, library, until_scan):
        """The page still shows a group the user just fixed in the File Manager;
        dismissing it is a success with nothing to store, not 'no longer exists'."""
        a, b = self._two_identical(store, library)
        gid = lh.find_duplicates()["exact"][0]["id"]
        os.remove(b)
        store.execute("DELETE FROM file_index WHERE path = ?", (b,))
        store.commit()
        result = lh.dismiss_group(gid, until_scan=until_scan, seen_paths=[a, b])
        assert result == {"success": True, "gone": True}
        assert store.execute("SELECT COUNT(*) FROM duplicate_dismissals").fetchone()[0] == 0

    def test_a_failed_read_is_not_reported_as_gone(self, store, library):
        self._two_identical(store, library)
        gid = lh.find_duplicates()["exact"][0]["id"]
        # Raised, not swallowed into the result: the route logs it and answers
        # with a fixed message, so the exception text never reaches the client.
        with patch("core.database.db_conn", side_effect=RuntimeError("disk I/O error")):
            with pytest.raises(RuntimeError):
                lh.dismiss_group(gid, until_scan=True)

    def test_a_copy_the_user_never_saw_is_not_dismissed(self, store, library):
        a, b = self._two_identical(store, library)
        gid = lh.find_duplicates()["exact"][0]["id"]
        c = _good_cbz(library / "Batman 001 (2016) (2).cbz")
        _index(store, c)
        lh.run_batch(50, 90)
        result = lh.dismiss_group(gid, until_scan=True, seen_paths=[a, b])
        assert result["success"] is False and result["changed"] is True
        assert len(lh.find_duplicates()["exact"]) == 1

    def test_a_copy_removed_since_the_page_loaded_does_not_block_dismissal(self, store, library):
        a, b = self._two_identical(store, library)
        c = _good_cbz(library / "Batman 001 (2016) (2).cbz")
        _index(store, c)
        lh.run_batch(50, 90)
        gid = lh.find_duplicates()["exact"][0]["id"]
        os.remove(c)
        store.execute("DELETE FROM file_index WHERE path = ?", (c,))
        store.commit()
        assert lh.dismiss_group(gid, until_scan=True, seen_paths=[a, b, c])["success"] is True
        assert lh.find_duplicates()["exact"] == []

    def test_not_duplicates_survives_a_scan(self, store, library):
        self._two_identical(store, library)
        gid = lh.find_duplicates()["exact"][0]["id"]
        lh.dismiss_group(gid)
        self._backdate_dismissals(store)
        lh.run_scan(run_cheap=False, batch=10)
        assert lh.find_duplicates()["exact"] == []
        group = lh.find_duplicates(include_dismissed=True)["exact"][0]
        assert group["dismiss_mode"] == "not_duplicate"


class TestResolveGroup:
    def _setup(self, store, library):
        a = _good_cbz(library / "Batman 001 (2016).cbz")
        b = _good_cbz(library / "Batman 001 (2016) (1).cbz")
        _index(store, a)
        _index(store, b)
        lh.run_batch(50, 90)
        return a, b, lh.find_duplicates()["exact"][0]["id"]

    @staticmethod
    def _fake_trash(path):
        os.remove(path)
        return {"trashed": True, "path": path}

    def test_trashes_the_others_and_forgets_them(self, store, library):
        a, b, gid = self._setup(store, library)
        with patch("helpers.trash.move_to_trash", side_effect=self._fake_trash), \
             patch("helpers.library.is_critical_path", return_value=False):
            result = lh.resolve_group(gid, a, [b])
        assert result["success"] is True
        assert os.path.exists(a) and not os.path.exists(b)
        assert store.execute("SELECT 1 FROM file_index WHERE path = ?", (b,)).fetchone() is None
        assert _health(store, b) is None

    def test_refuses_paths_that_are_not_members(self, store, library):
        a, b, gid = self._setup(store, library)
        outsider = _good_cbz(library / "Other 009.cbz", payload=b"Q")
        with patch("helpers.trash.move_to_trash") as trash:
            result = lh.resolve_group(gid, a, [outsider])
            assert "not in this group" in result["error"]
            trash.assert_not_called()

    def test_cannot_trash_the_file_being_kept(self, store, library):
        a, b, gid = self._setup(store, library)
        with patch("helpers.trash.move_to_trash") as trash:
            result = lh.resolve_group(gid, a, [a, b])
            assert result["success"] is False
            trash.assert_not_called()
        assert os.path.exists(a) and os.path.exists(b)

    def test_unknown_group_is_refused(self, store, library):
        a, b, _gid = self._setup(store, library)
        assert lh.resolve_group("deadbeef", a, [b])["success"] is False

    def test_a_protected_path_is_not_trashed(self, store, library):
        a, b, gid = self._setup(store, library)
        with patch("helpers.trash.move_to_trash") as trash, \
             patch("helpers.library.is_critical_path", return_value=True):
            result = lh.resolve_group(gid, a, [b])
        trash.assert_not_called()
        assert result["success"] is False and os.path.exists(b)


class TestRenameMember:
    """#691: renaming one copy of a likely group from the compare view."""

    TAG = {"ci_series": "Wonder Woman", "ci_number": "5", "ci_year": 1987, "ci_publisher": "DC"}

    def _likely_pair(self, store, library):
        folder = library / "v1987"
        folder.mkdir()
        a = _good_cbz(folder / "Wonder Woman 005 (1987).cbz", payload=b"A")
        b = _good_cbz(folder / "Wonder Woman 005 (1996).cbz", payload=b"B")
        _index(store, a, **self.TAG)
        _index(store, b, **self.TAG)
        likely = lh.find_duplicates()["likely"]
        assert len(likely) == 1
        return a, b, likely[0]["id"]

    @staticmethod
    def _rename(store, old, new):
        """What routes.files.rename_path does: the file, then the index."""
        os.rename(old, new)
        store.execute("UPDATE file_index SET path = ?, name = ? WHERE path = ?",
                      (new, os.path.basename(new), old))
        store.commit()
        lh.follow_member_rename(old, new)

    def test_the_right_name_clears_the_group(self, store, library):
        a, b, gid = self._likely_pair(store, library)
        checked = lh.validate_member_rename(gid, b, "Wonder Woman Annual 005 (1996).cbz")
        assert checked == {"new_path": os.path.join(os.path.dirname(b),
                                                    "Wonder Woman Annual 005 (1996).cbz")}
        self._rename(store, b, checked["new_path"])
        assert lh.find_duplicates()["likely"] == []
        assert lh.group_exists(gid) is False

    def test_an_exact_group_cannot_be_renamed_away(self, store, library):
        a = _good_cbz(library / "Batman 001 (2016).cbz")
        b = _good_cbz(library / "Batman 001 (2016) (1).cbz")
        _index(store, a)
        _index(store, b)
        lh.run_batch(50, 90)
        gid = lh.find_duplicates()["exact"][0]["id"]
        assert "likely" in lh.validate_member_rename(gid, b, "Batman 002 (2016).cbz")["error"]

    def test_the_hash_follows_the_rename(self, store, library):
        """Otherwise an exact group containing the renamed file vanishes until
        the next scan, as though it had been resolved."""
        a = _good_cbz(library / "Batman 001 (2016).cbz")
        b = _good_cbz(library / "Batman 001 (2016) (1).cbz")
        _index(store, a)
        _index(store, b)
        lh.run_batch(50, 90)
        new = str(library / "Batman 001 (2016) scan.cbz")
        self._rename(store, b, new)
        assert _health(store, b) is None and _health(store, new) is not None
        exact = lh.find_duplicates()["exact"]
        assert len(exact) == 1
        assert {m["path"] for m in exact[0]["members"]} == {a, new}

    def test_a_file_outside_the_group_is_refused(self, store, library):
        a, b, gid = self._likely_pair(store, library)
        outsider = _good_cbz(library / "Other 009.cbz", payload=b"Q")
        assert "not in this group" in lh.validate_member_rename(gid, outsider, "x 009.cbz")["error"]

    def test_an_unknown_group_is_refused(self, store, library):
        a, b, gid = self._likely_pair(store, library)
        assert "no longer exists" in lh.validate_member_rename("deadbeef", b, "x.cbz")["error"]

    @pytest.mark.parametrize("name, needle", [
        ("", "Enter a file name"),
        ("   ", "Enter a file name"),
        ("..", "Enter a file name"),
        ("Sub/Wonder Woman 005.cbz", "cannot contain"),
        ("Sub\\Wonder Woman 005.cbz", "cannot contain"),
        ("Wonder Woman: Annual 005.cbz", "cannot contain"),
        ("Wonder\tWoman 005.cbz", "U+0009"),
        (".Wonder Woman 005.cbz", "cannot start"),
        ("_Wonder Woman 005.cbz", "cannot start"),
        ("Wonder Woman Annual 005 (1996).cbr", "Keep the .cbz"),
        ("Wonder Woman Annual 005 (1996)", "Keep the .cbz"),
        (".cbz", "cannot start"),
        ("Wonder Woman 005 (1996).cbz", "unchanged"),
    ])
    def test_bad_names_are_refused_not_cleaned(self, store, library, name, needle):
        a, b, gid = self._likely_pair(store, library)
        assert needle in lh.validate_member_rename(gid, b, name)["error"]

    def test_a_case_only_rename_is_allowed(self, store, library):
        a, b, gid = self._likely_pair(store, library)
        assert "new_path" in lh.validate_member_rename(gid, b, "WONDER WOMAN 005 (1996).CBZ")


class TestSummary:
    def test_score_terms_are_capped(self):
        assert lh.compute_score(0, 0, 0, 0, 0) == 100
        assert lh.compute_score(1, 0, 0, 0, 0) == 90
        # One noisy category cannot zero the score on its own.
        assert lh.compute_score(0, 999, 0, 0, 0) == 85
        assert lh.compute_score(99, 99, 99, 99, 99) == 0

    def test_level_is_red_for_damage_amber_for_housekeeping(self, store, library):
        s = lh.get_summary()
        assert s["level"] == "green" and s["last_run_at"] is None

        lh.save_state({"dup_exact_groups": 2, "last_run_at": 1.0})
        assert lh.get_summary()["level"] == "amber"

        bad = _corrupt_cbz(library / "Bad 001.cbz")
        _index(store, bad)
        lh.run_batch(50, 90)
        assert lh.get_summary()["level"] == "red"

    def test_coverage_counts_only_current_results(self, store, library):
        a = _good_cbz(library / "A 001.cbz", payload=b"A")
        b = _good_cbz(library / "B 001.cbz", payload=b"B")
        _index(store, a)
        _index(store, b)
        assert lh.coverage()["pct"] == 0
        lh.run_batch(1, 90)
        assert lh.coverage() == {"total": 2, "checked": 1, "pct": 50}

    def test_comicinfo_counts_are_counts_not_rows(self, store, library):
        for i, flag in enumerate((1, 0, 0)):
            _index(store, _good_cbz(library / f"T{i}.cbz", payload=bytes([65 + i])), has_comicinfo=flag)
        assert lh.comicinfo_counts() == {"total": 3, "missing": 2}
        assert list_problems() == []


class TestScheduledScan:
    def test_off_by_default(self, store):
        with patch.object(lh, "run_scan") as run:
            assert lh.run_scheduled_scan() is None
            run.assert_not_called()

    def test_interval_gates_the_run(self, store):
        from core.settings import set_setting

        set_setting("LIBRARY_HEALTH_SCAN_ENABLED", True)
        set_setting("LIBRARY_HEALTH_INTERVAL_HOURS", 6)
        lh.save_state({"last_run_at": time.time() - 3600, "cheap_checks_at": time.time()})
        with patch.object(lh, "run_scan") as run:
            assert lh.run_scheduled_scan() is None
            run.assert_not_called()

        lh.save_state({"last_run_at": time.time() - 7 * 3600, "cheap_checks_at": time.time()})
        with patch.object(lh, "run_scan", return_value={}) as run:
            lh.run_scheduled_scan()
            run.assert_called_once()
            # The walks are daily; this run is inside that window.
            assert run.call_args.kwargs["run_cheap"] is False

    def test_cheap_checks_run_when_a_day_has_passed(self, store):
        from core.settings import set_setting

        set_setting("LIBRARY_HEALTH_SCAN_ENABLED", True)
        lh.save_state({"last_run_at": 1.0, "cheap_checks_at": 1.0})
        with patch.object(lh, "run_scan", return_value={}) as run:
            lh.run_scheduled_scan()
            assert run.call_args.kwargs["run_cheap"] is True

    def test_single_flight_claim_is_released(self, store):
        from core.settings import set_setting

        set_setting("LIBRARY_HEALTH_SCAN_ENABLED", True)
        lh.save_state({})
        with patch.object(lh, "run_scan", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                lh.run_scheduled_scan()
        # A claim left held by one failure would lock the scan out for the
        # life of the process.
        assert lh.claim_scan() is True
        lh.release_scan()

    def test_a_second_claim_is_refused(self):
        assert lh.claim_scan() is True
        try:
            assert lh.claim_scan() is False
            assert lh.scan_running() is True
        finally:
            lh.release_scan()

    def test_full_scan_stores_state(self, store, library):
        _index(store, _good_cbz(library / "One 001.cbz"))
        with patch("core.series_json_check.scan_library", return_value={}):
            result = lh.run_scan(run_cheap=True, batch=10)
        assert "error" not in result
        state = lh.load_state()
        assert state["last_run_at"] and state["last_batch"]["checked"] == 1
        assert state["comicinfo"]["total"] == 1


class TestEmptyFolders:
    """``scan_empty_folders``: library folders with no comics in them."""

    @pytest.fixture(autouse=True)
    def _protected(self, library):
        self.protected = [str(library)]
        with patch("helpers.library.get_protected_roots",
                   side_effect=lambda: list(self.protected)):
            yield

    def _rows(self):
        from core.problem_files import SOURCE_EMPTY_FOLDER

        return {r["path"]: r for r in list_problems(source=SOURCE_EMPTY_FOLDER)}

    def test_empty_and_sidecar_only_folders_are_listed(self, store, library):
        (library / "Empty").mkdir()
        left = library / "Left Behind (2020)"
        left.mkdir()
        (left / "cvinfo").write_text("https://comicvine.gamespot.com/x/4050-1/")
        (left / "series.json").write_text("{}")
        (left / "folder.png").write_bytes(b"png")
        (left / ".DS_Store").write_bytes(b"x")
        full = library / "Full"
        full.mkdir()
        _good_cbz(full / "Full 001.cbz")
        (full / "cvinfo").write_text("x")

        stats = lh.scan_empty_folders()
        rows = self._rows()
        assert set(rows) == {str(library / "Empty"), str(left)}
        assert stats["flagged"] == 2
        assert rows[str(library / "Empty")]["error_class"] == "EmptyFolder"
        assert rows[str(left)]["error_class"] == "SidecarOnlyFolder"
        # OS litter is not a leftover worth naming.
        assert rows[str(left)]["error_message"] == "Only cvinfo, folder.png, series.json"

    def test_the_library_root_is_never_listed(self, store, library):
        """An empty library is not an empty folder to delete."""
        assert lh.scan_empty_folders()["flagged"] == 0
        assert self._rows() == {}

    def test_only_the_topmost_empty_folder_is_listed(self, store, library):
        """Deleting the parent takes the child with it; a second row would
        offer to delete a folder the first Delete already removed."""
        child = library / "Publisher" / "Series"
        child.mkdir(parents=True)
        (child / "series.json").write_text("{}")
        lh.scan_empty_folders()
        rows = self._rows()
        assert set(rows) == {str(library / "Publisher")}
        assert rows[str(library / "Publisher")]["error_message"] == "Only series.json"

    def test_a_user_file_keeps_the_folder_off_the_list(self, store, library):
        """Only names CLU writes count as leftovers."""
        folder = library / "Notes"
        folder.mkdir()
        (folder / "cvinfo").write_text("x")
        (folder / "readme.txt").write_text("mine")
        lh.scan_empty_folders()
        assert self._rows() == {}

    def test_hidden_and_protected_subfolders_count_as_content(self, store, library):
        """The walk does not look inside them, and "did not look" must not
        become "nothing there"."""
        hidden_parent = library / "A"
        (hidden_parent / ".stuff").mkdir(parents=True)
        staged_parent = library / "B"
        target = staged_parent / "processed"
        target.mkdir(parents=True)
        self.protected.append(str(target))
        lh.scan_empty_folders()
        assert self._rows() == {}

    def test_rows_clear_once_the_folder_fills_but_not_when_unmounted(self, store, library):
        folder = library / "Soon"
        folder.mkdir()
        lh.scan_empty_folders()
        assert str(folder) in self._rows()

        # The library is unreachable: nothing was walked, so nothing is cleared.
        with patch("helpers.library.get_library_roots",
                   return_value=[str(library / "not-mounted")]):
            assert lh.scan_empty_folders()["cleared"] == 0
        assert str(folder) in self._rows()

        _good_cbz(folder / "Soon 001.cbz")
        assert lh.scan_empty_folders()["cleared"] == 1
        assert self._rows() == {}

    def test_a_concurrent_scan_stands_down(self, store, library):
        assert lh._empty_scan_lock.acquire(blocking=False)
        try:
            assert lh.scan_empty_folders() is None
        finally:
            lh._empty_scan_lock.release()

    def test_new_rows_are_capped(self, store, library):
        for i in range(4):
            (library / f"E{i}").mkdir()
        with patch.object(lh, "MAX_EMPTY_FOLDER_ROWS", 3):
            stats = lh.scan_empty_folders()
        assert stats["flagged"] == 3 and stats["capped"] == 1
        assert len(self._rows()) == 3

    def test_recheck_clears_a_folder_that_filled_up(self, store, library):
        folder = library / "Later"
        folder.mkdir()
        lh.scan_empty_folders()
        assert lh.recheck_empty_folder(str(folder)) == (False, "Still empty")
        _good_cbz(folder / "Later 001.cbz")
        ok, _ = lh.recheck_empty_folder(str(folder))
        assert ok is True and self._rows() == {}

    def test_inspect_folder_refuses_roots(self, store, library):
        assert lh.inspect_folder(str(library)) is None
        (library / "X").mkdir()
        assert lh.inspect_folder(str(library / "X")) == ("EmptyFolder", "No files")

    def test_the_cheap_checks_include_it(self, store, library):
        (library / "Hollow").mkdir()
        result = lh.run_scan(batch=1)
        assert result["empty_folders"]["flagged"] == 1
        assert lh.get_summary()["empty_folders_open"] == 1

    def test_clear_problems_under_is_a_prefix_not_a_pattern(self, store, library):
        """A folder name with % or _ must not match its neighbours as LIKE would."""
        from core.problem_files import (
            SOURCE_SERIES_JSON, clear_problems_under, record_problem,
        )

        inside = os.path.join(str(library), "100%_Done", "series.json")
        sibling = os.path.join(str(library), "100X-Done", "series.json")
        own = os.path.join(str(library), "100%_Done")
        for p in (inside, sibling):
            record_problem(p, SOURCE_SERIES_JSON, error_class="x", error_message="x")
        record_problem(own, "empty-folder", error_class="x", error_message="x")

        assert clear_problems_under(own) == 1
        assert get_problem(inside, SOURCE_SERIES_JSON) is None
        assert get_problem(sibling, SOURCE_SERIES_JSON) is not None
        assert get_problem(own, "empty-folder") is not None


class TestStagingIsNotTheLibrary:
    """WATCH and TARGET hold downloads in transit. Nothing in them is hashed,
    counted, paired as a duplicate or listed -- whoever put a row there."""

    @pytest.fixture
    def staging(self, tmp_path):
        watch = tmp_path / "downloads" / "temp"
        target = tmp_path / "downloads" / "processed"
        watch.mkdir(parents=True)
        target.mkdir(parents=True)
        with patch("core.config.get_watch_dir", return_value=str(watch)), \
                patch("core.config.get_target_dir", return_value=str(target)):
            yield watch, target

    def test_staged_files_are_not_hashed_or_counted(self, store, library, staging):
        watch, target = staging
        mine = _good_cbz(library / "Mine 001.cbz")
        in_target = _corrupt_cbz(target / "Waiting 001.cbz")
        in_watch = _corrupt_cbz(watch / "Arriving 001.cbz")
        for p in (mine, in_target, in_watch):
            _index(store, p)

        assert [f["path"] for f in lh.files_needing_check(50, 90)] == [mine]
        stats = lh.run_batch(50, 90)
        assert stats["checked"] == 1 and stats["damaged"] == 0
        assert list_problems() == []
        assert _health(store, in_target) is None and _health(store, in_watch) is None

    def test_a_download_is_not_a_duplicate_of_the_library_copy(self, store, library, staging):
        _, target = staging
        mine = _good_cbz(library / "Batman 001 (2016).cbz")
        waiting = _good_cbz(target / "Batman 001 (2016).cbz")
        tags = {"ci_series": "Batman", "ci_number": "1", "ci_year": "2016",
                "ci_publisher": "DC", "has_comicinfo": 1}
        _index(store, mine, **tags)
        _index(store, waiting, **tags)
        # Identical bytes, hashed by an older version.
        digest = lh.hash_file(mine)
        for p in (mine, waiting):
            st = os.stat(p)
            lh._store_health(store, p, digest, st.st_size, st.st_mtime, "ok")
        store.commit()
        assert lh.find_duplicates(include_dismissed=True) == {"exact": [], "likely": []}

    def test_copy_markers_inside_target_are_not_a_group(self, store, library, staging):
        _, target = staging
        _index(store, _good_cbz(target / "Saga 001.cbz"))
        _index(store, _good_cbz(target / "Saga 001 (1).cbz"))
        assert lh.find_duplicates() == {"exact": [], "likely": []}

    def test_the_index_refuses_a_staged_path(self, store, library, staging):
        from core.database import add_file_index_entry

        watch, target = staging
        for folder in (watch, target):
            p = _good_cbz(folder / "New 001.cbz")
            assert add_file_index_entry("New 001.cbz", p, "file", size=1,
                                        parent=str(folder)) is False
        mine = _good_cbz(library / "Mine 001.cbz")
        assert add_file_index_entry("Mine 001.cbz", mine, "file", size=1,
                                    parent=str(library)) is True
        assert [r["path"] for r in store.execute("SELECT path FROM file_index")] == [mine]

    def test_nothing_in_target_is_ever_listed(self, store, library, staging):
        from core import problem_files as pf

        _, target = staging
        p = _corrupt_cbz(target / "Waiting 001.cbz")
        for source in (pf.SOURCE_THUMBNAIL, pf.SOURCE_REBUILD, pf.SOURCE_CONVERT,
                       pf.SOURCE_UNPACK, pf.SOURCE_METADATA_WRITE, pf.SOURCE_INTEGRITY,
                       pf.SOURCE_ORPHAN):
            assert record_problem(p, source, error_class="X", error_message="y") is False
        assert list_problems() == []

    def test_watch_is_listed_only_by_the_folder_monitor(self, store, library, staging):
        from core import problem_files as pf

        watch, _ = staging
        p = _corrupt_cbz(watch / "Arriving 001.cbz")
        assert record_problem(p, pf.SOURCE_THUMBNAIL, error_class="X", error_message="y") is False
        assert record_problem(p, pf.SOURCE_INTEGRITY, error_class="X", error_message="y") is False
        # What the monitor reports about its own inbox is the only place the
        # user sees it: the file never reaches a library.
        assert record_problem(p, pf.SOURCE_UNPACK, error_class="BadZipFile",
                              error_message="y") is True
        assert record_problem(p, pf.SOURCE_CONVERT, error_class=pf.CLASS_PDF_FAILED,
                              error_message="y") is True
        assert {r["source"] for r in list_problems()} == {pf.SOURCE_UNPACK, pf.SOURCE_CONVERT}

    def test_a_recheck_of_a_staged_file_drops_its_row(self, store, library, tmp_path):
        target = tmp_path / "processed"
        target.mkdir()
        p = _corrupt_cbz(target / "Waiting 001.cbz")
        record_problem(p, SOURCE_INTEGRITY, error_class="BadCRC", error_message="y")
        with patch("core.config.get_watch_dir", return_value=""), \
                patch("core.config.get_target_dir", return_value=str(target)):
            ok, _ = lh.recheck_file(p)
        assert ok is True
        assert get_problem(p, SOURCE_INTEGRITY) is None

    def test_purge_removes_what_older_versions_recorded(self, store, library, tmp_path):
        from core import problem_files as pf

        watch = tmp_path / "downloads" / "temp"
        target = tmp_path / "downloads" / "processed"
        watch.mkdir(parents=True)
        target.mkdir(parents=True)
        mine = _good_cbz(library / "Mine 001.cbz")
        in_target = _corrupt_cbz(target / "Waiting 001.cbz")
        in_watch = _corrupt_cbz(watch / "Arriving 001.cbz")
        # Recorded while no staging folder was configured, as an older version did.
        for p in (mine, in_target, in_watch):
            _index(store, p)
            st = os.stat(p)
            lh._store_health(store, p, "h", st.st_size, st.st_mtime, "ok")
        store.execute(
            "INSERT INTO file_index (name, path, type, parent) VALUES (?, ?, 'directory', ?)",
            ("Sub", str(target / "Sub"), str(target)),
        )
        store.commit()
        record_problem(mine, pf.SOURCE_THUMBNAIL, error_class="X", error_message="y")
        record_problem(in_target, SOURCE_INTEGRITY, error_class="BadCRC", error_message="y")
        record_problem(in_target, pf.SOURCE_CONVERT, error_class="X", error_message="y")
        record_problem(in_watch, pf.SOURCE_THUMBNAIL, error_class="X", error_message="y")
        record_problem(in_watch, pf.SOURCE_UNPACK, error_class="BadZipFile", error_message="y")
        record_problem(in_watch, SOURCE_ORPHAN, error_class="StrayInStaging",
                       error_message="Unfiled in WATCH for over a day")

        with patch("core.config.get_watch_dir", return_value=str(watch)), \
                patch("core.config.get_target_dir", return_value=str(target)):
            out = lh.purge_staging_rows()
            assert out == {"index": 3, "health": 2, "problems": 4}
            assert [r["path"] for r in store.execute("SELECT path FROM file_index")] == [mine]
            assert [r["path"] for r in store.execute("SELECT path FROM file_health")] == [mine]
            left = {(r["path"], r["source"]) for r in list_problems()}
            assert left == {(mine, pf.SOURCE_THUMBNAIL), (in_watch, pf.SOURCE_UNPACK)}
            # Nothing left to do the second time.
            assert lh.purge_staging_rows() == {"index": 0, "health": 0, "problems": 0}

    def test_a_scan_purges_first(self, store, library, staging):
        _, target = staging
        mine = _good_cbz(library / "Mine 001.cbz")
        waiting = _good_cbz(target / "Mine 001 (1).cbz")
        _index(store, mine)
        _index(store, waiting)
        with patch("core.series_json_check.scan_library", return_value={}):
            result = lh.run_scan(run_cheap=False, batch=10)
        assert result["staging_purged"]["index"] == 1
        assert result["batch"]["checked"] == 1
        assert lh.load_state()["coverage"] == {"total": 1, "checked": 1, "pct": 100}

    def test_staging_rows_are_purged_even_with_the_scan_off(self, store, library, staging):
        _, target = staging
        _index(store, _good_cbz(target / "Waiting 001.cbz"))
        lh._staging_purge_at["at"] = 0.0
        with patch.object(lh, "run_scan") as run:
            assert lh.run_scheduled_scan() is None
            run.assert_not_called()
        assert store.execute("SELECT COUNT(*) FROM file_index").fetchone()[0] == 0


class TestDuplicateGroupingIsReused:
    def _pair(self, store, library):
        a = _good_cbz(library / "Batman 001 (2016).cbz")
        b = _good_cbz(library / "Batman 001 (2016) (1).cbz")
        _index(store, a)
        _index(store, b)
        return a, b

    def test_an_unchanged_index_is_not_regrouped(self, store, library):
        self._pair(store, library)
        lh.invalidate_duplicates()
        with patch.object(lh, "name_key", wraps=lh.name_key) as key:
            first = lh.find_duplicates()
            assert key.call_count == 2
            assert lh.find_duplicates() == first
            assert lh.count_duplicates() == (0, 1)
            assert key.call_count == 2

    def test_any_change_to_the_rows_regroups(self, store, library):
        a, b = self._pair(store, library)
        lh.invalidate_duplicates()
        assert len(lh.find_duplicates()["likely"]) == 1
        # A rename made anywhere, by anything: no hook has to say so.
        renamed = str(library / "Batman 002 (2016).cbz")
        os.rename(b, renamed)
        store.execute("UPDATE file_index SET path = ?, name = ? WHERE path = ?",
                      (renamed, os.path.basename(renamed), b))
        store.commit()
        assert lh.find_duplicates() == {"exact": [], "likely": []}

    def test_dismissing_costs_no_regrouping(self, store, library):
        self._pair(store, library)
        lh.invalidate_duplicates()
        group = lh.find_duplicates()["likely"][0]
        with patch.object(lh, "name_key", wraps=lh.name_key) as key:
            assert lh.dismiss_group(group["id"], until_scan=True)["success"] is True
            assert lh.find_duplicates() == {"exact": [], "likely": []}
            shown = lh.find_duplicates(include_dismissed=True)["likely"]
            assert shown[0]["dismissed"] is True and shown[0]["dismiss_mode"] == "scan"
            key.assert_not_called()

    def test_a_cached_group_is_not_shared_with_the_caller(self, store, library):
        self._pair(store, library)
        lh.find_duplicates()["likely"][0]["dismissed"] = "tampered"
        assert lh.find_duplicates()["likely"][0]["dismissed"] is False


class TestScanStages:
    def test_series_json_walk_is_skipped_while_the_page_holds_the_claim(self, store, library):
        from core import series_json_check

        _index(store, _good_cbz(library / "One 001.cbz"))
        assert series_json_check.claim_scan()
        try:
            with patch("core.series_json_check.scan_library") as walk:
                result = lh.run_scan(run_cheap=True, batch=10)
            walk.assert_not_called()
        finally:
            series_json_check.release_scan()
        assert "skipped" in result["series_json"]
        # ...and it gives the claim back when it did take it.
        with patch("core.series_json_check.scan_library", return_value={}):
            lh.run_scan(run_cheap=True, batch=10)
        assert series_json_check.claim_scan()
        series_json_check.release_scan()

    def test_a_busy_empty_folder_scan_is_reported_as_skipped(self, store, library):
        _index(store, _good_cbz(library / "One 001.cbz"))
        with patch("core.series_json_check.scan_library", return_value={}), \
                patch.object(lh, "scan_empty_folders", return_value=None):
            result = lh.run_scan(run_cheap=True, batch=10)
        assert "skipped" in result["empty_folders"]

    def test_state_is_saved_after_the_batch_not_only_at_the_end(self, store, library):
        """A scan killed during the walks must not lose the batch it finished."""
        _index(store, _good_cbz(library / "One 001.cbz"))
        with patch("core.series_json_check.scan_library", side_effect=RuntimeError("killed")):
            result = lh.run_scan(run_cheap=True, batch=10)
        assert "error" in result
        state = lh.load_state()
        assert state["last_batch"]["checked"] == 1
        assert state["coverage"]["checked"] == 1
        assert not state.get("cheap_checks_at")

    def test_a_stopped_scan_skips_the_walks_and_keeps_the_batch(self, store, library):
        for i in range(3):
            _index(store, _good_cbz(library / f"Stop {i:03d}.cbz", payload=bytes([70 + i])))
        assert lh.claim_scan()
        try:
            real = lh.check_file

            def check(path):
                lh.request_stop()
                return real(path)

            with patch.object(lh, "check_file", side_effect=check), \
                    patch("core.series_json_check.scan_library") as walk:
                result = lh.run_scan(run_cheap=True, batch=10)
            walk.assert_not_called()
        finally:
            lh.release_scan()
        assert result["stopped"] is True
        state = lh.load_state()
        assert state["last_batch"]["checked"] == 1
        assert not state.get("cheap_checks_at")

    def test_the_summary_uses_the_stored_coverage(self, store, library):
        _index(store, _good_cbz(library / "One 001.cbz"))
        lh.save_state({"last_run_at": 1.0,
                       "coverage": {"total": 7, "checked": 3, "pct": 42}})
        with patch.object(lh, "coverage", side_effect=AssertionError("counted")):
            assert lh.get_summary()["coverage"]["pct"] == 42
        lh._coverage_cache["value"] = None
        assert lh.get_summary(live=True)["coverage"]["total"] == 1


class TestUnreadableFoldersAreNotEmpty:
    """"Could not look" must never become "nothing there"."""

    def test_a_folder_that_will_not_list_keeps_its_parent_off_the_list(self, store, library):
        series = library / "Publisher" / "Series"
        series.mkdir(parents=True)
        _good_cbz(series / "Issue 001.cbz")
        real_scandir = os.scandir

        def scandir(path="."):
            if os.path.normpath(str(path)) == os.path.normpath(str(series)):
                raise PermissionError(13, "Permission denied", str(path))
            return real_scandir(path)

        with patch("helpers.library.get_protected_roots", return_value=[str(library)]), \
                patch("os.scandir", side_effect=scandir):
            found = lh._empty_tree(str(library), {os.path.realpath(str(library))})
            assert str(series) not in found
            assert str(library / "Publisher") not in found
            assert lh.inspect_folder(str(library / "Publisher")) is None
            assert lh.inspect_folder(str(series)) is None

    def test_series_json_rows_under_an_unreadable_folder_are_kept(self, store, library):
        from core import problem_files as pf
        from core.series_json_check import scan_library

        _good_cbz(library / "Keep 001.cbz")
        locked = library / "Locked"
        locked.mkdir()
        listed = str(locked / "Series" / "series.json")
        record_problem(listed, pf.SOURCE_SERIES_JSON, error_class=pf.CLASS_SERIES_JSON_BLANK,
                       error_message="name is blank")
        real_scandir = os.scandir

        def scandir(path="."):
            if os.path.normpath(str(path)) == os.path.normpath(str(locked)):
                raise PermissionError(13, "Permission denied", str(path))
            return real_scandir(path)

        with patch("os.scandir", side_effect=scandir):
            stats = scan_library()
        assert stats["cleared"] == 0
        assert get_problem(listed, pf.SOURCE_SERIES_JSON) is not None
