"""Tests for wanted-list reconciliation when a file lands in a series folder.

Covers helpers/collection.py: reconcile_wanted_for_series, reconcile_wanted_for_path,
and the robust path->series resolver _series_id_for_path.
"""
import pytest
from tests.factories.db_factories import create_publisher, create_series, create_issue


def _wanted(issue_id, number):
    return {
        "id": issue_id,
        "number": number,
        "name": f"Issue {number}",
        "store_date": "2020-01-10",
        "cover_date": "2020-01-15",
        "image": None,
    }


class TestSeriesIdForPath:

    def test_resolves_file_in_series_folder(self, db_connection, tmp_path):
        from helpers.collection import _series_id_for_path

        series_dir = tmp_path / "Batman"
        series_dir.mkdir()
        series_id = create_series(name="Batman", mapped_path=str(series_dir))

        # File not yet on disk -> resolves via its parent directory.
        assert _series_id_for_path(str(series_dir / "Batman #1.cbz")) == series_id

    def test_resolves_file_in_nested_subfolder(self, db_connection, tmp_path):
        from helpers.collection import _series_id_for_path

        series_dir = tmp_path / "Flash"
        (series_dir / "sub").mkdir(parents=True)
        series_id = create_series(name="Flash", mapped_path=str(series_dir))

        # Fragile exact-match invalidation misses subfolders; prefix match resolves.
        assert _series_id_for_path(str(series_dir / "sub" / "Flash #2.cbz")) == series_id

    def test_unrelated_path_returns_none(self, db_connection, tmp_path):
        from helpers.collection import _series_id_for_path

        series_dir = tmp_path / "Batman"
        series_dir.mkdir()
        create_series(name="Batman", mapped_path=str(series_dir))

        assert _series_id_for_path(str(tmp_path / "Nowhere" / "x.cbz")) is None


class TestReconcileWantedForSeries:

    def test_prunes_only_satisfied_issue(self, db_connection, tmp_path):
        from core.database import save_wanted_issues_for_series, get_cached_wanted_issues
        from helpers.collection import reconcile_wanted_for_series

        series_dir = tmp_path / "Batman"
        series_dir.mkdir()
        series_id = create_series(name="Batman", mapped_path=str(series_dir))
        id5 = create_issue(series_id=series_id, number="5")
        id6 = create_issue(series_id=series_id, number="6")

        save_wanted_issues_for_series(
            series_id, "Batman", 2020, [_wanted(id5, "5"), _wanted(id6, "6")]
        )

        # A file satisfying #5 lands in the folder (generic issue-number fallback).
        (series_dir / "Batman #5.cbz").write_bytes(b"stub")

        removed = reconcile_wanted_for_series(series_id)
        assert removed == 1

        remaining = [w["issue_number"] for w in get_cached_wanted_issues()
                     if w["series_id"] == series_id]
        assert "5" not in remaining
        assert "6" in remaining

    def test_no_matching_file_leaves_wanted_intact(self, db_connection, tmp_path):
        from core.database import save_wanted_issues_for_series, get_cached_wanted_issues
        from helpers.collection import reconcile_wanted_for_series

        series_dir = tmp_path / "Batman"
        series_dir.mkdir()
        series_id = create_series(name="Batman", mapped_path=str(series_dir))
        id5 = create_issue(series_id=series_id, number="5")
        save_wanted_issues_for_series(series_id, "Batman", 2020, [_wanted(id5, "5")])

        removed = reconcile_wanted_for_series(series_id)
        assert removed == 0

        remaining = [w["issue_number"] for w in get_cached_wanted_issues()
                     if w["series_id"] == series_id]
        assert "5" in remaining


class TestReconcileWantedForPath:

    def test_path_reconcile_removes_wanted(self, db_connection, tmp_path):
        from core.database import save_wanted_issues_for_series, get_cached_wanted_issues
        from helpers.collection import reconcile_wanted_for_path

        series_dir = tmp_path / "Batman"
        series_dir.mkdir()
        series_id = create_series(name="Batman", mapped_path=str(series_dir))
        id5 = create_issue(series_id=series_id, number="5")
        save_wanted_issues_for_series(series_id, "Batman", 2020, [_wanted(id5, "5")])

        landed = series_dir / "Batman #5.cbz"
        landed.write_bytes(b"stub")

        removed = reconcile_wanted_for_path(str(landed))
        assert removed == 1
        assert not any(w["series_id"] == series_id for w in get_cached_wanted_issues())

    def test_path_outside_mapped_series_is_noop(self, db_connection, tmp_path):
        from helpers.collection import reconcile_wanted_for_path

        assert reconcile_wanted_for_path(str(tmp_path / "Unmapped" / "x.cbz")) == 0


class TestCollectionStatusSelfHeal:
    """A file added after the last scan must be detected even though nothing
    explicitly invalidated the cache (watchdog events can be missed)."""

    def _objs(self, series_id):
        from core.database import get_series_by_id, get_issues_for_series
        from models.issue import IssueObj, SeriesObj
        series = get_series_by_id(series_id)
        issues = get_issues_for_series(series_id)
        return [IssueObj(i) for i in issues], SeriesObj(series)

    def test_added_file_invalidates_stale_not_found(self, db_connection, tmp_path):
        from helpers.collection import match_issues_to_collection

        series_dir = tmp_path / "Sorcerer Supreme (2026)"
        series_dir.mkdir()
        series_id = create_series(
            name="Sorcerer Supreme", volume=2026, mapped_path=str(series_dir)
        )
        create_issue(series_id=series_id, number="8")

        issue_objs, series_obj = self._objs(series_id)

        # First scan: empty folder -> #8 cached as not-found (file_path=None).
        r1 = match_issues_to_collection(str(series_dir), issue_objs, series_obj)
        assert r1["8"]["found"] is False

        # A correctly-named file lands *after* the scan (no invalidation fires).
        (series_dir / "Sorcerer Supreme 008 (2026).cbz").write_bytes(b"stub")

        # Second scan must self-heal (dir now has more files than matched) and
        # recognize #8 via the generic issue-number fallback.
        r2 = match_issues_to_collection(str(series_dir), issue_objs, series_obj)
        assert r2["8"]["found"] is True

    def test_no_rescan_when_all_found(self, db_connection, tmp_path):
        """An untracked extra file must not force endless re-scans once every
        tracked issue is already satisfied."""
        from helpers.collection import match_issues_to_collection

        series_dir = tmp_path / "Batman (2020)"
        series_dir.mkdir()
        series_id = create_series(
            name="Batman", volume=2020, mapped_path=str(series_dir)
        )
        create_issue(series_id=series_id, number="1")
        (series_dir / "Batman 001 (2020).cbz").write_bytes(b"stub")

        issue_objs, series_obj = self._objs(series_id)

        r1 = match_issues_to_collection(str(series_dir), issue_objs, series_obj)
        assert r1["1"]["found"] is True

        # Add an untracked extra file; with no missing issues the cache stays valid.
        (series_dir / "Some Extra Variant.cbz").write_bytes(b"stub")
        r2 = match_issues_to_collection(str(series_dir), issue_objs, series_obj)
        assert r2["1"]["found"] is True
