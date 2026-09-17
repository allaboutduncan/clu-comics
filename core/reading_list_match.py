"""Re-running local file matching over a reading list's entries.

The matcher gets stricter over time, but a reading list stores the path it
picked, so a fix corrects nothing already imported. This is the pass that
corrects it, and it has three callers that must not drift:

- the Re-match button (``routes.reading_lists.process_rematch``),
- the nightly GetComics sweep, which re-matches opted-in lists *before* it
  decides what is still wanted, and
- ``app.process_incoming_wanted_issues``, which fills gaps the moment files are
  filed out of TARGET into the library (``fill_gaps_for_series``).

The sweep's call is what closes the download loop. A reading-list entry has no
``mapped_path``, so ``process_incoming_wanted_issues`` cannot file a completed
download against it; the entry would stay unmatched and be re-queued every
night. Re-matching first means that once the normal WATCH/TARGET pipeline has
filed the comic into the library, the entry matches itself and drops off the
wanted list.

``fill_gaps_for_series`` exists because the sweep cannot cover the one case that
needs it most. ``core.wanted_reading_lists._released()`` drops an entry whose
year is still in the future -- rightly, there is nothing to search GetComics for
-- so such an entry is never part of the sweep's work. When it finally ships it
arrives as a *mapped-series* wanted issue, and the arrival is then the only
event that can close the gap.
"""

import threading

from core.app_logging import app_logger
from core.database import set_reading_list_entry_auto_match
from models.cbl import CBLLoader

# Belt-and-braces bound on a single gap pass, on top of the series filter.
# ``process_incoming_wanted_issues`` runs inline on the request thread for
# POST /api/scan-downloads, and each entry costs up to ~8 LIKE scans over
# file_index.
MAX_GAP_ENTRIES_PER_PASS = 500

# Non-blocking, same shape as ``core.problem_replacements._pass_lock``: two
# callers drive a TARGET sweep (api.py's daemon thread and /api/scan-downloads)
# and both would walk the same gaps. Whoever is second stands down; the work is
# idempotent.
_gap_lock = threading.Lock()


def _resolve_rename_pattern(rename_pattern):
    """CUSTOM_RENAME_PATTERN, however the caller can get at it.

    The sweep and the TARGET hook run without an application context, so
    ``current_app`` is unavailable and the caller passes ``app.config``'s copy;
    the preference is read directly as a fallback. Defaulting straight to the
    built-in pattern would quietly match against filenames the user does not
    actually use, so that is the last resort, not the first.
    """
    if not rename_pattern:
        try:
            from core.database import get_user_preference

            rename_pattern = get_user_preference("custom_rename_pattern", default="")
        except Exception:
            rename_pattern = None
    return rename_pattern or "{series_name} {issue_number}"


def rematch_entries(entries, rename_pattern, progress_cb=None):
    """Re-run file matching over a list's entries.

    A hand-picked mapping is the user's answer, not the matcher's, so entries
    with ``manual_override_path`` are left alone.

    Args:
        entries: reading-list entry dicts (as ``get_reading_list`` returns).
        rename_pattern: CUSTOM_RENAME_PATTERN, for filename-tier matching.
        progress_cb: called as ``(index, total, entry)`` after each entry.

    Returns:
        ``{"matched": int, "cleared": int, "skipped": int}``
    """
    entries = list(entries or [])
    loader = CBLLoader(
        "<ReadingList><Name>x</Name><Books/></ReadingList>",
        rename_pattern=rename_pattern,
    )
    loader.prefetch_metron_ids([e.get("metron_id") for e in entries])

    matched = cleared = skipped = 0
    for i, entry in enumerate(entries):
        if entry.get("manual_override_path"):
            skipped += 1
        else:
            volume_year, issue_years = CBLLoader.entry_year_hints(entry)
            new_path = loader.match_file(
                entry.get("series"), str(entry.get("issue_number") or ""),
                volume_year=volume_year,
                issue_years=issue_years,
                metron_id=entry.get("metron_id"),
            )
            if new_path != entry.get("matched_file_path"):
                # Writes None as readily as a path: an entry the stricter
                # matcher now rejects must go back to showing as unmatched.
                set_reading_list_entry_auto_match(entry["id"], new_path)
            if new_path:
                matched += 1
            elif entry.get("matched_file_path"):
                cleared += 1

        if progress_cb:
            progress_cb(i, len(entries), entry)

    return {"matched": matched, "cleared": cleared, "skipped": skipped}


