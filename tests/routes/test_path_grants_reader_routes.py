"""Containment and folder grants on the path-taking listing and read routes.

``/count-files``, ``/list-new-files``, ``/list-downloads``, ``/cbz-metadata``
and ``/api/read-text-file`` take a raw absolute path from the query string.
Each resolves it through ``core.path_access`` before anything is listed,
counted or opened: the realpath must lie inside a root the route serves, and
inside a library a non-owner needs a folder grant. Listings run in browse mode
and filter their results, so a traverse-only parent reveals nothing outside
the grant.

Every test here builds real directories under ``tmp_path`` and replaces the
library table with them, so nothing depends on a ``/data`` folder existing on
the machine running the suite. Each test logs in as exactly one user (or none,
for the single-user cases): under pytest-flask's shared application context
``g.current_user`` survives from one request to the next, so switching users
inside one test would be testing that leak rather than the route.
"""
import io
import os
import zipfile

import pytest
from flask import g

from core.database import (
    add_library,
    create_user,
    get_db_connection,
    get_libraries,
    get_user_by_username,
    set_user_folders,
    set_user_libraries,
)


def _login(client, username, password):
    resp = client.post("/login", data={"username": username, "password": password})
    assert resp.status_code == 302
    return resp


def _cbz(path):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 6)).save(buf, format="PNG")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("p01.png", buf.getvalue())
    return path


def _symlink_or_skip(src, dst):
    try:
        os.symlink(src, dst, target_is_directory=os.path.isdir(src))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")


@pytest.fixture
def layout(app, db_connection, monkeypatch, tmp_path):
    """One library (LibA) with a granted folder and an ungranted sibling,
    a TARGET and a WATCH outside it, and a folder outside everything."""
    monkeypatch.delenv("CLU_USERNAME", raising=False)
    monkeypatch.delenv("CLU_PASSWORD", raising=False)

    root = tmp_path / "LibA"
    grant = root / "Marvel"
    sibling = root / "DC"
    target = tmp_path / "processed"          # also app.config["TARGET"]
    watch = tmp_path / "watch"
    outside = tmp_path / "secret"
    for d in (grant, sibling, target, watch, outside):
        d.mkdir(parents=True, exist_ok=True)

    (root / "Loose 001.cbz").write_bytes(b"x")
    _cbz(str(grant / "XMen 001.cbz"))
    _cbz(str(sibling / "Batman 001.cbz"))
    _cbz(str(target / "Incoming 001.cbz"))
    _cbz(str(watch / "Watched 001.cbz"))
    _cbz(str(outside / "Private 001.cbz"))
    (grant / "notes.txt").write_text("granted notes")
    (sibling / "notes.txt").write_text("sibling notes")
    (target / "notes.txt").write_text("target notes")
    (outside / "notes.txt").write_text("outside notes")

    monkeypatch.setattr("core.config.get_target_dir", lambda: str(target))
    monkeypatch.setattr("core.config.get_watch_dir", lambda: str(watch))

    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM libraries")
        conn.commit()
    finally:
        conn.close()
    assert add_library("Library A", str(root))
    lib_id = get_libraries(enabled_only=True)[0]["id"]

    class L:
        pass

    L.root, L.grant, L.sibling = str(root), str(grant), str(sibling)
    L.target, L.watch, L.outside = str(target), str(watch), str(outside)
    L.lib_id = lib_id
    return L


@pytest.fixture
def multi_user(layout):
    """Owner, a Reader and a Clerk; both non-owners hold only LibA/Marvel."""
    create_user("pg_owner", password="ownerpass", role="owner")
    for name, role in (("pg_reader", "reader"), ("pg_clerk", "clerk")):
        create_user(name, password="pw-" + role, role=role)
        uid = get_user_by_username(name)["id"]
        set_user_libraries(uid, [layout.lib_id])
        set_user_folders(uid, [layout.grant])
    return layout


def _as_reader(client):
    _login(client, "pg_reader", "pw-reader")


