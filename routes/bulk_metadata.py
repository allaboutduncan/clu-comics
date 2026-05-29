"""
Bulk metadata routes.

Endpoints in this blueprint orchestrate "Fetch Metadata" jobs that span
multiple folders. They feed a background worker in core/bulk_metadata.py
that auto-accepts confident matches and queues everything else for review.
"""
from __future__ import annotations

import json
import os
import time
import zipfile

from flask import Blueprint, jsonify, request, render_template

import core.app_state as app_state
from core.app_logging import app_logger
from core.bulk_metadata import get_op_id_for_job, start_bulk_job
from core.comicinfo import find_comicinfo_in_zip
from core.database import (
    get_audit_history,
    get_bulk_audit,
    get_bulk_job,
    get_review_item,
    get_review_queue,
    list_bulk_jobs,
    log_bulk_audit,
    mark_audit_reverted,
    update_bulk_job_counts,
    update_review_status,
)
from helpers.library import is_valid_library_path
from models.providers import ProviderCredentials, get_provider_by_name


bulk_metadata_bp = Blueprint('bulk_metadata', __name__)


def _err(message, status=400):
    return jsonify({"success": False, "error": message}), status


class _ProviderCallError(RuntimeError):
    """Raised by helpers when a provider RPC fails — caller maps to 502."""


def _cascade_resolve_group(job_id, folder_path, parsed_series, exclude_id=None):
    """Mark every other pending review row in the same (folder, series) group
    as resolved. Returns the list of cascaded review_ids.

    Filters by both ``folder_path`` AND ``parsed_series`` so a cv_id entered
    for "Avengers West Coast Annual" doesn't accidentally resolve the regular
    "Avengers West Coast" rows that share the same folder.
    """
    target_series = (parsed_series or '')
    cascaded = []
    for sib in get_review_queue(job_id=job_id, status='pending'):
        if exclude_id is not None and sib['id'] == exclude_id:
            continue
        if sib.get('folder_path') != folder_path:
            continue
        if (sib.get('parsed_series') or '') != target_series:
            continue
        update_review_status(sib['id'], 'resolved')
        update_bulk_job_counts(job_id, needs_review=-1)
        cascaded.append(sib['id'])
    return cascaded


def _pick_issue_for_number(issues, issue_text):
    """Find the best provider issue matching ``issue_text`` from a filename.

    Strict equality of the de-zero-padded number wins. Falls back to a
    leading-digit match so GCD-API descriptors like ``"21 [Cover by X]"`` or
    ``"21A"`` still resolve to issue 21. Returns the single matched
    ``IssueResult`` or ``None`` when nothing maps unambiguously.
    """
    if not issue_text:
        return None
    target = issue_text.lstrip('0') or '0'

    strict = [
        i for i in issues
        if ((i.issue_number or '').lstrip('0') or '0') == target
    ]
    if len(strict) == 1:
        return strict[0]

    import re as _re
    def _leading_digits(s):
        m = _re.match(r'^\s*(\d+)', s or '')
        return (m.group(1).lstrip('0') or '0') if m else None

    relaxed = [i for i in issues if _leading_digits(i.issue_number) == target]
    if len(relaxed) == 1:
        return relaxed[0]
    if len(relaxed) > 1:
        # Prefer the candidate with the shortest issue_number — that's almost
        # always the "vanilla" printing over the variants ("21" beats "21A").
        return min(relaxed, key=lambda i: len(i.issue_number or ''))
    return None


