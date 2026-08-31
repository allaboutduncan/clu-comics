"""Content-aware classification of a loose archive dropped in WATCH.

Unpacking is unconditional, so the only remaining question is *what* the archive
is. Getting it wrong is visible either way: a pack of comics turned into a "cbz"
is unreadable, and a comic exploded into loose pages is moved to TARGET one page
at a time.

RAR cases stub ``list_rar_entries`` so the suite needs no RAR binary; the zip
cases use real archives, since zipfile is always available.
"""
import os
import zipfile

import pytest

from helpers.unwrap import (
    ARCHIVE_EXTS,
    COMIC_ARCHIVE,
    PACKED_COMICS,
    UNKNOWN_ARCHIVE,
    classify_archive,
    is_loose_archive,
    list_archive_entries,
    _meaningful_members,
)


def _zip(tmp_path, name, members):
    path = os.path.join(str(tmp_path), name)
    with zipfile.ZipFile(path, "w") as z:
        for m in members:
            z.writestr(m, b"x" * 8)
    return path


# --------------------------------------------------------------------------
# is_loose_archive
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["a.zip", "a.ZIP", "a.rar", "a.Rar"])
def test_recognises_archives(name):
    assert is_loose_archive(name) is True


@pytest.mark.parametrize("name", ["a.cbz", "a.cbr", "a.pdf", "a", "", None])
def test_ignores_non_archives(name):
    assert is_loose_archive(name) is False


def test_archive_exts_is_the_shared_constant():
    """monitor.py, core.download_utils and this module must agree."""
    from core.download_utils import ARCHIVE_EXTS as policy_exts
    assert ARCHIVE_EXTS is policy_exts


# --------------------------------------------------------------------------
# _meaningful_members
# --------------------------------------------------------------------------

def test_directory_entries_and_cruft_are_dropped():
    members = [
        "Batman/",
        "Batman/001.jpg",
        "__MACOSX/Batman/._001.jpg",
        ".DS_Store",
        r"windows\style\002.jpg",
    ]
    assert _meaningful_members(members) == ["001.jpg", "002.jpg"]


def test_empty_listing_survives_none():
    assert _meaningful_members(None) == []


# --------------------------------------------------------------------------
# classify_archive - zip
# --------------------------------------------------------------------------

def test_zip_of_comics_is_a_pack(tmp_path):
    path = _zip(tmp_path, "pack.zip", ["Batman 001.cbz", "Batman 002.cbz"])
    assert classify_archive(path) == PACKED_COMICS


def test_zip_of_pages_is_the_comic(tmp_path):
    path = _zip(tmp_path, "Batman 001.zip", ["001.jpg", "002.jpg", "003.png"])
    assert classify_archive(path) == COMIC_ARCHIVE


def test_comics_win_over_a_stray_image(tmp_path):
    """A pack with a cover jpg alongside the comics is still a pack."""
    path = _zip(tmp_path, "pack.zip", ["Batman 001.cbz", "cover.jpg"])
    assert classify_archive(path) == PACKED_COMICS


def test_pdf_counts_as_a_comic(tmp_path):
    path = _zip(tmp_path, "pack.zip", ["Heavy Metal 5.pdf"])
    assert classify_archive(path) == PACKED_COMICS


def test_zip_of_junk_is_unknown(tmp_path):
    path = _zip(tmp_path, "notes.zip", ["readme.txt", "info.nfo"])
    assert classify_archive(path) == UNKNOWN_ARCHIVE


def test_empty_zip_is_unknown(tmp_path):
    path = _zip(tmp_path, "empty.zip", [])
    assert classify_archive(path) == UNKNOWN_ARCHIVE


def test_unreadable_archive_is_unknown(tmp_path):
    """Falls back to UNKNOWN so the caller still opens it rather than stranding
    it -- 'cannot tell' must never be mistaken for 'contains pages'."""
    path = os.path.join(str(tmp_path), "broken.zip")
    with open(path, "wb") as f:
        f.write(b"not a zip at all")
    assert list_archive_entries(path) is None
    assert classify_archive(path) == UNKNOWN_ARCHIVE


def test_non_archive_extension_is_unknown(tmp_path):
    path = os.path.join(str(tmp_path), "Batman 001.cbz")
    with open(path, "wb") as f:
        f.write(b"x")
    assert list_archive_entries(path) is None
    assert classify_archive(path) == UNKNOWN_ARCHIVE


# --------------------------------------------------------------------------
# classify_archive - rar (listing stubbed; no RAR binary needed)
# --------------------------------------------------------------------------

@pytest.mark.parametrize("members,expected", [
    (["Batman 001.cbr"], PACKED_COMICS),
    (["001.jpg", "002.jpg"], COMIC_ARCHIVE),
    (["file_id.diz"], UNKNOWN_ARCHIVE),
    ([], UNKNOWN_ARCHIVE),
    (None, UNKNOWN_ARCHIVE),
])
def test_rar_classification(tmp_path, monkeypatch, members, expected):
    import helpers
    path = os.path.join(str(tmp_path), "release.rar")
    with open(path, "wb") as f:
        f.write(b"Rar!")
    monkeypatch.setattr(helpers, "list_rar_entries", lambda p: members)
    assert classify_archive(path) == expected


def test_rar_listing_errors_are_swallowed(tmp_path, monkeypatch):
    import helpers
    path = os.path.join(str(tmp_path), "release.rar")
    with open(path, "wb") as f:
        f.write(b"Rar!")

    def _boom(p):
        raise RuntimeError("no unrar here")
    monkeypatch.setattr(helpers, "list_rar_entries", _boom)

    assert list_archive_entries(path) is None
    assert classify_archive(path) == UNKNOWN_ARCHIVE


# --------------------------------------------------------------------------
# helpers._parse_rar_listing - one shape per tool
# --------------------------------------------------------------------------

def test_parse_unrar_listing():
    from helpers import _parse_rar_listing
    out = _parse_rar_listing("unrar", b"Batman 001.cbz\nBatman 002.cbz\n")
    assert out == ["Batman 001.cbz", "Batman 002.cbz"]


def test_parse_7z_listing_keeps_spaces():
    from helpers import _parse_rar_listing
    stdout = b"Path = Batman 001.cbz\nSize = 12\n\nPath = Batman 002.cbz\nSize = 12\n"
    assert _parse_rar_listing("7z", stdout) == ["Batman 001.cbz", "Batman 002.cbz"]


def test_parse_lsar_drops_the_banner():
    from helpers import _parse_rar_listing
    stdout = b"release.rar: RAR 5\n001.jpg\n002.jpg\n"
    assert _parse_rar_listing("lsar", stdout) == ["001.jpg", "002.jpg"]
