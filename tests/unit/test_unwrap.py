"""Unit tests for helpers/unwrap.py — the recursive Hybrid/Multipart release
unwrapper.

Covers folder classification (conservative — never fire on a ready comic),
primary-volume selection for the common packaging styles, the recursive
zip->rar->comic happy path (RAR layer stubbed since Python can't write RAR),
and the archive-bomb depth guard.
"""
import os
import zipfile

import pytest

import helpers.unwrap as U


def _write(path, content=b"x"):
    with open(path, "wb") as f:
        f.write(content)


# --------------------------- classify_release_folder ---------------------------

def test_classify_multipart_release(tmp_path):
    rel = tmp_path / "Europe.Comics-Pin.Up.10 (2022)"
    rel.mkdir()
    _write(str(rel / "--bbyvt3ga.zip"))
    _write(str(rel / "--bbyvt3gb.zip"))
    _write(str(rel / "--bb.nfo"))
    _write(str(rel / "file_id.diz"))
    assert U.classify_release_folder(str(rel)) == U.MULTIPART_ARCHIVE


def test_classify_normal_when_comic_present(tmp_path):
    """A folder with a ready comic is a normal job even if a zip sits alongside."""
    rel = tmp_path / "folder"
    rel.mkdir()
    _write(str(rel / "Series 001 (2024).cbz"))
    _write(str(rel / "extra.zip"))
    assert U.classify_release_folder(str(rel)) == U.NORMAL


def test_classify_normal_lone_plain_zip(tmp_path):
    """One non-obfuscated zip with no cruft is not a multipart release."""
    rel = tmp_path / "folder"
    rel.mkdir()
    _write(str(rel / "Batman 001.zip"))
    assert U.classify_release_folder(str(rel)) == U.NORMAL


def test_classify_normal_empty(tmp_path):
    rel = tmp_path / "folder"
    rel.mkdir()
    assert U.classify_release_folder(str(rel)) == U.NORMAL


# ---------------------------- pick_primary_volume -----------------------------

@pytest.mark.parametrize("parts,expected", [
    (["x.part03.rar", "x.part01.rar", "x.part02.rar"], "x.part01.rar"),
    (["x.r01", "x.rar", "x.r00"], "x.rar"),
    (["x.z02", "x.zip", "x.z01"], "x.zip"),
    (["a.003", "a.001", "a.002"], "a.001"),
    (["--bbyvt3gb.zip", "--bbyvt3ga.zip", "--bbyvt3gc.zip"], "--bbyvt3ga.zip"),
])
def test_pick_primary_volume(parts, expected):
    assert U.pick_primary_volume(parts) == expected


def test_pick_primary_volume_empty():
    assert U.pick_primary_volume([]) is None


# ------------------------------ unwrap_release --------------------------------

def test_unwrap_release_zip_to_rar_to_comic(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "is_allowed_path", lambda p: True)

    # Stand in for the RAR extractor: emit the final comic (Python cannot write
    # a real RAR, so the RAR layer is simulated).
    def fake_rar(primary, out_dir):
        os.makedirs(out_dir, exist_ok=True)
        _write(os.path.join(out_dir, "TheComic.cbz"), b"PK\x03\x04comic")
        return True, 0
    monkeypatch.setattr(U, "extract_rar_with_unar", fake_rar)

    rel = tmp_path / "release"
    rel.mkdir()
    # Two independent obfuscated zip parts, each holding one RAR volume.
    for part, inner in (("--bbyvt3ga.zip", "payload.rar"), ("--bbyvt3gb.zip", "payload.r00")):
        with zipfile.ZipFile(rel / part, "w") as z:
            z.writestr(inner, b"rar-volume-bytes")
    _write(str(rel / "--bb.nfo"), b"nfo")

    result = U.unwrap_release(str(rel), str(tmp_path / "work"))

    assert result.ok
    assert result.reason is None
    assert not result.partial
    assert len(result.comics) == 1
    assert result.comics[0].lower().endswith("thecomic.cbz")
    # Source folder is left untouched — unwrap never mutates it.
    assert (rel / "--bbyvt3ga.zip").exists()


def test_unwrap_release_depth_guard(tmp_path, monkeypatch):
    """A self-nesting archive that never yields a comic aborts at max_depth."""
    monkeypatch.setattr(U, "is_allowed_path", lambda p: True)

    counter = {"n": 0}

    def fake_extract(primary, out_dir):
        counter["n"] += 1
        os.makedirs(out_dir, exist_ok=True)
        # Unique name each layer so the visited-name guard doesn't short-circuit
        # before the depth guard is exercised.
        _write(os.path.join(out_dir, f"nested{counter['n']}.rar"))
        return True, 0
    monkeypatch.setattr(U, "_extract_archive", fake_extract)

    rel = tmp_path / "release"
    rel.mkdir()
    _write(str(rel / "start.rar"))

    result = U.unwrap_release(str(rel), str(tmp_path / "work"), max_depth=3)

    assert not result.ok
    assert result.reason == "max_depth"
    assert result.comics == []


def test_unwrap_release_no_archives(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "is_allowed_path", lambda p: True)
    rel = tmp_path / "release"
    rel.mkdir()
    _write(str(rel / "readme.txt"))
    result = U.unwrap_release(str(rel), str(tmp_path / "work"))
    assert not result.ok
    assert result.reason == "no_archives"
