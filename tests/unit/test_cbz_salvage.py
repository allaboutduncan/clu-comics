"""Salvage rebuild against real damaged archives.

The fixtures are built here rather than mocked: the whole feature rests on what
CPython's ``zipfile`` and Pillow actually do with a CRC-mismatched entry, and a
mock would only prove what the test author assumed.
"""

import io
import zipfile

import pytest
from PIL import Image

from cbz_ops.salvage import read_entry_lenient, salvage_cbz


def _jpeg(color):
    buf = io.BytesIO()
    Image.new("RGB", (64, 96), color).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _build(path, entries):
    # ZIP_STORED so a byte flipped in the file is a byte flipped in the entry.
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        for name, data in entries:
            zf.writestr(name, data)


# A byte inside the entropy-coded scan, just before the EOI marker. Near the
# front of a small JPEG the same flip lands in the Huffman tables and the page
# no longer decodes -- which is the other test.
SCAN_DATA = -40


def _corrupt(path, entry, offset_in_entry):
    """Flip one byte of *entry*'s stored data, leaving its recorded CRC alone."""
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo(entry)
    raw = bytearray(path.read_bytes())
    name_len = int.from_bytes(raw[info.header_offset + 26:info.header_offset + 28], "little")
    extra_len = int.from_bytes(raw[info.header_offset + 28:info.header_offset + 30], "little")
    start = info.header_offset + 30 + name_len + extra_len
    if offset_in_entry < 0:
        offset_in_entry += info.file_size
    raw[start + offset_in_entry] ^= 0xFF
    path.write_bytes(bytes(raw))


@pytest.fixture
def comic(tmp_path):
    path = tmp_path / "Comic 001.cbz"
    _build(path, [
        ("001.jpg", _jpeg("red")),
        ("002.jpg", _jpeg("green")),
        ("003.jpg", _jpeg("blue")),
        ("ComicInfo.xml", b"<ComicInfo><Series>X</Series></ComicInfo>"),
    ])
    return path


def test_python_still_honours_expected_crc_none(comic):
    """``_expected_crc`` is private. If a Python upgrade stops honouring it,
    this fails loudly instead of Salvage silently becoming a no-op."""
    _corrupt(comic, "002.jpg", SCAN_DATA)
    with zipfile.ZipFile(comic) as zf:
        with pytest.raises(zipfile.BadZipFile, match="Bad CRC-32"):
            zf.read("002.jpg")
        data, crc_ok = read_entry_lenient(zf, zf.getinfo("002.jpg"))
    assert crc_ok is False
    assert len(data) == len(_jpeg("green"))


def test_a_flipped_byte_in_the_image_data_is_salvaged(comic):
    _corrupt(comic, "002.jpg", SCAN_DATA)

    result = salvage_cbz(str(comic))

    assert result["ok"] is True
    assert result["salvaged"] == [{"name": "002.jpg", "page": 2}]
    assert "page 2" in result["message"]
    with zipfile.ZipFile(comic) as zf:
        assert zf.testzip() is None
        assert zf.namelist() == ["001.jpg", "002.jpg", "003.jpg", "ComicInfo.xml"]
        assert zf.read("001.jpg") == _jpeg("red")


def test_an_undecodable_page_leaves_the_file_byte_identical(comic):
    _corrupt(comic, "003.jpg", 0)  # the JPEG SOI marker: no longer an image
    before = comic.read_bytes()

    result = salvage_cbz(str(comic))

    assert result["ok"] is False
    assert result["failed"][0]["name"] == "003.jpg"
    assert result["failed"][0]["page"] == 3
    assert "Edit" in result["message"]
    assert comic.read_bytes() == before


def test_one_bad_page_blocks_the_rewrite_even_if_another_decodes(comic):
    _corrupt(comic, "002.jpg", SCAN_DATA)
    _corrupt(comic, "003.jpg", 0)
    before = comic.read_bytes()

    result = salvage_cbz(str(comic))

    assert result["ok"] is False
    assert [e["page"] for e in result["salvaged"]] == [2]
    assert [e["page"] for e in result["failed"]] == [3]
    assert comic.read_bytes() == before


def test_a_clean_archive_is_not_rewritten(comic):
    before = comic.read_bytes()
    result = salvage_cbz(str(comic))
    assert result["ok"] is False
    assert result["salvaged"] == [] and result["failed"] == []
    assert comic.read_bytes() == before


def test_a_non_zip_reports_instead_of_raising(tmp_path):
    path = tmp_path / "x.cbz"
    path.write_bytes(b"Rar!\x1a\x07\x00 not a zip")
    result = salvage_cbz(str(path))
    assert result["ok"] is False
    assert "open" in result["message"].lower()
