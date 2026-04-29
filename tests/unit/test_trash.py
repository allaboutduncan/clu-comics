"""Tests for helpers/trash.py — Trash Can module."""

import json
import os
import time
import pytest
from flask import Flask


@pytest.fixture
def trash_dir(tmp_path):
    """Create a temporary trash directory."""
    d = tmp_path / "trash"
    d.mkdir()
    return d


@pytest.fixture
def app(trash_dir, tmp_path):
    """Flask app with trash config and active app context."""
    flask_app = Flask(__name__)
    flask_app.config["TRASH_ENABLED"] = True
    flask_app.config["TRASH_DIR"] = str(trash_dir)
    flask_app.config["TRASH_MAX_SIZE_MB"] = 1024
    flask_app.config["CACHE_DIR"] = str(tmp_path / "cache")
    flask_app.config["DATA_DIR"] = str(tmp_path / "data")
    return flask_app


@pytest.fixture
def disabled_app(tmp_path):
    """Flask app with trash disabled."""
    flask_app = Flask(__name__)
    flask_app.config["TRASH_ENABLED"] = False
    flask_app.config["TRASH_DIR"] = ""
    flask_app.config["TRASH_MAX_SIZE_MB"] = 1024
    flask_app.config["CACHE_DIR"] = str(tmp_path / "cache")
    return flask_app


class TestMoveToTrash:

    def test_move_file_to_trash(self, app, trash_dir, tmp_path):
        """File is moved to trash dir, original is gone."""
        source = tmp_path / "comic.cbz"
        source.write_bytes(b"x" * 100)

        with app.app_context():
            from helpers.trash import move_to_trash
            result = move_to_trash(str(source))

        assert result["trashed"] is True
        assert not source.exists()
        assert os.path.exists(os.path.join(str(trash_dir), "comic.cbz"))

    def test_move_to_trash_disabled(self, disabled_app, tmp_path):
        """When disabled, file is permanently deleted."""
        source = tmp_path / "comic.cbz"
        source.write_bytes(b"data")

        with disabled_app.app_context():
            from helpers.trash import move_to_trash
            result = move_to_trash(str(source))

        assert result["trashed"] is False
        assert not source.exists()

    def test_collision_handling(self, app, trash_dir, tmp_path):
        """Duplicate names get timestamp suffix."""
        existing = trash_dir / "comic.cbz"
        existing.write_bytes(b"old")

        source = tmp_path / "comic.cbz"
        source.write_bytes(b"new")

        with app.app_context():
            from helpers.trash import move_to_trash
            result = move_to_trash(str(source))

        assert result["trashed"] is True
        assert not source.exists()
        trash_files = [f for f in os.listdir(str(trash_dir)) if f != "trash_manifest.json"]
        assert len(trash_files) == 2
        assert "comic.cbz" in trash_files

    def test_size_enforcement(self, tmp_path):
        """Oldest items are evicted when over limit."""
        trash_dir = tmp_path / "trash"
        trash_dir.mkdir()

        flask_app = Flask(__name__)
        flask_app.config["TRASH_ENABLED"] = True
        flask_app.config["TRASH_DIR"] = str(trash_dir)
        flask_app.config["TRASH_MAX_SIZE_MB"] = 0  # 0 bytes max — force eviction
        flask_app.config["CACHE_DIR"] = str(tmp_path / "cache")

        old_file = trash_dir / "old.cbz"
        old_file.write_bytes(b"old data")

        source = tmp_path / "new.cbz"
        source.write_bytes(b"new")

        with flask_app.app_context():
            from helpers.trash import move_to_trash
            result = move_to_trash(str(source))

        assert result["trashed"] is True
        assert not old_file.exists()

    def test_move_directory_to_trash(self, app, trash_dir, tmp_path):
        """Directories work too."""
        source_dir = tmp_path / "Batman"
        source_dir.mkdir()
        (source_dir / "issue1.cbz").write_bytes(b"data")
        (source_dir / "issue2.cbz").write_bytes(b"data")

        with app.app_context():
            from helpers.trash import move_to_trash
            result = move_to_trash(str(source_dir))

        assert result["trashed"] is True
        assert not source_dir.exists()
        assert os.path.isdir(os.path.join(str(trash_dir), "Batman"))

    def test_empty_dir_deleted_not_trashed(self, app, trash_dir, tmp_path):
        """Empty directories are deleted directly, not moved to trash."""
        source_dir = tmp_path / "EmptySeries"
        source_dir.mkdir()

        with app.app_context():
            from helpers.trash import move_to_trash
            result = move_to_trash(str(source_dir))

        assert result["trashed"] is False
        assert not source_dir.exists()
        assert not os.path.exists(os.path.join(str(trash_dir), "EmptySeries"))

    def test_cvinfo_only_dir_deleted_not_trashed(self, app, trash_dir, tmp_path):
        """Directories with only cvinfo are deleted directly, not trashed."""
        source_dir = tmp_path / "CvinfoSeries"
        source_dir.mkdir()
        (source_dir / "cvinfo").write_text("https://comicvine.gamespot.com/test/4050-123/")

        with app.app_context():
            from helpers.trash import move_to_trash
            result = move_to_trash(str(source_dir))

        assert result["trashed"] is False
        assert not source_dir.exists()
        assert not os.path.exists(os.path.join(str(trash_dir), "CvinfoSeries"))