def rematch_tracked_lists(rename_pattern=None):
    """Re-match every reading list opted into the wanted list.

    Run by the sweep before it collects work, so an issue whose download has
    since been filed into the library is no longer considered wanted. Failures
    are logged and swallowed -- a list that cannot be re-matched must not abort
    the sweep, it just stays wanted until the next run.

    Args:
        rename_pattern: CUSTOM_RENAME_PATTERN. The sweep runs on a scheduler
            thread with no application context, so ``current_app`` is not
            available to look it up -- the caller passes ``app.config``'s copy,
            and the preference is read directly as a fallback. Defaulting to
            the built-in pattern instead would quietly match against filenames
            the user does not actually use.

    Returns:
        ``{"lists": int, "matched": int, "cleared": int}``
    """
    from core.database import get_reading_list, get_db_connection

    try:
        conn = get_db_connection()
        if not conn:
            return {"lists": 0, "matched": 0, "cleared": 0}
        c = conn.cursor()
        c.execute(
            "SELECT id FROM reading_lists WHERE COALESCE(track_wanted, 0) = 1"
        )
        list_ids = [r[0] for r in c.fetchall()]
        conn.close()
    except Exception as e:
        app_logger.error(f"Could not list tracked reading lists: {e}")
        return {"lists": 0, "matched": 0, "cleared": 0}

    if not list_ids:
        return {"lists": 0, "matched": 0, "cleared": 0}

    rename_pattern = _resolve_rename_pattern(rename_pattern)

    totals = {"lists": 0, "matched": 0, "cleared": 0}
    for list_id in list_ids:
        try:
            reading_list = get_reading_list(list_id)
            if not reading_list:
                continue
            result = rematch_entries(
                reading_list.get("entries") or [], rename_pattern
            )
            totals["lists"] += 1
            totals["matched"] += result["matched"]
            totals["cleared"] += result["cleared"]
        except Exception as e:
            app_logger.error(f"Re-match of reading list {list_id} failed: {e}")

    if totals["lists"]:
        app_logger.info(
            f"Re-matched {totals['lists']} tracked reading list(s): "
            f"{totals['matched']} matched, {totals['cleared']} cleared"
        )
    return totals


def _norm(name):
    """The series-name equivalence class used to scope a gap pass.

    Deliberately the normaliser ``core.wanted_reading_lists`` already uses for
    de-duplication, so "Batman: Year One" filed from a mapped series and
    "Batman - Year One" in a CBL collapse together rather than missing.
    """
    try:
        from core.wanted_reading_lists import _norm_series

        return _norm_series(name)
    except Exception:
        return str(name or "").strip().lower()


