"""Tests for helpers.collection.get_series_name_from_files.

Regression coverage for the wanted-issue matching bug: a series stored with
"NNN of M" filenames (e.g. 'Is Ted Ok 001 of 5.cbz') had only the trailing
" M" stripped, leaving 'Is Ted Ok 001 of' as the derived series name — which
was then baked into the match regex as a literal and never matched the wanted
file. See helpers/collection.py.
"""
from helpers.collection import get_series_name_from_files


def _make_comic(dir_path, filename):
    f = dir_path / filename
    f.write_bytes(b"PK\x03\x04")  # minimal zip-ish header; contents are irrelevant
    return f


def test_strips_issue_of_total_count(tmp_path):
    _make_comic(tmp_path, "Is Ted Ok 001 of 5.cbz")
    assert get_series_name_from_files(str(tmp_path), "Is Ted OK?") == "Is Ted Ok"


def test_plain_issue_with_year(tmp_path):
    _make_comic(tmp_path, "Hidden Springs 001 (2026).cbz")
    assert get_series_name_from_files(str(tmp_path), "Hidden Springs") == "Hidden Springs"


def test_preserves_of_within_series_name(tmp_path):
    _make_comic(tmp_path, "Crisis of Infinite Earths 001.cbz")
    assert (
        get_series_name_from_files(str(tmp_path), "Crisis of Infinite Earths")
        == "Crisis of Infinite Earths"
    )


def test_empty_folder_falls_back_to_db_name(tmp_path):
    assert get_series_name_from_files(str(tmp_path), "Hidden Springs") == "Hidden Springs"


def test_missing_path_falls_back_to_db_name(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert get_series_name_from_files(str(missing), "Hidden Springs") == "Hidden Springs"
