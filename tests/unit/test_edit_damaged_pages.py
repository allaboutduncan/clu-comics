"""The Edit modal on a CRC-damaged comic: flag the page, restore on failure,
and only clear the Problem Files row on evidence.

Real fixture archives, as in test_cbz_salvage.py -- what matters is what
zipfile and Pillow really do with a damaged entry.
"""

import io
import json
import zipfile
from unittest.mock import patch

import pytest
from flask import Flask
from PIL import Image

from cbz_ops import edit


def _jpeg(color):
    buf = io.BytesIO()
    Image.new("RGB", (64, 96), color).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _corrupt(path, entry, offset):
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo(entry)
    raw = bytearray(path.read_bytes())
    name_len = int.from_bytes(raw[info.header_offset + 26:info.header_offset + 28], "little")
    extra_len = int.from_bytes(raw[info.header_offset + 28:info.header_offset + 30], "little")
    start = info.header_offset + 30 + name_len + extra_len
    if offset < 0:
        offset += info.file_size
    raw[start + offset] ^= 0xFF
    path.write_bytes(bytes(raw))


@pytest.fixture
def flask_app():
    return Flask(__name__)


@pytest.fixture
def comic(tmp_path):
    path = tmp_path / "Comic 001.cbz"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("001.jpg", _jpeg("red"))
        zf.writestr("002.jpg", _jpeg("green"))
        zf.writestr("003.jpg", _jpeg("blue"))
    _corrupt(path, "002.jpg", -40)  # still decodes
    _corrupt(path, "003.jpg", 0)    # does not
    return path


def _cards(html):
    return {
        name: ("Damaged" in chunk, "CRC mismatch" in chunk)
        for chunk in html.split('<div class="col" data-rel-path="')[1:]
        for name in [chunk.split('"', 1)[0]]
    }


def test_damaged_pages_are_flagged_in_the_modal(flask_app, comic):
    with flask_app.app_context():
        result = edit.get_edit_modal(str(comic))
    cards = _cards(result["modal_body"])
    assert cards["001.jpg"] == (False, False)
    assert cards["002.jpg"] == (False, True)
    assert cards["003.jpg"] == (True, False)


def test_a_failed_open_puts_the_comic_back(tmp_path):
    path = tmp_path / "Broken.cbz"
    path.write_bytes(b"Rar!\x1a\x07\x00 not a zip at all")
    with pytest.raises(zipfile.BadZipFile):
        edit.process_cbz_file(str(path))
    assert path.exists()
    assert not (tmp_path / "Broken.zip").exists()
    assert not (tmp_path / "Broken_folder").exists()


def _save(flask_app, opened, deletes):
    form = {
        "folder_name": opened["folder_name"],
        "zip_file_path": opened["zip_file_path"],
        "original_file_path": opened["original_file_path"],
        "pending_deletes": json.dumps(deletes),
        "pending_order": "[]",
    }
    with flask_app.test_request_context("/save", method="POST", data=form):
        with patch.object(edit, "regenerate_thumbnail"):
            return edit.save_cbz()


@patch("core.library_health.recheck_file", return_value=(True, "Archive verified"))
@patch("core.problem_files.record_problem")
@patch("core.problem_files.clear_problem")
@patch("core.problem_files.has_problem", return_value=True)
def test_removing_the_bad_page_clears_the_row(_has, clear, record, recheck, flask_app, comic):
    with flask_app.app_context():
        opened = edit.get_edit_modal(str(comic))
    _save(flask_app, opened, ["003.jpg"])

    with zipfile.ZipFile(comic) as zf:
        assert zf.namelist() == ["001.jpg", "002.jpg"]
        assert zf.testzip() is None
    recheck.assert_called_once_with(str(comic))
    clear.assert_called_once()
    record.assert_not_called()


@patch("core.library_health.recheck_file", return_value=(True, "Archive verified"))
@patch("core.problem_files.record_problem")
@patch("core.problem_files.clear_problem")
@patch("core.problem_files.has_problem", return_value=True)
def test_keeping_the_bad_page_does_not_launder_it(_has, clear, record, _recheck, flask_app, comic):
    """The repacked archive passes its CRCs whatever is in it; the page that
    does not decode must keep the row open."""
    with flask_app.app_context():
        opened = edit.get_edit_modal(str(comic))
    _save(flask_app, opened, [])

    clear.assert_not_called()
    record.assert_called_once()
    assert "003.jpg" in record.call_args.kwargs["error_message"]


@patch("core.library_health.recheck_file")
@patch("core.problem_files.has_problem", return_value=False)
def test_an_ordinary_edit_does_not_recheck(_has, recheck, flask_app, comic):
    with flask_app.app_context():
        opened = edit.get_edit_modal(str(comic))
    _save(flask_app, opened, ["003.jpg"])
    recheck.assert_not_called()