def _apply_series_to_folder(
    *,
    job_id,
    folder_path,
    provider_name,
    provider,
    series,
    series_id,
    parsed_year,
    matched_via,
    on_progress=None,
    skip_existing=True,
):
    """Walk every CBZ/ZIP in folder_path and write ComicInfo for issues that
    match an issue from ``provider.get_issues(series_id)`` by parsed number.

    Returns (written, errors). Skips files where the parsed issue number can't
    be matched, and (when ``skip_existing=True``, the default) files that
    already carry a ComicInfo.xml — matching the orchestrator's initial-pass
    behaviour so users don't lose hand-curated metadata when they later
    resolve a review row for a different file in the same folder.

    ``on_progress(current, total, filename)`` is invoked once per **eligible**
    file (cbz/zip with a matched issue) so the UI can render per-file status.
    """
    from core.bulk_metadata import _has_existing_comicinfo, _write_metadata
    from models.providers import extract_issue_number

    try:
        issues = provider.get_issues(str(series_id)) or []
    except Exception as e:
        raise _ProviderCallError(f"get_issues failed: {e}")

    issues_by_norm = {}
    for i in issues:
        key = (i.issue_number or '').lstrip('0') or '0'
        issues_by_norm.setdefault(key, []).append(i)

    # Pre-compute the eligible (filename, issue) work list so on_progress can
    # report accurate total + position counters instead of counting against
    # the unfiltered directory.
    eligible = []
    skipped_existing = 0
    for entry in sorted(os.listdir(folder_path)):
        fp = os.path.join(folder_path, entry)
        if not (os.path.isfile(fp) and entry.lower().endswith(('.cbz', '.zip'))):
            continue
        issue_text = extract_issue_number(entry)
        if not issue_text:
            continue
        norm = issue_text.lstrip('0') or '0'
        matches = issues_by_norm.get(norm, [])
        if len(matches) != 1:
            continue
        if skip_existing and _has_existing_comicinfo(fp):
            skipped_existing += 1
            continue
        eligible.append((fp, entry, matches[0]))

    if skipped_existing:
        app_logger.info(
            f"[bulk-meta] _apply_series_to_folder skipped {skipped_existing} "
            f"file(s) in {folder_path} that already have ComicInfo.xml"
        )

    total = len(eligible)
    written = 0
    errors = 0
    for idx, (fp, entry, issue_obj) in enumerate(eligible, start=1):
        if on_progress is not None:
            try:
                on_progress(idx, total, entry)
            except Exception:
                pass
        ok = _write_metadata(
            job_id=job_id,
            folder_path=folder_path,
            file_path=fp,
            provider_name=provider_name,
            series=series,
            issue=issue_obj,
            matched_via=matched_via,
            parsed_year=parsed_year,
        )
        if ok:
            written += 1
        else:
            errors += 1
    return written, errors


# ----------------------------------------------------------------------------
# Job lifecycle
# ----------------------------------------------------------------------------


@bulk_metadata_bp.route('/api/bulk-metadata/start', methods=['POST'])
def start_job():
    """Start a bulk metadata job.

    Body:
        scope: 'files' | 'folder'
        paths: list of file paths (scope='files') or root folders (scope='folder')
        library_id: int (optional, used to pick provider priority order)
        overwrite_existing: bool (default false — skip files that already have XML)
    """
    data = request.get_json(silent=True) or {}
    scope = data.get('scope')
    paths = data.get('paths') or []
    library_id = data.get('library_id')
    overwrite_existing = bool(data.get('overwrite_existing', False))

    if scope not in ('files', 'folder'):
        return _err("scope must be 'files' or 'folder'")
    if not isinstance(paths, list) or not paths:
        return _err("paths must be a non-empty list")

    # Guard: paths must resolve under a configured library root.
    for p in paths:
        if not is_valid_library_path(p):
            return _err(f"path outside library: {p}", status=403)

    try:
        job_id = start_bulk_job(
            scope=scope,
            paths=paths,
            library_id=library_id,
            overwrite_existing=overwrite_existing,
        )
    except Exception as e:
        app_logger.error(f"start_bulk_job failed: {e}")
        return _err(f"failed to start job: {e}", status=500)

    job = get_bulk_job(job_id) or {}
    return jsonify({
        "success": True,
        "job_id": job_id,
        "op_id": get_op_id_for_job(job_id),
        "total_files": job.get('total_files', 0),
        "total_folders": job.get('total_folders', 0),
    })


