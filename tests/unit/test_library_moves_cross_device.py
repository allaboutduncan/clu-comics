"""Moves onto the library must survive a mount that refuses utime/chmod (#582).

``shutil.move`` across devices is ``copy2`` (``copyfile`` + ``copystat``), or
``copytree`` of ``copy2`` for a folder. A CIFS/SMB or Windows-backed mount that
refuses ``utime``/``chmod`` raises EPERM from ``copystat`` *after* every byte
has been written, so the caller reports a failure with a complete copy at the
destination and the source still in place. These sites now go through
``helpers.move_file`` / ``helpers.move_tree`` and normalise the moved comic's
permissions with ``match_parent_permissions``:

- ``helpers.move_tree`` (folders, used by ``/move-batch`` and trash restore)
- ``models.comicvine.auto_move_file``
- ``core.problem_replacements._unstage`` / ``_restore_trashed``
- ``helpers.trash.restore_from_trash``
- ``models.usenet._move_into_watch``

The ``_swap`` staging step is covered in
``tests/integration/test_problem_replacements.py`` and the ``/move-batch``
route in ``tests/routes/test_move_batch_cross_device.py``.

Two mounts are simulated the way ``test_move_file_metadata_tolerance.py``
does it: a rename between the top-level folders under ``tmp_path`` fails with
EXDEV, and ``copystat`` onto the ``library`` mount raises EPERM.
"""
import errno
import json
import os
import shutil
from unittest.mock import patch

import pytest
from flask import Flask

import helpers


@pytest.fixture
def mounts(tmp_path, monkeypatch):
    """``tmp_path/<name>`` folders that behave like separate filesystems.

    ``os.rename``/``os.replace`` between two of them fail with EXDEV, as across
    real mounts; within one they work. ``copystat`` onto ``library`` raises
    EPERM, as a CIFS mount without ``noperm`` does; elsewhere it is real.
    """
    root = os.path.abspath(str(tmp_path))
    library = tmp_path / "library"
    library.mkdir()
    real_rename, real_replace, real_copystat = os.rename, os.replace, shutil.copystat

    def _volume(p):
        rel = os.path.relpath(os.path.abspath(str(p)), root)
        return rel.split(os.sep)[0]

    def _guard(real):
        def _move(src, dst, *a, **k):
            if _volume(src) != _volume(dst):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real(src, dst, *a, **k)
        return _move

    def _copystat(src, dst, *a, **k):
        if _volume(dst) == "library":
            raise PermissionError(errno.EPERM, "Operation not permitted", str(dst))
        return real_copystat(src, dst, *a, **k)

    monkeypatch.setattr(os, "rename", _guard(real_rename))
    monkeypatch.setattr(os, "replace", _guard(real_replace))
    monkeypatch.setattr(shutil, "copystat", _copystat)
    # On Windows copy2 is CopyFile2 and never calls copystat; take the POSIX
    # path (copyfile + copystat) so the old shutil.move fails here as well.
    monkeypatch.setattr(shutil, "_winapi", None)
    return tmp_path


@pytest.fixture
def perms():
    """Record match_parent_permissions calls (the real one is a no-op on Windows)."""
    calls = []
    with patch("helpers.match_parent_permissions", side_effect=calls.append):
        yield calls


def test_the_simulation_breaks_shutil_move(mounts):
    """Guard on the fixture: the old call really fails under it."""
    src = mounts / "downloads" / "a.cbz"
    src.parent.mkdir()
    src.write_bytes(b"comic")
    with pytest.raises(OSError):
        shutil.move(str(src), str(mounts / "library" / "a.cbz"))


def _no_staging_left(folder):
    return [p.name for p in folder.rglob("*") if "clu_incoming" in p.name] == []


