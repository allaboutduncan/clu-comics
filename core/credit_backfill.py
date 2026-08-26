"""Repair Metron-tagged comics that were written with no creator credits.

Metron records are routinely completed *after* a comic ships -- credits and
characters get entered hours into release day -- so a file tagged the morning it
lands can end up with a perfectly valid ComicInfo.xml that simply has no
creators in it. Nothing recovers that on its own: once ComicInfo carries a
``Notes`` value, every automatic tagging path treats the file as done and skips
it forever (``app.py``, ``models/comicvine.py``, ``routes/metadata.py``).

This sweep is the recovery. It re-fetches issues from Metron through
``metron.fetch_issue_detail`` -- which drops mokkari's cached body first, or the
half-entered response would just be replayed -- and rewrites only the files
where Metron now actually has credits.

Candidates come from two passes, because the two causes leave different traces:

1. **The changed-record pass.** ``modified`` is the field that says an editor
   finished the record, so the sweep asks Metron directly which issues changed
   since it last ran (``metron.list_issues_modified_since``) and joins that
   against ``file_index.ci_metronid``. This is the exact question -- it finds a
   comic tagged a year ago whose record was completed last night, which no
   local timestamp can reveal -- and it spends one paged list call instead of a
   detail fetch per file.
2. **The credit-less pass**, over recently written files. This is the safety
   net for the other cause: when the file was tagged from a *stale cached body*,
   Metron's record may not have changed since, so the feed in pass 1 never
   mentions it. Recency here is the archive's mtime, stamped on the index row
   at tag time by ``update_file_index_from_comicinfo``.

Both passes share one fetch budget, and a file is rewritten only when the fresh
payload really has credits -- so a run that finds nothing new touches no bytes
and no mtimes.
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

# How far back the changed-record pass will ever look. mokkari follows every
# `next` link inside a single issues_list call and below the shared pacer, so an
# unbounded window is a long blocking burst of requests. A gap wider than this
# is covered by the credit-less pass instead.
MAX_LOOKBACK_DAYS = 30
LAST_RUN_KEY = "credit_backfill_last_run"


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


def backfill_file(
    api, file_path: str, dry_run: bool = False, issue_id: Optional[int] = None
) -> str:
    """Re-fetch one file's Metron issue and add credits if Metron now has them.

    Args:
        api: Mokkari API client
        file_path: Path to the CBZ
        dry_run: Report what would change without writing
        issue_id: Metron issue id when the caller already knows it (the
            changed-record pass joins on ``ci_metronid``, so it does). Saves
            re-deriving it from the XML, but the archive is still read: the
            index can be stale, and the credit check has to come from the file.

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

        issue = None
        if not issue_id:
            issue_id, issue = _resolve_metron_issue(api, file_path, existing)
        if not issue_id:
            return "skipped"

        from models import metron

        if issue is None:  # noqa: SIM108 -- the resolver may have prefetched it
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


def _lookback_start(days: int) -> str:
    """The ``YYYY-MM-DD`` lower bound for the changed-record pass.

    Normally the previous run's start time, so each sweep asks only about what
    happened since. Clamped to ``MAX_LOOKBACK_DAYS`` -- a container that has been
    off for a year must not ask Metron for a year of edits in one paged call.
    """
    from core.database import get_user_preference

    try:
        last_run = float(get_user_preference(LAST_RUN_KEY) or 0)
    except (TypeError, ValueError):
        last_run = 0.0

    now = time.time()
    since = last_run or (now - max(1, int(days)) * 86400)
    since = max(since, now - MAX_LOOKBACK_DAYS * 86400)
    return time.strftime("%Y-%m-%d", time.localtime(since))


