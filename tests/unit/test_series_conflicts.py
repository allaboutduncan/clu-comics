"""Series folder conflicts (core/series_conflicts.py) against a real schema.

Each class names a rule a later tidy-up could plausibly break.
"""

from unittest.mock import MagicMock, patch

import pytest

from core import series_conflicts as sc


@pytest.fixture
def store(db_connection):
    yield db_connection


@pytest.fixture
def library(tmp_path):
    root = tmp_path / "library"
    root.mkdir()
    # A mounted root is a directory with at least one entry.
    (root / "keep.txt").write_text("x")
    return root


def _folder(root, name, comics=("A 001.cbz",)):
    folder = root / name
    folder.mkdir(parents=True)
    for comic in comics:
        (folder / comic).write_bytes(b"PK")
    (folder / "series.json").write_text("{}")
    return str(folder)


def _review(folder, mapped, series_id=101, name="Savage Dragon"):
    return {
        "folder": folder, "metron_id": series_id, "series_name": name,
        "year": 1993, "publisher_name": "Image", "status": "Ongoing",
        "cv_id": None, "source": "series.json:metron_id",
        "conflict_with": mapped, "reason": f"Series already mapped to {mapped}",
    }


class TestRecordScan:
    def test_records_rows_with_file_counts(self, store, library):
        a = _folder(library, "Image/Savage Dragon", ("SD 001.cbz", "SD 002.cbz"))
        out = sc.record_scan([_review(a, "/x/Image Comics/Savage Dragon")], [str(library)])
        assert out["open"] == 1
        rows = sc.list_conflicts()
        assert len(rows) == 1
        assert rows[0]["folder"] == a
        assert rows[0]["file_count"] == 2
        assert rows[0]["mapped_folder"] == "/x/Image Comics/Savage Dragon"
        assert rows[0]["year"] == "1993"

    def test_rescan_updates_instead_of_duplicating(self, store, library):
        a = _folder(library, "Image/Kaya")
        sc.record_scan([_review(a, "/m1")], [str(library)])
        sc.record_scan([_review(a, "/m2")], [str(library)])
        rows = sc.list_conflicts()
        assert len(rows) == 1
        assert rows[0]["mapped_folder"] == "/m2"

    def test_a_folder_the_scan_resolved_is_pruned(self, store, library):
        a = _folder(library, "Image/Feral")
        sc.record_scan([_review(a, "/m")], [str(library)])
        sc.record_scan([], [str(library)], resolved=[a])
        assert sc.list_conflicts() == []

    def test_a_vanished_folder_is_pruned(self, store, library):
        a = _folder(library, "Image/Feral")
        sc.record_scan([_review(a, "/m")], [str(library)])
        import shutil
        shutil.rmtree(a)
        sc.record_scan([], [str(library)])
        assert sc.list_conflicts() == []

    def test_a_folder_that_became_the_series_folder_is_pruned(self, store, library):
        a = _folder(library, "Image/Feral")
        sc.record_scan([_review(a, "/m")], [str(library)])
        store.execute("INSERT INTO series (id, name, mapped_path) VALUES (101, 'Feral', ?)", (a,))
        store.commit()
        sc.record_scan([], [str(library)])
        assert sc.list_conflicts() == []

    def test_a_folder_now_reported_for_another_series_drops_the_old_row(self, store, library):
        a = _folder(library, "Image/Feral")
        sc.record_scan([_review(a, "/m", series_id=101)], [str(library)])
        sc.record_scan([_review(a, "/m2", series_id=202)], [str(library)])
        assert [r["series_id"] for r in sc.list_conflicts()] == [202]

    def test_an_unreported_folder_is_kept_with_its_ignore(self, store, library):
        """Metron down: the folder lands in the scan's errors or skipped, not in review.

        Missing from review is no evidence the conflict is gone, and one such
        scan used to clear the list and every Ignore.
        """
        a = _folder(library, "Image/Feral")
        sc.record_scan([_review(a, "/m")], [str(library)])
        sc.set_ignored(sc.list_conflicts()[0]["id"])
        sc.record_scan([], [str(library)])
        rows = sc.list_conflicts(include_ignored=True)
        assert len(rows) == 1 and rows[0]["ignored"] is True

    def test_disk_is_read_before_the_write_transaction(self, store, library):
        """Listing folders on a NAS must not hold the WAL write lock."""
        a = _folder(library, "Image/Feral")
        b = _folder(library, "Image/Redcoat")
        calls = []
        real = sc.folder_files

        def spy(folder):
            from core.database import get_db_connection
            probe = get_db_connection()
            try:
                # Fails at once with "database is locked" if a writer holds the lock.
                probe.execute("PRAGMA busy_timeout=0")
                probe.execute("BEGIN IMMEDIATE")
                probe.rollback()
                calls.append(folder)
            finally:
                probe.close()
            return real(folder)

        with patch.object(sc, "folder_files", side_effect=spy):
            out = sc.record_scan([_review(a, "/m"), _review(b, "/m", series_id=102)], [str(library)])
        assert calls == [a, b]
        assert out["open"] == 2

    def test_row_under_an_unmounted_root_is_kept(self, store, library):
        """An asleep NAS is an empty directory, not proof the conflict is resolved."""
        a = _folder(library, "Image/Redcoat")
        sc.record_scan([_review(a, "/m")], [str(library)])
        with patch("helpers.library.is_root_mounted", return_value=False):
            sc.record_scan([], [str(library)])
        assert len(sc.list_conflicts()) == 1

    def test_returns_none_when_the_database_is_unavailable(self):
        with patch("core.database.get_db_connection", return_value=None):
            assert sc.record_scan([_review("/a", "/m")], ["/"]) is None

    def test_list_returns_none_on_read_failure_not_empty(self):
        with patch("core.database.get_db_connection", return_value=None):
            assert sc.list_conflicts() is None