class TestMoveTree:
    def _tree(self, base):
        src = base / "Batman (2016)"
        (src / "Annuals").mkdir(parents=True)
        (src / "Batman 001 (2016).cbz").write_bytes(b"one")
        (src / "Annuals" / "Batman Annual 001 (2017).cbz").write_bytes(b"annual")
        (src / "cvinfo").write_text("https://comicvine.gamespot.com/x/4050-1/")
        return src

    def test_same_device_is_a_rename(self, tmp_path, monkeypatch):
        monkeypatch.setattr(helpers.shutil, "copyfile",
                            lambda *a, **k: pytest.fail("must not copy"))
        src = self._tree(tmp_path)
        dst = tmp_path / "Batman v3"
        helpers.move_tree(str(src), str(dst))
        assert (dst / "Annuals" / "Batman Annual 001 (2017).cbz").read_bytes() == b"annual"
        assert not src.exists()

    def test_cross_device_survives_refused_metadata(self, mounts):
        src = self._tree(mounts / "downloads")
        dst = mounts / "library" / "Batman (2016)"

        helpers.move_tree(str(src), str(dst))

        assert (dst / "Batman 001 (2016).cbz").read_bytes() == b"one"
        assert (dst / "Annuals" / "Batman Annual 001 (2017).cbz").read_bytes() == b"annual"
        assert (dst / "cvinfo").exists()
        assert not src.exists(), "the source must be removed once the copy is complete"
        assert _no_staging_left(mounts / "library")

    def test_a_failed_copy_leaves_nothing_at_the_destination(self, mounts, monkeypatch):
        src = self._tree(mounts / "downloads")
        dst = mounts / "library" / "Batman (2016)"
        real_copyfile = shutil.copyfile

        def _disk_full(s, d, *a, **k):
            if "Annual" in os.path.basename(s):
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_copyfile(s, d, *a, **k)

        monkeypatch.setattr(helpers.shutil, "copyfile", _disk_full)
        with pytest.raises(OSError):
            helpers.move_tree(str(src), str(dst))

        assert not dst.exists()
        assert (src / "Annuals" / "Batman Annual 001 (2017).cbz").exists()
        assert _no_staging_left(mounts / "library")

    def test_refuses_to_nest_into_an_existing_folder(self, tmp_path):
        """shutil.move moved src *into* an existing dst, so the index recorded
        a path the folder never landed at."""
        src = self._tree(tmp_path)
        dst = tmp_path / "Existing"
        dst.mkdir()
        with pytest.raises(FileExistsError):
            helpers.move_tree(str(src), str(dst))
        assert src.exists() and list(dst.iterdir()) == []

    def test_case_only_rename_is_allowed(self, tmp_path):
        src = self._tree(tmp_path)
        dst = tmp_path / "BATMAN (2016)"
        helpers.move_tree(str(src), str(dst))
        assert os.listdir(tmp_path) == ["BATMAN (2016)"]


class TestComicVineAutoMove:
    def test_cross_device_move_succeeds_and_normalises(self, mounts, perms):
        from models.comicvine import auto_move_file

        src = mounts / "downloads" / "Batman 001 (2016).cbz"
        src.parent.mkdir()
        src.write_bytes(b"comic")
        lib = str(mounts / "library")
        config = {"ENABLE_AUTO_MOVE": True,
                  "CUSTOM_MOVE_PATTERN": "{publisher}/{series_name} ({start_year})"}
        volume = {"name": "Batman", "start_year": 2016, "publisher_name": "DC Comics"}

        with patch("core.filename_chars.load_char_map", return_value={}), \
             patch("helpers.library.get_library_for_path", return_value={"path": lib}):
            new_path = auto_move_file(str(src), volume, config)

        # auto_move_file joins the '/'-separated pattern onto the host path.
        expected = os.path.join(lib, "DC Comics/Batman (2016)", "Batman 001 (2016).cbz")
        assert new_path == expected
        assert open(expected, "rb").read() == b"comic"
        assert not src.exists()
        assert perms == [expected]

    def test_an_existing_file_is_still_never_overwritten(self, mounts, perms):
        from models.comicvine import auto_move_file

        src = mounts / "downloads" / "Batman 001 (2016).cbz"
        src.parent.mkdir()
        src.write_bytes(b"new")
        filed = mounts / "library" / "DC Comics" / "Batman (2016)" / "Batman 001 (2016).cbz"
        filed.parent.mkdir(parents=True)
        filed.write_bytes(b"filed")
        config = {"ENABLE_AUTO_MOVE": True,
                  "CUSTOM_MOVE_PATTERN": "{publisher}/{series_name} ({start_year})"}
        volume = {"name": "Batman", "start_year": 2016, "publisher_name": "DC Comics"}

        with patch("core.filename_chars.load_char_map", return_value={}), \
             patch("helpers.library.get_library_for_path",
                   return_value={"path": str(mounts / "library")}):
            assert auto_move_file(str(src), volume, config) is None

        assert filed.read_bytes() == b"filed"
        assert src.exists()


