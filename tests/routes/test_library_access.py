"""
PR5: library-level access enforcement.

A Reader granted only Library A cannot see or open files in Library B — via
browse, file-serve, thumbnails, search, or the library list. Owners bypass all
library scoping, and implicit-owner mode (single-user) enforces nothing.
"""
import pytest

from core.database import (
    create_user,
    get_user_by_username,
    set_user_folders,
    set_user_libraries,
)
from tests.factories.db_factories import create_library


def _login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


A_FILE = "/data/LibA/Batman_001.cbz"
B_FILE = "/data/LibB/Spider-Man_001.cbz"


class TestLibraryAccessMultiUser:
    @pytest.fixture(autouse=True)
    def _setup(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("owner", password="ownerpass", role="owner")
        create_user("reader", password="readerpass", role="reader")
        self.lib_a = create_library(name="Library A", path="/data/LibA")
        self.lib_b = create_library(name="Library B", path="/data/LibB")
        # Reader is granted only Library A, with a whole-library folder grant
        # (equivalent to the upgrade backfill) so library-level scoping applies.
        reader_id = get_user_by_username("reader")["id"]
        set_user_libraries(reader_id, [self.lib_a])
        set_user_folders(reader_id, ["/data/LibA"])
        yield

    # --- Browse -----------------------------------------------------------
    def test_reader_can_browse_granted_library(self, client):
        _login(client, "reader", "readerpass")
        assert client.get("/api/browse?path=/data/LibA").status_code != 403

    def test_reader_denied_browse_ungranted_library(self, client):
        _login(client, "reader", "readerpass")
        assert client.get("/api/browse?path=/data/LibB").status_code == 403

    def test_owner_can_browse_any_library(self, client):
        _login(client, "owner", "ownerpass")
        assert client.get("/api/browse?path=/data/LibB").status_code != 403

    # --- File-access gate (the helper behind the reader/thumbnail/download
    #     routes; those app.py routes are stubbed in the blueprint test app,
    #     so we assert the shared gate directly). --------------------------
    def test_path_gate_grants_and_denies(self):
        from core.auth import user_can_access_path

        reader = get_user_by_username("reader")
        owner = get_user_by_username("owner")
        assert user_can_access_path(reader, A_FILE) is True    # granted library
        assert user_can_access_path(reader, B_FILE) is False   # ungranted library
        assert user_can_access_path(owner, B_FILE) is True     # owner bypass
        assert user_can_access_path(reader, "/tmp/outside.cbz") is False

    def test_accessible_ids_scoped(self):
        from core.auth import accessible_library_ids

        reader = get_user_by_username("reader")
        owner = get_user_by_username("owner")
        assert accessible_library_ids(reader) == {self.lib_a}
        assert accessible_library_ids(owner) == {self.lib_a, self.lib_b}

    # --- Search (blueprint route — end to end) ---------------------------
    def test_search_filtered_for_reader(self, client, db_connection):
        from tests.factories.db_factories import create_file_index_entry
        create_file_index_entry(name="Batman_001.cbz", path=A_FILE, parent="/data/LibA")
        create_file_index_entry(name="Batman_002.cbz",
                                path="/data/LibB/Batman_002.cbz", parent="/data/LibB")
        _login(client, "reader", "readerpass")
        results = client.get("/search-files?query=Batman").get_json()["results"]
        paths = {r["path"] for r in results}
        assert A_FILE in paths
        assert "/data/LibB/Batman_002.cbz" not in paths

    # --- Listings ---------------------------------------------------------
    def test_library_list_filtered_for_reader(self, client):
        _login(client, "reader", "readerpass")
        libs = client.get("/api/libraries").get_json()["libraries"]
        names = {lib["name"] for lib in libs}
        assert names == {"Library A"}

    def test_library_list_full_for_owner(self, client):
        _login(client, "owner", "ownerpass")
        libs = client.get("/api/libraries").get_json()["libraries"]
        names = {lib["name"] for lib in libs}
        assert names == {"Library A", "Library B"}


class TestLibraryAccessImplicitOwner:
    """With no real accounts, enforcement is a no-op (single-user install)."""

    @pytest.fixture(autouse=True)
    def _libs(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_library(name="Library A", path="/data/LibA")
        create_library(name="Library B", path="/data/LibB")
        yield

    def test_browse_any_library_allowed(self, client):
        assert client.get("/api/browse?path=/data/LibB").status_code != 403

    def test_filter_is_noop_in_implicit_mode(self):
        # No login required → no filtering, even for a non-owner-shaped user.
        from core.auth import filter_paths_for_user

        items = [{"path": A_FILE}, {"path": B_FILE}]
        assert filter_paths_for_user(None, items, key="path") == items

    def test_library_list_unfiltered(self, client):
        libs = client.get("/api/libraries").get_json()["libraries"]
        assert len(libs) == 2
