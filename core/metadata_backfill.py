"""Manual metadata backfill (#579): fill the blanks, never overwrite.

``core/credit_backfill.py`` is the *scheduled* sweep: Metron only, creator
credits only, recent files only. This is the on-demand one for a folder of any
age. For every comic it asks the provider that tagged the file (read from
Notes, see ``core.comicinfo.provider_from_notes``) for the same issue again and
copies in only the tags that are blank in the file -- so a field the provider
has learned since (Plot/Script credits, a description) arrives, and a value the
user edited by hand (a cover date, a rewritten summary) is never touched.

It runs as a bulk-metadata job (same table, progress modal, audit and undo, one
worker on ``core.job_queue.file_jobs``), differing from a re-tag in two ways:

* the provider is per FILE, not per folder, and the issue is found by the id the
  file already carries, so there is no series matching and nothing for a review
  queue to resolve;
* the merge direction is inverted: the file's own values win.
"""
import os
import traceback
import uuid
from typing import Dict, List, Optional, Tuple

from core import app_state
from core.app_logging import app_logger
from core.comicinfo import (
    PROVIDER_MANUAL,
    extract_issue_id,
    fill_blank_comicinfo_bytes,
    generate_comicinfo_xml,
    provider_from_notes,
    read_comicinfo_xml,
)
from core.database import (
    complete_bulk_job,
    create_bulk_job,
    log_bulk_audit,
    update_bulk_job_counts,
)

# Providers whose Notes/ComicInfo carries an id we can fetch the issue by.
# Anything else (INDUCKS' issue code, AniList/MangaDex series ids) is reported
# as unsupported rather than guessed at.
BACKFILL_PROVIDERS = ("metron", "comicvine", "comicvine_sqlite", "gcd", "gcd_api")

# extract_issue_id lives in core.comicinfo (re-exported here): the reading-list
# matcher reads a ComicVine id out of Notes with it too, and two Notes-to-id
# parsers would drift.

def backfill_file(
    job_id: str,
    folder_path: str,
    file_path: str,
    dry_run: bool,
    providers: Dict,
    series_cache: Dict,
) -> Tuple[str, List[str]]:
    """Backfill one file. Returns ``(outcome, filled_tags)``.

    outcome: ``filled`` | ``unchanged`` | ``unsupported`` | ``error``.
    ``providers`` and ``series_cache`` are per-job memos so a folder of 200
    issues builds each provider once and fetches each series once.
    """
    from core.bulk_metadata import _instantiate_provider, _read_existing_comicinfo_bytes

    prior = _read_existing_comicinfo_bytes(file_path)
    if not prior:
        return "unsupported", []
    try:
        info = read_comicinfo_xml(prior)
    except Exception:
        return "error", []

    provider_name = provider_from_notes(info.get("Notes"))
    if not provider_name or provider_name == PROVIDER_MANUAL \
            or provider_name not in BACKFILL_PROVIDERS:
        return "unsupported", []
    issue_id = extract_issue_id(provider_name, info)
    if not issue_id:
        return "unsupported", []

    try:
        if provider_name not in providers:
            providers[provider_name] = _instantiate_provider(provider_name)
        provider = providers[provider_name]
        if provider is None:
            return "unsupported", []

        issue = provider.get_issue(issue_id)
        if issue is None:
            return "unchanged", []

        series = None
        if issue.series_id:
            key = (provider_name, str(issue.series_id))
            if key not in series_cache:
                try:
                    series_cache[key] = provider.get_series(str(issue.series_id))
                except Exception as e:
                    app_logger.debug(f"[backfill] get_series failed for {key}: {e}")
                    series_cache[key] = None
            series = series_cache[key]

        new_xml = generate_comicinfo_xml(provider.to_comicinfo(issue, series))
        merged, filled = fill_blank_comicinfo_bytes(prior, new_xml)
        if not filled:
            return "unchanged", []
        if dry_run:
            return "filled", filled

        from routes.metadata import add_comicinfo_to_cbz
        # merge_existing=False: ``merged`` already IS the file's XML plus the
        # fills, so the writer's own carry-forward would be redundant.
        add_comicinfo_to_cbz(file_path, merged, merge_existing=False)
        try:
            from core.database import update_file_index_from_comicinfo
            update_file_index_from_comicinfo(file_path, read_comicinfo_xml(merged))
        except Exception as e:
            app_logger.warning(f"[backfill] file_index sync failed for {file_path}: {e}")

        log_bulk_audit(
            job_id=job_id,
            file_path=file_path,
            folder_path=folder_path,
            provider=provider_name,
            series_id=issue.series_id,
            issue_id=issue.id,
            series_name=(series.title if series else None),
            issue_number=issue.issue_number,
            year=None,
            matched_via="backfill",
            prior_xml=prior,
            new_xml=merged,
        )
        return "filled", filled
    except Exception as e:
        app_logger.error(f"[backfill] failed for {file_path}: {e}")
        app_logger.debug(traceback.format_exc())
        return "error", []


