"""core/kavita_sync.py: what a run writes, and above all what it leaves alone."""
from unittest.mock import patch

import pytest
import requests

pytest.importorskip("cryptography")

from core import kavita_store, kavita_sync
from models.kavita import KavitaAuthError

SERVER = "http://kavita.local:5000"


def chapter(chapter_id, name, pages=20, pages_read=20, when="2025-01-02T03:04:05.1234567", **extra):
    return {
        "id": chapter_id,
        "pages": pages,
        "pagesRead": pages_read,
        "lastReadingProgressUtc": when,
        "files": [{"filePath": f"/kavita/Series/{name}", "format": 1, "pages": pages}],
        **extra,
    }


class FakeKavita:
    """One Kavita user's view: {series_id: (series row, [chapters])}."""

    def __init__(self, series=None, error=None):
        self.series = series or {}
        self.error = error
        self.volume_calls = []

    def iter_series_with_progress(self, page_size=200):
        if self.error:
            raise self.error
        for series_id, (row, _chapters) in self.series.items():
            yield {"id": series_id, "pagesRead": 1, **row}

    def get_volumes(self, series_id):
        self.volume_calls.append(series_id)
        return [{"id": 1, "chapters": self.series[series_id][1]}]


def clu_path(name):
    return f"/data/Series/{name}"


@pytest.fixture
def users(db_connection, monkeypatch):
    from core.database import create_user

    monkeypatch.delenv("CLU_USERNAME", raising=False)
    monkeypatch.delenv("CLU_PASSWORD", raising=False)
    ids = {
        "owner": create_user("owner", password="ownerpass", role="owner"),
        "alice": create_user("alice", password="alicepass", role="reader"),
        "bob": create_user("bob", password="bobpass", role="reader"),
    }
    kavita_store.set_server_url(SERVER)
    return ids


@pytest.fixture
def servers():
    """auth key -> FakeKavita, and the factory the sync builds clients with."""
    return {}


@pytest.fixture
def run(users, servers):
    """Run a sync with every Kavita path resolving to a CLU path the user may read."""
    def resolve(remote_path, mappings, file_name=None, lookup=None):
        return clu_path(file_name), "mapping"

    def factory(server_url, auth_key):
        assert server_url == SERVER
        return servers[auth_key]

    def _run(user=None, full=False):
        with patch("core.kavita_sync.resolve_external_path", side_effect=resolve), \
                patch("core.auth.user_can_access_path", return_value=True):
            if user is None:
                return kavita_sync.sync_all(full=full, client_factory=factory)
            return kavita_sync.sync_user(users[user], full=full, client_factory=factory)

    return _run


def link(users, servers, name, fake):
    key = f"key-{name}"
    servers[key] = fake
    assert kavita_store.save_link(users[name], key, kavita_username=f"kavita-{name}")
    return fake


def read_row(db_connection, user_id, path):
    return db_connection.execute(
        "SELECT read_at, page_count, time_spent FROM issues_read "
        "WHERE user_id = ? AND issue_path = ?",
        (user_id, path),
    ).fetchone()