def _as_clerk(client):
    _login(client, "pg_clerk", "pw-clerk")


def _as_owner(client):
    _login(client, "pg_owner", "ownerpass")


# ---------------------------------------------------------------------------
# /count-files
# ---------------------------------------------------------------------------
class TestCountFiles:
    def test_reader_counts_granted_folder(self, client, multi_user):
        _as_reader(client)
        resp = client.get("/count-files", query_string={"path": multi_user.grant})
        assert resp.status_code == 200
        assert resp.get_json()["file_count"] == 2   # the cbz and notes.txt

    def test_reader_refused_ungranted_sibling(self, client, multi_user):
        _as_reader(client)
        resp = client.get("/count-files", query_string={"path": multi_user.sibling})
        assert resp.status_code == 403
        assert "file_count" not in resp.get_json()

    def test_traverse_only_parent_counts_only_granted_files(self, client, multi_user):
        _as_reader(client)
        resp = client.get("/count-files", query_string={"path": multi_user.root})
        assert resp.status_code == 200
        # Not the loose file at the root, not the sibling's two.
        assert resp.get_json()["file_count"] == 2

    def test_reader_refused_outside_libraries(self, client, multi_user):
        _as_reader(client)
        resp = client.get("/count-files", query_string={"path": multi_user.outside})
        assert resp.status_code == 403

    def test_dotdot_escape_judged_by_where_it_lands(self, client, multi_user):
        _as_reader(client)
        sneaky = os.path.join(multi_user.grant, "..", "DC")
        resp = client.get("/count-files", query_string={"path": sneaky})
        assert resp.status_code == 403

    def test_symlink_in_grant_to_sibling_refused(self, client, multi_user):
        link = os.path.join(multi_user.grant, "shortcut")
        _symlink_or_skip(multi_user.sibling, link)
        _as_reader(client)
        resp = client.get("/count-files", query_string={"path": link})
        assert resp.status_code == 403

    def test_owner_counts_whole_library(self, client, multi_user):
        _as_owner(client)
        resp = client.get("/count-files", query_string={"path": multi_user.root})
        assert resp.status_code == 200
        assert resp.get_json()["file_count"] == 5

    def test_owner_refused_outside_libraries(self, client, multi_user):
        _as_owner(client)
        resp = client.get("/count-files", query_string={"path": multi_user.outside})
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# /list-new-files
# ---------------------------------------------------------------------------
class TestListNewFiles:
    def _names(self, resp):
        return {f["name"] for f in resp.get_json()["files"]}

    def test_traverse_only_parent_lists_only_granted_files(self, client, multi_user):
        _as_reader(client)
        resp = client.get("/list-new-files", query_string={"path": multi_user.root})
        assert resp.status_code == 200
        assert self._names(resp) == {"XMen 001.cbz"}

    def test_reader_refused_ungranted_sibling(self, client, multi_user):
        _as_reader(client)
        resp = client.get("/list-new-files", query_string={"path": multi_user.sibling})
        assert resp.status_code == 403

    def test_reader_refused_outside_libraries(self, client, multi_user):
        _as_reader(client)
        resp = client.get("/list-new-files", query_string={"path": multi_user.outside})
        assert resp.status_code == 403

    def test_owner_sees_every_new_file(self, client, multi_user):
        _as_owner(client)
        resp = client.get("/list-new-files", query_string={"path": multi_user.root})
        assert resp.status_code == 200
        assert self._names(resp) == {"Loose 001.cbz", "XMen 001.cbz", "Batman 001.cbz"}

    def test_pathless_request_starts_at_default_library(self, client, multi_user):
        _as_owner(client)
        resp = client.get("/list-new-files")
        assert resp.status_code == 200
        assert os.path.normpath(resp.get_json()["current_path"]) == os.path.normpath(multi_user.root)