class TestIgnore:
    def test_ignored_row_is_hidden_and_counted(self, store, library):
        a = _folder(library, "Image/Monstress")
        sc.record_scan([_review(a, "/m")], [str(library)])
        row = sc.list_conflicts()[0]
        assert sc.set_ignored(row["id"]) is True
        assert sc.list_conflicts() == []
        assert sc.count_open() == 0
        assert sc.list_conflicts(include_ignored=True)[0]["ignored"] is True

    def test_ignored_row_survives_rescan_and_is_reported(self, store, library):
        a = _folder(library, "Image/Monstress")
        sc.record_scan([_review(a, "/m")], [str(library)])
        sc.set_ignored(sc.list_conflicts()[0]["id"])
        out = sc.record_scan([_review(a, "/m")], [str(library)])
        assert out["open"] == 0
        assert out["ignored"] == 1
        assert sc._norm(a) in out["ignored_folders"]

    def test_new_comic_reopens_an_ignored_folder(self, store, library):
        a = _folder(library, "Image/Monstress")
        sc.record_scan([_review(a, "/m")], [str(library)])
        sc.set_ignored(sc.list_conflicts()[0]["id"])
        with open(f"{a}/Monstress 002.cbz", "wb") as f:
            f.write(b"PK")
        out = sc.record_scan([_review(a, "/m")], [str(library)])
        assert out["open"] == 1
        assert sc.list_conflicts()[0]["ignored"] is False

    def test_unignore(self, store, library):
        a = _folder(library, "Image/Monstress")
        sc.record_scan([_review(a, "/m")], [str(library)])
        cid = sc.list_conflicts()[0]["id"]
        sc.set_ignored(cid)
        sc.set_ignored(cid, ignored=False)
        assert sc.count_open() == 1

    def test_unknown_id(self, store):
        assert sc.set_ignored(9999) is False


