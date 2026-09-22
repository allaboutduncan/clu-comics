"""The mapped-series queue stamp: one issue, one download per cooldown window.

Reading-list entries have carried a ``last_queued_at`` stamp since they joined
the Wanted list. Mapped-series issues had nothing -- the only de-duplication
protecting them was ``queued_download_urls``, a local of one sweep call -- so a
run that queued a download but never produced a filed comic re-queued the same
issue on every subsequent run. One reported library accumulated 22 copies of a
single issue that way.

The stamp lives in its own table rather than on ``wanted_issues`` because
``refresh_wanted_cache_background`` opens by wiping that table, which would
throw the stamp away exactly when it matters.
"""
from datetime import datetime, timedelta

import pytest


def _stamps(series_id):
    from core.database import get_wanted_queue_stamps

    return get_wanted_queue_stamps(series_id)


class TestStamping:

    def test_a_queued_issue_is_remembered(self, db_connection):
        from core.database import mark_wanted_issue_queued

        assert mark_wanted_issue_queued(42, "017")
        assert "017" in _stamps(42)

    def test_stamping_twice_updates_rather_than_duplicating(self, db_connection):
        from core.database import mark_wanted_issue_queued

        mark_wanted_issue_queued(42, "017")
        mark_wanted_issue_queued(42, "017")

        rows = db_connection.execute(
            "SELECT COUNT(*) FROM wanted_queue_log WHERE series_id = 42"
        ).fetchone()[0]
        assert rows == 1

    def test_series_are_kept_apart(self, db_connection):
        from core.database import mark_wanted_issue_queued

        mark_wanted_issue_queued(42, "017")
        mark_wanted_issue_queued(43, "017")
        assert _stamps(42) == {k: v for k, v in _stamps(42).items()}
        assert "017" in _stamps(43)
        assert len(_stamps(42)) == 1

    def test_a_blank_issue_number_is_not_stamped(self, db_connection):
        from core.database import mark_wanted_issue_queued

        assert mark_wanted_issue_queued(42, "") is False
        assert mark_wanted_issue_queued(None, "017") is False
        assert _stamps(42) == {}

    def test_filing_the_issue_clears_the_stamp(self, db_connection):
        """process_incoming_wanted_issues calls this when the comic arrives.

        Housekeeping rather than correctness -- nothing consults a stamp for an
        issue that is no longer wanted -- but without it the table only grows.
        """
        from core.database import mark_wanted_issue_queued, clear_wanted_queue_stamp

        mark_wanted_issue_queued(42, "017")
        assert clear_wanted_queue_stamp(42, "017")
        assert _stamps(42) == {}


class TestCooldownSemantics:
    """The sweep reuses core.wanted_reading_lists._off_cooldown, so one
    cooldown serves both work-item sources rather than two knobs meaning the
    same thing."""

    def test_a_fresh_stamp_holds_the_issue_back(self, db_connection):
        from core.database import mark_wanted_issue_queued
        from core.wanted_reading_lists import QUEUE_COOLDOWN_DAYS, _off_cooldown

        mark_wanted_issue_queued(42, "017")
        stamp = _stamps(42)["017"]
        assert not _off_cooldown(
            {"last_queued_at": stamp}, datetime.now(), QUEUE_COOLDOWN_DAYS
        )

    def test_an_expired_stamp_releases_it(self, db_connection):
        from core.database import mark_wanted_issue_queued
        from core.wanted_reading_lists import QUEUE_COOLDOWN_DAYS, _off_cooldown

        mark_wanted_issue_queued(42, "017")
        stamp = _stamps(42)["017"]
        later = datetime.now() + timedelta(days=QUEUE_COOLDOWN_DAYS + 1)
        assert _off_cooldown({"last_queued_at": stamp}, later, QUEUE_COOLDOWN_DAYS)

    def test_a_missing_stamp_never_holds_an_issue_back(self, db_connection):
        """An issue nobody has queued must be searchable, and so must one whose
        stamp we could not read -- pinning an issue off the list forever is a
        far worse failure than one extra search."""
        from core.wanted_reading_lists import QUEUE_COOLDOWN_DAYS, _off_cooldown

        now = datetime.now()
        assert _off_cooldown({"last_queued_at": None}, now, QUEUE_COOLDOWN_DAYS)
        assert _off_cooldown({"last_queued_at": "not a date"}, now, QUEUE_COOLDOWN_DAYS)


class TestReadFailuresFailOpen:

    def test_an_unreadable_table_returns_no_stamps(self, db_connection, monkeypatch):
        import core.database as db

        monkeypatch.setattr(db, "get_db_connection", lambda *a, **k: None)
        assert db.get_wanted_queue_stamps(42) == {}
