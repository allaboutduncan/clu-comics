"""Unit tests for helpers.prune_empty_dirs (empty/junk folder sweep in TARGET)."""

import os

from helpers import prune_empty_dirs


class TestPruneEmptyDirs:

    def test_removes_truly_empty_folder(self, tmp_path):
        root = tmp_path / "processed"
        empty = root / "Batman 1"
        empty.mkdir(parents=True)

        assert prune_empty_dirs(str(root)) == 1
        assert not empty.exists()
        assert root.exists()

    def test_keeps_folder_with_real_file(self, tmp_path):
        root = tmp_path / "processed"
        keep = root / "Series"
        keep.mkdir(parents=True)
        (keep / "issue.cbz").write_bytes(b"x")

        assert prune_empty_dirs(str(root)) == 0
        assert (keep / "issue.cbz").exists()

    def test_removes_folder_with_only_hidden_junk(self, tmp_path):
        # Use always-hidden names (leading '.'/'_') so the result does not depend
        # on the DB-backed configurable hidden-directory set.
        root = tmp_path / "processed"
        junk = root / "Batman 1"
        junk.mkdir(parents=True)
        (junk / ".DS_Store").write_bytes(b"x")
        hidden_sub = junk / ".thumbnails"
        hidden_sub.mkdir()
        (hidden_sub / "thumb.jpg").write_bytes(b"y")

        assert prune_empty_dirs(str(root)) == 1
        assert not junk.exists()

    def test_removes_folder_with_configured_hidden_dir(self, tmp_path, monkeypatch):
        # @eaDir-style names are only hidden when in the configured set; pin it.
        import helpers
        monkeypatch.setattr(helpers, "_hidden_directories", {"@eaDir"})
        root = tmp_path / "processed"
        junk = root / "Batman 1"
        eadir = junk / "@eaDir"
        eadir.mkdir(parents=True)
        (eadir / "thumb.jpg").write_bytes(b"y")

        assert prune_empty_dirs(str(root)) == 1
        assert not junk.exists()

    def test_collapses_nested_empty_folders(self, tmp_path):
        root = tmp_path / "processed"
        nested = root / "a" / "b" / "c"
        nested.mkdir(parents=True)

        removed = prune_empty_dirs(str(root))
        assert removed == 3
        assert not (root / "a").exists()
        assert root.exists()

    def test_never_removes_root_even_when_empty(self, tmp_path):
        root = tmp_path / "processed"
        root.mkdir()

        assert prune_empty_dirs(str(root)) == 0
        assert root.exists()

    def test_mixed_removes_only_empty(self, tmp_path):
        root = tmp_path / "processed"
        empty = root / "Emptied Wrapper"
        empty.mkdir(parents=True)
        populated = root / "Series"
        populated.mkdir()
        (populated / "issue.cbz").write_bytes(b"x")

        assert prune_empty_dirs(str(root)) == 1
        assert not empty.exists()
        assert populated.exists()

    def test_missing_root_is_noop(self, tmp_path):
        assert prune_empty_dirs(str(tmp_path / "does-not-exist")) == 0
