"""Tests for helpers/trash.py — Trash Can module."""

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
        trash_files = os.listdir(str(trash_dir))
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