# ---------------------------------------------------------------------------
# /list-downloads
# ---------------------------------------------------------------------------
def _real_listing(monkeypatch):
    """The routes conftest's mock ``app`` module answers every call with a
    MagicMock; give /list-downloads a real (uncached) directory listing."""
    import sys

    mock_app = sys.modules["app"]
    monkeypatch.setattr(mock_app, "is_cache_valid", lambda p: False, raising=False)
    monkeypatch.setattr(mock_app, "cleanup_cache", lambda: None, raising=False)
    monkeypatch.setattr(mock_app, "cache_stats", {"misses": 0}, raising=False)
    monkeypatch.setattr(mock_app, "MAX_CACHE_SIZE", 100, raising=False)
    monkeypatch.setattr(mock_app, "get_directory_listing", lambda p: {
        "directories": sorted(e.name for e in os.scandir(p) if e.is_dir()),
        "files": sorted(e.name for e in os.scandir(p) if e.is_file()),
    }, raising=False)


class TestListDownloads:
    @pytest.fixture(autouse=True)
    def _listing(self, app, monkeypatch):
        _real_listing(monkeypatch)

    def test_clerk_lists_target(self, client, multi_user):
        _as_clerk(client)
        resp = client.get("/list-downloads", query_string={"path": multi_user.target})
        assert resp.status_code == 200
        assert resp.get_json()["parent"] is None

    def test_clerk_lists_watch(self, client, multi_user):
        _as_clerk(client)
        resp = client.get("/list-downloads", query_string={"path": multi_user.watch})
        assert resp.status_code == 200

    def test_clerk_lists_library_folder(self, client, multi_user):
        # files.js reloads the panel's current folder -- usually a library
        # folder -- through this route after a bulk rename.
        _as_clerk(client)
        resp = client.get("/list-downloads", query_string={"path": multi_user.grant})
        assert resp.status_code == 200
        assert "XMen 001.cbz" in resp.get_json()["files"]
        assert os.path.normpath(resp.get_json()["parent"]) == os.path.normpath(multi_user.root)

    def test_library_listing_matches_file_manager_listing(self, client, multi_user):
        # Not folder-grant scoped, exactly like /list-directories
        # (test_file_manager_not_folder_scoped): an ungranted sibling lists.
        _as_clerk(client)
        resp = client.get("/list-downloads", query_string={"path": multi_user.sibling})
        assert resp.status_code == 200
        assert "Batman 001.cbz" in resp.get_json()["files"]

    def test_no_parent_above_library_root(self, client, multi_user):
        _as_clerk(client)
        resp = client.get("/list-downloads", query_string={"path": multi_user.root})
        assert resp.status_code == 200
        assert resp.get_json()["parent"] is None

    def test_symlink_out_of_library_refused(self, client, multi_user):
        link = os.path.join(multi_user.grant, "escape")
        _symlink_or_skip(multi_user.outside, link)
        _as_clerk(client)
        resp = client.get("/list-downloads", query_string={"path": link})
        assert resp.status_code == 403

    def test_clerk_refused_outside(self, client, multi_user):
        _as_clerk(client)
        resp = client.get("/list-downloads", query_string={"path": multi_user.outside})
        assert resp.status_code == 403

    def test_reader_refused_target(self, client, multi_user):
        # A Clerk route by policy: the role gate answers before the view runs
        # (a redirect, since the path is not under /api/).
        _as_reader(client)
        resp = client.get("/list-downloads", query_string={"path": multi_user.target})
        assert resp.status_code in (302, 403)
        assert b"Incoming 001.cbz" not in resp.get_data()

    def test_lists_target_contents(self, client, multi_user):
        _as_clerk(client)
        resp = client.get("/list-downloads", query_string={"path": multi_user.target})
        assert "Incoming 001.cbz" in resp.get_json()["files"]

    def test_owner_refused_outside(self, client, multi_user):
        _as_owner(client)
        resp = client.get("/list-downloads", query_string={"path": multi_user.outside})
        assert resp.status_code == 403

    def test_pathless_request_lists_target(self, client, multi_user):
        _as_owner(client)
        resp = client.get("/list-downloads")
        assert resp.status_code == 200
        assert os.path.normpath(resp.get_json()["current_path"]) == os.path.normpath(multi_user.target)


