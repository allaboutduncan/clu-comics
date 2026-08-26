"""Repair Metron-tagged comics that were written with no creator credits.

Metron records are routinely completed *after* a comic ships -- credits and
characters get entered hours into release day -- so a file tagged the morning it
lands can end up with a perfectly valid ComicInfo.xml that simply has no
creators in it. Nothing recovers that on its own: once ComicInfo carries a
``Notes`` value, every automatic tagging path treats the file as done and skips
it forever (``app.py``, ``models/comicvine.py``, ``routes/metadata.py``).

This sweep is the recovery. It picks credit-less files out of ``file_index``
(no archive I/O to find them), re-fetches each issue from Metron through
``metron.fetch_issue_detail`` -- which drops mokkari's cached body first, or the
half-entered response would just be replayed -- and rewrites only the files
where Metron now actually has credits.

Two deliberate limits keep it cheap and self-terminating:

* Candidates are restricted to files whose **mtime** is recent. mtime only moves
  when the archive is rewritten, so a comic Metron will never have credits for
  ages out of the window instead of being re-fetched every night.
  ``metadata_scanned_at`` would be wrong here -- the scanner refreshes it.
* A file is rewritten only when the fresh payload really has credits, so a run
  that finds nothing new touches no bytes and no mtimes.
"""

import os
import time
from typing import Any, Dict, List, Optional

from core.app_logging import app_logger

# ComicInfo credit tags this sweep cares about, in ComicInfo and file_index
# spellings. Editor is excluded on purpose even though the Metron mapper now
# fills it: a file carrying only an editor is still missing the creators, and
# file_index has no ci_editor column to select on anyway. Files that already
# have credits but predate the Editor mapping are left alone -- repairing those
# is a re-tag, not a backfill.
CREDIT_TAGS = ("Writer", "Penciller", "Inker", "Colorist", "Letterer", "CoverArtist")
_CREDIT_COLUMNS = (
    "ci_writer",
    "ci_penciller",
    "ci_inker",
    "ci_colorist",
    "ci_letterer",
    "ci_coverartist",
)

DEFAULT_DAYS = 45
# Metron fetches per run. Paced at ~15 requests/minute, so this is also what
# bounds how long a sweep runs.
DEFAULT_LIMIT = 200
# Files examined per run. Opening an archive is free next to an API call, and
# most candidates turn out not to be Metron-tagged at all -- without the wider
# scan those would fill the query's LIMIT every night and starve the files that
# actually need fetching.
SCAN_MULTIPLIER = 5
MAX_SCAN = 1000


def _has_credits(mapping: Dict[str, Any]) -> bool:
    """True when any ComicInfo credit field carries a non-empty value."""
    return any(str(mapping.get(tag) or "").strip() for tag in CREDIT_TAGS)


def find_credit_less_files(
    days: int = DEFAULT_DAYS, limit: int = DEFAULT_LIMIT
) -> List[str]:
    """Recently written CBZs whose indexed metadata has no creator credits.

    Args:
        days: Only consider files modified within this many days.
        limit: Maximum number of paths to return.

    Returns:
        List of file paths, newest first. Empty on any database problem.
    """
    from core.database import get_db_connection

    cutoff = time.time() - (max(1, int(days)) * 86400)
    empties = " AND ".join(f"COALESCE({col}, '') = ''" for col in _CREDIT_COLUMNS)

    try:
        conn = get_db_connection()
        if not conn:
            return []
        rows = conn.execute(
            f"""
            SELECT path FROM file_index
            WHERE type = 'file'
              AND has_comicinfo = 1
              AND LOWER(path) LIKE '%.cbz'
              AND {empties}
              AND modified_at >= ?
            ORDER BY modified_at DESC
            LIMIT ?
            """,
            (cutoff, max(1, int(limit))),
        ).fetchall()
        conn.close()
        return [row[0] for row in rows]
    except Exception as e:
        app_logger.error(f"Credit backfill could not list candidates: {e}")
        return []


def _resolve_metron_issue(api, file_path: str, existing: Dict[str, Any]):
    """Identify the Metron issue behind an already-tagged file.

    ``<MetronId>`` is written by ``generate_comicinfo_xml``, but files tagged
    before that tag existed only carry the Metron ``Notes`` line, whose resource
    URL is a slug rather than an id -- those fall back to a lookup by the
    folder's cvinfo series id and the file's issue number.

    Returns:
        ``(issue_id, payload)``. ``payload`` is the already-fetched issue data
        when the fallback lookup had to fetch it anyway (it goes through
        ``get_issue_metadata``, so it is live, not cached) and None otherwise.
        ``(None, None)`` when the file wasn't tagged from Metron or the issue
        can't be identified.
    """
    raw = str(existing.get("MetronId") or "").strip()
    if raw:
        try:
            return int(raw), None
        except (TypeError, ValueError):
            pass

    notes = str(existing.get("Notes") or "")
    if "metron" not in notes.lower():
        return None, None

    number = str(existing.get("Number") or "").strip()
    if not number:
        return None, None

    from models import metron
    from models.comicvine import find_cvinfo_in_folder

    cvinfo_path = find_cvinfo_in_folder(os.path.dirname(file_path))
    if not cvinfo_path:
        return None, None
    series_id = metron.parse_cvinfo_for_metron_id(cvinfo_path)
    if not series_id:
        return None, None

    issue_data = metron.get_issue_metadata(api, series_id, number)
    if not issue_data:
        return None, None
    try:
        return int(metron._get_attr(issue_data, "id", None)), issue_data
    except (TypeError, ValueError):
        return None, None


