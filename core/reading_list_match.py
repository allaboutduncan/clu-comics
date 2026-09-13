"""Re-running local file matching over a reading list's entries.

The matcher gets stricter over time, but a reading list stores the path it
picked, so a fix corrects nothing already imported. This is the pass that
corrects it, and it has two callers that must not drift:

- the Re-match button (``routes.reading_lists.process_rematch``), and
- the nightly GetComics sweep, which re-matches opted-in lists *before* it
  decides what is still wanted.

The sweep's call is what closes the download loop. A reading-list entry has no
``mapped_path``, so ``process_incoming_wanted_issues`` cannot file a completed
download against it; the entry would stay unmatched and be re-queued every
night. Re-matching first means that once the normal WATCH/TARGET pipeline has
filed the comic into the library, the entry matches itself and drops off the
wanted list.
"""

from core.app_logging import app_logger
from core.database import set_reading_list_entry_auto_match
from models.cbl import CBLLoader


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

    if not rename_pattern:
        try:
            from core.database import get_user_preference

            rename_pattern = get_user_preference("custom_rename_pattern", default="")
        except Exception:
            rename_pattern = None
    rename_pattern = rename_pattern or "{series_name} {issue_number}"

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