# ---------------------------------------------------------------------------
# /cbz-metadata
# ---------------------------------------------------------------------------
class TestCbzMetadata:
    def _get(self, client, path):
        return client.get("/cbz-metadata", query_string={"path": path})

    def test_reader_reads_granted_file(self, client, multi_user):
        _as_reader(client)
        resp = self._get(client, os.path.join(multi_user.grant, "XMen 001.cbz"))
        assert resp.status_code == 200
        assert resp.get_json()["image_files"] == 1

    def test_reader_refused_ungranted_sibling(self, client, multi_user):
        _as_reader(client)
        resp = self._get(client, os.path.join(multi_user.sibling, "Batman 001.cbz"))
        assert resp.status_code == 403
        assert "file_list" not in resp.get_json()

    def test_reader_refused_outside(self, client, multi_user):
        _as_reader(client)
        resp = self._get(client, os.path.join(multi_user.outside, "Private 001.cbz"))
        assert resp.status_code == 403

    def test_reader_refused_target(self, client, multi_user):
        _as_reader(client)
        resp = self._get(client, os.path.join(multi_user.target, "Incoming 001.cbz"))
        assert resp.status_code == 403

    def test_clerk_reads_target_and_watch(self, client, multi_user):
        _as_clerk(client)
        assert self._get(client, os.path.join(multi_user.target, "Incoming 001.cbz")).status_code == 200
        assert self._get(client, os.path.join(multi_user.watch, "Watched 001.cbz")).status_code == 200

    def test_symlinked_cbz_judged_by_its_target(self, client, multi_user):
        link = os.path.join(multi_user.grant, "Alias 001.cbz")
        _symlink_or_skip(os.path.join(multi_user.sibling, "Batman 001.cbz"), link)
        _as_reader(client)
        assert self._get(client, link).status_code == 403

    def test_owner_reads_sibling(self, client, multi_user):
        _as_owner(client)
        resp = self._get(client, os.path.join(multi_user.sibling, "Batman 001.cbz"))
        assert resp.status_code == 200

    def test_owner_refused_outside(self, client, multi_user):
        _as_owner(client)
        resp = self._get(client, os.path.join(multi_user.outside, "Private 001.cbz"))
        assert resp.status_code == 403