def backfill_file(api, file_path: str, dry_run: bool = False) -> str:
    """Re-fetch one file's Metron issue and add credits if Metron now has them.

    Returns one of ``'updated'``, ``'still-empty'``, ``'skipped'``, ``'error'``.
    """
    from core.comicinfo import read_comicinfo_from_zip

    try:
        if not os.path.isfile(file_path):
            return "skipped"

        existing = read_comicinfo_from_zip(file_path) or {}
        if not existing:
            return "skipped"
        # The index row can be stale; the archive is the authority.
        if _has_credits(existing):
            return "skipped"

        issue_id, issue = _resolve_metron_issue(api, file_path, existing)
        if not issue_id:
            return "skipped"

        from models import metron

        if issue is None:
            issue = metron.fetch_issue_detail(
                api, issue_id, context="(credit backfill)"
            )
        if issue is None:
            return "error"

        metadata = metron.map_to_comicinfo(issue)
        if not _has_credits(metadata):
            # Metron still has no creators for this issue. Leave the file
            # untouched so its mtime stays put and it ages out of the window.
            return "still-empty"

        if dry_run:
            return "updated"

        from core.comicinfo import generate_comicinfo_xml
        from core.database import update_file_index_from_comicinfo
        from models.comicvine import add_comicinfo_to_archive

        xml_content = generate_comicinfo_xml(metadata)
        # merge_existing (the default) matters here: the sweep adds credits, it
        # must not drop tags the file already carries that Metron doesn't supply.
        if not add_comicinfo_to_archive(file_path, xml_content):
            return "error"

        update_file_index_from_comicinfo(file_path, metadata)
        app_logger.info(
            f"Credit backfill: added credits to {os.path.basename(file_path)} "
            f"(Metron issue {issue_id})"
        )
        return "updated"
    except Exception as e:
        app_logger.error(f"Credit backfill failed for {file_path}: {e}")
        return "error"


def run_credit_backfill(
    days: int = DEFAULT_DAYS,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = False,
    app=None,
    on_progress=None,
) -> Dict[str, Any]:
    """Sweep recently tagged, credit-less files and repair the ones Metron can.

    Args:
        days: Only consider files modified within this many days.
        limit: Maximum number of files to fetch from Metron in one run.
        dry_run: Report what would change without writing anything.
        app: Flask app to read Metron credentials from (defaults to
            ``current_app``, so a scheduled caller should pass it explicitly).
        on_progress: Optional ``(current, total, filename)`` callback, invoked
            once per file. Metron pacing means a full run takes minutes, so the
            manual trigger needs something to report while it works.

    Returns:
        Summary counts plus ``stopped_early`` when the run hit its fetch budget
        or Metron's daily quota before reaching the end of the candidate list.
    """
    from models import metron

    summary = {
        "checked": 0,
        "updated": 0,
        "still_empty": 0,
        "skipped": 0,
        "errors": 0,
        "stopped_early": False,
        "dry_run": bool(dry_run),
    }

    api = metron.get_flask_api(app)
    if not api:
        app_logger.info("Credit backfill skipped: Metron API not configured")
        summary["skipped_reason"] = "metron-unavailable"
        return summary

    scan_limit = min(MAX_SCAN, max(1, int(limit)) * SCAN_MULTIPLIER)
    candidates = find_credit_less_files(days=days, limit=scan_limit)
    if not candidates:
        app_logger.info("Credit backfill: no credit-less files in the recent window")
        return summary

    app_logger.info(
        f"Credit backfill: examining {len(candidates)} file(s) tagged in the last "
        f"{days} days" + (" (dry run)" if dry_run else "")
    )

    for file_path in candidates:
        # Only files we actually fetch for count against the budget; a file
        # skipped without touching Metron costs nothing but a zip open.
        fetched = summary["updated"] + summary["still_empty"] + summary["errors"]
        if fetched >= limit:
            summary["stopped_early"] = True
            app_logger.info(f"Credit backfill stopped early: reached {limit} fetches")
            break

        # A spent daily quota makes every remaining fetch a no-op; stop rather
        # than churn through hundreds of files that cannot succeed.
        if metron._metron_pacer.daily_limit_reached():
            summary["stopped_early"] = True
            app_logger.info("Credit backfill stopped early: Metron daily limit reached")
            break

        summary["checked"] += 1
        if on_progress is not None:
            try:
                on_progress(summary["checked"], len(candidates), os.path.basename(file_path))
            except Exception:
                pass
        outcome = backfill_file(api, file_path, dry_run=dry_run)
        if outcome == "updated":
            summary["updated"] += 1
        elif outcome == "still-empty":
            summary["still_empty"] += 1
        elif outcome == "error":
            summary["errors"] += 1
        else:
            summary["skipped"] += 1

    app_logger.info(
        f"Credit backfill complete: {summary['checked']} checked, "
        f"{summary['updated']} updated, {summary['still_empty']} still empty on "
        f"Metron, {summary['skipped']} skipped, {summary['errors']} errors"
        + (" (dry run)" if dry_run else "")
    )
    return summary
