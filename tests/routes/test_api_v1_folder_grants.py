"""Folder grants on the /api/v1 token API.

Metadata mode always read the file index with the token user's folder grants
applied. Filesystem mode (``?mode=filesystem``) and the issue-id endpoints now
apply the same grants through ``core.path_access``: the folder must lie inside
a library (by realpath), needs at least browse access, child folders the user
cannot see are omitted, and the files of a traverse-only folder are withheld.
Owners, the legacy global token and single-user installs see what they saw
before.

Every test builds its library under ``tmp_path`` and replaces the library
table with it, so a ``/data`` folder on the machine running the suite cannot
leak in. Each test authenticates with exactly one token.
"""
import io
import os
import time
import zipfile

import pytest

from core.database import (
    add_file_index_entry,
    add_library,
    create_api_token,
    create_user,
    get_db_connection,
    get_libraries,
    get_recent_files_paginated,
    get_user_by_username,
    set_user_folders,
    set_user_libraries,
)


def _cbz(path):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 6)).save(buf, format="PNG")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("p01.png", buf.getvalue())
    return path


def _index_dir(path):
    add_file_index_entry(
        name=os.path.basename(path), path=path, entry_type="directory",
        parent=os.path.dirname(path), modified_at=time.time(),
    )


def _index_file(path):
    add_file_index_entry(
        name=os.path.basename(path), path=path, entry_type="file",
        size=os.path.getsize(path), parent=os.path.dirname(path),
        modified_at=os.path.getmtime(path),
    )
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT id FROM file_index WHERE path = ?", (path,)).fetchone()
        return row["id"]
    finally:
        conn.close()


def _symlink_or_skip(src, dst):
    try:
        os.symlink(src, dst, target_is_directory=os.path.isdir(src))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available here")


@pytest.fixture
def lib(app, db_connection, monkeypatch, tmp_path):
    """LibA/
         Loose 001.cbz              (root file)
         Marvel/                    (traverse-only for the reader)
           Marvel Loose 001.cbz
           X-Men/                   (granted)
             X-Men 001.cbz
           Avengers/                (not granted)
             Avengers 001.cbz
         DC/                        (not granted)
           Batman/
             Batman 001.cbz
       outside/Private 001.cbz      (no library)
    """
    monkeypatch.delenv("CLU_USERNAME", raising=False)
    monkeypatch.delenv("CLU_PASSWORD", raising=False)

    root = tmp_path / "LibA"
    marvel = root / "Marvel"
    xmen = marvel / "X-Men"
    avengers = marvel / "Avengers"
    dc = root / "DC"
    batman = dc / "Batman"
    outside = tmp_path / "outside"
    for d in (xmen, avengers, batman, outside):
        d.mkdir(parents=True, exist_ok=True)

    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM libraries")
        conn.commit()
    finally:
        conn.close()
    assert add_library("Library A", str(root))

    for d in (marvel, xmen, avengers, dc, batman):
        _index_dir(str(d))

    class L:
        pass

    L.root, L.marvel, L.xmen, L.avengers = str(root), str(marvel), str(xmen), str(avengers)
    L.dc, L.batman, L.outside = str(dc), str(batman), str(outside)
    L.loose = _index_file(_cbz(str(root / "Loose 001.cbz")))
    L.marvel_loose = _index_file(_cbz(str(marvel / "Marvel Loose 001.cbz")))
    L.xmen_id = _index_file(_cbz(str(xmen / "X-Men 001.cbz")))
    L.avengers_id = _index_file(_cbz(str(avengers / "Avengers 001.cbz")))
    L.batman_id = _index_file(_cbz(str(batman / "Batman 001.cbz")))
    _cbz(str(outside / "Private 001.cbz"))
    L.lib_id = get_libraries(enabled_only=True)[0]["id"]
    return L


@pytest.fixture
def tokens(lib):
    """An owner and a reader granted only LibA/Marvel/X-Men, each with a token."""
    owner_id = create_user("grant_owner", password="ownerpass", role="owner")
    create_user("grant_reader", password="readerpass", role="reader")
    reader_id = get_user_by_username("grant_reader")["id"]
    set_user_libraries(reader_id, [lib.lib_id])
    set_user_folders(reader_id, [lib.xmen])

    class T:
        pass

    T.owner = {"Authorization": "Bearer " + create_api_token(owner_id)}
    T.reader = {"Authorization": "Bearer " + create_api_token(reader_id)}
    return T


def _get(client, headers, url, **params):
    params.setdefault("mode", "filesystem")
    return client.get(url, headers=headers, query_string=params)


def _names(resp):
    return {it["name"] for it in resp.get_json()["items"]}