def start_backfill_job(
    paths: List[str],
    library_id: Optional[int] = None,
    dry_run: bool = False,
) -> str:
    """Start a backfill over one or more library folders (recursive).

    Returns the job id; progress, counts and undo are the bulk-metadata ones.
    Counts: ``auto_accepted`` = files filled (or that would be, on a dry run),
    ``skipped`` = nothing to add or no provider to ask, ``errors``.
    """
    from core.bulk_metadata import _OP_ID_MAP, _enumerate_folder_recursive

    job_id = uuid.uuid4().hex
    buckets: Dict[str, List[str]] = {}
    for root in paths:
        for folder, files in _enumerate_folder_recursive(root, only_missing=False).items():
            buckets.setdefault(folder, []).extend(files)
    for k, v in buckets.items():
        # AppleDouble sidecars (._x.cbz) are resource forks, not archives.
        buckets[k] = sorted(f for f in set(v) if not os.path.basename(f).startswith('.'))
    buckets = {k: v for k, v in buckets.items() if v}
    total_files = sum(len(v) for v in buckets.values())

    create_bulk_job(
        job_id=job_id,
        scope_type="folder_recursive",
        scope_payload={"paths": paths, "mode": "backfill", "dry_run": dry_run},
        library_id=library_id,
        overwrite_existing=False,
    )
    update_bulk_job_counts(job_id, total_folders=len(buckets), total_files=total_files)

    def _runner(op_id):
        done = 0
        providers: Dict = {}
        series_cache: Dict = {}
        try:
            for folder_path, files in buckets.items():
                for fp in files:
                    done += 1
                    app_state.update_operation(op_id, current=done, detail=os.path.basename(fp))
                    outcome, _filled = backfill_file(
                        job_id, folder_path, fp, dry_run, providers, series_cache)
                    if outcome == "filled":
                        update_bulk_job_counts(job_id, auto_accepted=1)
                    elif outcome == "error":
                        update_bulk_job_counts(job_id, errors=1)
                    else:
                        update_bulk_job_counts(job_id, skipped=1)
            complete_bulk_job(job_id, "completed")
            app_state.update_operation(op_id, current=total_files, detail="Done")
            app_state.complete_operation(op_id, error=False)
        except Exception as e:
            app_logger.error(f"Backfill job {job_id} failed: {e}")
            app_logger.debug(traceback.format_exc())
            complete_bulk_job(job_id, "error")
            app_state.complete_operation(op_id, error=True)

    from core.job_queue import file_jobs
    label = "Backfill preview" if dry_run else "Backfill metadata"
    op_id = file_jobs.submit(
        "bulk_metadata",
        f"{label}: {total_files} files across {len(buckets)} folder(s)",
        total_files,
        _runner,
    )
    _OP_ID_MAP[job_id] = op_id
    return job_id