def _changed_record_work(api, days: int, summary: Dict[str, Any]):
    """Files whose Metron record has been edited since the last sweep.

    Returns a list of ``(path, issue_id)``. Empty when Metron reports nothing,
    when the library owns none of what changed, or -- right after the migration
    that added the column -- while ``ci_metronid`` is still being backfilled by
    the metadata scanner.
    """
    from core.database import find_files_by_metron_ids
    from models import metron

    since_date = _lookback_start(days)
    summary["since"] = since_date

    changed = metron.list_issues_modified_since(
        api, since_date, context="(credit backfill)"
    )
    summary["changed_issues"] = len(changed)
    if not changed:
        return []

    matches = find_files_by_metron_ids(changed.keys())
    work = [
        (path, issue_id)
        for issue_id, paths in matches.items()
        for path in paths
    ]
    summary["matched_files"] = len(work)
    app_logger.info(
        f"Credit backfill: {len(changed)} issue(s) changed on Metron since "
        f"{since_date}, {len(work)} of them in the library"
    )
    return work


def run_credit_backfill(
    days: int = DEFAULT_DAYS,
    limit: int = DEFAULT_LIMIT,
    dry_run: bool = False,
    app=None,
    on_progress=None,
) -> Dict[str, Any]:
    """Repair Metron-tagged files that were written without creator credits.

    Runs the two passes described in the module docstring -- issues Metron has
    edited since the last sweep, then recently written files that still have no
    credits -- over one shared fetch budget.

    Args:
        days: Window for the credit-less pass, and the first run's lookback.
        limit: Maximum number of files to fetch from Metron in one run.
        dry_run: Report what would change without writing anything. Also leaves
            the last-run marker alone, so the live run that follows covers the
            same ground.
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

    started_at = time.time()
    summary = {
        "checked": 0,
        "updated": 0,
        "still_empty": 0,
        "skipped": 0,
        "errors": 0,
        "changed_issues": 0,
        "matched_files": 0,
        "stopped_early": False,
        "dry_run": bool(dry_run),
    }

    api = metron.get_flask_api(app)
    if not api:
        app_logger.info("Credit backfill skipped: Metron API not configured")
        summary["skipped_reason"] = "metron-unavailable"
        return summary

    # Pass 1: what Metron says changed.
    work = _changed_record_work(api, days, summary)

    # Pass 2: recently written files that still have no credits. Catches the
    # stale-cache case, where the record Metron holds never changed -- our copy
    # of it was simply out of date when the file was tagged.
    seen = {path for path, _ in work}
    scan_limit = min(MAX_SCAN, max(1, int(limit)) * SCAN_MULTIPLIER)
    work += [
        (path, None)
        for path in find_credit_less_files(days=days, limit=scan_limit)
        if path not in seen
    ]

    if not work:
        app_logger.info("Credit backfill: nothing to check")
        _mark_run_complete(started_at, dry_run, summary)
        return summary

    app_logger.info(
        f"Credit backfill: examining {len(work)} file(s)"
        + (" (dry run)" if dry_run else "")
    )

    for file_path, issue_id in work:
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
                on_progress(summary["checked"], len(work), os.path.basename(file_path))
            except Exception:
                pass
        outcome = backfill_file(api, file_path, dry_run=dry_run, issue_id=issue_id)
        if outcome == "updated":
            summary["updated"] += 1
        elif outcome == "still-empty":
            summary["still_empty"] += 1
        elif outcome == "error":
            summary["errors"] += 1
        else:
            summary["skipped"] += 1

    _mark_run_complete(started_at, dry_run, summary)

    app_logger.info(
        f"Credit backfill complete: {summary['checked']} checked, "
        f"{summary['updated']} updated, {summary['still_empty']} still empty on "
        f"Metron, {summary['skipped']} skipped, {summary['errors']} errors"
        + (" (dry run)" if dry_run else "")
    )
    return summary


def _mark_run_complete(started_at: float, dry_run: bool, summary: Dict[str, Any]) -> None:
    """Move the changed-record cursor forward, if this run earned it.

    Not after a dry run, and not after one that stopped on its budget: the
    window has to be re-listed next time or the changes it didn't reach would
    never be looked at again.

    The marker is set a day behind the run's own start. The filter is
    date-granular, so that is one extra page in exchange for covering a run
    whose list call failed -- which is indistinguishable from a run where
    nothing had changed.
    """
    if dry_run or summary.get("stopped_early"):
        return
    try:
        from core.database import set_user_preference

        set_user_preference(LAST_RUN_KEY, started_at - 86400, category="metadata")
    except Exception as e:
        app_logger.warning(f"Could not record credit backfill run time: {e}")