class TestSingleUserInstall:
    """No accounts: no grants to check, but containment still holds."""

    def test_count_files_in_library(self, client, layout):
        resp = client.get("/count-files", query_string={"path": layout.root})
        assert resp.status_code == 200
        assert resp.get_json()["file_count"] == 5

    def test_count_files_outside_refused(self, client, layout):
        resp = client.get("/count-files", query_string={"path": layout.outside})
        assert resp.status_code == 403

    def test_cbz_metadata_in_target(self, client, layout):
        resp = client.get("/cbz-metadata", query_string={
            "path": os.path.join(layout.target, "Incoming 001.cbz")})
        assert resp.status_code == 200

    def test_list_downloads_target(self, client, layout, monkeypatch):
        _real_listing(monkeypatch)
        resp = client.get("/list-downloads", query_string={"path": layout.target})
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /api/read-text-file -- its check lives in core.path_access because app.py
# cannot be imported in tests; tests/unit/test_read_text_file_wiring.py pins
# the call site.
# ---------------------------------------------------------------------------
class TestResolveTextFile:
    def _resolve(self, app, path, user=None):
        from core.path_access import resolve_text_file

        with app.test_request_context("/api/read-text-file"):
            if user is not None:
                g.current_user = user
            return resolve_text_file(path)

    def test_single_user_reads_library_txt(self, app, layout):
        served, refusal = self._resolve(app, os.path.join(layout.grant, "notes.txt"))
        assert refusal is None
        assert served.path.endswith("notes.txt")

    def test_single_user_reads_target_txt(self, app, layout):
        _, refusal = self._resolve(app, os.path.join(layout.target, "notes.txt"))
        assert refusal is None

    def test_outside_refused(self, app, layout):
        _, refusal = self._resolve(app, os.path.join(layout.outside, "notes.txt"))
        assert refusal[1] == 403

    def test_non_txt_in_library_refused(self, app, layout):
        db = os.path.join(layout.grant, "comic_utils.db")
        with open(db, "wb") as f:
            f.write(b"SQLite format 3\0")
        _, refusal = self._resolve(app, db)
        assert refusal[1] == 400

    def test_txt_symlink_to_outside_refused(self, app, layout):
        link = os.path.join(layout.grant, "leak.txt")
        _symlink_or_skip(os.path.join(layout.outside, "notes.txt"), link)
        _, refusal = self._resolve(app, link)
        assert refusal[1] == 403

    def test_txt_symlink_to_other_extension_refused(self, app, layout):
        real = os.path.join(layout.grant, "settings.ini")
        with open(real, "w") as f:
            f.write("secret=1")
        link = os.path.join(layout.grant, "readme.txt")
        _symlink_or_skip(real, link)
        _, refusal = self._resolve(app, link)
        assert refusal[1] == 400

    def test_missing_is_404(self, app, layout):
        _, refusal = self._resolve(app, os.path.join(layout.grant, "absent.txt"))
        assert refusal[1] == 404

    def test_relative_path_refused(self, app, layout):
        _, refusal = self._resolve(app, "notes.txt")
        assert refusal[1] == 400

    def test_oversized_refused(self, app, layout, monkeypatch):
        monkeypatch.setattr("core.path_access.TEXT_FILE_MAX_BYTES", 4)
        _, refusal = self._resolve(app, os.path.join(layout.grant, "notes.txt"))
        assert refusal[1] == 413

    def test_reader_granted_txt(self, app, multi_user):
        reader = get_user_by_username("pg_reader")
        _, refusal = self._resolve(app, os.path.join(multi_user.grant, "notes.txt"), reader)
        assert refusal is None

    def test_reader_ungranted_sibling_txt_refused(self, app, multi_user):
        reader = get_user_by_username("pg_reader")
        _, refusal = self._resolve(app, os.path.join(multi_user.sibling, "notes.txt"), reader)
        assert refusal[1] == 403

    def test_reader_target_txt_refused(self, app, multi_user):
        reader = get_user_by_username("pg_reader")
        _, refusal = self._resolve(app, os.path.join(multi_user.target, "notes.txt"), reader)
        assert refusal[1] == 403

    def test_clerk_target_txt_allowed(self, app, multi_user):
        clerk = get_user_by_username("pg_clerk")
        _, refusal = self._resolve(app, os.path.join(multi_user.target, "notes.txt"), clerk)
        assert refusal is None


# ---------------------------------------------------------------------------
# /api/missing-xml -- results filtered to the user's grants
# ---------------------------------------------------------------------------
class TestMissingXmlFiltered:
    def test_reader_sees_only_granted_files(self, client, multi_user):
        from tests.factories.db_factories import create_file_index_entry
        from core.database import get_db_connection as _conn

        granted = os.path.join(multi_user.grant, "XMen 001.cbz")
        hidden = os.path.join(multi_user.sibling, "Batman 001.cbz")
        create_file_index_entry(name="XMen 001.cbz", path=granted, parent=multi_user.grant)
        create_file_index_entry(name="Batman 001.cbz", path=hidden, parent=multi_user.sibling)
        conn = _conn()
        try:
            conn.execute("UPDATE file_index SET has_comicinfo = 0")
            conn.commit()
        finally:
            conn.close()

        _as_reader(client)
        resp = client.get("/api/missing-xml")
        assert resp.status_code == 200
        paths = {f["path"] for f in resp.get_json()["files"]}
        assert granted in paths
        assert hidden not in paths
