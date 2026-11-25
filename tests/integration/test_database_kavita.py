"""core/kavita_store.py: per-user links, library mappings and the import log."""
import pytest

pytest.importorskip("cryptography")

from core import kavita_store


@pytest.fixture
def users(db_connection):
    from core.database import create_user

    return {
        "owner": create_user("owner", password="ownerpass", role="owner"),
        "alice": create_user("alice", password="alicepass", role="reader"),
        "bob": create_user("bob", password="bobpass", role="reader"),
    }


class TestServerUrl:
    def test_unset_is_empty(self, db_connection):
        assert kavita_store.get_server_url() == ""

    def test_round_trip_strips_the_trailing_slash(self, db_connection):
        assert kavita_store.set_server_url(" http://kavita.local:5000/ ")
        assert kavita_store.get_server_url() == "http://kavita.local:5000"


class TestLinks:
    def test_no_link_is_none(self, users):
        assert kavita_store.get_link(users["alice"]) is None
        assert kavita_store.get_link_status(users["alice"]) is None

    def test_the_key_round_trips_and_is_not_stored_in_the_clear(self, users, db_connection):
        assert kavita_store.save_link(users["alice"], "alice-secret-key", "kavita-alice")

        link = kavita_store.get_link(users["alice"])
        assert link["auth_key"] == "alice-secret-key"
        assert link["kavita_username"] == "kavita-alice"
        assert link["connection_valid"] is True

        row = db_connection.execute(
            "SELECT credentials_encrypted FROM kavita_user_links WHERE user_id = ?",
            (users["alice"],),
        ).fetchone()
        assert b"alice-secret-key" not in bytes(row[0])

    def test_the_status_never_carries_the_key(self, users):
        kavita_store.save_link(users["alice"], "alice-secret-key", "kavita-alice")
        assert "alice-secret-key" not in repr(kavita_store.get_link_status(users["alice"]))
        assert "alice-secret-key" not in repr(kavita_store.list_links())

    def test_links_are_per_user(self, users):
        kavita_store.save_link(users["alice"], "alice-key", "kavita-alice")
        kavita_store.save_link(users["bob"], "bob-key", "kavita-bob")

        assert kavita_store.get_link(users["alice"])["auth_key"] == "alice-key"
        assert kavita_store.get_link(users["bob"])["auth_key"] == "bob-key"
        assert kavita_store.get_link(users["owner"]) is None
        assert [(l["username"], l["kavita_username"]) for l in kavita_store.list_links()] == [
            ("alice", "kavita-alice"), ("bob", "kavita-bob")]

    def test_saving_again_replaces_the_key_and_clears_a_rejection(self, users):
        kavita_store.save_link(users["alice"], "old-key")
        kavita_store.set_link_invalid(users["alice"], "HTTP 401")
        status = kavita_store.get_link_status(users["alice"])
        assert (status["connection_valid"], status["last_error"]) == (False, "HTTP 401")

        kavita_store.save_link(users["alice"], "new-key", "kavita-alice")

        link = kavita_store.get_link(users["alice"])
        assert (link["auth_key"], link["connection_valid"], link["last_error"]) == (
            "new-key", True, "")

    def test_a_sync_result_is_recorded(self, users):
        kavita_store.save_link(users["alice"], "alice-key")
        kavita_store.record_sync_error(users["alice"], "boom")
        assert kavita_store.get_link_status(users["alice"])["last_sync"] is None

        kavita_store.record_sync_result(users["alice"], 4, 2, "2025-01-02 03:04:05")

        status = kavita_store.get_link_status(users["alice"])
        assert status["last_sync"] == "2025-01-02 03:04:05"
        assert (status["last_sync_read_count"], status["last_sync_progress_count"]) == (4, 2)
        assert status["last_error"] == ""
        assert status["detail"] == {}

    def test_run_detail_keeps_known_counts_only(self, users):
        kavita_store.save_link(users["alice"], "alice-key")
        kavita_store.record_sync_result(
            users["alice"], 1, 0, "2025-01-02 03:04:05",
            detail={"chapters": 3, "no_match": 2, "read": 1, "status": "ok"},
        )
        assert kavita_store.get_link_status(users["alice"])["detail"] == {
            "chapters": 3, "no_match": 2}

    def test_an_older_database_gains_the_detail_column(self, users, db_connection):
        from core.database import init_db

        db_connection.execute("ALTER TABLE kavita_user_links DROP COLUMN last_sync_detail")
        db_connection.commit()
        init_db()
        kavita_store.save_link(users["alice"], "alice-key")
        assert kavita_store.get_link_status(users["alice"])["detail"] == {}

    def test_unlinking_keeps_the_import_log(self, users):
        kavita_store.save_link(users["alice"], "alice-key")
        kavita_store.log_synced(users["alice"], 10, "/kavita/a.cbz", "/data/a.cbz")

        assert kavita_store.delete_link(users["alice"])

        assert kavita_store.get_link(users["alice"]) is None
        assert kavita_store.synced_chapter_ids(users["alice"]) == {"10"}

    def test_deleting_the_user_removes_their_link_and_log(self, users, db_connection):
        from core.database import delete_user

        kavita_store.save_link(users["alice"], "alice-key")
        kavita_store.log_synced(users["alice"], 10, "/kavita/a.cbz", "/data/a.cbz")
        kavita_store.save_link(users["bob"], "bob-key")

        assert delete_user(users["alice"])

        assert kavita_store.get_link(users["alice"]) is None
        assert kavita_store.synced_chapter_ids(users["alice"]) == set()
        assert kavita_store.get_link(users["bob"]) is not None