@bulk_metadata_bp.route('/api/bulk-metadata/progress/<job_id>', methods=['GET'])
def progress(job_id):
    """Return current progress for a job.

    Falls back to the persisted bulk_metadata_job row once the in-memory
    app_state op has been pruned (app_state expires completed ops after 15s).
    """
    op_id = get_op_id_for_job(job_id)
    op = None
    if op_id:
        for o in app_state.get_active_operations():
            if o["id"] == op_id:
                op = o
                break

    job = get_bulk_job(job_id)
    if not job:
        return _err("job not found", status=404)

    # Live op takes priority — it has fresh detail/current.
    if op:
        return jsonify({
            "success": True,
            "status": op["status"],
            "current": op["current"],
            "total": op["total"],
            "detail": op["detail"],
            "job": job,
        })

    # No live op — read from the persisted row.
    return jsonify({
        "success": True,
        "status": job["status"],
        "current": job.get("total_files", 0) if job["status"] == "completed" else 0,
        "total": job.get("total_files", 0),
        "detail": "Completed" if job["status"] == "completed" else "",
        "job": job,
    })


@bulk_metadata_bp.route('/api/bulk-metadata/op-progress/<op_id>', methods=['GET'])
def op_progress(op_id):
    """Return the in-memory state of an app_state operation by id.

    The wizard's Apply/Apply-to-folder buttons generate a client-side token,
    POST it on the resolve request, then poll this endpoint to surface the
    per-file progress that the synchronous request emits via app_state
    update_operation calls. Returns 404 once the operation has been pruned
    (app_state expires completed ops after a short TTL).
    """
    for op in app_state.get_active_operations():
        if op["id"] == op_id:
            return jsonify({
                "success": True,
                "status": op["status"],
                "current": op["current"],
                "total": op["total"],
                "detail": op["detail"],
            })
    return jsonify({"success": False, "error": "operation not found"}), 404


@bulk_metadata_bp.route('/api/bulk-metadata/jobs', methods=['GET'])
def list_jobs():
    limit = int(request.args.get('limit', 50))
    offset = int(request.args.get('offset', 0))
    return jsonify({"success": True, "jobs": list_bulk_jobs(limit=limit, offset=offset)})


# ----------------------------------------------------------------------------
# Review queue
# ----------------------------------------------------------------------------


@bulk_metadata_bp.route('/api/bulk-metadata/review/<job_id>', methods=['GET'])
def review_queue(job_id):
    """Return pending review items for a job. candidates is JSON-decoded."""
    status = request.args.get('status', 'pending')
    rows = get_review_queue(job_id=job_id, status=status)

    # Cache "first CBZ per folder" lookups so a 50-row queue with rows from
    # the same folder only walks the directory once.
    folder_cover_cache = {}

    def _first_cbz_in(folder):
        if folder in folder_cover_cache:
            return folder_cover_cache[folder]
        cover = None
        try:
            if os.path.isdir(folder):
                for entry in sorted(os.listdir(folder)):
                    if entry.lower().endswith(('.cbz', '.zip')):
                        cover = os.path.join(folder, entry)
                        break
        except OSError:
            pass
        folder_cover_cache[folder] = cover
        return cover

    for r in rows:
        if r.get('candidates'):
            try:
                r['candidates'] = json.loads(r['candidates'])
            except Exception:
                r['candidates'] = []
        else:
            r['candidates'] = []

        # /api/thumbnail only opens CBZ/ZIP archives — passing a folder path
        # returns a broken image. For folder-level reviews (file_path is NULL)
        # we surface the first CBZ in the folder so the modal still shows a
        # meaningful cover.
        if r.get('file_path'):
            r['cover_path'] = r['file_path']
        else:
            r['cover_path'] = _first_cbz_in(r.get('folder_path') or '')

    return jsonify({"success": True, "items": rows, "count": len(rows)})


