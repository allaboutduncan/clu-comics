"""Tests for Pull List collection-status aggregation and the monitored flag."""
import pytest
from tests.factories.db_factories import create_publisher, create_series, create_issue


def _mark_found(series_id, issue_id, issue_number, found):
    from core.database import save_collection_status_bulk
    save_collection_status_bulk([{
        "series_id": series_id,
        "issue_id": issue_id,
        "issue_number": str(issue_number),
        "found": 1 if found else 0,
        "file_path": "/data/x.cbz" if found else None,
        "file_mtime": 0.0,
        "matched_via": "filename",
    }])


class TestSeriesMonitoredFlag:

    def test_defaults_to_monitored(self, db_connection):
        from core.database import get_series_monitored
        pub_id = create_publisher()
        sid = create_series(series_id=100, publisher_id=pub_id)
        # New rows default to monitored (column DEFAULT 1).
        assert get_series_monitored(sid) is True

    def test_set_and_get(self, db_connection):
        from core.database import set_series_monitored, get_series_monitored
        pub_id = create_publisher()
        sid = create_series(series_id=100, publisher_id=pub_id)

        set_series_monitored(sid, False)
        assert get_series_monitored(sid) is False
        set_series_monitored(sid, True)
        assert get_series_monitored(sid) is True

    def test_flows_into_mapped_series(self, db_connection):
        from core.database import set_series_monitored, get_all_mapped_series
        pub_id = create_publisher()
        sid = create_series(series_id=100, publisher_id=pub_id)
        set_series_monitored(sid, False)

        row = next(s for s in get_all_mapped_series() if s["id"] == sid)
        assert row["monitored"] == 0


class TestPullListCollectionCounts:

    def test_complete_when_all_found(self, db_connection):
        from core.database import get_pull_list_collection_counts
        pub_id = create_publisher()
        sid = create_series(series_id=100, publisher_id=pub_id)
        i1 = create_issue(series_id=sid, number="1", store_date="2020-01-10")
        i2 = create_issue(series_id=sid, number="2", store_date="2020-02-10")
        _mark_found(sid, i1, "1", True)
        _mark_found(sid, i2, "2", True)

        counts = get_pull_list_collection_counts("2026-01-01")[sid]
        assert counts["total"] == 2
        assert counts["found"] == 2
        assert counts["missing_past"] == 0
        assert counts["missing_future"] == 0

    def test_missing_past(self, db_connection):
        from core.database import get_pull_list_collection_counts
        pub_id = create_publisher()
        sid = create_series(series_id=100, publisher_id=pub_id)
        i1 = create_issue(series_id=sid, number="1", store_date="2020-01-10")
        _mark_found(sid, i1, "1", False)

        counts = get_pull_list_collection_counts("2026-01-01")[sid]
        assert counts["missing_past"] == 1
        assert counts["missing_future"] == 0

    def test_missing_future_only(self, db_connection):
        from core.database import get_pull_list_collection_counts
        pub_id = create_publisher()
        sid = create_series(series_id=100, publisher_id=pub_id)
        i1 = create_issue(series_id=sid, number="1", store_date="2099-01-10")
        _mark_found(sid, i1, "1", False)

        counts = get_pull_list_collection_counts("2026-01-01")[sid]
        assert counts["missing_past"] == 0
        assert counts["missing_future"] == 1

    def test_manual_status_counts_as_present(self, db_connection):
        from core.database import get_pull_list_collection_counts, set_manual_status
        pub_id = create_publisher()
        sid = create_series(series_id=100, publisher_id=pub_id)
        i1 = create_issue(series_id=sid, number="1", store_date="2020-01-10")
        _mark_found(sid, i1, "1", False)
        # Owned/skipped issues are not "missing" (mirrors series.html row logic).
        set_manual_status(sid, "1", "owned")

        counts = get_pull_list_collection_counts("2026-01-01")[sid]
        assert counts["missing_past"] == 0
        assert counts["missing_future"] == 0

    def test_unscanned_series_reports_zero_scanned(self, db_connection):
        from core.database import get_pull_list_collection_counts
        pub_id = create_publisher()
        sid = create_series(series_id=100, publisher_id=pub_id)
        create_issue(series_id=sid, number="1", store_date="2020-01-10")
        # No collection_status rows written -> not yet matched.

        counts = get_pull_list_collection_counts("2026-01-01")[sid]
        assert counts["total"] == 1
        assert counts["scanned"] == 0
        assert counts["found"] == 0


class TestWantedIssuesRespectsMonitored:

    def test_unmonitored_series_excluded(self, db_connection):
        from core.database import (
            set_series_monitored, get_wanted_issues,
        )
        pub_id = create_publisher()
        on_id = create_series(series_id=100, name="Monitored", publisher_id=pub_id)
        off_id = create_series(series_id=200, name="Silenced", publisher_id=pub_id)
        create_issue(series_id=on_id, number="1", store_date="2020-01-10")
        create_issue(series_id=off_id, number="1", store_date="2020-01-10")

        set_series_monitored(off_id, False)

        wanted_ids = {w["series_id"] for w in get_wanted_issues()}
        assert on_id in wanted_ids
        assert off_id not in wanted_ids
