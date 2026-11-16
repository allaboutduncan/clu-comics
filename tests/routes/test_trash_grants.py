"""Folder grants on the shared trash (``/api/trash/*``).

The trash is one folder under the cache volume shared by every user. The
manifest records each item's original path and, from now on, the id of the
user who trashed it. A non-owner sees -- and may restore or permanently
delete -- only items whose original location they may still act on (a full
folder grant, or WATCH/TARGET for a Clerk); an item with no recorded location
is visible only to the user who trashed it, and an item with neither is the
owner's alone. Emptying the trash as a non-owner purges only what they see.

Real files throughout: items are trashed with ``move_to_trash`` into a trash
folder under ``tmp_path``, and every refusal is followed by a check that the
file is still in the trash. Each test logs in as exactly one user; items
"trashed by user A" are created before that login with the acting user
patched in, so no identity is carried between requests.
"""
import json
import os

import pytest

from core.database import (
    add_library,
    create_user,
    get_db_connection,
    get_libraries,
    get_user_by_username,
    set_user_folders,
    set_user_libraries,
)
from helpers.trash import MANIFEST_FILENAME


def _login(client, username, password):
    resp = client.post("/login", data={"username": username, "password": password})
    assert resp.status_code == 302


def _manifest(trash_dir):
    path = os.path.join(trash_dir, MANIFEST_FILENAME)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def layout(app, db_connection, monkeypatch, tmp_path):
    monkeypatch.delenv("CLU_USERNAME", raising=False)
    monkeypatch.delenv("CLU_PASSWORD", raising=False)

    root = tmp_path / "LibA"
    marvel = root / "Marvel"
    dc = root / "DC"
    target = tmp_path / "processed"
    watch = tmp_path / "watch"
    trash = tmp_path / "trash"
    for d in (marvel, dc, target, watch, trash):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("core.config.get_target_dir", lambda: str(target))
    monkeypatch.setattr("core.config.get_watch_dir", lambda: str(watch))
    monkeypatch.setitem(app.config, "TRASH_ENABLED", True)
    monkeypatch.setitem(app.config, "TRASH_DIR", str(trash))
    monkeypatch.setitem(app.config, "TRASH_MAX_SIZE_MB", 1024)

    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM libraries")
        conn.commit()
    finally:
        conn.close()
    assert add_library("Library A", str(root))

    class L:
        pass

    L.root, L.marvel, L.dc = str(root), str(marvel), str(dc)
    L.target, L.watch, L.trash = str(target), str(watch), str(trash)
    L.lib_id = get_libraries(enabled_only=True)[0]["id"]
    return L


@pytest.fixture
def users(layout):
    """Owner; clerk_a granted DC; clerk_b and reader_b granted Marvel only."""
    create_user("tr_owner", password="ownerpass", role="owner")
    ids = {}
    for name, role, grant in (
        ("tr_clerk_a", "clerk", layout.dc),
        ("tr_clerk_b", "clerk", layout.marvel),
        ("tr_reader_b", "reader", layout.marvel),
    ):
        create_user(name, password="pw-" + name, role=role)
        uid = get_user_by_username(name)["id"]
        set_user_libraries(uid, [layout.lib_id])
        set_user_folders(uid, [grant])
        ids[name] = uid
    layout.ids = ids
    return layout


def _trash_as(app, monkeypatch, user_id, path, content=b"comic"):
    """Create ``path`` and move it to the trash as ``user_id``."""
    from helpers.trash import move_to_trash

    os.makedirs(os.path.dirname(path), exist_ok=True)
    # A sibling keeps the folder from being tidied away with its last item.
    keep = os.path.join(os.path.dirname(path), "keep.txt")
    if not os.path.exists(keep):
        with open(keep, "w") as f:
            f.write("x")
    with open(path, "wb") as f:
        f.write(content)
    monkeypatch.setattr("helpers.trash._acting_user_id", lambda: user_id)
    with app.app_context():
        result = move_to_trash(path)
    assert result["trashed"]
    return os.path.basename(result["path"])