class TestCompletedReads:
    def test_a_finished_chapter_becomes_a_read_dated_by_kavita(self, run, users, servers, db_connection):
        link(users, servers, "alice", FakeKavita({1: ({}, [chapter(10, "A 001.cbz")])}))

        result = run("alice")

        assert result["users"][0]["status"] == "ok"
        assert result["users"][0]["read"] == 1
        row = read_row(db_connection, users["alice"], clu_path("A 001.cbz"))
        assert row["read_at"] == "2025-01-02 03:04:05"
        assert row["page_count"] == 20

    def test_a_read_clu_already_has_is_left_alone(self, run, users, servers, db_connection):
        from core.database import mark_issue_read

        mark_issue_read(
            clu_path("A 001.cbz"), read_at="2020-05-05 05:05:05", page_count=22,
            time_spent=900, user_id=users["alice"],
        )
        link(users, servers, "alice", FakeKavita({1: ({}, [chapter(10, "A 001.cbz")])}))

        result = run("alice")

        assert result["users"][0]["read"] == 0
        row = read_row(db_connection, users["alice"], clu_path("A 001.cbz"))
        assert (row["read_at"], row["page_count"], row["time_spent"]) == (
            "2020-05-05 05:05:05", 22, 900)

    def test_a_read_unmarked_in_clu_does_not_come_back(self, run, users, servers, db_connection):
        from core.database import unmark_issue_read

        link(users, servers, "alice", FakeKavita({1: ({}, [chapter(10, "A 001.cbz")])}))
        run("alice")
        unmark_issue_read(clu_path("A 001.cbz"), user_id=users["alice"])

        result = run("alice", full=True)

        assert result["users"][0]["read"] == 0
        assert read_row(db_connection, users["alice"], clu_path("A 001.cbz")) is None

    def test_a_second_run_imports_nothing_twice(self, run, users, servers):
        link(users, servers, "alice", FakeKavita({1: ({}, [chapter(10, "A 001.cbz")])}))
        assert run("alice")["users"][0]["read"] == 1
        second = run("alice", full=True)["users"][0]
        assert second["read"] == 0
        # ...and says why: the issue was found, and CLU already had it.
        assert (second["chapters"], second["matched"], second["already"]) == (1, 1, 1)
        detail = kavita_store.get_link_status(users["alice"])["detail"]
        assert (detail["chapters"], detail["already"], detail["no_match"]) == (1, 1, 0)

    def test_an_unreadable_import_log_stops_the_run(self, run, users, servers, db_connection):
        """None is "could not ask", not "nothing imported": re-importing would
        bring back reads the user un-marked."""
        link(users, servers, "alice", FakeKavita({1: ({}, [chapter(10, "A 001.cbz")])}))
        with patch("core.kavita_store.synced_chapter_ids", return_value=None):
            result = run("alice")
        assert result["users"][0]["status"] == "error"
        assert read_row(db_connection, users["alice"], clu_path("A 001.cbz")) is None


class TestCredits:
    """An imported read carries the file's publisher and credits, as a read
    made in CLU's own reader does; without them the timeline says
    "Unknown Publisher"."""

    def _index(self, db_connection, name, **ci):
        from core.database import add_file_index_entry

        add_file_index_entry(name, clu_path(name), "file", size=1, parent="/data/Series")
        if ci:
            sets = ", ".join(f"{k} = ?" for k in ci)
            db_connection.execute(
                f"UPDATE file_index SET {sets} WHERE path = ?", (*ci.values(), clu_path(name)))
            db_connection.commit()

    def _credits(self, db_connection, user_id, name):
        return tuple(db_connection.execute(
            "SELECT writer, penciller, characters, publisher FROM issues_read "
            "WHERE user_id = ? AND issue_path = ?", (user_id, clu_path(name))).fetchone())

    def test_a_new_read_takes_its_credits_from_the_file_index(self, run, users, servers, db_connection):
        self._index(db_connection, "A 001.cbz", ci_publisher="DC Comics [10]",
                    ci_writer="Mark Waid", ci_penciller="Todd Nauck", ci_characters="Barry Allen")
        link(users, servers, "alice", FakeKavita({1: ({}, [chapter(10, "A 001.cbz")])}))

        run("alice")

        assert self._credits(db_connection, users["alice"], "A 001.cbz") == (
            "Mark Waid", "Todd Nauck", "Barry Allen", "DC Comics")

    def test_an_untagged_file_is_still_imported(self, run, users, servers, db_connection):
        link(users, servers, "alice", FakeKavita({1: ({}, [chapter(10, "A 001.cbz")])}))
        assert run("alice")["users"][0]["read"] == 1
        assert self._credits(db_connection, users["alice"], "A 001.cbz") == ("", "", "", "")

    def test_an_earlier_import_without_credits_is_filled_on_the_next_run(
            self, run, users, servers, db_connection):
        link(users, servers, "alice", FakeKavita({1: ({}, [chapter(10, "A 001.cbz")])}))
        run("alice")  # the file is not indexed yet: imported with no credits
        self._index(db_connection, "A 001.cbz", ci_publisher="DC Comics")

        run("alice", full=True)

        assert self._credits(db_connection, users["alice"], "A 001.cbz")[3] == "DC Comics"

    def test_credits_clu_recorded_are_never_replaced(self, run, users, servers, db_connection):
        from core.database import mark_issue_read

        mark_issue_read(clu_path("A 001.cbz"), publisher="Vertigo", user_id=users["alice"])
        self._index(db_connection, "A 001.cbz", ci_publisher="DC Comics", ci_writer="Someone")
        link(users, servers, "alice", FakeKavita({1: ({}, [chapter(10, "A 001.cbz")])}))

        run("alice")
        run("alice", full=True)

        assert self._credits(db_connection, users["alice"], "A 001.cbz") == ("", "", "", "Vertigo")

    def test_filling_touches_only_reads_this_sync_imported(self, run, users, servers, db_connection):
        from core.database import mark_issue_read

        mark_issue_read(clu_path("Mine 001.cbz"), user_id=users["alice"])
        self._index(db_connection, "Mine 001.cbz", ci_publisher="Image")
        link(users, servers, "alice", FakeKavita({1: ({}, [chapter(10, "A 001.cbz")])}))

        run("alice")

        assert self._credits(db_connection, users["alice"], "Mine 001.cbz")[3] == ""