@bulk_metadata_bp.route('/api/bulk-metadata/review/<int:review_id>/resolve', methods=['POST'])
def resolve_review(review_id):
    """Apply the user's chosen match to a review item.

    Body:
        provider: provider name (e.g. 'metron')
        series_id: provider series/volume id
        issue_id: provider issue id (optional — required for issue-level items)
    """
    data = request.get_json(silent=True) or {}
    provider_name = (data.get('provider') or '').strip().lower()
    series_id = str(data.get('series_id') or '').strip()
    issue_id = data.get('issue_id')
    op_token = (data.get('op_token') or '').strip() or None

    app_logger.info(
        f"[bulk-meta] resolve review={review_id} "
        f"provider={provider_name} series_id={series_id} issue_id={issue_id} "
        f"op={op_token}"
    )

    if not provider_name or not series_id:
        return _err("provider and series_id are required")

    item = get_review_item(review_id)
    if not item:
        return _err("review item not found", status=404)
    if item.get('status') != 'pending':
        return _err(f"review item already {item.get('status')}", status=409)

    # Lazy import to avoid circular import at module load.
    from core.bulk_metadata import _instantiate_provider, _write_metadata
    from models.providers import extract_issue_number

    provider = _instantiate_provider(provider_name)
    if provider is None:
        return _err(f"provider {provider_name} is not available")

    # Series lookup (always — we need it for the audit row title field).
    try:
        series = provider.get_series(series_id)
    except Exception as e:
        return _err(f"get_series failed: {e}", status=502)
    if series is None:
        return _err("series not found in provider")

    file_path = item.get('file_path')
    folder_path = item['folder_path']
    parsed_year = item.get('parsed_year')

    # Folder-level review: walk every CBZ in the folder and try to write metadata.
    if not file_path:
        if not os.path.isdir(folder_path):
            return _err(f"folder no longer exists: {folder_path}", status=410)
        if op_token:
            app_state.register_operation(
                'bulk_review_apply',
                f"Resolving series for {os.path.basename(folder_path)}",
                total=0,
                op_id=op_token,
            )
        def _progress_folder(current, total, filename):
            if op_token:
                app_state.update_operation(
                    op_token, current=current, total=total, detail=filename
                )
        try:
            written, errors = _apply_series_to_folder(
                job_id=item['job_id'],
                folder_path=folder_path,
                provider_name=provider_name,
                provider=provider,
                series=series,
                series_id=series_id,
                parsed_year=parsed_year,
                matched_via='manual_review',
                on_progress=_progress_folder,
            )
        except _ProviderCallError as e:
            if op_token:
                app_state.complete_operation(op_token, error=True)
            return _err(str(e), status=502)
        if op_token:
            app_state.complete_operation(op_token)

        cascaded_ids = _cascade_resolve_group(
            job_id=item['job_id'],
            folder_path=folder_path,
            parsed_series=item.get('parsed_series'),
            exclude_id=review_id,
        )
        update_bulk_job_counts(item['job_id'], auto_accepted=written, errors=errors, needs_review=-1)
        update_review_status(review_id, 'resolved')
        return jsonify({
            "success": True,
            "written": written,
            "skipped": 0,
            "errors": errors,
            "resolved_review_ids": [review_id] + cascaded_ids,
        })

    # File-level review: write metadata for the one file.
    if not os.path.exists(file_path):
        return _err(f"file no longer exists: {file_path}", status=410)

    # If the user didn't pass issue_id, infer it from the filename's parsed number.
    issue_obj = None
    if issue_id:
        try:
            issue_obj = provider.get_issue(str(issue_id))
        except Exception as e:
            return _err(f"get_issue failed: {e}", status=502)
    else:
        try:
            issues = provider.get_issues(series_id) or []
        except Exception as e:
            return _err(f"get_issues failed: {e}", status=502)
        issue_text = extract_issue_number(os.path.basename(file_path))
        if not issue_text:
            return _err("could not parse issue number from filename")
        issue_obj = _pick_issue_for_number(issues, issue_text)
        if issue_obj is None:
            # User explicitly picked this series — synthesize an IssueResult so
            # we still write at least series-level metadata. Better than refusing
            # to act after the user manually resolved the match.
            from models.providers.base import IssueResult as _IR
            issue_obj = _IR(
                provider=series.provider,
                id='',
                series_id=str(series_id),
                issue_number=issue_text,
                title=None,
                cover_date=None,
                store_date=None,
                cover_url=None,
                summary=None,
            )

    if issue_obj is None:
        return _err("issue not found")

    ok = _write_metadata(
        job_id=item['job_id'],
        folder_path=folder_path,
        file_path=file_path,
        provider_name=provider_name,
        series=series,
        issue=issue_obj,
        matched_via='manual_review',
        parsed_year=parsed_year,
    )
    if not ok:
        update_bulk_job_counts(item['job_id'], errors=1)
        return _err("write failed", status=500)

    written = 1
    errors = 0

    # Fan out across the rest of the (folder, parsed_series) group: every
    # other pending review row with the same parsed_series in this folder
    # gets the same series applied + a per-file issue lookup. Without this
    # the wizard would force the user to re-pick the same series 10+ times
    # for a multi-file group.
    try:
        all_issues = provider.get_issues(str(series_id)) or []
    except Exception as e:
        app_logger.warning(f"group-cascade get_issues failed: {e}")
        all_issues = []

    siblings = get_review_queue(job_id=item['job_id'], status='pending')
    target_series = (item.get('parsed_series') or '')
    # Build the eligible sibling list up-front so progress reporting can show
    # an accurate (current, total) pair instead of guessing per iteration.
    eligible_sibs = [
        sib for sib in siblings
        if sib['id'] != review_id
        and sib.get('folder_path') == folder_path
        and (sib.get('parsed_series') or '') == target_series
    ]
    total_in_group = 1 + len(eligible_sibs)  # +1 for the trigger row just written

    if op_token:
        app_state.register_operation(
            'bulk_review_apply',
            f"Resolving group ({total_in_group} files)",
            total=total_in_group,
            op_id=op_token,
        )
        # Trigger file already done.
        app_state.update_operation(
            op_token, current=1, total=total_in_group,
            detail=os.path.basename(file_path),
        )

    cascaded_ids = []
    for sib_idx, sib in enumerate(eligible_sibs, start=2):
        sib_file = sib.get('file_path')
        if op_token and sib_file:
            app_state.update_operation(
                op_token, current=sib_idx, total=total_in_group,
                detail=os.path.basename(sib_file),
            )
        if sib_file and os.path.exists(sib_file):
            # Defensive skip: if the file picked up metadata between the
            # orchestrator's initial pass and this cascade (e.g. another bulk
            # job ran in parallel), don't overwrite it.
            from core.bulk_metadata import _has_existing_comicinfo
            if _has_existing_comicinfo(sib_file):
                update_review_status(sib['id'], 'resolved')
                update_bulk_job_counts(item['job_id'], needs_review=-1)
                cascaded_ids.append(sib['id'])
                continue
            sib_issue_text = extract_issue_number(os.path.basename(sib_file))
            sib_issue_obj = (
                _pick_issue_for_number(all_issues, sib_issue_text)
                if sib_issue_text else None
            )
            if sib_issue_obj is None and sib_issue_text:
                from models.providers.base import IssueResult as _IR
                sib_issue_obj = _IR(
                    provider=series.provider, id='', series_id=str(series_id),
                    issue_number=sib_issue_text,
                    title=None, cover_date=None, store_date=None,
                    cover_url=None, summary=None,
                )
            if sib_issue_obj is not None:
                if _write_metadata(
                    job_id=item['job_id'],
                    folder_path=folder_path,
                    file_path=sib_file,
                    provider_name=provider_name,
                    series=series,
                    issue=sib_issue_obj,
                    matched_via='manual_review',
                    parsed_year=sib.get('parsed_year') or parsed_year,
                ):
                    written += 1
                else:
                    errors += 1
        update_review_status(sib['id'], 'resolved')
        update_bulk_job_counts(item['job_id'], needs_review=-1)
        cascaded_ids.append(sib['id'])

    if op_token:
        app_state.complete_operation(op_token)

    update_bulk_job_counts(
        item['job_id'], auto_accepted=written, errors=errors, needs_review=-1
    )
    update_review_status(review_id, 'resolved')
    return jsonify({
        "success": True,
        "written": written,
        "errors": errors,
        "resolved_review_ids": [review_id] + cascaded_ids,
    })


