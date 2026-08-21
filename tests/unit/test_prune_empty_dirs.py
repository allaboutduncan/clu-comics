"""Unit tests for helpers.prune_empty_dirs (empty/junk folder sweep in TARGET)."""

import os

import pytest

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


class TestProtectedRoots:
    """The sweep must never delete a directory that is configured as a root.

    WATCH nested inside TARGET is a supported layout, and the sweep used to
    delete it. The automated trigger (api.check_wanted_after_watch_empty) waits
    for WATCH to be *empty* before running the wanted match that schedules the
    sweep -- so it fired on exactly the condition that made WATCH deletable.
    """

    @pytest.fixture
    def set_dirs(self, monkeypatch):
        """Point WATCH/TARGET at test paths.

        Patches core.config, which get_protected_roots imports lazily, so the
        real resolution path (fallbacks, realpath normalization) is exercised
        rather than stubbed out.
        """
        import core.config

        def _set(watch="", target=""):
            monkeypatch.setattr(core.config, "get_watch_dir", lambda: watch)
            monkeypatch.setattr(core.config, "get_target_dir", lambda: target)
        return _set

    def test_never_removes_watch_nested_inside_target(self, tmp_path, set_dirs):
        """The reported case: /downloads/processed/temp deleted for being empty."""
        root = tmp_path / "processed"
        watch = root / "temp"
        watch.mkdir(parents=True)
        set_dirs(watch=str(watch), target=str(root))

        assert prune_empty_dirs(str(root)) == 0
        assert watch.exists()

    def test_watch_with_trailing_slash_is_still_protected(self, tmp_path, set_dirs):
        """Nothing normalizes the stored preference, so the guard must.

        This is the case a raw string comparison fails -- get_watch_dir()
        preserves whatever the user typed.
        """
        root = tmp_path / "processed"
        watch = root / "temp"
        watch.mkdir(parents=True)
        set_dirs(watch=str(watch) + os.sep, target=str(root))

        assert prune_empty_dirs(str(root)) == 0
        assert watch.exists()

    def test_does_not_delete_empty_folders_inside_watch(self, tmp_path, set_dirs):
        """A download client creates its job folder before writing into it."""
        root = tmp_path / "processed"
        watch = root / "temp"
        job = watch / "Batman 001"
        job.mkdir(parents=True)
        set_dirs(watch=str(watch), target=str(root))

        assert prune_empty_dirs(str(root)) == 0
        assert job.exists()

    def test_hidden_named_watch_is_not_rmtree_as_junk(self, tmp_path, set_dirs):
        """is_hidden() is true for any name starting with '_' or '.'.

        A WATCH called "_incoming" reads as hidden junk, so its *parent* looks
        empty and the rmtree branch deletes it without the walk ever visiting
        WATCH as a directory of its own. A guard that only checks each walked
        directory misses this entirely.
        """
        root = tmp_path / "processed"
        watch = root / "Series" / "_incoming"
        watch.mkdir(parents=True)
        set_dirs(watch=str(watch), target=str(root))

        assert prune_empty_dirs(str(root)) == 0
        assert watch.exists()

    def test_watch_under_hidden_ancestor_survives(self, tmp_path, set_dirs):
        """Same hole one level up: the hidden ancestor is rmtree'd wholesale."""
        root = tmp_path / "processed"
        watch = root / "_staging" / "temp"
        watch.mkdir(parents=True)
        set_dirs(watch=str(watch), target=str(root))

        assert prune_empty_dirs(str(root)) == 0
        assert watch.exists()

    def test_prunes_siblings_while_skipping_watch(self, tmp_path, set_dirs):
        """The #423 behaviour still works alongside a protected sibling."""
        root = tmp_path / "processed"
        watch = root / "temp"
        junk = root / "Emptied Wrapper"
        watch.mkdir(parents=True)
        junk.mkdir()
        set_dirs(watch=str(watch), target=str(root))

        assert prune_empty_dirs(str(root)) == 1
        assert watch.exists()
        assert not junk.exists()

    def test_watch_above_the_sweep_root_does_not_disable_the_sweep(self, tmp_path, set_dirs):
        """WATCH=/downloads with TARGET=/downloads/processed is a real layout.

        The Unraid template maps the whole /downloads mount, so a user pointing
        WATCH at the mount root is natural. Treating "inside WATCH" as protected
        without qualification would mark the entire sweep tree off-limits and
        silently turn the feature off.
        """
        root = tmp_path / "processed"
        junk = root / "Emptied Wrapper"
        junk.mkdir(parents=True)
        set_dirs(watch=str(tmp_path), target=str(root))

        assert prune_empty_dirs(str(root)) == 1
        assert not junk.exists()

    def test_target_nested_in_the_sweep_root_is_protected(self, tmp_path, set_dirs):
        """Sweeping a parent of TARGET must not remove TARGET."""
        target = tmp_path / "processed"
        target.mkdir()
        set_dirs(watch=str(tmp_path / "temp"), target=str(target))

        assert prune_empty_dirs(str(tmp_path)) == 0
        assert target.exists()

    def test_trash_dir_under_the_root_is_protected(self, tmp_path, set_dirs, monkeypatch):
        from core.config import config

        root = tmp_path / "processed"
        trash = root / "trash"
        trash.mkdir(parents=True)
        set_dirs(watch=str(tmp_path / "temp"), target=str(root))
        monkeypatch.setitem(config["SETTINGS"], "TRASH_DIR", str(trash))

        assert prune_empty_dirs(str(root)) == 0
        assert trash.exists()

    def test_library_root_under_the_sweep_root_is_protected(self, tmp_path, set_dirs, monkeypatch):
        import helpers.library

        root = tmp_path / "processed"
        lib = root / "library"
        lib.mkdir(parents=True)
        set_dirs(watch=str(tmp_path / "temp"), target=str(root))
        monkeypatch.setattr(helpers.library, "get_library_roots", lambda: [str(lib)])

        assert prune_empty_dirs(str(root)) == 0
        assert lib.exists()

    def test_refuses_to_sweep_a_library_root(self, tmp_path, set_dirs, monkeypatch):
        """A TARGET misconfigured to the collection would sweep every empty series folder."""
        import helpers.library

        root = tmp_path / "data"
        series = root / "Batman"
        series.mkdir(parents=True)
        set_dirs(watch=str(tmp_path / "temp"), target=str(root))
        monkeypatch.setattr(helpers.library, "get_library_roots", lambda: [str(root)])

        assert prune_empty_dirs(str(root)) == 0
        assert series.exists()

    def test_configured_root_that_does_not_exist_is_harmless(self, tmp_path, set_dirs):
        root = tmp_path / "processed"
        junk = root / "Emptied Wrapper"
        junk.mkdir(parents=True)
        set_dirs(watch=str(root / "never-created"), target=str(root))

        assert prune_empty_dirs(str(root)) == 1
        assert not junk.exists()

    def test_config_read_failure_skips_the_sweep(self, tmp_path, monkeypatch):
        """Fail closed when the protected set cannot be resolved.

        Skipping leaves wrapper folders to accumulate until the next sweep;
        sweeping blind can delete WATCH. Only one of those is recoverable.
        """
        import core.config

        def boom():
            raise RuntimeError("db gone")

        monkeypatch.setattr(core.config, "get_watch_dir", boom)
        root = tmp_path / "processed"
        junk = root / "Emptied Wrapper"
        junk.mkdir(parents=True)

        assert prune_empty_dirs(str(root)) == 0
        assert junk.exists()

    @pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges on Windows")
    def test_symlinked_config_path_still_protects_watch(self, tmp_path, set_dirs):
        """WATCH reached through a symlink must resolve to the same directory."""
        real_root = tmp_path / "real" / "processed"
        watch = real_root / "temp"
        watch.mkdir(parents=True)
        link = tmp_path / "link"
        link.symlink_to(real_root, target_is_directory=True)
        set_dirs(watch=str(link / "temp"), target=str(real_root))

        assert prune_empty_dirs(str(real_root)) == 0
        assert watch.exists()