class TestProgress:
    def _position(self, users, name="A 002.cbz", user="alice"):
        from core.database import get_reading_position

        return get_reading_position(clu_path(name), user_id=users[user])

    def test_progress_is_saved_one_based(self, run, users, servers):
        link(users, servers, "alice", FakeKavita(
            {1: ({}, [chapter(11, "A 002.cbz", pages=20, pages_read=4)])}))

        result = run("alice")

        assert result["users"][0]["progress"] == 1
        position = self._position(users)
        assert position["page_number"] == 5
        assert position["total_pages"] == 20

    def test_a_position_further_along_in_clu_is_not_moved_back(self, run, users, servers):
        from core.database import save_reading_position

        save_reading_position(clu_path("A 002.cbz"), 15, total_pages=20,
                              time_spent=600, user_id=users["alice"])
        # Kavita is behind, and its progress is older than the row just saved.
        link(users, servers, "alice", FakeKavita(
            {1: ({}, [chapter(11, "A 002.cbz", pages_read=4, when="2001-01-01T00:00:00")])}))

        result = run("alice")

        assert result["users"][0]["progress"] == 0
        position = self._position(users)
        assert (position["page_number"], position["time_spent"]) == (15, 600)

    def test_moving_forward_keeps_time_spent(self, run, users, servers):
        from core.database import save_reading_position

        save_reading_position(clu_path("A 002.cbz"), 3, total_pages=20,
                              time_spent=420, user_id=users["alice"])
        link(users, servers, "alice", FakeKavita(
            {1: ({}, [chapter(11, "A 002.cbz", pages_read=9, when="2001-01-01T00:00:00")])}))

        run("alice")

        position = self._position(users)
        assert (position["page_number"], position["time_spent"]) == (10, 420)

    def test_a_newer_kavita_position_behind_clu_is_a_reread(self, run, users, servers):
        from core.database import save_reading_position

        save_reading_position(clu_path("A 002.cbz"), 15, total_pages=20,
                              time_spent=600, user_id=users["alice"])
        link(users, servers, "alice", FakeKavita(
            {1: ({}, [chapter(11, "A 002.cbz", pages_read=1, when="2999-01-01T00:00:00")])}))

        run("alice")

        position = self._position(users)
        assert (position["page_number"], position["time_spent"]) == (2, 600)

    def test_progress_on_an_issue_read_in_clu_is_ignored(self, run, users, servers):
        from core.database import mark_issue_read

        mark_issue_read(clu_path("A 002.cbz"), user_id=users["alice"])
        link(users, servers, "alice", FakeKavita(
            {1: ({}, [chapter(11, "A 002.cbz", pages_read=4)])}))

        assert run("alice")["users"][0]["progress"] == 0
        assert self._position(users) is None

    def test_an_untouched_chapter_writes_nothing(self, run, users, servers, db_connection):
        link(users, servers, "alice", FakeKavita(
            {1: ({}, [chapter(11, "A 002.cbz", pages_read=0)])}))
        result = run("alice")
        assert (result["users"][0]["read"], result["users"][0]["progress"]) == (0, 0)
        assert self._position(users) is None