class TestEmptyTrash:

    def test_empty_trash(self, app, trash_dir):
        """All items removed, correct count/size returned."""
        (trash_dir / "a.cbz").write_bytes(b"x" * 100)
        (trash_dir / "b.cbz").write_bytes(b"y" * 200)

        with app.app_context():
            from helpers.trash import empty_trash
            result = empty_trash()

        assert result["count"] == 2
        assert result["size_freed"] == 300
        assert os.listdir(str(trash_dir)) == []


class TestPermanentlyDeleteFromTrash:

    def test_delete_specific_item(self, app, trash_dir):
        """Specific item is removed."""
        target = trash_dir / "delete_me.cbz"
        target.write_bytes(b"x" * 50)
        (trash_dir / "keep_me.cbz").write_bytes(b"y" * 50)

        with app.app_context():
            from helpers.trash import permanently_delete_from_trash
            result = permanently_delete_from_trash("delete_me.cbz")

        assert result["success"] is True
        assert result["size_freed"] == 50
        assert not target.exists()
        assert (trash_dir / "keep_me.cbz").exists()

    def test_delete_nonexistent_item(self, app, trash_dir):
        """Returns error for missing item."""
        with app.app_context():
            from helpers.trash import permanently_delete_from_trash
            result = permanently_delete_from_trash("nope.cbz")

        assert result["success"] is False
        assert "not found" in result["error"].lower()


class TestIsTrashPath:

    def test_identifies_trash_paths(self, app, trash_dir):
        """Correctly identifies paths within trash."""
        with app.app_context():
            from helpers.trash import is_trash_path

            assert is_trash_path(str(trash_dir)) is True
            assert is_trash_path(os.path.join(str(trash_dir), "file.cbz")) is True
            assert is_trash_path("/some/other/path") is False

    def test_disabled_returns_false(self, disabled_app):
        """When trash disabled, all paths return False."""
        with disabled_app.app_context():
            from helpers.trash import is_trash_path
            assert is_trash_path("/cache/trash/file.cbz") is False


class TestGetTrashContents:

    def test_returns_sorted_items(self, app, trash_dir):
        """Items are sorted oldest first."""
        f1 = trash_dir / "old.cbz"
        f1.write_bytes(b"old")
        old_time = time.time() - 3600
        os.utime(str(f1), (old_time, old_time))

        f2 = trash_dir / "new.cbz"
        f2.write_bytes(b"newer data")

        with app.app_context():
            from helpers.trash import get_trash_contents
            contents = get_trash_contents()

        assert len(contents) == 2
        assert contents[0]["name"] == "old.cbz"
        assert contents[1]["name"] == "new.cbz"
        assert contents[0]["size"] == 3
        assert contents[1]["size"] == 10


