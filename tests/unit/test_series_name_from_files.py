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


def test_strips_trailing_dash_separator(tmp_path):
    # "Series - NNN (Month, Year)" naming must not leave a trailing " -".
    _make_comic(tmp_path, "Black Cat - 001 (October, 2025).cbz")
    assert get_series_name_from_files(str(tmp_path), "Black Cat") == "Black Cat"


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


# A bracketed rename pattern -- "{series_name} #{issue_number}
# [{issue_month_M} {issue_year}]" -- used to leave "[...]" at the end of the
# name, so the end-anchored issue strip never fired and the whole filename
# became the series name. Every name below is from a real support log.
def test_strips_bracketed_month_year(tmp_path):
    _make_comic(tmp_path, "Sicko #01 [July 2026].cbz")
    assert get_series_name_from_files(str(tmp_path), "Sicko") == "Sicko"


def test_strips_bracket_with_blank_month(tmp_path):
    _make_comic(tmp_path, "Bleeding Hearts #01 [ 2026].cbz")
    assert get_series_name_from_files(str(tmp_path), "Bleeding Hearts") == "Bleeding Hearts"


def test_strips_bracket_with_comma(tmp_path):
    _make_comic(tmp_path, "Of the Earth #002 [June, 2026].cbr")
    assert get_series_name_from_files(str(tmp_path), "Of the Earth") == "Of the Earth"


def test_keeps_punctuation_inside_series_name(tmp_path):
    _make_comic(tmp_path, "Shaolin Cowboy - Staying A.i. Live #002 [ 2026].cbr")
    assert (
        get_series_name_from_files(str(tmp_path), "The Shaolin Cowboy: Staying A.I. Live")
        == "Shaolin Cowboy - Staying A.i. Live"
    )


def test_hash_marker_drops_trailing_issue_title(tmp_path):
    _make_comic(tmp_path, "Nightwing #117 - Absolute Power.cbz")
    assert get_series_name_from_files(str(tmp_path), "Nightwing") == "Nightwing"


# #790: an issue title after the year. The parentheticals were removed first,
# so the number was no longer at the end and " 001 - Title" stayed in the name.
# The wider table is tests/unit/test_filename_corpus.py.
def test_title_after_year_is_not_part_of_the_series(tmp_path):
    _make_comic(
        tmp_path,
        "Avengers Assemble Featuring Captain Citrus 001 (2014) - Choose Wisely.cbr",
    )
    assert (
        get_series_name_from_files(
            str(tmp_path), "Avengers Assemble Featuring Captain Citrus"
        )
        == "Avengers Assemble Featuring Captain Citrus"
    )


def test_title_ending_in_a_number_is_not_part_of_the_series(tmp_path):
    _make_comic(tmp_path, "Batman 497 (1993) - Knightfall, Part 11.cbz")
    assert get_series_name_from_files(str(tmp_path), "Batman") == "Batman"


def test_number_inside_series_name_survives(tmp_path):
    # Cutting at the first number followed by a dash would leave "Spider-Man",
    # which claims more files than the series owns.
    _make_comic(tmp_path, "Spider-Man 2099 - Dark Genesis 001 (2023).cbz")
    assert (
        get_series_name_from_files(str(tmp_path), "Spider-Man 2099: Dark Genesis")
        == "Spider-Man 2099 - Dark Genesis"
    )


def test_negative_issue_is_stripped_with_its_sign(tmp_path):
    _make_comic(tmp_path, "Amazing Spider-Man -001 (1997) - Flashback.cbz")
    assert (
        get_series_name_from_files(str(tmp_path), "The Amazing Spider-Man")
        == "Amazing Spider-Man"
    )