@pytest.fixture
def trashed(app, monkeypatch, users):
    """dc_item trashed by clerk_a from DC; marvel_item trashed by clerk_a
    from Marvel; legacy_item sits in the trash with no manifest entry;
    staging_item came from TARGET; mine_item has no original path but was
    trashed by clerk_b."""
    a = users.ids["tr_clerk_a"]
    users.dc_item = _trash_as(app, monkeypatch, a, os.path.join(users.dc, "Batman 001.cbz"))
    users.marvel_item = _trash_as(app, monkeypatch, a, os.path.join(users.marvel, "XMen 001.cbz"))
    users.staging_item = _trash_as(app, monkeypatch, a, os.path.join(users.target, "Incoming 001.cbz"))
    monkeypatch.setattr("helpers.trash._acting_user_id", lambda: None)

    users.legacy_item = "Legacy 001.cbz"
    with open(os.path.join(users.trash, users.legacy_item), "wb") as f:
        f.write(b"legacy")

    users.mine_item = "Mine 001.cbz"
    with open(os.path.join(users.trash, users.mine_item), "wb") as f:
        f.write(b"mine")
    manifest = _manifest(users.trash)
    manifest[users.mine_item] = {"deleted_at": 1000, "trashed_by": users.ids["tr_clerk_b"]}
    with open(os.path.join(users.trash, MANIFEST_FILENAME), "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    return users


def _in_trash(layout, name):
    return os.path.exists(os.path.join(layout.trash, name))


def _listed(client):
    resp = client.get("/api/trash/list")
    assert resp.status_code == 200
    return {it["name"] for it in resp.get_json()["items"]}


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------
class TestTrashedByRecorded:
    def test_delete_route_records_the_acting_user(self, client, users):
        victim = os.path.join(users.dc, "Batman 002.cbz")
        with open(os.path.join(users.dc, "keep.txt"), "w") as f:
            f.write("x")
        with open(victim, "wb") as f:
            f.write(b"comic")
        _login(client, "tr_clerk_a", "pw-tr_clerk_a")
        resp = client.post("/delete", json={"target": victim})
        assert resp.status_code == 200
        assert resp.get_json()["trashed"] is True
        entry = _manifest(users.trash)["Batman 002.cbz"]
        assert entry["trashed_by"] == users.ids["tr_clerk_a"]
        assert os.path.normpath(entry["original_path"]) == os.path.normpath(victim)

    def test_no_request_records_no_user(self, app, layout):
        from helpers.trash import move_to_trash

        path = os.path.join(layout.dc, "Background 001.cbz")
        with open(os.path.join(layout.dc, "keep.txt"), "w") as f:
            f.write("x")
        with open(path, "wb") as f:
            f.write(b"comic")
        with app.app_context():
            assert move_to_trash(path)["trashed"]
        assert "trashed_by" not in _manifest(layout.trash)["Background 001.cbz"]


# ---------------------------------------------------------------------------
# A Clerk without the grant (clerk_b holds Marvel, not DC)
# ---------------------------------------------------------------------------
class TestClerkWithoutGrant:
    def _login(self, client):
        _login(client, "tr_clerk_b", "pw-tr_clerk_b")

    def test_list_shows_only_items_from_granted_folders(self, client, trashed):
        self._login(client)
        assert _listed(client) == {trashed.marvel_item, trashed.staging_item, trashed.mine_item}

    def test_info_counts_only_visible_items(self, client, trashed):
        self._login(client)
        assert client.get("/api/trash/info").get_json()["item_count"] == 3

    def test_restore_ungranted_item_is_not_found(self, client, trashed):
        self._login(client)
        resp = client.post("/api/trash/restore", json={"name": trashed.dc_item})
        assert resp.status_code == 404
        assert _in_trash(trashed, trashed.dc_item)
        assert not os.path.exists(os.path.join(trashed.dc, "Batman 001.cbz"))

    def test_restore_legacy_item_is_not_found(self, client, trashed):
        self._login(client)
        resp = client.post("/api/trash/restore", json={"name": trashed.legacy_item})
        assert resp.status_code == 404
        assert _in_trash(trashed, trashed.legacy_item)

    def test_permanent_delete_ungranted_item_is_not_found(self, client, trashed):
        self._login(client)
        resp = client.post("/api/trash/delete", json={"name": trashed.dc_item})
        assert resp.status_code == 404
        assert _in_trash(trashed, trashed.dc_item)
        assert trashed.dc_item in _manifest(trashed.trash)

    def test_empty_purges_only_visible_items(self, client, trashed):
        self._login(client)
        resp = client.post("/api/trash/empty")
        assert resp.status_code == 200
        assert resp.get_json()["count"] == 3
        assert _in_trash(trashed, trashed.dc_item)
        assert _in_trash(trashed, trashed.legacy_item)
        assert not _in_trash(trashed, trashed.marvel_item)
        assert not _in_trash(trashed, trashed.staging_item)
        assert not _in_trash(trashed, trashed.mine_item)
        # The surviving item can still be restored by someone who may see it.
        assert trashed.dc_item in _manifest(trashed.trash)

    def test_restore_granted_item(self, client, trashed):
        self._login(client)
        resp = client.post("/api/trash/restore", json={"name": trashed.marvel_item})
        assert resp.status_code == 200
        assert os.path.exists(os.path.join(trashed.marvel, "XMen 001.cbz"))
        assert not _in_trash(trashed, trashed.marvel_item)

    def test_permanent_delete_granted_item(self, client, trashed):
        self._login(client)
        resp = client.post("/api/trash/delete", json={"name": trashed.marvel_item})
        assert resp.status_code == 200
        assert not _in_trash(trashed, trashed.marvel_item)

    def test_restore_refused_when_destination_not_writable(self, client, trashed, monkeypatch):
        # Visible, but the destination check refuses: nothing moves.
        monkeypatch.setattr(
            "routes.files.file_op_refusal", lambda *p: ("Access denied", 403))
        self._login(client)
        resp = client.post("/api/trash/restore", json={"name": trashed.marvel_item})
        assert resp.status_code == 403
        assert _in_trash(trashed, trashed.marvel_item)


# ---------------------------------------------------------------------------
# The Clerk who holds the grant (clerk_a holds DC)
# ---------------------------------------------------------------------------
class TestClerkWithGrant:
    def test_list_and_restore_own_folder(self, client, trashed):
        _login(client, "tr_clerk_a", "pw-tr_clerk_a")
        # marvel_item was trashed by clerk_a, but a recorded location wins:
        # clerk_a holds no Marvel grant now, so it is not theirs to restore.
        assert _listed(client) == {trashed.dc_item, trashed.staging_item}
        resp = client.post("/api/trash/restore", json={"name": trashed.dc_item})
        assert resp.status_code == 200
        assert os.path.exists(os.path.join(trashed.dc, "Batman 001.cbz"))


# ---------------------------------------------------------------------------
# A Reader (reader_b holds Marvel)
# ---------------------------------------------------------------------------
class TestReader:
    def test_list_shows_granted_library_items_but_not_staging(self, client, trashed):
        _login(client, "tr_reader_b", "pw-tr_reader_b")
        assert _listed(client) == {trashed.marvel_item}

    def test_reader_cannot_empty(self, client, trashed):
        _login(client, "tr_reader_b", "pw-tr_reader_b")
        resp = client.post("/api/trash/empty")
        assert resp.status_code == 403
        for name in (trashed.dc_item, trashed.marvel_item, trashed.legacy_item):
            assert _in_trash(trashed, name)

    def test_reader_cannot_restore(self, client, trashed):
        _login(client, "tr_reader_b", "pw-tr_reader_b")
        resp = client.post("/api/trash/restore", json={"name": trashed.marvel_item})
        assert resp.status_code == 403
        assert _in_trash(trashed, trashed.marvel_item)


# ---------------------------------------------------------------------------
# Owner and single-user installs: unchanged
# ---------------------------------------------------------------------------
class TestOwner:
    def test_owner_lists_everything(self, client, trashed):
        _login(client, "tr_owner", "ownerpass")
        assert _listed(client) == {
            trashed.dc_item, trashed.marvel_item, trashed.staging_item,
            trashed.legacy_item, trashed.mine_item,
        }

    def test_owner_restores_any_item(self, client, trashed):
        _login(client, "tr_owner", "ownerpass")
        resp = client.post("/api/trash/restore", json={"name": trashed.dc_item})
        assert resp.status_code == 200
        assert os.path.exists(os.path.join(trashed.dc, "Batman 001.cbz"))

    def test_owner_empties_everything(self, client, trashed):
        _login(client, "tr_owner", "ownerpass")
        resp = client.post("/api/trash/empty")
        assert resp.status_code == 200
        assert resp.get_json()["count"] == 5
        assert [n for n in os.listdir(trashed.trash) if n != MANIFEST_FILENAME] == []


class TestSingleUser:
    def test_lists_and_empties_everything(self, app, client, layout, monkeypatch):
        name = _trash_as(app, monkeypatch, None, os.path.join(layout.dc, "Solo 001.cbz"))
        with open(os.path.join(layout.trash, "Legacy 001.cbz"), "wb") as f:
            f.write(b"legacy")
        assert _listed(client) == {name, "Legacy 001.cbz"}
        resp = client.post("/api/trash/empty")
        assert resp.status_code == 200
        assert resp.get_json()["count"] == 2
