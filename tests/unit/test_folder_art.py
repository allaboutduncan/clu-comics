"""core/folder_art.py: folder covers, headers and overlays in the cache volume.

#720: folder art used to be written into the library folder, so generating or
viewing it woke the data disks. These pin the store's contract -- what is
written where, which art wins, what follows a move, and what the disk is still
asked (once) about.
"""

import os
import time
from io import BytesIO
from unittest.mock import patch

import pytest
from PIL import Image

from core import folder_art


def _png(color="red"):
    buf = BytesIO()
    Image.new("RGB", (4, 6), color).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def store(db_connection, tmp_path):
    """A fresh schema and a library folder to hang art on."""
    folder = tmp_path / "data" / "Batman"
    folder.mkdir(parents=True)
    with patch("core.database.get_db_path", return_value=str(tmp_path / "test_comic_utils.db")):
        yield folder


class TestPutAndGet:

    def test_round_trip(self, store):
        rec = folder_art.put_art(str(store), "folder", _png(), ".png", "uploaded")
        assert rec["location"] == "cache"
        got = folder_art.get_art(str(store), "folder")
        assert got["path"] == rec["path"]
        assert got["source"] == "uploaded"
        with open(got["path"], "rb") as fh:
            assert fh.read() == _png()

    def test_nothing_is_written_into_the_folder(self, store):
        folder_art.put_art(str(store), "folder", _png(), ".png", "generated")
        folder_art.put_art(str(store), "header", _png(), ".jpg", "uploaded")
        assert list(store.iterdir()) == []

    def test_lives_under_the_cache_root_at_depth_two(self, store, tmp_path):
        rec = folder_art.put_art(str(store), "folder", _png(), ".png", "generated")
        rel = os.path.relpath(rec["path"], str(tmp_path / "cache"))
        parts = rel.split(os.sep)
        assert parts[0] == "folder_art"
        assert len(parts) == 3, "shard dirs must stay within entrypoint's -maxdepth 2"

    def test_a_pil_image_is_encoded(self, store):
        rec = folder_art.put_art(
            str(store), "folder", Image.new("RGBA", (200, 300)), ".png", "generated")
        with Image.open(rec["path"]) as img:
            assert img.size == (200, 300)

    def test_replacing_removes_the_old_file(self, store):
        first = folder_art.put_art(str(store), "folder", _png("red"), ".png", "generated")
        second = folder_art.put_art(str(store), "folder", _png("blue"), ".png", "uploaded")
        assert first["path"] != second["path"]
        assert not os.path.exists(first["path"])
        assert folder_art.get_art(str(store), "folder")["source"] == "uploaded"

    def test_kinds_are_independent(self, store):
        folder_art.put_art(str(store), "folder", _png(), ".png", "generated")
        folder_art.put_art(str(store), "header", _png(), ".gif", "uploaded")
        assert folder_art.get_art(str(store), "folder")
        assert folder_art.get_art(str(store), "header")
        assert folder_art.get_art(str(store), "overlay") is None

    @pytest.mark.parametrize("kind,ext", [
        ("overlay", ".jpg"), ("folder", ".bmp"), ("bogus", ".png"),
    ])
    def test_refuses_what_a_kind_cannot_be(self, store, kind, ext):
        assert folder_art.put_art(str(store), kind, _png(), ext, "uploaded") is None
        assert not os.path.isdir(folder_art.art_root()) or not any(
            files for _, _, files in os.walk(folder_art.art_root()))

    def test_a_row_whose_file_vanished_is_dropped(self, store):
        rec = folder_art.put_art(str(store), "folder", _png(), ".png", "generated")
        os.unlink(rec["path"])
        assert folder_art.get_art(str(store), "folder") is None

    def test_delete(self, store):
        rec = folder_art.put_art(str(store), "folder", _png(), ".png", "generated")
        assert folder_art.delete_art(str(store)) == 1
        assert folder_art.get_art(str(store), "folder") is None
        assert not os.path.exists(rec["path"])


