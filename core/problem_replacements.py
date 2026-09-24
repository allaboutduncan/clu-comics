"""Filing a downloaded replacement straight onto the damaged file it replaces.

When a user searches from the Problem Files page, they have already told us
where the download belongs: the damaged file's own path. Without that, a
replacement just sits in TARGET forever -- the wanted sweep only files issues
that are *missing*, and a corrupt file is still a file, so nothing claims it.

The flow is:

    claim_replacement(...)   at the moment the download is queued
    apply_pending(...)       whenever something finishes landing in TARGET
    acknowledge(...)         when the user dismisses the "Replaced" banner

Things here that look arbitrary and are not:

- **Matching reuses `match_wanted_issues_to_files`.** It is the codebase's one
  matcher that moves and renames files, it already opts into `strict_gap=True`
  (so `TMNT - Nightwatcher 003` cannot satisfy TMNT #3), and it already handles
  aliases and the ComicInfo fallback. A second matcher here would be a third
  system to keep in step with the other two.
- **The old file goes to the trash, it is not overwritten in place.** The user
  asked for the bad copy to be replaced, and it is -- but "replace" and
  "destroy with no way back" are not the same promise. `move_to_trash` is
  size-capped and has a restore manifest, so a wrong swap costs a restore rather
  than a re-download. It also means the swap is never a partial write over a
  file something else may be reading.
- **The replacement is verified before anything is moved.** Overwriting a
  damaged file with an equally damaged one would destroy the original and leave
  the user no better off -- and a partly-readable original is worth more than a
  broken replacement. `verify_replacement` CRC-checks the whole archive, so a
  file that fails is left in TARGET and the entry is marked failed rather than
  silently retried forever.
- **A failed apply is terminal until the user acts.** It stays visible with its
  reason instead of going back to `pending`; otherwise every sweep would retry
  the same broken download and the page would never say why nothing happened.
"""

import os
import shutil
import threading
import zipfile

from core.app_logging import app_logger

STATUS_PENDING = "pending"
STATUS_APPLIED = "applied"
STATUS_FAILED = "failed"

# Re-exported, not redefined: the TARGET scan is shared with the wanted pass
# via helpers.collection.collect_target_candidates, and two lists of what
# counts as a comic would let the two passes disagree about what is in TARGET.
from helpers.collection import TARGET_COMIC_EXTENSIONS as COMIC_EXTENSIONS

# Image extensions that count as "this archive contains pages".
_PAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")

# Two callers can drive the pass at once -- the download pipeline's sweep
# (a daemon thread) and the page, which polls every 10s while a swap is
# outstanding. Both walk the same TARGET and would race to move the same
# file. Whoever is second simply does nothing; the work is idempotent and
# the next tick picks it up.
_pass_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------

def claim_replacement(target_path, series="", issue="", query="", source=""):
    """Record that a download now in flight is meant to replace ``target_path``.

    Re-claiming an existing row resets it to pending and clears any previous
    failure, so a user who tries a different result is not blocked by the last
    attempt. Never raises.
    """
    try:
        from core.database import get_db_connection

        conn = get_db_connection()
        if not conn:
            return False
        try:
            conn.execute(
                """
                INSERT INTO problem_file_replacements
                    (target_path, series, issue, query, source, status,
                     detail, trashed_path, new_filename,
                     requested_at, applied_at, acknowledged_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL,
                        CURRENT_TIMESTAMP, NULL, NULL)
                ON CONFLICT(target_path) DO UPDATE SET
                    series          = excluded.series,
                    issue           = excluded.issue,
                    query           = excluded.query,
                    source          = excluded.source,
                    status          = excluded.status,
                    detail          = NULL,
                    trashed_path    = NULL,
                    new_filename    = NULL,
                    requested_at    = CURRENT_TIMESTAMP,
                    applied_at      = NULL,
                    acknowledged_at = NULL
                """,
                (target_path, series or "", issue or "", query or "",
                 source or "", STATUS_PENDING),
            )
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception as e:
        app_logger.error(f"Could not claim replacement for {target_path}: {e}")
        return False


