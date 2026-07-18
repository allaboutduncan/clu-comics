"""Tests for helpers/comicvine_ids.py -- synthetic ids for ComicVine-only series."""
from helpers.comicvine_ids import (
    COMICVINE_SERIES_ID_OFFSET,
    cv_id_from_series_id,
    is_comicvine_series_id,
    make_comicvine_series_id,
)


def test_round_trip():
    sid = make_comicvine_series_id(18705)
    assert sid == COMICVINE_SERIES_ID_OFFSET + 18705
    assert cv_id_from_series_id(sid) == 18705


def test_is_comicvine_series_id():
    assert is_comicvine_series_id(make_comicvine_series_id(1)) is True
    assert is_comicvine_series_id(COMICVINE_SERIES_ID_OFFSET) is True
    assert is_comicvine_series_id(12345) is False
    assert is_comicvine_series_id(None) is False
    assert is_comicvine_series_id("not-an-int") is False


def test_metron_ids_never_flagged():
    # Realistic Metron series ids are far below the offset.
    for mid in (1, 100, 50_000, 999_999):
        assert is_comicvine_series_id(mid) is False