class TestKindForFilename:

    @pytest.mark.parametrize("name,kind", [
        ("folder.png", "folder"), ("FOLDER.JPG", "folder"), ("folder.webp", "folder"),
        ("header.png", "header"), ("header.gif", "header"), ("header.jpg", "header"),
        ("overlay.png", "overlay"),
        ("overlay.jpg", None), ("page01.png", None), ("folder.cbz", None),
        ("cover.jpg", None),
    ])
    def test_kinds(self, name, kind):
        assert folder_art.kind_for_filename(name) == kind


class TestLegacyDiskArt:

    def test_disk_art_is_found(self, store):
        (store / "folder.jpg").write_bytes(_png())
        rec = folder_art.find_art(str(store), "folder")
        assert rec["location"] == "disk"
        assert rec["path"] == str(store / "folder.jpg")

    def test_cache_wins_over_disk(self, store):
        (store / "folder.png").write_bytes(b"old")
        folder_art.put_art(str(store), "folder", _png(), ".png", "generated")
        assert folder_art.find_art(str(store), "folder")["location"] == "cache"

    def test_import_copies_and_keeps_the_original(self, store):
        disk = store / "header.jpg"
        disk.write_bytes(_png())
        rec = folder_art.import_disk_art(str(store), "header", str(disk))
        assert rec["source"] == "imported"
        assert rec["path"].endswith(".jpg")
        assert disk.exists()

    def test_after_import_the_disk_is_not_asked(self, store):
        disk = store / "folder.png"
        disk.write_bytes(_png())
        folder_art.import_disk_art(str(store), "folder", str(disk))

        real_isfile = os.path.isfile
        asked = []

        def spy(p):
            if str(store) in str(p):
                asked.append(p)
            return real_isfile(p)

        with patch("core.folder_art.os.path.isfile", side_effect=spy):
            assert folder_art.find_art(str(store), "folder")["location"] == "cache"
        assert asked == []

    def test_a_disk_miss_is_remembered(self, store):
        assert folder_art.find_art(str(store), "folder") is None
        (store / "folder.png").write_bytes(_png())
        # Within the TTL the miss stands: no stat on the data disk.
        assert folder_art.find_art(str(store), "folder") is None

    def test_the_miss_memo_expires(self, store):
        assert folder_art.find_art(str(store), "folder") is None
        (store / "folder.png").write_bytes(_png())
        later = time.monotonic() + folder_art.DISK_MISS_TTL_SECONDS + 1
        with patch("core.folder_art.time.monotonic", return_value=later):
            assert folder_art.find_art(str(store), "folder")["location"] == "disk"

    def test_storing_art_clears_the_miss_memo(self, store):
        assert folder_art.find_art(str(store), "header") is None
        folder_art.put_art(str(store), "header", _png(), ".png", "uploaded")
        assert folder_art.find_art(str(store), "header")["location"] == "cache"


class TestBatch:

    def test_one_query_for_cached_and_disk_for_the_rest(self, store, tmp_path):
        a, b, c = store, tmp_path / "data" / "Superman", tmp_path / "data" / "Flash"
        b.mkdir()
        c.mkdir()
        folder_art.put_art(str(a), "folder", _png(), ".png", "generated")
        (b / "folder.gif").write_bytes(b"gif")

        found = folder_art.find_art_batch([str(a), str(b), str(c)])
        assert found[str(a)]["location"] == "cache"
        assert found[str(b)]["location"] == "disk"
        assert found[str(c)] is None

    def test_folders_with_cached_art(self, store):
        folder_art.put_art(str(store), "folder", _png(), ".png", "generated")
        folder_art.put_art(str(store), "header", _png(), ".png", "uploaded")
        assert folder_art.folders_with_cached_art() == {str(store)}


