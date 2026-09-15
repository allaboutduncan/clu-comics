"""A rebuild that cannot finish must leave the comic where it found it.

`rebuild_single_cbz_file` renames the comic to `.zip` as step 1 and to
`.zip.bak` before recompressing, then extracts every entry inside one `try`. A
per-entry CRC error -- the commonest failure on a damaged archive, and exactly
what the Problem Files page points this operation at -- aborted mid-way and left
the comic stranded under a name nothing in the library recognises, beside a
scratch folder of loose pages. Clicking Rebuild made the comic disappear.

These are the regression tests for that, and the fixture check CLAUDE.md
requires for any change under cbz_ops/.
"""

import io
import os
import zipfile

import pytest
from PIL import Image

from cbz_ops import rebuild as rebuild_mod
from cbz_ops import single_file
from cbz_ops.single_file import rebuild_single_cbz_file


@pytest.fixture(autouse=True)
def quiet_side_effects(monkeypatch):
    """Keep the rebuild off the database and the thumbnail cache."""
    monkeypatch.setattr(single_file, "regenerate_thumbnail", lambda *a, **kw: True)
    monkeypatch.setattr(rebuild_mod, "_refresh_thumbnail", lambda *a, **kw: None)
    monkeypatch.setattr(
        "core.database.get_db_connection", lambda *a, **kw: None, raising=False
    )


@pytest.fixture
def problems(monkeypatch):
    recorded, cleared = [], []
    monkeypatch.setattr(
        single_file, "_record_rebuild_problem",
        lambda path, exc=None, error_class=None, error_message=None: recorded.append(path),
    )
    monkeypatch.setattr(
        single_file, "_clear_rebuild_problem", lambda path: cleared.append(path)
    )
    return recorded, cleared


def _page_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (400, 600), (90, 90, 90)).save(buf, format="JPEG")
    return buf.getvalue()


def _make_good_cbz(path, pages=("001.jpg", "002.jpg")):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in pages:
            zf.writestr(name, _page_bytes())


def _make_bad_crc_cbz(path):
    """A structurally valid ZIP whose second entry fails its CRC check.

    Built by corrupting the compressed bytes in place, so the central directory
    still parses and the failure only shows up on extract -- which is precisely
    how a real damaged scan behaves.
    """
    _make_good_cbz(path)
    raw = bytearray(path.read_bytes())
    # Flip bytes deep inside the file, past the first local header, leaving the
    # end-of-central-directory record intact.
    start = len(raw) // 2
    for i in range(start, min(start + 400, len(raw) - 200)):
        raw[i] ^= 0xFF
    path.write_bytes(bytes(raw))


class TestFailedRebuildLeavesTheFileAlone:
    def test_a_corrupt_archive_is_restored_and_leaves_no_litter(
        self, tmp_path, problems
    ):
        recorded, _cleared = problems
        cbz = tmp_path / "Wizard Magazine 150 (2004).cbz"
        _make_bad_crc_cbz(cbz)
        before = cbz.read_bytes()

        assert rebuild_single_cbz_file(str(cbz)) is False

        # The comic is still a comic, byte for byte.
        assert cbz.exists(), "the .cbz must survive a failed rebuild"
        assert cbz.read_bytes() == before

        # And nothing is left behind.
        assert not (tmp_path / "Wizard Magazine 150 (2004).zip").exists()
        assert not (tmp_path / "Wizard Magazine 150 (2004).zip.bak").exists()
        assert not (tmp_path / "Wizard Magazine 150 (2004)_folder").exists()

        assert recorded == [str(cbz)]

    def test_a_file_that_is_not_an_archive_at_all_is_restored(self, tmp_path, problems):
        recorded, _cleared = problems
        cbz = tmp_path / "Garbage.cbz"
        cbz.write_bytes(b"this is not an archive of any kind")

        # Either outcome is acceptable here (the RAR fallback may run); what is
        # not acceptable is losing the file.
        rebuild_single_cbz_file(str(cbz))

        leftovers = sorted(p.name for p in tmp_path.iterdir())
        assert not any(n.endswith("_folder") for n in leftovers), leftovers
        assert not any(n.endswith(".zip") for n in leftovers), leftovers

    def test_a_missing_file_does_not_create_anything(self, tmp_path, problems):
        missing = tmp_path / "Nope.cbz"

        assert rebuild_single_cbz_file(str(missing)) is False

        assert list(tmp_path.iterdir()) == []


class TestSuccessfulRebuild:
    def test_a_healthy_archive_still_rebuilds_and_clears_its_entry(
        self, tmp_path, problems
    ):
        _recorded, cleared = problems
        cbz = tmp_path / "Good.cbz"
        _make_good_cbz(cbz)

        assert rebuild_single_cbz_file(str(cbz)) is True

        assert cbz.exists()
        with zipfile.ZipFile(cbz) as zf:
            assert len(zf.namelist()) == 2
            assert zf.testzip() is None
        assert cleared == [str(cbz)]

        # The scratch artefacts are cleaned up on the happy path too.
        assert not (tmp_path / "Good_folder").exists()
        assert not (tmp_path / "Good.zip").exists()