class TestMultiUser:
    def test_each_user_gets_only_their_own_progress(self, run, users, servers, db_connection):
        link(users, servers, "alice", FakeKavita({1: ({}, [chapter(10, "A 001.cbz")])}))
        link(users, servers, "bob", FakeKavita({1: ({}, [chapter(20, "B 001.cbz")])}))

        result = run()

        assert sorted(u["user_id"] for u in result["users"]) == sorted(
            [users["alice"], users["bob"]])
        assert read_row(db_connection, users["alice"], clu_path("A 001.cbz"))
        assert read_row(db_connection, users["alice"], clu_path("B 001.cbz")) is None
        assert read_row(db_connection, users["bob"], clu_path("B 001.cbz"))
        assert read_row(db_connection, users["bob"], clu_path("A 001.cbz")) is None
        assert read_row(db_connection, users["owner"], clu_path("A 001.cbz")) is None

    def test_the_same_chapter_is_logged_per_user(self, run, users, servers, db_connection):
        """Chapter ids are the server's, shared by its users: one user's import
        must not mark the chapter as done for another."""
        link(users, servers, "alice", FakeKavita({1: ({}, [chapter(10, "A 001.cbz")])}))
        link(users, servers, "bob", FakeKavita({1: ({}, [chapter(10, "A 001.cbz")])}))

        run()

        assert read_row(db_connection, users["alice"], clu_path("A 001.cbz"))
        assert read_row(db_connection, users["bob"], clu_path("A 001.cbz"))

    def test_one_users_failure_does_not_stop_the_others(self, run, users, servers, db_connection):
        link(users, servers, "alice", FakeKavita(error=requests.ConnectionError("down")))
        link(users, servers, "bob", FakeKavita({1: ({}, [chapter(20, "B 001.cbz")])}))

        result = run()

        statuses = {u["user_id"]: u["status"] for u in result["users"]}
        assert statuses == {users["alice"]: "error", users["bob"]: "ok"}
        assert read_row(db_connection, users["bob"], clu_path("B 001.cbz"))

    def test_a_path_outside_the_users_folders_is_not_written(self, users, servers, db_connection):
        link(users, servers, "alice", FakeKavita({1: ({}, [chapter(10, "A 001.cbz")])}))

        with patch("core.kavita_sync.resolve_external_path",
                   return_value=(clu_path("A 001.cbz"), "mapping")):
            # The real access check: alice is a reader with no library grants.
            result = kavita_sync.sync_user(
                users["alice"], client_factory=lambda url, key: servers[key])

        assert result["users"][0]["no_access"] == 1
        assert read_row(db_connection, users["alice"], clu_path("A 001.cbz")) is None