def list_replacements(include_acknowledged=False):
    """Every replacement record, newest first."""
    try:
        from core.database import get_db_connection

        conn = get_db_connection()
        if not conn:
            return []
        try:
            sql = "SELECT * FROM problem_file_replacements"
            if not include_acknowledged:
                sql += " WHERE acknowledged_at IS NULL"
            sql += " ORDER BY requested_at DESC"
            rows = [dict(r) for r in conn.execute(sql).fetchall()]
            for row in rows:
                row["filename"] = os.path.basename(row["target_path"])
            return rows
        finally:
            conn.close()
    except Exception as e:
        app_logger.error(f"Could not list replacements: {e}")
        return []


def get_replacement(target_path):
    try:
        from core.database import get_db_connection

        conn = get_db_connection()
        if not conn:
            return None
        try:
            row = conn.execute(
                "SELECT * FROM problem_file_replacements WHERE target_path = ?",
                (target_path,),
            ).fetchone()
            if not row:
                return None
            record = dict(row)
            record["filename"] = os.path.basename(record["target_path"])
            return record
        finally:
            conn.close()
    except Exception as e:
        app_logger.error(f"Could not read replacement for {target_path}: {e}")
        return None


def _set_status(target_path, status, detail=None, trashed_path=None,
                new_filename=None):
    try:
        from core.database import get_db_connection

        conn = get_db_connection()
        if not conn:
            return
        try:
            conn.execute(
                """
                UPDATE problem_file_replacements
                   SET status = ?, detail = ?,
                       trashed_path = COALESCE(?, trashed_path),
                       new_filename = COALESCE(?, new_filename),
                       applied_at = CASE WHEN ? = 'applied'
                                         THEN CURRENT_TIMESTAMP ELSE applied_at END
                 WHERE target_path = ?
                """,
                (status, detail, trashed_path, new_filename, status, target_path),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        app_logger.error(f"Could not update replacement {target_path}: {e}")


def acknowledge(target_path):
    """Dismiss a finished replacement from the page's banner."""
    try:
        from core.database import get_db_connection

        conn = get_db_connection()
        if not conn:
            return False
        try:
            cur = conn.execute(
                "UPDATE problem_file_replacements "
                "SET acknowledged_at = CURRENT_TIMESTAMP WHERE target_path = ?",
                (target_path,),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception as e:
        app_logger.error(f"Could not acknowledge replacement {target_path}: {e}")
        return False


def cancel(target_path):
    """Drop the record entirely -- the user no longer wants this swap."""
    try:
        from core.database import get_db_connection

        conn = get_db_connection()
        if not conn:
            return False
        try:
            cur = conn.execute(
                "DELETE FROM problem_file_replacements WHERE target_path = ?",
                (target_path,),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()
    except Exception as e:
        app_logger.error(f"Could not cancel replacement {target_path}: {e}")
        return False


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_replacement(path):
    """``(ok, reason)`` -- is this file safe to put in the library?

    Deliberately strict, because the alternative to swapping is keeping a file
    that at least partly reads. A replacement has to be a complete archive with
    pages in it: ``testzip`` walks every entry's CRC, which is exactly the
    damage class that put the original on the page in the first place.

    Non-ZIP archives (a ``.cbr`` that never got converted) pass the structural
    check by default -- CLU has no CRC check for RAR, and refusing them would
    reject a perfectly good download.
    """
    try:
        if not os.path.isfile(path):
            return False, "the downloaded file is no longer there"
        if os.path.getsize(path) == 0:
            return False, "the downloaded file is empty"
    except OSError as e:
        return False, f"could not read the downloaded file ({e})"

    if not zipfile.is_zipfile(path):
        # A RAR/CBR: nothing here can check it, and the WATCH pipeline converts
        # these anyway. Accept rather than block a good download.
        return True, ""

    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad is not None:
                return False, f"the replacement is damaged too ({bad} failed its checksum)"
            pages = [
                n for n in zf.namelist()
                if n.lower().endswith(_PAGE_EXTENSIONS)
                and not os.path.basename(n).startswith(("._", "."))
            ]
            if not pages:
                return False, "the replacement contains no page images"
    except Exception as e:
        from helpers import archive_error_detail

        return False, f"the replacement could not be read ({archive_error_detail(e)})"

    return True, ""


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------

def is_acceptable_replacement(target_path, src):
    """``(ok, reason)`` -- may this file stand in for the damaged one?

    A `.cbr` must not replace a `.cbz`. TARGET holds a `.cbr` only when the
    WATCH pipeline has not converted it yet, and swapping one in would quietly
    downgrade the library to a format the rest of CLU treats as second-class.
    This is a "not ready", not a failure: the entry stays pending and the next
    pass picks the file up once it has been converted.
    """
    src_ext = os.path.splitext(src)[1].lower()
    target_ext = os.path.splitext(target_path)[1].lower()
    if src_ext == target_ext:
        return True, ""
    if src_ext == ".cbz":
        # An upgrade (the damaged file was itself a .cbr) -- always welcome.
        return True, ""
    return False, (
        f"waiting for {os.path.basename(src)} to be converted to "
        f"{target_ext or '.cbz'}"
    )


def _destination_for(target_path, src):
    """Where the replacement lands.

    The damaged file's own path, exactly -- that is the whole point. The one
    exception is an extension change (a ``.cbr`` that reached TARGET
    unconverted): keep the original folder and stem so the library's naming
    holds, and take the new extension so the file is not mislabelled.
    """
    src_ext = os.path.splitext(src)[1].lower()
    target_ext = os.path.splitext(target_path)[1].lower()
    if src_ext and src_ext != target_ext:
        return os.path.splitext(target_path)[0] + src_ext
    return target_path


def _swap(row, src):
    """Stage the replacement beside the damaged file, then trade them over.

    Returns ``(ok, detail, trashed_to)``.

    The order is load-bearing, and it is not the obvious one. Trashing first and
    moving second looks natural and is wrong: ``move_to_trash`` prunes the
    parent directory once it empties, so on a single-issue folder the
    destination stopped existing between the two steps and the move failed with
    "cannot find the path specified" -- leaving the library slot empty and the
    replacement still sitting in TARGET.

    So the replacement is staged into the destination folder *first*, under a
    hidden name. The folder therefore never becomes empty, nothing prunes it,
    and the final step is an ``os.replace`` within one directory, which is
    atomic: no reader ever sees a half-written comic, and there is no window
    where the issue is absent from the library.
    """
    target_path = row["target_path"]
    ok, why = verify_replacement(src)
    if not ok:
        return False, why, None

    dest = _destination_for(target_path, src)
    dest_dir = os.path.dirname(dest)
    staged = os.path.join(dest_dir, "." + os.path.basename(dest) + ".clu_incoming")
    trashed_to = None

    try:
        os.makedirs(dest_dir, exist_ok=True)
        if os.path.exists(staged):
            os.remove(staged)
        shutil.move(src, staged)
    except Exception as e:
        return False, f"could not stage the replacement ({e})", None

    try:
        if os.path.exists(target_path):
            from helpers.trash import move_to_trash

            result = move_to_trash(target_path)
            trashed_to = result.get("path")
    except Exception as e:
        _unstage(staged, src)
        return False, f"could not move the damaged file to the trash ({e})", None

    try:
        # Same directory, so this is atomic and cannot fail cross-device.
        os.replace(staged, dest)
        if dest != target_path and os.path.exists(target_path):
            os.remove(target_path)
    except Exception as e:
        _unstage(staged, src)
        _restore_trashed(trashed_to, target_path)
        return False, f"could not move the replacement into place ({e})", None

    try:
        from helpers import match_parent_permissions

        match_parent_permissions(dest)
    except Exception as e:
        # A permissions miss makes thumbnails fail later, but the file is in
        # place and the swap did happen -- report the swap, log the rest.
        app_logger.error(f"Could not normalise permissions on {dest}: {e}")

    _refresh_after_swap(target_path, dest)
    return True, "", trashed_to


def _unstage(staged, original_src):
    """Put a staged replacement back in TARGET so the download is not lost.

    The destination folder may be gone: the wanted sweep calls
    `schedule_target_cleanup`, which prunes TARGET's empty folders, and staging
    the file is exactly what empties one. So recreate the folder rather than
    assume it survived.

    If it still cannot go back, the staged file is *kept* and its location
    logged. A stranded download is recoverable by hand; a deleted one is not.
    It is hidden (a leading dot), so `is_hidden` keeps it out of the library
    index in the meantime.
    """
    if not os.path.exists(staged):
        return
    try:
        if os.path.exists(original_src):
            os.remove(staged)
            return
        os.makedirs(os.path.dirname(original_src), exist_ok=True)
        shutil.move(staged, original_src)
    except Exception as e:
        app_logger.error(
            f"Could not return the download to {original_src}: {e}. "
            f"It is still on disk at {staged} — move it back by hand or delete it."
        )


def _restore_trashed(trashed_to, target_path):
    """Put the damaged file back rather than leave the library slot empty.

    A failed swap must be a no-op from the user's point of view. A damaged
    comic is still the comic they had, and it is what the Problem Files entry
    describes.
    """
    if not trashed_to:
        return
    try:
        if os.path.exists(trashed_to) and not os.path.exists(target_path):
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            shutil.move(trashed_to, target_path)
            app_logger.info(f"Restored {target_path} after a failed replacement")
    except Exception as e:
        app_logger.error(
            f"Could not restore {target_path} from {trashed_to} after a failed "
            f"replacement: {e}"
        )


def _refresh_after_swap(target_path, dest):
    """Clear everything that still describes the file we just retired."""
    try:
        from core.problem_files import clear_problem

        clear_problem(target_path)
        if dest != target_path:
            clear_problem(dest)
    except Exception as e:
        app_logger.error(f"Could not clear problem entries for {target_path}: {e}")

    try:
        from core.thumbnail_cache import invalidate_thumbnail, regenerate_thumbnail

        # The path is usually unchanged, so the cache cannot notice on its own.
        invalidate_thumbnail(target_path)
        if dest != target_path:
            invalidate_thumbnail(dest)
        regenerate_thumbnail(dest)
    except Exception as e:
        app_logger.error(f"Could not refresh the thumbnail for {dest}: {e}")

    try:
        from core.database import invalidate_browse_cache

        invalidate_browse_cache(os.path.dirname(dest))
    except Exception as e:
        app_logger.error(f"Could not invalidate the browse cache for {dest}: {e}")


def apply_pending(target_folder, match_pattern, alias_lookup=None):
    """File any finished downloads onto the damaged files they replace.

    Returns the list of records that changed state, so a caller can report
    them. Never raises: this runs inside the download pipeline, and a failure
    to file a replacement must not break the sweep it is attached to.
    """
    if not _pass_lock.acquire(blocking=False):
        # Another pass is already walking TARGET. Doing it twice would race to
        # move the same file; the work is idempotent, so let the next tick do it.
        app_logger.debug("Replacement pass already running; skipping this call")
        return []
    try:
        return _apply_pending_locked(target_folder, match_pattern, alias_lookup)
    finally:
        _pass_lock.release()


def _apply_pending_locked(target_folder, match_pattern, alias_lookup=None):
    try:
        rows = [r for r in list_replacements(include_acknowledged=True)
                if r["status"] == STATUS_PENDING]
        if not rows:
            return []

        if not target_folder or not os.path.isdir(target_folder):
            return []

        # Only rows we can actually match on. A claim with no series or issue
        # (an unparseable filename) would match the first file in TARGET.
        wanted = []
        for row in rows:
            if not (row.get("series") or "").strip():
                continue
            if not str(row.get("issue") or "").strip():
                continue
            wanted.append({
                "number": row["issue"],
                "series_name": row["series"],
                "mapped_path": os.path.dirname(row["target_path"]),
                "_row": row,
            })
        if not wanted:
            return []

        # The destructive twin of app.process_incoming_wanted_issues' scan: a
        # match here TRASHES the file it replaces, so a bare recursive walk of
        # a TARGET that sits inside a library could take an already-filed comic
        # and destroy another one with it. Same collector, same exclusions.
        #
        # skip_converted_siblings=False: this pass decides for itself whether a
        # .cbr may stand in for a .cbz, in is_acceptable_replacement, and it
        # holds the entry rather than dropping the candidate.
        from helpers.collection import (
            collect_target_candidates,
            match_wanted_issues_to_files,
        )

        files, _restricted = collect_target_candidates(
            target_folder, skip_converted_siblings=False
        )
        if not files:
            return []

        matches = match_wanted_issues_to_files(
            wanted, files, match_pattern, alias_lookup=alias_lookup
        )

        changed = []
        for match in matches:
            row = match["issue"]["_row"]
            src = match["src"]
            target_path = row["target_path"]

            ready, why = is_acceptable_replacement(target_path, src)
            if not ready:
                # Not a failure: leave the entry pending so the next pass can
                # take the file once the pipeline has converted it.
                app_logger.info(f"Holding replacement for {target_path}: {why}")
                continue

            ok, detail, trashed_to = _swap(row, src)
            if ok:
                app_logger.info(
                    f"Replaced damaged file {target_path} with {match['filename']}"
                )
                _set_status(target_path, STATUS_APPLIED, detail=None,
                            trashed_path=trashed_to,
                            new_filename=match["filename"])
            else:
                app_logger.warning(
                    f"Could not replace {target_path} with {match['filename']}: {detail}"
                )
                _set_status(target_path, STATUS_FAILED, detail=detail,
                            trashed_path=trashed_to,
                            new_filename=match["filename"])
            changed.append(get_replacement(target_path))

        return [c for c in changed if c]
    except Exception as e:
        app_logger.error(f"Replacement pass failed: {e}")
        return []


def apply_pending_for_app(app_config=None):
    """Convenience wrapper that sources TARGET and the rename pattern itself.

    Exists so both callers -- the download pipeline's sweep and the page's own
    poll -- go through one place, rather than each assembling the pattern (and
    drifting on which tokens get stripped).
    """
    try:
        from cbz_ops.rename import load_custom_rename_config
        from helpers.collection import (
            strip_empty_groups,
            strip_month_token,
            strip_title_token,
            strip_year_token,
        )

        target_folder = ""
        if app_config is not None:
            target_folder = (app_config.get("TARGET") or "").strip()
        if not target_folder:
            from core.config import get_target_dir

            target_folder = (get_target_dir() or "").strip()
        if not target_folder:
            return []

        # Identical resolution to app.process_incoming_wanted_issues, including
        # the default. The two passes look at the same TARGET files, so a
        # different pattern here would mean they disagree about what a file is.
        enabled, pattern = load_custom_rename_config()
        if not enabled or not pattern:
            pattern = "{series_name} {issue_number} ({volume_year})"

        match_pattern = strip_year_token(pattern)
        match_pattern = strip_title_token(match_pattern)
        match_pattern = strip_month_token(match_pattern)
        match_pattern = strip_empty_groups(match_pattern)

        return apply_pending(target_folder, match_pattern)
    except Exception as e:
        app_logger.error(f"Could not run the replacement pass: {e}")
        return []