class TestProblemReplacementRollback:
    def test_restore_trashed_survives_refused_metadata(self, mounts, perms):
        from core.problem_replacements import _restore_trashed

        trashed = mounts / "cache" / "trash" / "Batman 001.cbz"
        trashed.parent.mkdir(parents=True)
        trashed.write_bytes(b"damaged original")
        target = mounts / "library" / "Batman" / "Batman 001.cbz"

        _restore_trashed(str(trashed), str(target))

        assert target.read_bytes() == b"damaged original"
        assert not trashed.exists()
        assert perms == [str(target)]

    def test_unstage_returns_the_download_to_target(self, mounts, monkeypatch):
        """TARGET is the refusing mount here: the download must go back whole,
        and the staged copy must not be left in the library."""
        from core.problem_replacements import _unstage

        def _refuse_everywhere(src, dst, *a, **k):
            raise PermissionError(errno.EPERM, "Operation not permitted", str(dst))

        monkeypatch.setattr(shutil, "copystat", _refuse_everywhere)
        staged = mounts / "library" / "Batman" / ".Batman 001.cbz.clu_incoming"
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b"download")
        original = mounts / "downloads" / "processed" / "Batman 001.cbz"

        _unstage(str(staged), str(original))

        assert original.read_bytes() == b"download"
        assert not staged.exists()


class TestTrashRestore:
    @pytest.fixture
    def app(self, mounts):
        trash = mounts / "cache" / "trash"
        trash.mkdir(parents=True)
        flask_app = Flask(__name__)
        flask_app.config.update(TRASH_ENABLED=True, TRASH_DIR=str(trash),
                                TRASH_MAX_SIZE_MB=1024,
                                CACHE_DIR=str(mounts / "cache"),
                                DATA_DIR=str(mounts / "library"))
        return flask_app

    def _manifest(self, trash, name, original):
        (trash / "trash_manifest.json").write_text(json.dumps({
            name: {"original_path": original, "deleted_at": 1000}
        }))

    def test_a_file_is_restored_across_mounts(self, app, mounts, perms):
        trash = mounts / "cache" / "trash"
        (trash / "issue1.cbz").write_bytes(b"comic")
        original = str(mounts / "library" / "DC" / "issue1.cbz")
        self._manifest(trash, "issue1.cbz", original)

        with app.app_context():
            from helpers.trash import restore_from_trash
            result = restore_from_trash("issue1.cbz")

        assert result["success"] is True, result
        assert open(original, "rb").read() == b"comic"
        assert not (trash / "issue1.cbz").exists()
        assert perms == [original]

    def test_a_folder_is_restored_across_mounts(self, app, mounts, perms):
        trash = mounts / "cache" / "trash"
        folder = trash / "Batman"
        folder.mkdir()
        (folder / "Batman 001.cbz").write_bytes(b"one")
        original = str(mounts / "library" / "DC" / "Batman")
        self._manifest(trash, "Batman", original)

        with app.app_context():
            from helpers.trash import restore_from_trash
            result = restore_from_trash("Batman")

        assert result["success"] is True, result
        assert open(os.path.join(original, "Batman 001.cbz"), "rb").read() == b"one"
        assert not folder.exists()


class TestUsenetImport:
    def test_move_into_watch_survives_refused_metadata(self, mounts, perms):
        from models.usenet import _move_into_watch

        src = mounts / "sab" / "complete" / "Batman 001.cbz"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"comic")
        watch = mounts / "library" / "watch"   # the refusing mount
        watch.mkdir()

        assert _move_into_watch(str(src), str(watch)) == 1

        dest = watch / "Batman 001.cbz"
        assert dest.read_bytes() == b"comic"
        assert not src.exists()
        assert perms == [str(dest)]
        assert _no_staging_left(watch)