# ---------------------------------------------------------------------------
# /library/publishers?mode=filesystem
# ---------------------------------------------------------------------------
class TestPublishersFilesystem:
    def test_reader_sees_only_folders_on_the_way_to_a_grant(self, client, lib, tokens):
        resp = _get(client, tokens.reader, "/api/v1/library/publishers")
        assert resp.status_code == 200
        assert _names(resp) == {"Marvel"}
        assert resp.get_json()["total"] == 1

    def test_traverse_only_folder_counts_only_granted_comics(self, client, lib, tokens):
        resp = _get(client, tokens.reader, "/api/v1/library/publishers")
        (marvel,) = resp.get_json()["items"]
        # X-Men 001 only: not Avengers 001, not the loose Marvel issue.
        assert marvel["count"] == 1

    def test_owner_sees_every_publisher(self, client, lib, tokens):
        resp = _get(client, tokens.owner, "/api/v1/library/publishers")
        assert resp.status_code == 200
        assert _names(resp) == {"Marvel", "DC"}
        counts = {it["name"]: it["count"] for it in resp.get_json()["items"]}
        assert counts["Marvel"] == 3

    def test_reader_with_no_grants_sees_nothing(self, client, lib, tokens):
        set_user_folders(get_user_by_username("grant_reader")["id"], [])
        resp = _get(client, tokens.reader, "/api/v1/library/publishers")
        assert resp.status_code == 200
        assert resp.get_json()["items"] == []
        assert resp.get_json()["total"] == 0

    def test_metadata_mode_still_scoped(self, client, lib, tokens):
        resp = _get(client, tokens.reader, "/api/v1/library/publishers", mode="metadata")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /library/series?mode=filesystem
# ---------------------------------------------------------------------------
class TestSeriesFilesystem:
    def test_reader_lists_traverse_folder_without_ungranted_children(self, client, lib, tokens):
        resp = _get(client, tokens.reader, "/api/v1/library/series", publisher=lib.marvel)
        assert resp.status_code == 200
        assert _names(resp) == {"X-Men"}
        assert resp.get_json()["total"] == 1

    def test_reader_refused_ungranted_folder(self, client, lib, tokens):
        resp = _get(client, tokens.reader, "/api/v1/library/series", publisher=lib.dc)
        assert resp.status_code == 404
        assert "items" not in resp.get_json()

    def test_reader_refused_by_relative_name(self, client, lib, tokens):
        resp = _get(client, tokens.reader, "/api/v1/library/series", publisher="DC")
        assert resp.status_code == 404

    def test_dotdot_rejected(self, client, lib, tokens):
        resp = _get(client, tokens.reader, "/api/v1/library/series",
                    publisher=os.path.join(lib.xmen, "..", "..", "DC"))
        assert resp.status_code == 400

    def test_outside_every_library_rejected(self, client, lib, tokens):
        resp = _get(client, tokens.owner, "/api/v1/library/series", publisher=lib.outside)
        assert resp.status_code == 400

    def test_symlink_to_ungranted_folder_refused(self, client, lib, tokens):
        link = os.path.join(lib.xmen, "shortcut")
        _symlink_or_skip(lib.dc, link)
        resp = _get(client, tokens.reader, "/api/v1/library/series", publisher=link)
        assert resp.status_code == 404

    def test_symlink_out_of_library_rejected_for_owner(self, client, lib, tokens):
        link = os.path.join(lib.root, "escape")
        _symlink_or_skip(lib.outside, link)
        resp = _get(client, tokens.owner, "/api/v1/library/series", publisher=link)
        assert resp.status_code == 400

    def test_owner_sees_every_series(self, client, lib, tokens):
        resp = _get(client, tokens.owner, "/api/v1/library/series", publisher=lib.marvel)
        assert resp.status_code == 200
        assert _names(resp) == {"X-Men", "Avengers"}


# ---------------------------------------------------------------------------
# /library/issues?mode=filesystem
# ---------------------------------------------------------------------------
class TestIssuesFilesystem:
    def test_reader_lists_granted_series(self, client, lib, tokens):
        resp = _get(client, tokens.reader, "/api/v1/library/issues",
                    publisher=lib.marvel, series=lib.xmen)
        assert resp.status_code == 200
        assert _names(resp) == {"X-Men 001.cbz"}
        assert resp.get_json()["items"][0]["id"] == lib.xmen_id

    def test_traverse_only_folder_withholds_its_files(self, client, lib, tokens):
        resp = _get(client, tokens.reader, "/api/v1/library/issues",
                    publisher=lib.root, series=lib.marvel)
        assert resp.status_code == 200
        assert resp.get_json()["items"] == []
        assert resp.get_json()["total"] == 0

    def test_reader_refused_ungranted_series(self, client, lib, tokens):
        resp = _get(client, tokens.reader, "/api/v1/library/issues",
                    publisher=lib.marvel, series=lib.avengers)
        assert resp.status_code == 404

    def test_owner_lists_traverse_folder_files(self, client, lib, tokens):
        resp = _get(client, tokens.owner, "/api/v1/library/issues",
                    publisher=lib.root, series=lib.marvel)
        assert resp.status_code == 200
        assert _names(resp) == {"Marvel Loose 001.cbz"}


