"""Stable synthetic ids for ComicVine-only series.

The ``series`` table is keyed by the Metron series id. A series that exists only
in ComicVine (a sidecar with a ``cv_id`` but no Metron match) has no Metron id,
so we mint a stable, positive, collision-free key by offsetting its ComicVine
volume id into a reserved high range. Metron series ids are small (< ~10^6), so
the offset guarantees no overlap while keeping the id positive (the series-slug
/URL scheme extracts a trailing positive integer, which a negative id would
break).

An id at or above the offset flags the series as ComicVine-sourced, so Metron
sync paths can skip it and ComicVine paths can recover the real ``cv_id``.
"""

COMICVINE_SERIES_ID_OFFSET = 2_000_000_000


def make_comicvine_series_id(cv_id):
    """Map a ComicVine volume id to its reserved series-table id."""
    return COMICVINE_SERIES_ID_OFFSET + int(cv_id)


def is_comicvine_series_id(series_id):
    """True when ``series_id`` is a ComicVine-sourced (offset) id."""
    try:
        return series_id is not None and int(series_id) >= COMICVINE_SERIES_ID_OFFSET
    except (TypeError, ValueError):
        return False


def cv_id_from_series_id(series_id):
    """Recover the ComicVine volume id from a ComicVine-sourced series id."""
    return int(series_id) - COMICVINE_SERIES_ID_OFFSET