@bulk_metadata_bp.route('/api/bulk-metadata/review/<int:review_id>/mark-applied', methods=['POST'])
def mark_review_applied(review_id):
    """Mark a pending review row resolved when metadata has already been
    written by an external path (e.g. /api/search-metadata writing directly
    on a unique match from the bulk modal's manual-search action).

    Does not write its own audit row — the originating endpoint owns the
    file state. Use /resolve when you want the orchestrator to do the write
    and produce an audit entry.
    """
    item = get_review_item(review_id)
    if not item:
        return _err("review item not found", status=404)
    if item.get('status') != 'pending':
        return _err(f"review item already {item.get('status')}", status=409)
    update_review_status(review_id, 'resolved')
    update_bulk_job_counts(item['job_id'], auto_accepted=1, needs_review=-1)
    return jsonify({"success": True})


@bulk_metadata_bp.route('/api/bulk-metadata/review/<int:review_id>/dismiss', methods=['POST'])
def dismiss_review(review_id):
    item = get_review_item(review_id)
    if not item:
        return _err("review item not found", status=404)
    if item.get('status') != 'pending':
        return _err(f"review item already {item.get('status')}", status=409)
    update_review_status(review_id, 'dismissed')
    update_bulk_job_counts(item['job_id'], needs_review=-1, skipped=1)
    return jsonify({"success": True})