def unmatched_tracked_entries(series_names=None):
    """Unmatched entries of every reading list opted into the wanted list.

    Shaped exactly as ``rematch_entries`` consumes entries, which is why this is
    its own query rather than a call to
    ``core.wanted_reading_lists.reading_list_wanted_rows()``: that one keys rows
    as ``entry_id`` and selects no ``volume``, and ``CBLLoader.entry_year_hints``
    needs ``volume`` for the volume-year hint. The WHERE clause is deliberately
    the same predicate -- tracked, both path columns NULL, non-blank series and
    issue number (``CBLLoader.match_file`` returns None immediately for a blank
    either side, so such an entry could never be filled).

    Args:
        series_names: when given, keep only entries whose series matches one of
            these, compared through ``_norm``. Filtered in Python, not SQL:
            the normaliser is shared code, not something to re-implement as a
            LIKE clause.

    Returns:
        List of entry dicts, or ``[]`` on any failure.
    """
    from core.database import get_db_connection

    try:
        conn = get_db_connection()
        if not conn:
            return []
        c = conn.cursor()
        c.execute(
            """
            SELECT e.id                   AS id,
                   e.series               AS series,
                   e.issue_number         AS issue_number,
                   e.volume               AS volume,
                   e.year                 AS year,
                   e.issue_year           AS issue_year,
                   e.metron_id            AS metron_id,
                   e.matched_file_path    AS matched_file_path,
                   e.manual_override_path AS manual_override_path,
                   e.reading_list_id      AS reading_list_id
            FROM reading_list_entries e
            JOIN reading_lists rl ON rl.id = e.reading_list_id
            WHERE COALESCE(rl.track_wanted, 0) = 1
              AND e.matched_file_path IS NULL
              AND e.manual_override_path IS NULL
              AND TRIM(COALESCE(e.series, '')) <> ''
              AND TRIM(COALESCE(e.issue_number, '')) <> ''
            ORDER BY e.reading_list_id, e.sort_order, e.id
            """
        )
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
    except Exception as e:
        app_logger.error(f"Could not read unmatched reading-list entries: {e}")
        return []

    if series_names is None:
        return rows

    wanted = {_norm(n) for n in series_names if n}
    if not wanted:
        return []
    return [r for r in rows if _norm(r.get("series")) in wanted]


def fill_gaps_for_series(series_names, rename_pattern=None):
    """Try to fill tracked lists' gaps with files that just entered the library.

    Called from ``app.process_incoming_wanted_issues`` once files have been
    filed out of TARGET. Every entry handed to ``rematch_entries`` here is
    already unmatched, so that loop can only ever *write* a path -- a miss
    leaves ``matched_file_path`` NULL and writes nothing. That is the whole
    reason this is not simply ``rematch_tracked_lists``: clearing a match is a
    decision for the nightly sweep and the explicit Re-match button, not for a
    file arriving.

    The pass is scoped to the series that just moved because it can run on the
    request thread (POST /api/scan-downloads calls the sweep inline) and an
    unscoped pass is ~8 LIKE scans per gap. A series whose name does not
    normalise onto an entry's is simply not covered here; the nightly
    ``rematch_tracked_lists`` still catches it.

    Everything is swallowed: a reading-list re-match must never break the
    wanted-issue import it is hooked onto.

    Args:
        series_names: series names of the issues just filed into the library.
        rename_pattern: CUSTOM_RENAME_PATTERN, for filename-tier matching.

    Returns:
        ``{"entries": int, "matched": int}``
    """
    none = {"entries": 0, "matched": 0}
    names = [n for n in (series_names or ()) if n]
    if not names:
        return none

    if not _gap_lock.acquire(blocking=False):
        app_logger.debug("Reading-list gap pass already running; standing down")
        return none
    try:
        entries = unmatched_tracked_entries(names)
        if not entries:
            return none
        if len(entries) > MAX_GAP_ENTRIES_PER_PASS:
            app_logger.info(
                f"Reading-list gap pass capped at {MAX_GAP_ENTRIES_PER_PASS} "
                f"of {len(entries)} unmatched entries"
            )
            entries = entries[:MAX_GAP_ENTRIES_PER_PASS]

        result = rematch_entries(entries, _resolve_rename_pattern(rename_pattern))
        if result["matched"]:
            app_logger.info(
                f"Filled {result['matched']} reading-list gap(s) from "
                f"{len(entries)} unmatched entr(ies) after a TARGET move"
            )
        return {"entries": len(entries), "matched": result["matched"]}
    except Exception as e:
        app_logger.error(f"Reading-list gap pass failed: {e}")
        return none
    finally:
        _gap_lock.release()
