"""Tests for helpers.open_zip_for_write -- the shared "assemble locally, then
move onto the data volume" archive writer.

The helper exists so CBZ writes never rely on the destination mount being
seekable: some backends used for /data (mergerfs / network / FUSE) raise
"OSError: [Errno 29] Illegal seek" when zipfile seeks back to finalize an
archive written directly onto them. Assembling on a local volume and moving the
finished file into place avoids that.
"""
import os
import zipfile

import pytest

import helpers


@pytest.fixture
def local_staging(tmp_path, monkeypatch):
    """Force the staging dir onto a real local temp dir under tmp_path, and
    confirm assembly actually happens there (not on the destination path)."""
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(helpers, "_zip_assembly_dir", lambda: str(staging))
    monkeypatch.setattr(helpers, "match_parent_permissions", lambda p: None)
    return staging


def test_writes_and_moves_into_place(tmp_path, local_staging):
    dest = tmp_path / "out.cbz"
    with helpers.open_zip_for_write(str(dest)) as zf:
        zf.writestr("001.jpg", b"img-a")
        zf.writestr("002.jpg", b"img-b")

    assert dest.exists()
    with zipfile.ZipFile(str(dest)) as zf:
        assert zf.namelist() == ["001.jpg", "002.jpg"]
        assert zf.read("002.jpg") == b"img-b"


def test_assembles_on_staging_not_destination(tmp_path, local_staging):
    """Mid-write, the archive must live in the staging dir, and the destination
    must not yet exist -- proving nothing is written/seeked on the dest volume."""
    dest = tmp_path / "out.cbz"
    with helpers.open_zip_for_write(str(dest)) as zf:
        zf.writestr("001.jpg", b"x")
        assert not dest.exists(), "destination must not exist until the move"
        staged = os.listdir(local_staging)
        assert staged, "archive should be assembled in the staging dir"

    assert dest.exists()
    assert os.listdir(local_staging) == [], "staging temp must be moved out"


def test_supports_write_from_disk(tmp_path, local_staging):
    src = tmp_path / "page.jpg"
    src.write_bytes(b"disk-bytes")
    dest = tmp_path / "out.cbz"

    with helpers.open_zip_for_write(str(dest)) as zf:
        zf.write(str(src), "page.jpg")

    with zipfile.ZipFile(str(dest)) as zf:
        assert zf.read("page.jpg") == b"disk-bytes"


def test_forwards_compression_and_compresslevel(tmp_path, local_staging):
    dest = tmp_path / "stored.cbz"
    with helpers.open_zip_for_write(str(dest), zipfile.ZIP_STORED) as zf:
        zf.writestr("001.jpg", b"raw")
    with zipfile.ZipFile(str(dest)) as zf:
        assert zf.getinfo("001.jpg").compress_type == zipfile.ZIP_STORED

    dest2 = tmp_path / "deflated.cbz"
    with helpers.open_zip_for_write(str(dest2), zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.writestr("001.jpg", b"data" * 100)
    with zipfile.ZipFile(str(dest2)) as zf:
        assert zf.getinfo("001.jpg").compress_type == zipfile.ZIP_DEFLATED


def test_failure_leaves_destination_untouched_and_cleans_temp(tmp_path, local_staging):
    dest = tmp_path / "out.cbz"
    dest.write_bytes(b"original-contents")

    with pytest.raises(RuntimeError):
        with helpers.open_zip_for_write(str(dest)) as zf:
            zf.writestr("001.jpg", b"x")
            raise RuntimeError("boom")

    # Original destination is untouched (no partial overwrite) ...
    assert dest.read_bytes() == b"original-contents"
    # ... and the staging temp was cleaned up.
    assert os.listdir(local_staging) == []


def test_overwrites_existing_destination_on_success(tmp_path, local_staging):
    dest = tmp_path / "out.cbz"
    dest.write_bytes(b"stale")

    with helpers.open_zip_for_write(str(dest)) as zf:
        zf.writestr("new.jpg", b"fresh")

    with zipfile.ZipFile(str(dest)) as zf:
        assert zf.namelist() == ["new.jpg"]


def test_calls_match_parent_permissions_on_success(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    staging.mkdir()
    monkeypatch.setattr(helpers, "_zip_assembly_dir", lambda: str(staging))
    called = []
    monkeypatch.setattr(helpers, "match_parent_permissions", lambda p: called.append(p))

    dest = tmp_path / "out.cbz"
    with helpers.open_zip_for_write(str(dest)) as zf:
        zf.writestr("001.jpg", b"x")

    assert called == [str(dest)]