@bulk_metadata_bp.route('/api/bulk-metadata/review/<int:review_id>/skip-series', methods=['POST'])
def skip_series(review_id):
    """Dismiss every pending review row in the same (folder_path, parsed_series)
    group as the given row. Used by the wizard's "Skip this series" button
    when no provider can resolve the group.
    """
    item = get_review_item(review_id)
    if not item:
        return _err("review item not found", status=404)
    if item.get('status') != 'pending':
        return _err(f"review item already {item.get('status')}", status=409)

    job_id = item['job_id']
    folder_path = item['folder_path']
    parsed_series = item.get('parsed_series') or ''

    app_logger.info(
        f"[bulk-meta] skip-series review={review_id} folder={folder_path} "
        f"series='{parsed_series}'"
    )

    target = (parsed_series or '')
    dismissed = []
    for sib in get_review_queue(job_id=job_id, status='pending'):
        if sib.get('folder_path') != folder_path:
            continue
        if (sib.get('parsed_series') or '') != target:
            continue
        update_review_status(sib['id'], 'dismissed')
        update_bulk_job_counts(job_id, needs_review=-1, skipped=1)
        dismissed.append(sib['id'])

    return jsonify({
        "success": True,
        "dismissed": dismissed,
        "count": len(dismissed),
    })