class TestLibraryMappings:
    @pytest.fixture
    def library_id(self, db_connection):
        from core.database import add_library

        return add_library("Kavita Comics", "/kavita-comics")

    def _prefixes(self):
        return {m["library_id"]: m["kavita_path_prefix"] for m in kavita_store.get_library_mappings()}

    def test_every_enabled_library_is_listed(self, library_id):
        assert self._prefixes()[library_id] == ""

    def test_round_trip_and_clear(self, library_id):
        assert kavita_store.save_library_mappings(
            [{"library_id": library_id, "kavita_path_prefix": " /manga/comics "}])
        assert self._prefixes()[library_id] == "/manga/comics"

        assert kavita_store.save_library_mappings(
            [{"library_id": library_id, "kavita_path_prefix": ""}])
        assert self._prefixes()[library_id] == ""

    def test_a_bad_row_changes_nothing(self, library_id):
        kavita_store.save_library_mappings(
            [{"library_id": library_id, "kavita_path_prefix": "/keep"}])
        assert kavita_store.save_library_mappings([{"kavita_path_prefix": "/no-id"}]) is False
        assert self._prefixes()[library_id] == "/keep"


class TestImportLog:
    def test_the_log_is_per_user_and_per_type(self, users):
        kavita_store.log_synced(users["alice"], 10, "/kavita/a.cbz", "/data/a.cbz", "read")
        kavita_store.log_synced(users["alice"], 10, "/kavita/a.cbz", "/data/a.cbz", "read")

        assert kavita_store.synced_chapter_ids(users["alice"], "read") == {"10"}
        assert kavita_store.synced_chapter_ids(users["bob"], "read") == set()
        assert kavita_store.synced_chapter_ids(users["alice"], "progress") == set()

    def test_clearing_forgets_everyone(self, users):
        kavita_store.log_synced(users["alice"], 10, "/kavita/a.cbz", "/data/a.cbz")
        kavita_store.log_synced(users["bob"], 11, "/kavita/b.cbz", "/data/b.cbz")
        assert kavita_store.clear_sync_log()
        assert kavita_store.synced_chapter_ids(users["alice"]) == set()
        assert kavita_store.synced_chapter_ids(users["bob"]) == set()


class TestImportRead:
    def test_adds_a_read_once(self, users, db_connection):
        assert kavita_store.import_read(
            "/data/a.cbz", users["alice"], read_at="2025-01-02 03:04:05", page_count=20) is True
        assert kavita_store.import_read("/data/a.cbz", users["alice"]) is False

        rows = db_connection.execute(
            "SELECT user_id, read_at, page_count FROM issues_read WHERE issue_path = ?",
            ("/data/a.cbz",),
        ).fetchall()
        assert [tuple(r) for r in rows] == [(users["alice"], "2025-01-02 03:04:05", 20)]

    def test_no_date_falls_back_to_now(self, users, db_connection):
        assert kavita_store.import_read("/data/a.cbz", users["alice"]) is True
        row = db_connection.execute(
            "SELECT read_at FROM issues_read WHERE issue_path = ?", ("/data/a.cbz",)
        ).fetchone()
        assert row[0]

    def test_another_users_read_does_not_block_it(self, users):
        assert kavita_store.import_read("/data/a.cbz", users["alice"]) is True
        assert kavita_store.import_read("/data/a.cbz", users["bob"]) is True


class TestSchema:
    def test_the_kavita_schedule_row_exists_and_is_disabled(self, db_connection):
        from core.database import get_schedule

        schedule = get_schedule("kavita")
        assert schedule["frequency"] == "disabled"
        assert schedule["time"] == "05:30"

    def test_the_tables_exist(self, db_connection):
        names = {r[0] for r in db_connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"kavita_library_mappings", "kavita_user_links", "kavita_sync_log"} <= names