class TestCleanupEmptyParent:

    def test_removes_empty_folder(self, app, trash_dir, tmp_path):
        """Parent folder is removed when last file is trashed."""
        series_dir = tmp_path / "data" / "publisher" / "Batman"
        series_dir.mkdir(parents=True)
        comic = series_dir / "issue1.cbz"
        comic.write_bytes(b"data")

        with app.app_context():
            from helpers.trash import move_to_trash
            move_to_trash(str(comic))

        assert not series_dir.exists()

    def test_removes_folder_with_only_cvinfo(self, app, trash_dir, tmp_path):
        """Parent folder is removed when only cvinfo remains."""
        series_dir = tmp_path / "data" / "publisher" / "Batman"
        series_dir.mkdir(parents=True)
        (series_dir / "cvinfo").write_text("https://comicvine.gamespot.com/batman/4050-796/")
        comic = series_dir / "issue1.cbz"
        comic.write_bytes(b"data")

        with app.app_context():
            from helpers.trash import move_to_trash
            move_to_trash(str(comic))

        assert not series_dir.exists()

    def test_keeps_folder_with_other_files(self, app, trash_dir, tmp_path):
        """Parent folder is kept when other comic files remain."""
        series_dir = tmp_path / "data" / "publisher" / "Batman"
        series_dir.mkdir(parents=True)
        (series_dir / "issue1.cbz").write_bytes(b"data1")
        (series_dir / "issue2.cbz").write_bytes(b"data2")

        with app.app_context():
            from helpers.trash import move_to_trash
            move_to_trash(str(series_dir / "issue1.cbz"))

        assert series_dir.exists()
        assert (series_dir / "issue2.cbz").exists()

    def test_does_not_remove_data_dir(self, app, trash_dir, tmp_path):
        """DATA_DIR root is never removed even if empty."""
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True)
        comic = data_dir / "comic.cbz"
        comic.write_bytes(b"data")

        with app.app_context():
            from helpers.trash import move_to_trash
            move_to_trash(str(comic))

        assert data_dir.exists()


class TestManifest:

    def test_move_to_trash_records_manifest(self, app, trash_dir, tmp_path):
        """Trashing a file creates a manifest entry with original_path."""
        source = tmp_path / "comic.cbz"
        source.write_bytes(b"data")

        with app.app_context():
            from helpers.trash import move_to_trash, MANIFEST_FILENAME
            move_to_trash(str(source))

            manifest_path = trash_dir / MANIFEST_FILENAME
            assert manifest_path.exists()
            manifest = json.loads(manifest_path.read_text())
            assert "comic.cbz" in manifest
            assert manifest["comic.cbz"]["original_path"] == str(source)
            assert "deleted_at" in manifest["comic.cbz"]

    def test_collision_records_suffixed_name(self, app, trash_dir, tmp_path):
        """Collision-suffixed filenames are keyed correctly in manifest."""
        existing = trash_dir / "comic.cbz"
        existing.write_bytes(b"old")

        source = tmp_path / "comic.cbz"
        source.write_bytes(b"new")

        with app.app_context():
            from helpers.trash import move_to_trash, _load_manifest
            move_to_trash(str(source))

            manifest = _load_manifest()
            # Should have the collision-suffixed entry, not "comic.cbz"
            assert len(manifest) == 1
            key = list(manifest.keys())[0]
            assert key.startswith("comic_")
            assert key.endswith(".cbz")
            assert manifest[key]["original_path"] == str(source)

    def test_empty_trash_clears_manifest(self, app, trash_dir):
        """Emptying trash removes the manifest file."""
        (trash_dir / "a.cbz").write_bytes(b"data")
        manifest_path = trash_dir / "trash_manifest.json"
        manifest_path.write_text(json.dumps({"a.cbz": {"original_path": "/data/a.cbz", "deleted_at": 1000}}))

        with app.app_context():
            from helpers.trash import empty_trash, MANIFEST_FILENAME
            empty_trash()

            assert not manifest_path.exists()

    def test_permanently_delete_removes_entry(self, app, trash_dir):
        """Permanently deleting one item removes only its manifest entry."""
        (trash_dir / "a.cbz").write_bytes(b"data")
        (trash_dir / "b.cbz").write_bytes(b"data")
        manifest_path = trash_dir / "trash_manifest.json"
        manifest_path.write_text(json.dumps({
            "a.cbz": {"original_path": "/data/a.cbz", "deleted_at": 1000},
            "b.cbz": {"original_path": "/data/b.cbz", "deleted_at": 1001},
        }))

        with app.app_context():
            from helpers.trash import permanently_delete_from_trash, _load_manifest
            permanently_delete_from_trash("a.cbz")

            manifest = _load_manifest()
            assert "a.cbz" not in manifest
            assert "b.cbz" in manifest

    def test_load_manifest_handles_corruption(self, app, trash_dir):
        """Corrupt manifest returns empty dict without crashing."""
        manifest_path = trash_dir / "trash_manifest.json"
        manifest_path.write_text("NOT VALID JSON {{{{")

        with app.app_context():
            from helpers.trash import _load_manifest
            manifest = _load_manifest()
            assert manifest == {}

    def test_manifest_excluded_from_contents(self, app, trash_dir):
        """Manifest file is not listed in get_trash_contents()."""
        (trash_dir / "comic.cbz").write_bytes(b"data")
        manifest_path = trash_dir / "trash_manifest.json"
        manifest_path.write_text(json.dumps({"comic.cbz": {"original_path": "/data/comic.cbz", "deleted_at": 1000}}))

        with app.app_context():
            from helpers.trash import get_trash_contents
            contents = get_trash_contents()
            names = [item["name"] for item in contents]
            assert "trash_manifest.json" not in names
            assert "comic.cbz" in names