@bulk_metadata_bp.route('/api/bulk-metadata/review/<int:review_id>/apply-cvinfo', methods=['POST'])
def apply_cvinfo(review_id):
    """Resolve a review item by direct ID entry — write cvinfo to the folder,
    apply metadata to every CBZ in it, and cascade-resolve sibling rows in the
    same folder/job.

    Body:
        cv_volume_id: ComicVine volume ID (optional)
        metron_series_id: Metron series ID (optional)
    At least one must be present.
    """
    from core.bulk_metadata import _instantiate_provider
    from models import metron as metron_mod

    data = request.get_json(silent=True) or {}
    cv_volume_id = str(data.get('cv_volume_id') or '').strip() or None
    metron_series_id = str(data.get('metron_series_id') or '').strip() or None
    op_token = (data.get('op_token') or '').strip() or None

    app_logger.info(
        f"[bulk-meta] apply-cvinfo review={review_id} "
        f"cv_id={cv_volume_id} metron_id={metron_series_id} op={op_token}"
    )

    if not cv_volume_id and not metron_series_id:
        return _err("provide cv_volume_id and/or metron_series_id")

    item = get_review_item(review_id)
    if not item:
        return _err("review item not found", status=404)
    if item.get('status') != 'pending':
        return _err(f"review item already {item.get('status')}", status=409)

    folder_path = item['folder_path']
    if not os.path.isdir(folder_path):
        return _err(f"folder no longer exists: {folder_path}", status=410)

    # Resolve the series via whichever ID was supplied. Prefer Metron.
    provider_name = None
    provider = None
    series = None

    if metron_series_id:
        provider = _instantiate_provider('metron')
        if provider is not None:
            try:
                series = provider.get_series(metron_series_id)
            except Exception as e:
                return _err(f"metron get_series failed: {e}", status=502)
            if series is not None:
                provider_name = 'metron'

    if series is None and cv_volume_id:
        provider = _instantiate_provider('comicvine')
        if provider is not None:
            try:
                series = provider.get_series(cv_volume_id)
            except Exception as e:
                return _err(f"comicvine get_series failed: {e}", status=502)
            if series is not None:
                provider_name = 'comicvine'

    if series is None or provider is None:
        return _err("could not resolve series from the provided ID(s)", status=502)

    series_id_for_provider = metron_series_id if provider_name == 'metron' else cv_volume_id

    # Write the cvinfo file. We mirror the layout _process_directory_metadata
    # produces so future runs of the bulk orchestrator pick it up via cvinfo.
    cvinfo_path = os.path.join(folder_path, 'cvinfo')
    try:
        if metron_series_id:
            metron_mod.create_cvinfo_file(
                cvinfo_path,
                cv_id=cv_volume_id,
                series_id=metron_series_id,
                publisher_name=getattr(series, 'publisher', None),
                start_year=getattr(series, 'year', None),
            )
        else:
            # CV-only: write the canonical URL line ourselves (mirrors
            # routes/metadata.py:1123–1127 in the legacy SSE path).
            with open(cvinfo_path, 'w', encoding='utf-8') as f:
                f.write(f"https://comicvine.gamespot.com/volume/4050-{cv_volume_id}/")
            # Best-effort publisher + start_year append using the same helper.
            try:
                from models import comicvine as cv_mod
                cv_mod.write_cvinfo_fields(
                    cvinfo_path,
                    getattr(series, 'publisher', None),
                    getattr(series, 'year', None),
                )
            except Exception:
                pass
    except Exception as e:
        app_logger.warning(f"cvinfo write failed for {folder_path}: {e}")

    # Register an app_state op so the frontend can poll per-file progress
    # while this synchronous request is in flight.
    if op_token:
        app_state.register_operation(
            'bulk_review_apply',
            f"Applying {provider_name} series {series_id_for_provider}",
            total=0,
            op_id=op_token,
        )

    def _progress(current, total, filename):
        if op_token:
            app_state.update_operation(
                op_token, current=current, total=total, detail=filename
            )

    # Apply metadata to every matching CBZ in the folder.
    try:
        written, errors = _apply_series_to_folder(
            job_id=item['job_id'],
            folder_path=folder_path,
            provider_name=provider_name,
            provider=provider,
            series=series,
            series_id=series_id_for_provider,
            parsed_year=item.get('parsed_year'),
            matched_via='manual_id',
            on_progress=_progress,
        )
    except _ProviderCallError as e:
        if op_token:
            app_state.complete_operation(op_token, error=True)
        return _err(str(e), status=502)

    if op_token:
        app_state.complete_operation(op_token)

    # Cascade-resolve only siblings that share the SAME parsed_series. Avoids
    # the prior bug where an Annual cv_id would resolve regular-series rows in
    # the same folder.
    cascaded_ids = _cascade_resolve_group(
        job_id=item['job_id'],
        folder_path=folder_path,
        parsed_series=item.get('parsed_series'),
        exclude_id=review_id,
    )

    # Resolve the triggering row.
    update_review_status(review_id, 'resolved')
    update_bulk_job_counts(
        item['job_id'],
        auto_accepted=written,
        errors=errors,
        needs_review=-1,
    )

    return jsonify({
        "success": True,
        "written": written,
        "errors": errors,
        "resolved_review_ids": [review_id] + cascaded_ids,
        "provider": provider_name,
    })


# ----------------------------------------------------------------------------
# Audit history + revert
# ----------------------------------------------------------------------------


