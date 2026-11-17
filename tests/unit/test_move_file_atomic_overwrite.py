"""``helpers.move_file`` must never truncate the destination before the new
copy is complete.

Every in-place CBZ rewrite is assembled in ``CACHE_DIR/tmp`` by
``open_zip_for_write`` and moved over the original with ``move_file``. Those
are normally two volumes, so the move is a copy -- and it used to be
``shutil.copyfile(src, dst)`` straight onto the existing comic. A disk-full,
a NAS drop or a container stop part-way through left the original truncated,
and the ``finally`` in ``open_zip_for_write`` then deleted the staged archive:
the comic was gone.

The copy now goes to a hidden ``.clu_incoming`` sibling in the destination
folder and is ``os.replace``'d over the destination, so the original is
untouched until the new file is complete.
"""
import errno
import os
import shutil
import time
import zipfile

import pytest

import helpers


ORIGINAL = b"PK original comic bytes " * 512


@pytest.fixture
def volumes(tmp_path, monkeypatch):
    """Two folders that behave like two filesystems.

    os.replace between them fails with EXDEV, exactly as across mounts; a
    rename within one folder still works, as it does on a real volume.
    """
    staging = tmp_path / "cache_tmp"
    library = tmp_path / "library" / "Series (2020)"
    staging.mkdir()
    library.mkdir(parents=True)
    real_replace = os.replace

    def _replace(src, dst):
        if os.path.dirname(os.path.abspath(src)) != os.path.dirname(os.path.abspath(dst)):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_replace(src, dst)

    monkeypatch.setattr(helpers.os, "replace", _replace)
    monkeypatch.setattr(helpers, "_zip_assembly_dir", lambda: str(staging))
    return staging, library


@pytest.fixture
def existing_comic(volumes):
    _, library = volumes
    comic = library / "Series 001 (2020).cbz"
    comic.write_bytes(ORIGINAL)
    return comic


