"""
Per-user folder-level access enforcement (refines library grants).

A Reader granted Library A but only the folder A/Marvel sees Marvel and its
descendants, can traverse the library root to reach it (siblings hidden), and is
denied everything else — via browse, the metadata browser, the file-access gate,
OPDS, and the token API. The File Manager (/list-directories, a live filesystem
view) is deliberately NOT folder-scoped. Owners bypass all scoping; single-user
(implicit-owner) mode enforces nothing.
"""
import os

import pytest

from core.database import (
    create_user,
    get_user_by_username,
    set_user_folders,
    set_user_libraries,
)
from tests.factories.db_factories import create_library, create_file_index_entry


def _login(client, username, password):
    return client.post("/login", data={"username": username, "password": password})


LIB_A = "/data/LibA"
LIB_B = "/data/LibB"
GRANT = "/data/LibA/Marvel"          # granted subtree
SIBLING = "/data/LibA/DC"            # ungranted sibling
GRANTED_FILE = "/data/LibA/Marvel/XMen_001.cbz"
HIDDEN_FILE = "/data/LibA/DC/Batman_001.cbz"


class TestFolderAccessMultiUser:
    @pytest.fixture(autouse=True)
    def _setup(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("owner", password="ownerpass", role="owner")
        create_user("reader", password="readerpass", role="reader")
        self.lib_a = create_library(name="Library A", path=LIB_A)
        self.lib_b = create_library(name="Library B", path=LIB_B)
        reader_id = get_user_by_username("reader")["id"]
        set_user_libraries(reader_id, [self.lib_a])
        set_user_folders(reader_id, [GRANT])   # only the Marvel subtree
        yield

    # --- Browse -----------------------------------------------------------
    def test_browse_granted_folder(self, client):
        _login(client, "reader", "readerpass")
        assert client.get(f"/api/browse?path={GRANT}").status_code != 403

    def test_browse_ungranted_sibling_denied(self, client):
        _login(client, "reader", "readerpass")
        assert client.get(f"/api/browse?path={SIBLING}").status_code == 403

    def test_browse_ancestor_traverses_and_hides_siblings(self, client, db_connection):
        # Library root lists Marvel (on the path to the grant) but not DC.
        create_file_index_entry(name="Marvel", path=GRANT, entry_type="directory",
                                parent=LIB_A)
        create_file_index_entry(name="DC", path=SIBLING, entry_type="directory",
                                parent=LIB_A)
        _login(client, "reader", "readerpass")
        resp = client.get(f"/api/browse?path={LIB_A}")
        assert resp.status_code != 403
        names = {d["name"] for d in resp.get_json()["directories"]}
        assert names == {"Marvel"}

    def test_browse_ungranted_library_denied(self, client):
        _login(client, "reader", "readerpass")
        assert client.get(f"/api/browse?path={LIB_B}").status_code == 403

    def test_owner_browses_everything(self, client):
        _login(client, "owner", "ownerpass")
        assert client.get(f"/api/browse?path={SIBLING}").status_code != 403
        assert client.get(f"/api/browse?path={LIB_B}").status_code != 403

    # --- File-access gate + level helper ----------------------------------
    def test_path_gate_full_only_under_grant(self):
        from core.auth import user_can_access_path, folder_access_level

        reader = get_user_by_username("reader")
        owner = get_user_by_username("owner")
        assert user_can_access_path(reader, GRANTED_FILE) is True
        assert user_can_access_path(reader, HIDDEN_FILE) is False
        # The library root is only traversable, never a full grant.
        assert folder_access_level(reader, LIB_A) == "traverse"
        assert user_can_access_path(reader, LIB_A) is False
        assert user_can_access_path(owner, HIDDEN_FILE) is True

    def test_granted_library_without_folders_sees_nothing(self):
        # No-pick default: a library grant with no folder rows -> no access.
        from core.auth import folder_access_level

        create_user("bare", password="pw", role="reader")
        bare = get_user_by_username("bare")
        set_user_libraries(bare["id"], [self.lib_a])
        assert folder_access_level(bare, GRANTED_FILE) == "none"

    def test_accessible_prefixes(self):
        from core.auth import accessible_folder_prefixes

        reader = get_user_by_username("reader")
        owner = get_user_by_username("owner")
        assert accessible_folder_prefixes(reader) == [os.path.normpath(GRANT)]
        assert accessible_folder_prefixes(owner) is None   # unrestricted

    # --- Metadata browser (DB aggregate) ---------------------------------
    def test_metadata_browse_scoped_to_grant(self, client, db_connection):
        from core.database import invalidate_metadata_browser_cache

        create_file_index_entry(name="XMen_001.cbz", path=GRANTED_FILE, parent=GRANT)
        create_file_index_entry(name="Batman_001.cbz", path=HIDDEN_FILE, parent=SIBLING)
        db_connection.execute(
            "UPDATE file_index SET ci_publisher=?, ci_series=?, ci_year=? WHERE path=?",
            ("Marvel", "X-Men", "1991", GRANTED_FILE))
        db_connection.execute(
            "UPDATE file_index SET ci_publisher=?, ci_series=?, ci_year=? WHERE path=?",
            ("DC", "Batman", "1940", HIDDEN_FILE))
        db_connection.commit()
        invalidate_metadata_browser_cache()

        _login(client, "reader", "readerpass")
        pubs = {it["value"] for it in
                client.get("/api/metadata/browse?axis=publisher").get_json()["items"]}
        assert pubs == {"Marvel"}

        _login(client, "owner", "ownerpass")
        pubs = {it["value"] for it in
                client.get("/api/metadata/browse?axis=publisher").get_json()["items"]}
        assert pubs == {"Marvel", "DC"}

    # --- Recursive "All Books" view (paginated, SQL-scoped) --------------
    def test_recursive_paged_scoped_by_prefix(self, db_connection):
        # /api/browse-recursive pushes folder scope into SQL via
        # get_files_recursive_paged(allowed_prefixes=...); test that directly so
        # totals/pagination stay correct (the route itself needs a real on-disk
        # dir, which the DB-only test env doesn't have).
        from core.database import get_files_recursive_paged

        root = os.path.normpath(LIB_A)
        grant = os.path.join(root, "Marvel")
        sibling = os.path.join(root, "DC")
        gfile = os.path.join(grant, "XMen_001.cbz")
        hfile = os.path.join(sibling, "Batman_001.cbz")
        create_file_index_entry(name="XMen_001.cbz", path=gfile, parent=grant)
        create_file_index_entry(name="Batman_001.cbz", path=hfile, parent=sibling)

        rows, total, _ = get_files_recursive_paged(root, allowed_prefixes=[grant])
        assert total == 1
        assert {r["name"] for r in rows} == {"XMen_001.cbz"}

        # No-pick default: empty prefixes -> nothing.
        _, total0, _ = get_files_recursive_paged(root, allowed_prefixes=[])
        assert total0 == 0

        # Unrestricted (owner) -> both files.
        _, total_all, _ = get_files_recursive_paged(root, allowed_prefixes=None)
        assert total_all == 2

    # --- Dashboard sections (Want to Read) --------------------------------
    def test_to_read_hides_inaccessible_folders(self, client, db_connection):
        # A file marked "want to read" before a permission change must drop out
        # of the list once the user loses access to its folder.
        from core.database import add_to_read

        reader_id = get_user_by_username("reader")["id"]
        add_to_read(GRANTED_FILE, user_id=reader_id)
        add_to_read(HIDDEN_FILE, user_id=reader_id)
        _login(client, "reader", "readerpass")
        paths = {it["path"] for it in
                 client.get("/api/favorites/to-read").get_json()["items"]}
        assert GRANTED_FILE in paths
        assert HIDDEN_FILE not in paths

    # --- File Manager exemption -------------------------------------------
    def test_file_manager_not_folder_scoped(self, client):
        # /list-directories is a live filesystem view; folder scope must NOT
        # apply, so a Reader can still list an ungranted sibling here.
        _login(client, "reader", "readerpass")
        # Path is inside a real library -> passes is_valid_library_path; the
        # folder-scope gate is intentionally absent, so this is never 403.
        assert client.get(f"/list-directories?path={SIBLING}").status_code != 403


class TestFolderAccessImplicitOwner:
    """Single-user install: folder scope is a no-op."""

    @pytest.fixture(autouse=True)
    def _libs(self, db_connection, monkeypatch):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_library(name="Library A", path=LIB_A)
        yield

    def test_browse_any_folder_allowed(self, client):
        assert client.get(f"/api/browse?path={SIBLING}").status_code != 403

    def test_level_is_full_without_login(self):
        from core.auth import folder_access_level
        assert folder_access_level(None, HIDDEN_FILE) == "full"

    def test_prefixes_unrestricted(self):
        from core.auth import accessible_folder_prefixes
        assert accessible_folder_prefixes(None) is None
