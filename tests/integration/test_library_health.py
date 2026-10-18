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
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("p001.jpg", payload * 64)
        zf.writestr("p002.jpg", payload * 64)
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
        assert stats["aborted"] is True
        assert stats["checked"] == 0
        assert list_problems() == []
        assert store.execute("SELECT COUNT(*) FROM file_health").fetchone()[0] == 0

    def test_a_file_gone_from_a_present_folder_stops_heading_the_queue(self, store, library):
        p = _good_cbz(library / "Gone 001.cbz")
        _index(store, p)
        os.remove(p)
        stats = lh.run_batch(50, 90)
        assert stats["missing"] == 1
        # No ledger row: the listing would prune it on sight (_is_definitely_gone).
        assert list_problems() == []
        assert _health(store, p)["status"] == "missing"
        assert lh.run_batch(50, 90)["missing"] == 0

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

    def test_old_comic_in_target_is_stray_and_young_one_is_not(self, store, tmp_path, library):
        target = tmp_path / "processed"
        target.mkdir()
        old = target / "Old 001.cbz"
        young = target / "Young 001.cbz"
        _good_cbz(old)
        _good_cbz(young, payload=b"Y")
        long_ago = time.time() - 3 * 86400
        os.utime(old, (long_ago, long_ago))
        with patch("core.config.get_target_dir", return_value=str(target)), \
             patch("core.config.get_watch_dir", return_value=""):
            stats = lh.scan_orphans()
        assert stats["strays"] == 1
        assert get_problem(str(old), SOURCE_ORPHAN)["error_class"] == "StrayInStaging"
        assert get_problem(str(young), SOURCE_ORPHAN) is None

    def test_partial_downloads_are_never_strays(self, store, tmp_path, library):
        watch = tmp_path / "watch"
        watch.mkdir()
        part = watch / "Big 001.cbz.0.crdownload"
        part.write_bytes(b"x")
        long_ago = time.time() - 3 * 86400
        os.utime(part, (long_ago, long_ago))
        with patch("core.config.get_target_dir", return_value=""), \
             patch("core.config.get_watch_dir", return_value=str(watch)):
            assert lh.scan_orphans()["strays"] == 0

    def test_staging_inside_a_library_is_left_alone(self, store, library):
        """Inside a library every folder is library structure; guessing is how
        26 comics were once moved into the wrong series."""
        inside = library / "incoming"
        inside.mkdir()
        old = inside / "Old 001.cbz"
        _good_cbz(old)
        long_ago = time.time() - 3 * 86400
        os.utime(old, (long_ago, long_ago))
        with patch("core.config.get_target_dir", return_value=str(inside)), \
             patch("core.config.get_watch_dir", return_value=""):
            assert lh.scan_orphans()["strays"] == 0

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
        assert lh.dismiss_group(gid) == (True, None)
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