class TestAfterSwitch:
    def test_old_folder_becomes_the_conflict(self, store, library):
        a = _folder(library, "Image/Ghost Pepper")
        old = _folder(library, "Skybound/Ghost Pepper", ("GP 001.cbz", "GP 002.cbz", "GP 003.cbz"))
        sc.record_scan([_review(a, old)], [str(library)])
        cid = sc.list_conflicts()[0]["id"]
        assert sc.after_switch(cid, old) is True
        row = sc.get_conflict(cid)
        assert row["folder"] == old
        assert row["mapped_folder"] == a
        assert row["file_count"] == 3

    def test_other_rows_of_the_series_follow_the_new_folder(self, store, library):
        a = _folder(library, "Image/Ghost Pepper")
        b = _folder(library, "Other/Ghost Pepper")
        old = _folder(library, "Skybound/Ghost Pepper")
        sc.record_scan([_review(a, old), _review(b, old)], [str(library)])
        rows = {r["folder"]: r for r in sc.list_conflicts()}
        sc.after_switch(rows[a]["id"], old)
        assert sc.get_conflict(rows[b]["id"])["mapped_folder"] == a

    def test_old_folder_with_its_own_row_does_not_break_the_key(self, store, library):
        a = _folder(library, "A/Ghost Pepper")
        b = _folder(library, "B/Ghost Pepper")
        sc.record_scan([_review(a, b), _review(b, a)], [str(library)])
        rows = {r["folder"]: r for r in sc.list_conflicts()}
        assert sc.after_switch(rows[a]["id"], b) is True
        remaining = sc.list_conflicts()
        assert [r["folder"] for r in remaining] == [b]

    def test_no_previous_folder_drops_the_row(self, store, library):
        a = _folder(library, "Image/Ghost Pepper")
        sc.record_scan([_review(a, None)], [str(library)])
        cid = sc.list_conflicts()[0]["id"]
        sc.after_switch(cid, None)
        assert sc.get_conflict(cid) is None


class TestLiveMappedFolder:
    def test_series_mapped_path_wins_over_the_stored_one(self, store, library):
        a = _folder(library, "Image/Kaya")
        sc.record_scan([_review(a, "/stale", series_id=555)], [str(library)])
        store.execute(
            "INSERT INTO series (id, name, mapped_path) VALUES (?, ?, ?)",
            (555, "Kaya", "/now/here"),
        )
        store.commit()
        assert sc.list_conflicts()[0]["mapped_folder"] == "/now/here"


class TestScanJobRecordsConflicts:
    """The scan job stores conflicts and leaves ignored folders out of the dialog."""

    def _run(self, scan_result, recorded):
        from models import library_automap

        op_id = "test-conflicts"
        library_automap._jobs[op_id] = {
            "id": op_id, "status": "running", "current": 0, "total": 1,
            "detail": "", "result": None, "error": None,
        }
        try:
            with patch.object(library_automap.metron, "get_flask_api", return_value=None), \
                 patch.object(library_automap.comicvine_source, "is_available", return_value=False), \
                 patch.object(library_automap, "scan_library_for_automap", return_value=scan_result), \
                 patch.object(library_automap, "apply_automap",
                              return_value={"applied": 0, "failed": [], "applied_ids": []}), \
                 patch.object(library_automap, "get_library_roots", return_value=["/data"]), \
                 patch.object(library_automap, "repair_volume_named_series"), \
                 patch.object(library_automap, "match_unmatched_mapped_series"), \
                 patch("core.series_conflicts.record_scan", return_value=recorded) as rec:
                library_automap._run_scan_job_inner(op_id, app=MagicMock())
            return library_automap._jobs[op_id]["result"], rec
        finally:
            library_automap._jobs.pop(op_id, None)

    def test_every_review_item_is_recorded_and_ignored_ones_hidden(self):
        review = [{"folder": "/data/A", "metron_id": 1}, {"folder": "/data/B", "metron_id": 2}]
        result, rec = self._run(
            {"auto": [], "review": list(review), "skipped": [], "errors": [], "total_candidates": 2},
            {"open": 1, "ignored": 1, "ignored_folders": {"/data/B"}},
        )
        assert rec.call_args[0][0] == review
        assert [it["folder"] for it in result["review"]] == ["/data/A"]
        assert result["review_total"] == 1
        assert result["conflicts_ignored"] == 1
        assert result["conflicts_saved"] is True

    def test_unsaved_conflicts_are_still_reported(self):
        review = [{"folder": "/data/A", "metron_id": 1}]
        result, _ = self._run(
            {"auto": [], "review": review, "skipped": [], "errors": [], "total_candidates": 1},
            None,
        )
        assert result["review_total"] == 1
        assert result["conflicts_saved"] is False
