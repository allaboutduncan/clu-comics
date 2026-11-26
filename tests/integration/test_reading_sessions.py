"""core/reading_sessions.py: sittings derived from the saves the reader makes."""
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest

from core.database import get_db_connection, move_path_references
from core.reading_sessions import (
    IDLE_CAP_SECONDS,
    SESSION_GAP_SECONDS,
    clean_session_id,
    load_sessions,
    note_reading_activity,
    record_reading_activity,
    session_pages,
    session_span,
)

COMIC = "/data/DC/Batman/Batman 001.cbz"
OTHER = "/data/DC/Batman/Batman 002.cbz"
SID = "3f2b6c1e-8a44-4d0b-9a57-0c1f5f1f0a11"
T0 = datetime(2024, 6, 10, 20, 0, 0, tzinfo=timezone.utc)


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def ping(page, seconds, sid=SID, path=COMIC, **kwargs):
    return record_reading_activity(path, page, 22, session_id=sid, now=at(seconds), **kwargs)


def rows(user_id=1):
    return load_sessions(user_id=user_id)


class TestOneSitting:
    def test_page_turns_extend_one_session(self, db_connection):
        first = ping(2, 0)
        assert ping(5, 60) == first
        assert ping(9, 150) == first

        (session,) = rows()
        assert session["comic_path"] == COMIC
        assert session["active_seconds"] == 150
        assert (session["start_page"], session["end_page"], session["max_page"]) == (2, 9, 9)
        assert session["total_pages"] == 22
        assert session["finished"] is False
        assert session["source"] == "web"
        assert session_pages(session) == 8
        assert session_span(session) == timedelta(seconds=150)

    def test_paging_back_keeps_the_furthest_page(self, db_connection):
        ping(2, 0)
        ping(10, 60)
        ping(4, 90)
        (session,) = rows()
        assert (session["end_page"], session["max_page"]) == (4, 10)

    def test_timestamps_are_the_servers(self, db_connection):
        ping(2, 0)
        ping(3, 45)
        (session,) = rows()
        assert session["started_at"] == T0
        assert session["ended_at"] == at(45)


class TestIdleAndGaps:
    def test_an_idle_stretch_counts_only_up_to_the_cap(self, db_connection):
        ping(2, 0)
        ping(3, 20 * 60)          # 20 minutes of silence, then a page turn
        (session,) = rows()
        assert session["active_seconds"] == IDLE_CAP_SECONDS
        assert session_span(session) == timedelta(minutes=20)

    def test_a_long_silence_starts_a_new_sitting_for_the_same_reader(self, db_connection):
        first = ping(2, 0)
        second = ping(3, SESSION_GAP_SECONDS + 1)
        assert second != first
        assert [s["start_page"] for s in rows()] == [2, 3]

    def test_exactly_the_gap_is_still_one_sitting(self, db_connection):
        assert ping(2, 0) == ping(3, SESSION_GAP_SECONDS)

    def test_a_clock_step_backwards_adds_nothing(self, db_connection):
        ping(2, 100)
        ping(3, 40)
        (session,) = rows()
        assert session["active_seconds"] == 0


class TestWhoAndWhat:
    def test_another_reader_open_is_another_session(self, db_connection):
        ping(2, 0, sid="aaaaaaaa-1111")
        ping(3, 30, sid="bbbbbbbb-2222")
        assert len(rows()) == 2

    def test_another_comic_is_another_session(self, db_connection):
        ping(2, 0)
        ping(2, 30, path=OTHER)
        assert sorted(s["comic_path"] for s in rows()) == [COMIC, OTHER]

    def test_sessions_are_per_user(self, db_connection):
        ping(2, 0, user_id=1)
        ping(4, 30, user_id=2)
        assert [s["start_page"] for s in rows(1)] == [2]
        assert [s["start_page"] for s in rows(2)] == [4]

    def test_without_a_session_id_saves_on_one_comic_group_by_time(self, db_connection):
        first = ping(2, 0, sid=None, source="api")
        assert ping(6, 120, sid=None, source="api") == first
        assert ping(7, 120 + SESSION_GAP_SECONDS + 5, sid=None, source="api") != first
        assert {s["source"] for s in rows()} == {"api"}

    def test_an_anonymous_save_does_not_extend_a_readers_session(self, db_connection):
        ping(2, 0)
        ping(3, 30, sid=None)
        assert len(rows()) == 2

    def test_nothing_without_a_path(self, db_connection):
        assert record_reading_activity("", 3, 22, session_id=SID) is None
        assert rows() == []