@bulk_metadata_bp.route('/api/bulk-metadata/audit', methods=['GET'])
def audit_list():
    limit = max(1, min(500, int(request.args.get('limit', 100))))
    offset = max(0, int(request.args.get('offset', 0)))
    provider = request.args.get('provider') or None
    status = request.args.get('status') or None  # 'written' | 'reverted' | None
    search = request.args.get('search') or None
    rows, total = get_audit_history(
        limit=limit, offset=offset, provider=provider, status=status, search=search
    )
    return jsonify({"success": True, "items": rows, "total": total})


@bulk_metadata_bp.route('/api/bulk-metadata/audit/<int:audit_id>/revert', methods=['POST'])
def audit_revert(audit_id):
    """Restore the prior ComicInfo.xml for an audited write.

    If prior_xml is NULL the file had no ComicInfo before — revert removes
    the ComicInfo.xml we wrote. Otherwise the prior bytes are re-applied.
    """
    row = get_bulk_audit(audit_id)
    if not row:
        return _err("audit entry not found", status=404)
    if row.get('reverted_at'):
        return _err("already reverted", status=409)

    file_path = row['file_path']
    if not os.path.exists(file_path):
        return _err(f"file no longer exists: {file_path}", status=410)

    prior = row.get('prior_xml')

    try:
        if prior:
            # Re-apply the prior XML (add_comicinfo_to_cbz overwrites by design).
            from routes.metadata import add_comicinfo_to_cbz
            add_comicinfo_to_cbz(file_path, bytes(prior))
        else:
            # No prior metadata — strip ComicInfo.xml from the archive.
            _strip_comicinfo_from_cbz(file_path)
    except Exception as e:
        app_logger.error(f"Revert failed for audit {audit_id}: {e}")
        return _err(f"revert failed: {e}", status=500)

    # Keep file_index in sync with the on-disk state.
    try:
        from core.database import set_has_comicinfo
        set_has_comicinfo(file_path, 1 if prior else 0)
    except Exception as e:
        app_logger.warning(f"file_index sync failed for revert of {file_path}: {e}")

    mark_audit_reverted(audit_id)
    return jsonify({"success": True})


def _strip_comicinfo_from_cbz(file_path):
    """Rewrite a CBZ in place without its ComicInfo.xml entries."""
    file_dir = os.path.dirname(file_path) or '.'
    base_name = os.path.splitext(os.path.basename(file_path))[0]
    temp_zip = os.path.join(file_dir, f".tmp_revert_{base_name}_{os.getpid()}.cbz")

    with zipfile.ZipFile(file_path, 'r') as src:
        with zipfile.ZipFile(temp_zip, 'w', zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                if os.path.basename(info.filename).lower() == 'comicinfo.xml':
                    continue
                dst.writestr(info, src.read(info.filename))
    os.replace(temp_zip, file_path)


# ----------------------------------------------------------------------------
# History page (HTML)
# ----------------------------------------------------------------------------


@bulk_metadata_bp.route('/metadata/history', methods=['GET'])
def history_page():
    return render_template('metadata_history.html')


# ----------------------------------------------------------------------------
# Nested-folder probe (used by files.js to decide whether to use bulk flow)
# ----------------------------------------------------------------------------


@bulk_metadata_bp.route('/api/bulk-metadata/has-subfolders', methods=['GET'])
def has_subfolders():
    """Lightweight probe: does ``path`` contain any subdirectories?

    Used by files.js to decide between the existing single-folder SSE flow
    and the new bulk flow when the user clicks the per-folder fetch button.
    """
    path = request.args.get('path')
    if not path:
        return _err("path is required")
    if not is_valid_library_path(path):
        return _err("path outside library", status=403)
    if not os.path.isdir(path):
        return _err("not a directory", status=404)
    try:
        for entry in os.scandir(path):
            if entry.is_dir(follow_symlinks=False):
                return jsonify({"success": True, "has_subfolders": True})
    except OSError as e:
        return _err(f"scandir failed: {e}", status=500)
    return jsonify({"success": True, "has_subfolders": False})