class TestFollowsMoves:
    """A folder rename is a row update; the art (often an upload) must follow."""

    # POSIX path strings, as in production: the subtree prefix is "{old}/%",
    # and nothing here needs the folders to exist -- the store keys on the
    # string.

    def test_the_folder_itself_and_its_subtree(self, store):
        from core.database import move_path_references

        folder_art.put_art("/data/Batman", "folder", _png(), ".png", "uploaded")
        folder_art.put_art("/data/Batman/Vol 1", "folder", _png(), ".png", "generated")

        assert move_path_references("/data/Batman", "/data/Batman (2016)", is_dir=True)

        assert folder_art.get_art("/data/Batman (2016)", "folder")["source"] == "uploaded"
        assert folder_art.get_art("/data/Batman (2016)/Vol 1", "folder")
        assert folder_art.get_art("/data/Batman", "folder") is None

    def test_a_sibling_with_a_common_prefix_stays(self, store):
        from core.database import move_path_references

        folder_art.put_art("/data/Batman Beyond", "folder", _png(), ".png", "uploaded")
        move_path_references("/data/Batman", "/data/Bats", is_dir=True)
        assert folder_art.get_art("/data/Batman Beyond", "folder")

    def test_an_underscore_is_not_a_wildcard(self, store):
        from core.database import move_path_references

        folder_art.put_art("/data/X-Men/Vol 1", "folder", _png(), ".png", "uploaded")
        move_path_references("/data/X_Men", "/data/Mutants", is_dir=True)
        assert folder_art.get_art("/data/X-Men/Vol 1", "folder")

    def test_a_file_move_leaves_folder_art_alone(self, store):
        from core.database import move_path_references

        folder_art.put_art("/data/Batman", "folder", _png(), ".png", "uploaded")
        move_path_references("/data/Batman", "/data/elsewhere", is_dir=False)
        assert folder_art.get_art("/data/Batman", "folder")


class TestPruneOrphans:

    def test_a_provably_gone_folder_loses_its_art(self, store, tmp_path):
        keep = tmp_path / "data" / "Superman"
        keep.mkdir()
        gone = folder_art.put_art(str(store), "folder", _png(), ".png", "uploaded")
        folder_art.put_art(str(keep), "folder", _png(), ".png", "uploaded")
        store.rmdir()

        with patch("helpers.library.get_library_roots",
                   return_value=[str(tmp_path / "data")]):
            rows, _ = folder_art.prune_orphans()

        assert rows == 1
        assert not os.path.exists(gone["path"])
        assert folder_art.get_art(str(keep), "folder")

    def test_an_unmounted_library_keeps_its_art(self, store, tmp_path):
        """An empty root reads as unmounted: absence is not proof (sleeping NAS)."""
        folder_art.put_art(str(store), "folder", _png(), ".png", "uploaded")
        store.rmdir()  # the library root is now empty

        with patch("helpers.library.get_library_roots",
                   return_value=[str(tmp_path / "data")]):
            rows, _ = folder_art.prune_orphans()
        assert rows == 0

    def test_old_unreferenced_files_are_removed_new_ones_kept(self, store):
        folder_art.put_art(str(store), "folder", _png(), ".png", "generated")
        shard = os.path.join(folder_art.art_root(), "ab")
        os.makedirs(shard, exist_ok=True)
        stale = os.path.join(shard, "abandoned.png")
        fresh = os.path.join(shard, "inflight.png")
        for p in (stale, fresh):
            with open(p, "wb") as fh:
                fh.write(b"x")
        old = time.time() - folder_art.ORPHAN_FILE_MIN_AGE_SECONDS - 10
        os.utime(stale, (old, old))

        with patch("helpers.library.get_library_roots", return_value=[]):
            _, files = folder_art.prune_orphans()

        assert files == 1
        assert not os.path.exists(stale)
        assert os.path.exists(fresh)
        assert folder_art.get_art(str(store), "folder")