class TestFinishing:
    def test_finishing_closes_the_open_session(self, db_connection):
        first = ping(2, 0)
        assert ping(22, 300, finished=True) == first
        (session,) = rows()
        assert session["finished"] is True
        assert session["max_page"] == 22

    def test_a_reader_finish_with_no_earlier_save_is_still_a_session(self, db_connection):
        # A two-page preview comic: the only request is the finish.
        assert ping(2, 0, finished=True) is not None
        assert rows()[0]["finished"] is True

    def test_a_manual_mark_read_is_not_a_sitting(self, db_connection):
        # The grid's "Mark as read" and an API history import carry no session.
        assert ping(22, 0, sid=None, finished=True) is None
        assert rows() == []

    def test_an_api_finish_closes_what_that_client_had_open(self, db_connection):
        first = ping(5, 0, sid=None, source="api")
        assert ping(22, 200, sid=None, finished=True, source="api") == first
        assert rows()[0]["finished"] is True

    def test_finished_is_not_undone_by_a_later_save(self, db_connection):
        ping(22, 0, finished=True)
        ping(21, 30)
        assert rows()[0]["finished"] is True


class TestLoadSessions:
    def test_period_is_half_open_on_the_start_time(self, db_connection):
        ping(2, 0, sid="aaaaaaaa-0001")
        ping(2, 3 * 3600, sid="aaaaaaaa-0002")
        start = T0.replace(tzinfo=None)
        assert len(load_sessions(start_utc=start, end_utc=start + timedelta(hours=3))) == 1
        assert len(load_sessions(start_utc=start + timedelta(hours=3))) == 1
        assert len(load_sessions(end_utc=start)) == 0


class TestSessionId:
    @pytest.mark.parametrize("value", [SID, "0123456789abcdef0123456789abcdef", "abc_DEF-12"])
    def test_accepts_reader_ids(self, value):
        assert clean_session_id(value) == value

    @pytest.mark.parametrize("value", [
        None, "", 12345, "short", "x" * 65, "has space in it", "semi;colon-injection",
    ])
    def test_ignores_anything_else(self, value):
        assert clean_session_id(value) is None

    def test_a_bad_id_is_recorded_as_anonymous(self, db_connection):
        ping(2, 0, sid="not a valid id!")
        conn = get_db_connection()
        stored = conn.execute("SELECT client_session_id FROM reading_sessions").fetchone()[0]
        conn.close()
        assert stored is None


class TestNeverBreaksTheSave:
    def test_a_database_error_is_swallowed(self, db_connection, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr("core.reading_sessions.record_reading_activity", boom)
        assert note_reading_activity(COMIC, 3, 22, session_id=SID) is None


class TestPathFollowsRename:
    NEW = "/data/DC/Batman/Batman 001 (2016).cbz"

    def test_file_rename(self, db_connection):
        ping(2, 0)
        ping(2, 0, path=OTHER, sid="aaaaaaaa-0003")
        assert move_path_references(COMIC, self.NEW) is True
        assert sorted(s["comic_path"] for s in rows()) == sorted([self.NEW, OTHER])

    def test_folder_rename_keeps_every_sitting(self, db_connection):
        # A comic has many rows here, so the move must not be OR REPLACE.
        ping(2, 0, sid="aaaaaaaa-0004")
        ping(2, 5 * 3600, sid="aaaaaaaa-0005")
        assert move_path_references("/data/DC/Batman", "/data/DC/Batman (2016)", is_dir=True)
        assert [s["comic_path"] for s in rows()] == ["/data/DC/Batman (2016)/Batman 001.cbz"] * 2


class TestReaderStaysOutOfTheTimeAccounting:
    """The reader sends an id and nothing else. Its time totals are absolute
    (accumulatedTime is the server's total at open, readingStartTime is set
    once), and a session id must never become part of that arithmetic."""

    SOURCE = (
        pathlib.Path(__file__).resolve().parents[2] / "static" / "js" / "reader.js"
    ).read_text(encoding="utf-8")

    def test_session_id_is_sent_with_saves_and_the_finish(self):
        assert self.SOURCE.count("session_id: readerSessionId") == 2

    def test_a_new_id_per_open(self):
        opener = self.SOURCE[self.SOURCE.index("function openComicReader("):]
        opener = opener[:opener.index("\n}\n")]
        assert "readerSessionId = _newReaderSessionId();" in opener

    def test_time_variables_are_assigned_where_they_always_were(self):
        # One reset at open each, plus accumulatedTime taking the stored total.
        assert len(re.findall(r"\breadingStartTime\s*=[^=]", self.SOURCE)) == 2
        assert len(re.findall(r"\baccumulatedTime\s*=[^=]", self.SOURCE)) == 3

    def test_total_time_does_not_read_the_session(self):
        body = self.SOURCE[self.SOURCE.index("function _readerTotalTime()"):]
        body = body[:body.index("\n}\n")]
        assert "readerSessionId" not in body