class TestRestoreFromTrash:

    def test_restore_file(self, app, trash_dir, tmp_path):
        """File is moved back to original location and manifest entry removed."""
        original_dir = tmp_path / "data" / "DC" / "Batman"
        original_dir.mkdir(parents=True)
        original_path = str(original_dir / "issue1.cbz")

        trashed = trash_dir / "issue1.cbz"
        trashed.write_bytes(b"comic data")
        manifest_path = trash_dir / "trash_manifest.json"
        manifest_path.write_text(json.dumps({
            "issue1.cbz": {"original_path": original_path, "deleted_at": 1000}
        }))

        with app.app_context():
            from helpers.trash import restore_from_trash, _load_manifest
            result = restore_from_trash("issue1.cbz")

        assert result["success"] is True
        assert result["restored_path"] == original_path
        assert os.path.exists(original_path)
        assert not trashed.exists()

        with app.app_context():
            manifest = _load_manifest()
            assert "issue1.cbz" not in manifest

    def test_restore_recreates_parent_dirs(self, app, trash_dir, tmp_path):
        """Parent directories are recreated if they were cleaned up."""
        original_path = str(tmp_path / "data" / "DC" / "Batman" / "issue1.cbz")

        trashed = trash_dir / "issue1.cbz"
        trashed.write_bytes(b"comic data")
        manifest_path = trash_dir / "trash_manifest.json"
        manifest_path.write_text(json.dumps({
            "issue1.cbz": {"original_path": original_path, "deleted_at": 1000}
        }))

        with app.app_context():
            from helpers.trash import restore_from_trash
            result = restore_from_trash("issue1.cbz")

        assert result["success"] is True
        assert os.path.exists(original_path)

    def test_restore_conflict(self, app, trash_dir, tmp_path):
        """Returns conflict error when original path is occupied."""
        original_dir = tmp_path / "data" / "DC"
        original_dir.mkdir(parents=True)
        original_path = str(original_dir / "issue1.cbz")
        (original_dir / "issue1.cbz").write_bytes(b"new version")

        trashed = trash_dir / "issue1.cbz"
        trashed.write_bytes(b"old version")
        manifest_path = trash_dir / "trash_manifest.json"
        manifest_path.write_text(json.dumps({
            "issue1.cbz": {"original_path": original_path, "deleted_at": 1000}
        }))

        with app.app_context():
            from helpers.trash import restore_from_trash
            result = restore_from_trash("issue1.cbz")

        assert result["success"] is False
        assert result.get("conflict") is True
        assert trashed.exists()  # Not moved

    def test_restore_no_manifest_entry(self, app, trash_dir):
        """Returns no_manifest error for untracked items."""
        trashed = trash_dir / "orphan.cbz"
        trashed.write_bytes(b"data")

        with app.app_context():
            from helpers.trash import restore_from_trash
            result = restore_from_trash("orphan.cbz")

        assert result["success"] is False
        assert result.get("no_manifest") is True
        assert trashed.exists()  # Not touched

    def test_restore_not_found(self, app, trash_dir):
        """Returns error for missing item."""
        with app.app_context():
            from helpers.trash import restore_from_trash
            result = restore_from_trash("nope.cbz")

        assert result["success"] is False
        assert "not found" in result["error"].lower()

    def test_restore_directory(self, app, trash_dir, tmp_path):
        """Directories can be restored too."""
        original_path = str(tmp_path / "data" / "DC" / "Batman")

        trashed_dir = trash_dir / "Batman"
        trashed_dir.mkdir()
        (trashed_dir / "issue1.cbz").write_bytes(b"data")

        manifest_path = trash_dir / "trash_manifest.json"
        manifest_path.write_text(json.dumps({
            "Batman": {"original_path": original_path, "deleted_at": 1000}
        }))

        with app.app_context():
            from helpers.trash import restore_from_trash
            result = restore_from_trash("Batman")

        assert result["success"] is True
        assert os.path.isdir(original_path)
        assert os.path.exists(os.path.join(original_path, "issue1.cbz"))
