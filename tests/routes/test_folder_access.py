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

    # --- Folder art (file-serve, but traverse-aware) ----------------------
    def test_folder_art_denied_for_ungranted_sibling(self, client, tmp_path,
                                                     monkeypatch):
        # GHSA-vhvw-93fg-whm8: /api/folder-thumbnail served any readable file.
        # It is a file-serve route, so an ungranted sibling's art is refused
        # even though the file is inside a granted *library*.
        _login(client, "reader", "readerpass")
        resp = client.get(f"/api/folder-thumbnail?path={SIBLING}/folder.png")
        assert resp.status_code == 403

    def test_folder_art_denied_outside_every_library(self, client):
        _login(client, "reader", "readerpass")
        resp = client.get("/api/folder-thumbnail?path=/etc/folder.png")
        assert resp.status_code == 403

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


class TestFolderArtScope:
    """/api/folder-thumbnail end to end, over a real on-disk library.

    The class above uses /data paths that exist only in the database, which is
    enough for the DB-scoped readers but not for a route that ends in
    ``send_file``. These libraries are real directories, so the whole route
    runs: confinement, the per-user grant, and the read.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, db_connection, monkeypatch, tmp_path):
        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        create_user("art_owner", password="ownerpass", role="owner")
        create_user("art_reader", password="readerpass", role="reader")

        self.root = tmp_path / "LibArt"
        self.grant = self.root / "Marvel"
        self.sibling = self.root / "DC"
        self.grant.mkdir(parents=True)
        self.sibling.mkdir(parents=True)
        for folder in (self.root, self.grant, self.sibling):
            (folder / "folder.png").write_text("art")
        (self.grant / "page01.png").write_text("a page, not cover art")

        lib = create_library(name="Library Art", path=str(self.root))
        reader_id = get_user_by_username("art_reader")["id"]
        set_user_libraries(reader_id, [lib])
        set_user_folders(reader_id, [str(self.grant)])
        yield

    def _art(self, client, folder):
        return client.get(f"/api/folder-thumbnail?path={folder}/folder.png")

    def test_granted_folder_art_served(self, client):
        _login(client, "art_reader", "readerpass")
        resp = self._art(client, self.grant)
        assert resp.status_code == 200
        assert resp.content_type == "image/png"

    def test_ungranted_sibling_art_denied(self, client):
        # GHSA-vhvw-93fg-whm8: this used to be served to anyone logged in.
        _login(client, "art_reader", "readerpass")
        assert self._art(client, self.sibling).status_code == 403

    def test_traversable_ancestor_art_served(self, client):
        # The library root is 'traverse', never 'full', but the grid draws its
        # card on the way down to the grant -- so the route asks in
        # mode='browse', and asks about the containing folder rather than the
        # image (which is never itself a grant path).
        _login(client, "art_reader", "readerpass")
        assert self._art(client, self.root).status_code == 200

    def test_non_art_image_under_a_grant_denied(self, client):
        # Even with full access to the folder, this route serves cover art and
        # nothing else -- it is not a general image reader.
        _login(client, "art_reader", "readerpass")
        resp = client.get(f"/api/folder-thumbnail?path={self.grant}/page01.png")
        assert resp.status_code == 403

    def test_owner_sees_every_folder_art(self, client):
        _login(client, "art_owner", "ownerpass")
        assert self._art(client, self.sibling).status_code == 200
        assert self._art(client, self.root).status_code == 200


class TestFileManagerOpsFolderScoped:
    """GHSA-3258-jj8j-8878: the File Manager *operations* honour folder grants.

    Its listing is a live filesystem view, but the routes in routes/files.py
    read, write, move and delete raw paths from the request body. A Clerk
    granted only LibA/Marvel must not be able to reach LibA/DC (or anything
    outside the libraries) through them. WATCH/TARGET stay open to Clerks:
    they carry no grants and Clerks own the download pipeline.
    """

    @pytest.fixture(autouse=True)
    def _setup(self, db_connection, monkeypatch, tmp_path):
        from PIL import Image

        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        self.root = str(tmp_path / "LibA")
        self.grant = os.path.join(self.root, "Marvel")
        self.sibling = os.path.join(self.root, "DC")
        self.target = str(tmp_path / "processed")
        self.outside = str(tmp_path / "secret")
        for d in (self.grant, self.sibling, self.target, self.outside):
            os.makedirs(d)
        for d in (self.grant, self.sibling, self.outside):
            Image.new("RGB", (4, 4), "red").save(os.path.join(d, "page.png"))
        monkeypatch.setattr("core.config.get_target_dir", lambda: self.target)
        monkeypatch.setattr("core.config.get_watch_dir", lambda: "")

        create_user("fm_owner", password="ownerpass", role="owner")
        create_user("fm_clerk", password="clerkpass", role="clerk")
        lib = create_library(name="Library A", path=self.root)
        clerk_id = get_user_by_username("fm_clerk")["id"]
        set_user_libraries(clerk_id, [lib])
        set_user_folders(clerk_id, [self.grant])
        yield

    def _image(self, client, path):
        return client.post("/get-image-data", json={"target": path})

    # --- The advisory's proof of concept ----------------------------------
    def test_image_data_ungranted_sibling_denied(self, client):
        _login(client, "fm_clerk", "clerkpass")
        resp = self._image(client, os.path.join(self.sibling, "page.png"))
        assert resp.status_code == 403
        assert "imageData" not in (resp.get_json() or {})

    def test_image_data_outside_every_library_denied(self, client):
        _login(client, "fm_clerk", "clerkpass")
        assert self._image(client, os.path.join(self.outside, "page.png")).status_code == 403

    def test_image_data_dotdot_escape_denied(self, client):
        _login(client, "fm_clerk", "clerkpass")
        sneaky = os.path.join(self.grant, "..", "DC", "page.png")
        assert self._image(client, sneaky).status_code == 403

    def test_image_data_granted_folder_served(self, client):
        _login(client, "fm_clerk", "clerkpass")
        resp = self._image(client, os.path.join(self.grant, "page.png"))
        assert resp.status_code == 200
        assert resp.get_json()["imageData"].startswith("data:image/jpeg;base64,")

    def test_owner_unrestricted(self, client):
        _login(client, "fm_owner", "ownerpass")
        assert self._image(client, os.path.join(self.sibling, "page.png")).status_code == 200

    # --- Writes -----------------------------------------------------------
    def test_create_folder_in_sibling_denied(self, client):
        _login(client, "fm_clerk", "clerkpass")
        new_dir = os.path.join(self.sibling, "New")
        assert client.post("/create-folder", json={"path": new_dir}).status_code == 403
        assert not os.path.exists(new_dir)

    def test_create_folder_in_target_allowed(self, client, monkeypatch):
        monkeypatch.setattr("app.update_index_on_create", lambda p: None, raising=False)
        _login(client, "fm_clerk", "clerkpass")
        new_dir = os.path.join(self.target, "Incoming")
        assert client.post("/create-folder", json={"path": new_dir}).status_code == 200
        assert os.path.isdir(new_dir)

    def test_custom_rename_into_sibling_denied(self, client):
        _login(client, "fm_clerk", "clerkpass")
        src = os.path.join(self.grant, "page.png")
        dst = os.path.join(self.sibling, "stolen.png")
        resp = client.post("/custom-rename", json={"old": src, "new": dst})
        assert resp.status_code == 403
        assert os.path.exists(src) and not os.path.exists(dst)

    def test_rename_out_of_sibling_denied(self, client):
        _login(client, "fm_clerk", "clerkpass")
        src = os.path.join(self.sibling, "page.png")
        dst = os.path.join(self.grant, "taken.png")
        assert client.post("/rename", json={"old": src, "new": dst}).status_code == 403
        assert os.path.exists(src)

    def test_custom_rename_batch_one_bad_entry_rejects_batch(self, client):
        _login(client, "fm_clerk", "clerkpass")
        ok = os.path.join(self.grant, "page.png")
        resp = client.post("/custom-rename-batch", json={"renames": [
            {"old": ok, "new": os.path.join(self.grant, "renamed.png")},
            {"old": os.path.join(self.sibling, "page.png"),
             "new": os.path.join(self.sibling, "x.png")},
        ]})
        assert resp.status_code == 403

    def test_move_batch_into_sibling_denied(self, client):
        _login(client, "fm_clerk", "clerkpass")
        src = os.path.join(self.grant, "page.png")
        resp = client.post("/move-batch", json={"items": [
            {"source": src, "destination": os.path.join(self.sibling, "page2.png")},
        ]})
        assert resp.status_code == 403
        assert os.path.exists(src)

    def test_delete_in_sibling_denied(self, client):
        _login(client, "fm_clerk", "clerkpass")
        victim = os.path.join(self.sibling, "page.png")
        assert client.post("/delete", json={"target": victim}).status_code == 403
        assert os.path.exists(victim)

    def test_delete_multiple_skips_ungranted(self, client):
        _login(client, "fm_clerk", "clerkpass")
        victim = os.path.join(self.sibling, "page.png")
        resp = client.post("/api/delete-multiple", json={"targets": [victim]})
        assert resp.get_json()["results"][0]["success"] is False
        assert os.path.exists(victim)

    def test_folder_size_of_sibling_denied(self, client):
        _login(client, "fm_clerk", "clerkpass")
        assert client.get(f"/folder-size?path={self.sibling}").status_code == 403

    def test_smart_rename_plan_entry_outside_root_denied(self, client):
        # The plan is client-supplied: a granted root must not smuggle in an
        # entry that renames a file somewhere else.
        _login(client, "fm_clerk", "clerkpass")
        victim = os.path.join(self.sibling, "page.png")
        plan = {"root": self.grant, "directories": [{
            "dir": self.sibling, "status": "ok",
            "files": [{"status": "ok", "old_path": victim,
                       "new_path": os.path.join(self.sibling, "gone.png")}],
        }]}
        assert client.post("/smart-rename", json={"plan": plan}).status_code == 403
        assert os.path.exists(victim)


class TestFileOpRefusalImplicitOwner:
    """Single-user install: the file-op guard is a no-op, as before."""

    def test_no_refusal_without_login(self, db_connection, monkeypatch, app):
        from core.auth import file_op_refusal

        monkeypatch.delenv("CLU_USERNAME", raising=False)
        monkeypatch.delenv("CLU_PASSWORD", raising=False)
        with app.test_request_context("/"):
            assert file_op_refusal("/anywhere/at/all.png") is None