def _copy_half_then_fail(monkeypatch, exc=None):
    """copyfile as a full disk reports it: some bytes written, then ENOSPC."""
    targets = []

    def _partial(src, dst, **kwargs):
        targets.append(dst)
        with open(src, "rb") as fin, open(dst, "wb") as fout:
            data = fin.read()
            fout.write(data[: len(data) // 2])
        raise exc or OSError(errno.ENOSPC, "No space left on device", str(dst))

    monkeypatch.setattr(helpers.shutil, "copyfile", _partial)
    return targets


def _incoming(folder):
    return [p.name for p in folder.iterdir() if "clu_incoming" in p.name]


class TestCopyFailurePreservesOriginal:
    def test_partial_copy_leaves_the_original_byte_identical(
            self, volumes, existing_comic, monkeypatch):
        staging, library = volumes
        src = staging / "new.cbz"
        src.write_bytes(b"the rewritten comic" * 1000)
        targets = _copy_half_then_fail(monkeypatch)

        with pytest.raises(OSError):
            helpers.move_file(str(src), str(existing_comic))

        assert existing_comic.read_bytes() == ORIGINAL
        assert _incoming(library) == [], "the partial sibling must be removed"
        assert src.exists(), "the source must survive a failed move"
        assert targets and os.path.abspath(targets[0]) != os.path.abspath(str(existing_comic))

    def test_rewrite_through_open_zip_for_write_keeps_the_original(
            self, volumes, existing_comic, monkeypatch):
        """The reported path: an in-place CBZ rewrite failing mid-copy."""
        staging, library = volumes
        _copy_half_then_fail(monkeypatch)

        with pytest.raises(OSError):
            with helpers.open_zip_for_write(str(existing_comic)) as zf:
                zf.writestr("001.jpg", b"new page")

        assert existing_comic.read_bytes() == ORIGINAL
        assert _incoming(library) == []
        assert sorted(p.name for p in library.iterdir()) == [existing_comic.name]
        assert os.listdir(str(staging)) == [], "the staged archive is cleaned up"

    def test_a_failed_final_rename_keeps_the_original(
            self, volumes, existing_comic, monkeypatch):
        staging, library = volumes
        src = staging / "new.cbz"
        src.write_bytes(b"new")
        cross_device = helpers.os.replace

        def _refuse_swap(a, b):
            if os.path.dirname(os.path.abspath(a)) == os.path.dirname(os.path.abspath(b)):
                raise PermissionError(errno.EACCES, "Permission denied", b)
            return cross_device(a, b)

        monkeypatch.setattr(helpers.os, "replace", _refuse_swap)

        with pytest.raises(PermissionError):
            helpers.move_file(str(src), str(existing_comic))

        assert existing_comic.read_bytes() == ORIGINAL
        assert _incoming(library) == []
        assert src.exists()


class TestSuccessfulCrossDeviceOverwrite:
    def test_overwrite_lands_and_leaves_no_sibling(self, volumes, existing_comic):
        staging, library = volumes
        src = staging / "new.cbz"
        src.write_bytes(b"the rewritten comic")

        helpers.move_file(str(src), str(existing_comic))

        assert existing_comic.read_bytes() == b"the rewritten comic"
        assert not src.exists()
        assert sorted(p.name for p in library.iterdir()) == [existing_comic.name]

    def test_zip_rewrite_lands_with_parent_permissions(
            self, volumes, existing_comic, monkeypatch):
        """match_parent_permissions still runs on the final path."""
        _, library = volumes
        seen = []
        monkeypatch.setattr(helpers, "match_parent_permissions", seen.append)

        with helpers.open_zip_for_write(str(existing_comic)) as zf:
            zf.writestr("001.jpg", b"new page")

        with zipfile.ZipFile(str(existing_comic)) as zf:
            assert zf.namelist() == ["001.jpg"]
        assert seen == [str(existing_comic)]
        assert _incoming(library) == []

    def test_the_copy_never_opens_the_destination(self, volumes, existing_comic, monkeypatch):
        staging, _ = volumes
        src = staging / "new.cbz"
        src.write_bytes(b"new")
        targets = []
        real_copyfile = shutil.copyfile
        monkeypatch.setattr(
            helpers.shutil, "copyfile",
            lambda s, d, **k: (targets.append(d), real_copyfile(s, d, **k))[1],
        )

        helpers.move_file(str(src), str(existing_comic))

        assert len(targets) == 1
        name = os.path.basename(targets[0])
        assert name.startswith(".") and name.endswith(helpers.INCOMING_SUFFIX)
        assert helpers.is_hidden(targets[0]), "a staged copy must never look like a comic"


class TestCrashLeftovers:
    """The reaper deletes only move_file's own leftovers, and only old ones.

    "Old" is the newer of mtime and ctime: move_file copystat's its sibling
    before the final os.replace, so a copy staged a moment ago carries the
    source's (often months-old) mtime. ctime cannot be set from a test, so an
    old leftover is made by moving the reaper's clock forward instead.
    """

    @staticmethod
    def _shaped(folder, ch):
        return folder / (".%s%s" % (ch * 32, helpers.INCOMING_SUFFIX))

    @staticmethod
    def _age_mtime(path):
        old = time.time() - helpers.STALE_INCOMING_SECONDS - 60
        os.utime(str(path), (old, old))

    @staticmethod
    def _clock_ahead(monkeypatch):
        later = time.time() + helpers.STALE_INCOMING_SECONDS + 60
        monkeypatch.setattr(helpers.time, "time", lambda: later)

    def test_an_old_leftover_is_reaped(self, volumes, monkeypatch):
        _, library = volumes
        stale = self._shaped(library, "a")
        stale.write_bytes(b"x")
        self._age_mtime(stale)
        self._clock_ahead(monkeypatch)

        helpers._reap_stale_incoming(str(library))

        assert not stale.exists()

    def test_a_fresh_sibling_with_an_old_mtime_survives(self, volumes):
        """Exactly what move_file leaves between copystat and os.replace."""
        _, library = volumes
        live = self._shaped(library, "b")
        live.write_bytes(b"x")
        self._age_mtime(live)

        helpers._reap_stale_incoming(str(library))

        assert live.exists(), "a copy staged a moment ago must not be reaped"

    def test_a_cross_device_move_reaps_without_touching_live_copies(self, volumes):
        staging, library = volumes
        live = self._shaped(library, "c")
        other_hidden = library / ".keepme"
        for p in (live, other_hidden):
            p.write_bytes(b"x")
            self._age_mtime(p)

        src = staging / "new.cbz"
        src.write_bytes(b"new")
        helpers.move_file(str(src), str(library / "Series 001 (2020).cbz"))

        assert live.exists()
        assert other_hidden.exists(), "only .clu_incoming files are reaped"

    def test_a_problem_replacement_stage_is_never_reaped(self, volumes, monkeypatch):
        """core.problem_replacements stages ``.<name>.clu_incoming`` with the
        download's own mtime and holds it across the trash step."""
        _, library = volumes
        staged = library / ".Series 001 (2020).cbz.clu_incoming"
        upper = library / (".%s%s" % ("A" * 32, helpers.INCOMING_SUFFIX))
        for p in (staged, upper):
            p.write_bytes(b"replacement")
            self._age_mtime(p)
        self._clock_ahead(monkeypatch)

        helpers._reap_stale_incoming(str(library))

        assert staged.exists()
        assert upper.exists(), "only the exact uuid4-hex shape move_file writes"

    def test_a_move_tree_stage_directory_is_never_reaped(self, volumes, monkeypatch):
        _, library = volumes
        stage = self._shaped(library, "d")
        stage.mkdir()
        (stage / "Series 001 (2020).cbz").write_bytes(b"being copied")
        self._age_mtime(stage)
        self._clock_ahead(monkeypatch)

        helpers._reap_stale_incoming(str(library))

        assert (stage / "Series 001 (2020).cbz").exists()


class TestSameFileGuard:
    def test_case_only_rename_fallback_never_deletes_the_file(self, tmp_path, monkeypatch):
        """If the rename fails and dst is src under another case, do not copy.

        Copying would land on the very file and the os.remove(src) after it
        would delete the comic.
        """
        src = tmp_path / "series 001.cbz"
        src.write_bytes(ORIGINAL)
        dst = tmp_path / "Series 001.cbz"
        monkeypatch.setattr(helpers.os.path, "samefile", lambda a, b: True)
        monkeypatch.setattr(helpers.os.path, "exists", lambda p: True)

        def _refuse(a, b):
            raise PermissionError(errno.EACCES, "Permission denied", b)

        monkeypatch.setattr(helpers.os, "replace", _refuse)
        monkeypatch.setattr(helpers.shutil, "copyfile",
                            lambda *a, **k: pytest.fail("must not copy onto itself"))

        with pytest.raises(PermissionError):
            helpers.move_file(str(src), str(dst))

        monkeypatch.undo()
        assert src.read_bytes() == ORIGINAL


def _name_of_bytes(n_bytes, char):
    """A '.cbz' name of exactly *n_bytes* UTF-8 bytes built from *char*."""
    width = len(char.encode("utf-8"))
    body = n_bytes - len(".cbz")
    count, rest = divmod(body, width)
    return char * count + "x" * rest + ".cbz"


def _can_create(folder, name):
    try:
        (folder / name).write_bytes(b"")
    except OSError:
        return False
    (folder / name).unlink()
    return True


class TestLongDestinationNames:
    """The sibling's name must not grow with the destination's (ENAMETOOLONG)."""

    @pytest.mark.parametrize("char", ["a", "コ"], ids=["ascii", "utf8-3byte"])
    def test_max_length_name_survives_a_cross_device_overwrite(
            self, volumes, char):
        staging, library = volumes
        # 255 bytes is the per-component cap on ext4 and most Linux
        # filesystems; Windows caps at 255 UTF-16 units, so the ASCII name is
        # at its limit there too, and the 3-byte name is 85 characters.
        name = _name_of_bytes(255, char)
        assert len(name.encode("utf-8")) == 255
        if not _can_create(library, name):
            pytest.skip("this filesystem cannot hold a 255-byte name")
        dest = library / name
        dest.write_bytes(ORIGINAL)
        src = staging / "new.cbz"
        src.write_bytes(b"the rewritten comic")

        helpers.move_file(str(src), str(dest))

        assert dest.read_bytes() == b"the rewritten comic"
        assert sorted(p.name for p in library.iterdir()) == [name]

    def test_sibling_name_length_is_fixed(self, volumes, monkeypatch):
        staging, library = volumes
        targets = []
        real_copyfile = shutil.copyfile
        monkeypatch.setattr(
            helpers.shutil, "copyfile",
            lambda s, d, **k: (targets.append(d), real_copyfile(s, d, **k))[1],
        )
        for name in ("a.cbz", _name_of_bytes(200, "a")):
            src = staging / "new.cbz"
            src.write_bytes(b"x")
            helpers.move_file(str(src), str(library / name))

        lengths = {len(os.path.basename(t)) for t in targets}
        assert len(lengths) == 1 and lengths.pop() <= 64


class TestNoReplace:
    """``move_file(..., replace=False)`` files a download without ever
    overwriting (#731). The refusal comes from the rename, not from an
    ``os.path.exists`` check made beforehand."""

    def test_same_volume_refuses_an_existing_destination(self, tmp_path):
        src, dst = tmp_path / "new.cbz", tmp_path / "Series 001.cbz"
        src.write_bytes(b"download")
        dst.write_bytes(ORIGINAL)
        with pytest.raises(FileExistsError):
            helpers.move_file(str(src), str(dst), replace=False)
        assert dst.read_bytes() == ORIGINAL
        assert src.read_bytes() == b"download"

    def test_same_volume_moves_onto_a_free_name(self, tmp_path):
        src, dst = tmp_path / "new.cbz", tmp_path / "Series 001.cbz"
        src.write_bytes(b"download")
        helpers.move_file(str(src), str(dst), replace=False)
        assert dst.read_bytes() == b"download"
        assert not src.exists()

    def test_case_only_rename_is_not_a_collision(self, tmp_path):
        src = tmp_path / "series 001.cbz"
        src.write_bytes(b"download")
        dst = tmp_path / "Series 001.cbz"
        if not dst.exists():
            pytest.skip("case-sensitive filesystem")
        helpers.move_file(str(src), str(dst), replace=False)
        assert "Series 001.cbz" in os.listdir(tmp_path)
        assert dst.read_bytes() == b"download"

    def _cross_device(self, monkeypatch, on_install=None):
        """Make the direct rename fail with EXDEV; a rename within one folder
        (the staged sibling onto its destination) still works."""
        import helpers.no_clobber as nc

        real = nc.rename_no_clobber

        def _rename(src, dst):
            if os.path.dirname(os.path.abspath(src)) != os.path.dirname(os.path.abspath(dst)):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            if on_install:
                on_install(dst)
            return real(src, dst)

        monkeypatch.setattr(helpers, "rename_no_clobber", _rename)

    def test_cross_volume_moves_onto_a_free_name(self, volumes, monkeypatch):
        staging, library = volumes
        self._cross_device(monkeypatch)
        src, dst = staging / "new.cbz", library / "Series 002 (2020).cbz"
        src.write_bytes(b"download")
        helpers.move_file(str(src), str(dst), replace=False)
        assert dst.read_bytes() == b"download"
        assert not src.exists()
        assert os.listdir(library) == ["Series 002 (2020).cbz"]

    def test_cross_volume_refuses_an_existing_destination(
        self, volumes, existing_comic, monkeypatch
    ):
        staging, library = volumes
        self._cross_device(monkeypatch)
        src = staging / "new.cbz"
        src.write_bytes(b"download")
        with pytest.raises(FileExistsError):
            helpers.move_file(str(src), str(existing_comic), replace=False)
        assert existing_comic.read_bytes() == ORIGINAL
        assert src.read_bytes() == b"download"
        assert os.listdir(library) == [existing_comic.name]

    def test_a_comic_that_appears_during_the_copy_is_not_replaced(
        self, volumes, monkeypatch
    ):
        """The race itself: the name was free when the move started and is
        taken by the time the staged copy is installed."""
        staging, library = volumes
        dst = library / "Series 003 (2020).cbz"

        def _someone_else_files_it(path):
            with open(path, "wb") as fh:
                fh.write(ORIGINAL)

        self._cross_device(monkeypatch, on_install=_someone_else_files_it)
        src = staging / "new.cbz"
        src.write_bytes(b"download")
        with pytest.raises(FileExistsError):
            helpers.move_file(str(src), str(dst), replace=False)
        assert dst.read_bytes() == ORIGINAL
        assert src.read_bytes() == b"download"
        assert os.listdir(library) == [dst.name], "the staged copy must be removed"

    def test_default_still_replaces(self, tmp_path):
        """Every in-place CBZ rewrite relies on the overwrite."""
        src, dst = tmp_path / "new.cbz", tmp_path / "Series 001.cbz"
        src.write_bytes(b"rewritten")
        dst.write_bytes(ORIGINAL)
        helpers.move_file(str(src), str(dst))
        assert dst.read_bytes() == b"rewritten"
