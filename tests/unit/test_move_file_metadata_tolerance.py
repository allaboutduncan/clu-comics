"""``helpers.move_file``: a metadata copy must never fail a completed move.

The bug this pins produced a CBR sitting next to its own CBZ in TARGET, on
every single download, for one user's whole library.

``shutil.move`` falls back to ``copy2`` for a cross-device move, and ``copy2``
is ``copyfile`` followed by ``copystat``. ``copystat`` calls ``os.utime(dst)``
unguarded and guards ``os.chmod(dst)`` only against ``NotImplementedError``, so
a mount that refuses either -- CIFS/SMB without ``noperm``, a Windows-backed
WSL2 bind mount -- raises ``EPERM`` *after* the destination has been written in
full. Every caller read that as "the move failed":

    app.log     12:11:43  ERROR  Failed to convert Stuff of Nightmares 001.cbr:
                                 [Errno 1] Operation not permitted: '...001.cbz'
    monitor.log 12:11:44  INFO   Converted to: /downloads/processed/...001.cbz

``convert_to_cbz`` therefore skipped ``os.remove(<source>.cbr)``, while
monitor.py -- which tested ``os.path.exists`` rather than the return value --
declared success one second later. Nothing retried, nothing cleaned up.
"""
import errno
import os
import shutil
import zipfile

import pytest

import helpers


def _force_cross_device(monkeypatch):
    """Make os.replace fail as it would across two mounts.

    The tolerant path only runs on the copy fallback; on one filesystem
    os.replace succeeds and never touches metadata at all.
    """
    def _exdev(src, dst):
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(helpers.os, "replace", _exdev)


def _refuse_metadata(monkeypatch):
    """copystat as an EPERM mount reports it: after the contents are written."""
    def _eperm(src, dst, **kwargs):
        raise PermissionError(errno.EPERM, "Operation not permitted", str(dst))

    monkeypatch.setattr(helpers.shutil, "copystat", _eperm)


class TestMoveFile:
    def test_same_device_move_needs_no_copy(self, tmp_path, monkeypatch):
        """The fast path is a rename: nothing is copied, nothing is stat'd."""
        monkeypatch.setattr(helpers.shutil, "copyfile",
                            lambda *a, **k: pytest.fail("must not copy"))
        src = tmp_path / "a.cbz"
        src.write_bytes(b"payload")
        dst = tmp_path / "b.cbz"

        helpers.move_file(str(src), str(dst))

        assert dst.read_bytes() == b"payload"
        assert not src.exists()

    def test_cross_device_move_survives_refused_metadata(self, tmp_path, monkeypatch):
        """The whole point: EPERM from copystat must not fail the move."""
        _force_cross_device(monkeypatch)
        _refuse_metadata(monkeypatch)

        src = tmp_path / "Stuff of Nightmares 001.cbz"
        src.write_bytes(b"a complete comic")
        dst = tmp_path / "dest" / "Stuff of Nightmares 001.cbz"
        dst.parent.mkdir()

        helpers.move_file(str(src), str(dst))  # must not raise

        assert dst.read_bytes() == b"a complete comic"
        assert not src.exists(), "the source must still be consumed"

    def test_cross_device_move_copies_metadata_when_allowed(self, tmp_path, monkeypatch):
        """Only the *failure* is demoted; timestamps are still copied."""
        _force_cross_device(monkeypatch)
        seen = []
        real_copystat = shutil.copystat
        monkeypatch.setattr(
            helpers.shutil, "copystat",
            lambda s, d, **k: (seen.append((s, d)), real_copystat(s, d, **k))[1],
        )

        src = tmp_path / "a.cbz"
        src.write_bytes(b"payload")
        os.utime(str(src), (1_000_000_000, 1_000_000_000))
        dst = tmp_path / "b.cbz"

        helpers.move_file(str(src), str(dst))

        assert seen, "copystat must still be attempted"
        assert int(os.path.getmtime(str(dst))) == 1_000_000_000

    def test_a_failed_copy_still_raises(self, tmp_path, monkeypatch):
        """Contents not copied is a real failure and must reach the caller."""
        _force_cross_device(monkeypatch)

        def _boom(src, dst, **kwargs):
            raise PermissionError(errno.EACCES, "Permission denied", str(dst))

        monkeypatch.setattr(helpers.shutil, "copyfile", _boom)

        src = tmp_path / "a.cbz"
        src.write_bytes(b"payload")

        with pytest.raises(PermissionError):
            helpers.move_file(str(src), str(tmp_path / "b.cbz"))
        assert src.exists()

    def test_an_unremovable_source_still_raises(self, tmp_path, monkeypatch):
        """A source left behind is a duplicate, which callers must hear about.

        This is the #582 failure mode and stays a failure: in TARGET the
        leftover is re-matched and re-copied into the library on every sweep.
        """
        _force_cross_device(monkeypatch)

        def _boom(path):
            raise PermissionError(errno.EPERM, "Operation not permitted", str(path))

        monkeypatch.setattr(helpers.os, "remove", _boom)

        src = tmp_path / "a.cbz"
        src.write_bytes(b"payload")

        with pytest.raises(PermissionError):
            helpers.move_file(str(src), str(tmp_path / "b.cbz"))


class TestZipWriterSurvivesRefusedMetadata:
    """open_zip_for_write is where the user's CBZs were being written."""

    def test_archive_lands_when_copystat_is_refused(self, tmp_path, monkeypatch):
        staging = tmp_path / "staging"
        staging.mkdir()
        monkeypatch.setattr(helpers, "_zip_assembly_dir", lambda: str(staging))
        monkeypatch.setattr(helpers, "match_parent_permissions", lambda p: None)
        _force_cross_device(monkeypatch)
        _refuse_metadata(monkeypatch)

        dest = tmp_path / "out.cbz"
        with helpers.open_zip_for_write(str(dest)) as zf:
            zf.writestr("001.jpg", b"img-a")

        with zipfile.ZipFile(str(dest)) as zf:
            assert zf.namelist() == ["001.jpg"]
        assert os.listdir(str(staging)) == [], "staging temp must be consumed"