class TestFailures:
    def test_a_rejected_key_is_latched_and_then_skipped(self, run, users, servers):
        fake = link(users, servers, "alice", FakeKavita(error=KavitaAuthError("HTTP 401")))

        assert run("alice")["users"][0]["status"] == "key_rejected"
        status = kavita_store.get_link_status(users["alice"])
        assert status["connection_valid"] is False
        assert status["last_sync"] is None

        # The key now works again, but nobody has re-saved it: still skipped.
        fake.error = None
        assert run("alice")["users"][0]["status"] == "key_rejected"
        assert run()["users"] == []

    def test_saving_a_key_again_clears_the_latch(self, run, users, servers):
        link(users, servers, "alice", FakeKavita(error=KavitaAuthError("HTTP 401")))
        run("alice")
        link(users, servers, "alice", FakeKavita({1: ({}, [chapter(10, "A 001.cbz")])}))
        assert run("alice")["users"][0]["status"] == "ok"

    def test_a_failed_run_does_not_stamp_last_sync(self, run, users, servers):
        link(users, servers, "alice", FakeKavita(error=requests.Timeout("slow")))

        assert run("alice")["users"][0]["status"] == "error"

        status = kavita_store.get_link_status(users["alice"])
        assert status["last_sync"] is None
        assert status["connection_valid"] is True
        assert "slow" in status["last_error"]

    def test_a_good_run_stamps_last_sync_and_clears_the_error(self, run, users, servers):
        fake = link(users, servers, "alice", FakeKavita(error=requests.Timeout("slow")))
        run("alice")
        fake.error = None
        run("alice")
        status = kavita_store.get_link_status(users["alice"])
        assert status["last_sync"]
        assert status["last_error"] == ""

    def test_an_unlinked_user_is_reported_not_synced(self, run, users):
        assert run("alice")["users"][0]["status"] == "not_linked"

    def test_no_server_url_means_nothing_runs(self, run, users, servers):
        link(users, servers, "alice", FakeKavita({1: ({}, [chapter(10, "A 001.cbz")])}))
        kavita_store.set_server_url("")
        assert run()["status"] == "not_configured"

    def test_a_second_run_stands_down(self, run, users):
        assert kavita_sync._run_lock.acquire(blocking=False)
        try:
            assert kavita_sync.is_running() is True
            assert run() is None
        finally:
            kavita_sync._run_lock.release()
        assert kavita_sync.is_running() is False


class TestIncremental:
    def _fake(self):
        return FakeKavita({
            1: ({"latestReadDate": "2001-01-01T00:00:00"}, [chapter(10, "Old 001.cbz")]),
            2: ({"latestReadDate": "2999-01-01T00:00:00"}, [chapter(11, "New 001.cbz")]),
            3: ({"latestReadDate": None}, [chapter(12, "Undated 001.cbz")]),
        })

    def test_the_first_run_checks_every_series(self, run, users, servers):
        fake = link(users, servers, "alice", self._fake())
        run("alice")
        assert fake.volume_calls == [1, 2, 3]

    def test_a_later_run_skips_series_not_read_since(self, run, users, servers):
        fake = link(users, servers, "alice", self._fake())
        run("alice")
        fake.volume_calls.clear()

        result = run("alice")

        # No date is not evidence of "unchanged", so series 3 is still checked.
        assert fake.volume_calls == [2, 3]
        assert result["users"][0]["series_skipped"] == 1

    def test_a_full_run_checks_everything_again(self, run, users, servers):
        fake = link(users, servers, "alice", self._fake())
        run("alice")
        fake.volume_calls.clear()
        run("alice", full=True)
        assert fake.volume_calls == [1, 2, 3]


class TestUnmatched:
    def test_an_unmatched_file_is_counted_and_skipped(self, users, servers, db_connection):
        link(users, servers, "alice", FakeKavita({1: ({}, [chapter(10, "Nowhere 001.cbz")])}))
        # The real resolver: no mapping, and nothing in the file index.
        result = kavita_sync.sync_user(
            users["alice"], client_factory=lambda url, key: servers[key])
        assert result["users"][0]["no_match"] == 1
        assert result["users"][0]["read"] == 0
        # Stored, so the page can tell "nothing matched" from "up to date".
        detail = kavita_store.get_link_status(users["alice"])["detail"]
        assert (detail["chapters"], detail["matched"], detail["no_match"]) == (1, 0, 1)

    def test_a_chapter_without_one_comic_file_is_counted(self, users, servers):
        no_files = chapter(10, "A 001.cbz")
        no_files["files"] = []
        link(users, servers, "alice", FakeKavita({1: ({}, [no_files])}))
        result = kavita_sync.sync_user(
            users["alice"], client_factory=lambda url, key: servers[key])
        assert result["users"][0]["unusable"] == 1
        assert kavita_store.get_link_status(users["alice"])["detail"]["unusable"] == 1