# ---------------------------------------------------------------------------
# Issue-id endpoints
# ---------------------------------------------------------------------------
class TestIssueEndpoints:
    @pytest.mark.parametrize("suffix", ["", "/cover", "/download"])
    def test_reader_opens_granted_issue(self, client, lib, tokens, suffix):
        resp = client.get(f"/api/v1/issue/{lib.xmen_id}{suffix}", headers=tokens.reader)
        assert resp.status_code == 200

    @pytest.mark.parametrize("suffix", ["", "/cover", "/download"])
    def test_reader_refused_ungranted_issue(self, client, lib, tokens, suffix):
        resp = client.get(f"/api/v1/issue/{lib.batman_id}{suffix}", headers=tokens.reader)
        assert resp.status_code == 404

    def test_reader_refused_issue_in_traverse_folder(self, client, lib, tokens):
        resp = client.get(f"/api/v1/issue/{lib.marvel_loose}", headers=tokens.reader)
        assert resp.status_code == 404

    def test_symlinked_issue_judged_by_its_target(self, client, lib, tokens):
        link = os.path.join(lib.xmen, "Alias 001.cbz")
        _symlink_or_skip(os.path.join(lib.batman, "Batman 001.cbz"), link)
        add_file_index_entry(name="Alias 001.cbz", path=link, entry_type="file",
                             size=1, parent=lib.xmen, modified_at=time.time())
        conn = get_db_connection()
        try:
            alias_id = conn.execute(
                "SELECT id FROM file_index WHERE path = ?", (link,)).fetchone()["id"]
        finally:
            conn.close()
        resp = client.get(f"/api/v1/issue/{alias_id}/download", headers=tokens.reader)
        assert resp.status_code == 404

    def test_owner_opens_any_issue(self, client, lib, tokens):
        resp = client.get(f"/api/v1/issue/{lib.batman_id}", headers=tokens.owner)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# /library/recent
# ---------------------------------------------------------------------------
class TestRecentScoped:
    def test_reader_request_passes_its_folder_scope(self, client, lib, tokens, monkeypatch):
        seen = {}

        def fake(offset=0, limit=50, allowed_prefixes=None):
            seen["prefixes"] = allowed_prefixes
            return [], 0

        monkeypatch.setattr("routes.api_v1.get_recent_files_paginated", fake)
        resp = client.get("/api/v1/library/recent", headers=tokens.reader)
        assert resp.status_code == 200
        assert [os.path.normpath(p) for p in seen["prefixes"]] == [os.path.normpath(lib.xmen)]

    def test_owner_request_is_unscoped(self, client, lib, tokens, monkeypatch):
        seen = {}

        def fake(offset=0, limit=50, allowed_prefixes=None):
            seen["prefixes"] = allowed_prefixes
            return [], 0

        monkeypatch.setattr("routes.api_v1.get_recent_files_paginated", fake)
        client.get("/api/v1/library/recent", headers=tokens.owner)
        assert seen["prefixes"] is None


class TestRecentFilesQueryScope:
    """The query itself, with POSIX-style paths so the library clause (which
    joins on '/') matches on every platform."""

    @pytest.fixture
    def rows(self, db_connection, monkeypatch):
        monkeypatch.setattr(
            "core.database.get_libraries",
            lambda enabled_only=True: [{"id": 1, "path": "/lib"}],
        )
        now = time.time()
        conn = get_db_connection()
        try:
            for i, path in enumerate(("/lib/Marvel/a.cbz", "/lib/DC/b.cbz", "/lib/Marvel_2/c.cbz")):
                conn.execute(
                    "INSERT INTO file_index (name, path, type, size, parent, "
                    "has_thumbnail, modified_at, first_indexed_at) "
                    "VALUES (?, ?, 'file', 1, ?, 0, ?, ?)",
                    (os.path.basename(path), path, path.rsplit("/", 1)[0], now - i, now - i),
                )
            conn.commit()
        finally:
            conn.close()

    def test_unscoped_returns_all(self, rows):
        _rows, total = get_recent_files_paginated()
        assert total == 3

    def test_scoped_to_granted_folder(self, rows):
        found, total = get_recent_files_paginated(allowed_prefixes=["/lib/Marvel"])
        assert total == 1
        assert [r["file_path"] for r in found] == ["/lib/Marvel/a.cbz"]

    def test_empty_scope_returns_nothing(self, rows):
        assert get_recent_files_paginated(allowed_prefixes=[]) == ([], 0)


# ---------------------------------------------------------------------------
# Single-user install: no grants, containment still applies
# ---------------------------------------------------------------------------
class TestSingleUserFilesystem:
    @pytest.fixture
    def global_token(self, lib):
        from core.database import set_user_preference

        value = "single-user-api-value"
        set_user_preference("api_token", value, category="security")
        return {"Authorization": "Bearer " + value}

    def test_lists_every_series(self, client, lib, global_token):
        resp = _get(client, global_token, "/api/v1/library/series", publisher=lib.marvel)
        assert resp.status_code == 200
        assert _names(resp) == {"X-Men", "Avengers"}

    def test_outside_rejected(self, client, lib, global_token):
        resp = _get(client, global_token, "/api/v1/library/series", publisher=lib.outside)
        assert resp.status_code == 400
