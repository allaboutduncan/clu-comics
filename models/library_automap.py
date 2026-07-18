"""
Sidecar-based automatic library mapping.

Walks the configured library roots looking for folders that carry a Mylar3-style
``series.json`` or ``cvinfo`` sidecar, resolves each to a Metron series id, and
maps the folder to that series (writes ``series.mapped_path``). This lets a user
who imported an existing, already-tagged library get every series onto their
Pull List without picking folders by hand.

Match source is sidecars ONLY -- no folder-name/year guessing -- so false
matches are near-zero. Resolution cascade per folder (first hit wins):

  1. series.json ``metron_id``            -> direct, no API call
  2. cvinfo ``series_id:``                 -> direct, no API call
  3. series.json ``comicid`` (ComicVine)   -> Metron lookup (1 API call)
  4. cvinfo ComicVine URL (``/4050-<id>``) -> Metron lookup (1 API call)

Candidates are classified into:
  * auto    -- resolved and unambiguous; applied automatically.
  * review  -- resolved but the series is already mapped elsewhere, or two
               scanned folders resolve to the same series (conflict).
  * skipped -- a sidecar with no usable id, or a ComicVine id Metron can't
               resolve. Reported with a reason, never applied.

Reuses existing parsers/writers rather than re-implementing them:
``models.series_json``, ``models.comicvine``, ``models.metron`` and
``core.database.save_series_mapping``.
"""

import os
import threading
import time
import uuid

from core.app_logging import app_logger
from helpers.comicvine_ids import (
    cv_id_from_series_id,
    is_comicvine_series_id,
    make_comicvine_series_id,
)
from models import metron, comicvine
from models.series_json import read_series_json, write_series_json

COMIC_EXTENSIONS = (".cbz", ".cbr", ".zip", ".rar")
SIDECAR_NAMES = {"series.json", "cvinfo"}


def _norm(path):
    """Normalise a path for comparison against DB-stored mapped paths.

    The file index stores forward-slash paths; mirror that so a Windows os.walk
    result compares equal to a mapped_path saved on Linux.
    """
    if not path:
        return ""
    return os.path.normpath(path).replace("\\", "/").rstrip("/")


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sidecar_metadata(series_json):
    """Return the field dict from a parsed series.json.

    CLU/Mylar3 nest the fields under a ``metadata`` key
    (``{"metadata": {"name": ..., "metron_id": ..., "comicid": ...}}``). Fall
    back to the top-level dict to tolerate flat/hand-authored files.
    """
    if not isinstance(series_json, dict):
        return {}
    meta = series_json.get("metadata")
    if isinstance(meta, dict):
        return meta
    return series_json


def _count_comics(folder):
    """Count comic archives directly inside a folder (display only)."""
    try:
        return sum(
            1 for f in os.listdir(folder) if f.lower().endswith(COMIC_EXTENSIONS)
        )
    except OSError:
        return 0


def _resolve_identity(folder, api, cv_api_key=None):
    """Resolve a candidate folder to a Metron series id via the sidecar cascade.

    Returns a candidate dict, or None if the folder has no sidecar at all.
    On success ``metron_id`` is set and ``reason`` is None; when a sidecar is
    present but cannot be resolved, ``metron_id`` is None and ``reason`` explains
    why (so the caller can report it as skipped).
    """
    series_json = read_series_json(folder)
    cvinfo_path = comicvine.find_cvinfo_in_folder(folder)
    if not series_json and not cvinfo_path:
        return None

    meta = _sidecar_metadata(series_json)
    name = meta.get("name")
    publisher = meta.get("publisher")
    year = meta.get("year")
    status = meta.get("status")
    cv_id = _to_int(meta.get("comicid"))

    if cvinfo_path:
        fields = comicvine.read_cvinfo_fields(cvinfo_path)
        publisher = publisher or fields.get("publisher_name")
        year = year or fields.get("start_year")

    def _candidate(metron_id, source, reason=None):
        return {
            "folder": folder,
            "metron_id": metron_id,
            "series_name": name or os.path.basename(folder.rstrip("/\\")),
            "publisher_name": publisher,
            "year": year,
            "status": status,
            "cv_id": cv_id,
            "source": source,
            "reason": reason,
            "conflict_with": None,
        }

    # 1. series.json -> metron_id (direct)
    mid = _to_int(meta.get("metron_id"))
    if mid:
        return _candidate(mid, "series.json:metron_id")

    # 2. cvinfo -> series_id: (direct)
    if cvinfo_path:
        mid = metron.parse_cvinfo_for_metron_id(cvinfo_path)
        if mid:
            return _candidate(mid, "cvinfo:series_id")

    # 3/4. ComicVine id (series.json comicid, else cvinfo URL) -> Metron lookup
    if not cv_id and cvinfo_path:
        cv_id = metron.parse_cvinfo_for_comicvine_id(cvinfo_path)

    if cv_id:
        # Prefer Metron: if the ComicVine id maps to a Metron series, use it.
        if api:
            mid = metron.get_series_id_by_comicvine_id(api, cv_id)
            if mid:
                return _candidate(mid, "comicvine_id")
        # Not in Metron (or no Metron API). Fall back to a ComicVine-sourced
        # identity when the ComicVine API is enabled — cvinfo/series.json are
        # natively ComicVine, so this is the original, not an edge case.
        if cv_api_key:
            return _candidate(make_comicvine_series_id(cv_id), "comicvine_api")
        if api:
            return _candidate(
                None, "comicvine_id",
                f"ComicVine ID {cv_id} not in Metron; enable the ComicVine API to map it",
            )
        return _candidate(
            None, "comicvine_id",
            "Not resolvable: no Metron match and ComicVine API not enabled",
        )

    return _candidate(None, "sidecar", "Sidecar has no Metron or ComicVine ID")


def _find_candidate_folders(roots):
    """Return every folder under the library roots that holds a sidecar file."""
    folders = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            # Don't descend into hidden dirs (.git, .@__thumb, etc.)
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            if SIDECAR_NAMES.intersection(f.lower() for f in filenames):
                folders.append(dirpath)
    return folders


def scan_library_for_automap(api=None, cv_api_key=None, progress_cb=None):
    """Scan library roots and classify sidecar folders into auto/review/skipped.

    Args:
        api: Optional Metron API client. Without it, only steps 1-2 (direct
            Metron id) can resolve.
        cv_api_key: Optional ComicVine API key. When set, sidecars carrying a
            ComicVine id that isn't in Metron resolve to a ComicVine-sourced
            series instead of being skipped.
        progress_cb: Optional callable(current, total, folder_or_None).

    Returns:
        dict with keys ``auto``, ``review``, ``skipped``, ``total_candidates``.
    """
    from core.database import get_all_mapped_series

    roots = get_library_roots()
    mapped_rows = get_all_mapped_series()
    mapped_paths = {}
    mapped_ids = {}
    for row in mapped_rows:
        path = row.get("mapped_path")
        if path:
            mapped_paths[_norm(path)] = row.get("id")
            mapped_ids[row.get("id")] = _norm(path)

    candidates = _find_candidate_folders(roots)
    total = len(candidates)

    auto = []
    review = []
    skipped = []
    seen_ids = {}  # metron_id -> normalised folder that first claimed it

    for index, folder in enumerate(candidates):
        if progress_cb:
            progress_cb(index, total, folder)

        nfolder = _norm(folder)
        if nfolder in mapped_paths:
            continue  # already tracked -- leave it alone

        ident = _resolve_identity(folder, api, cv_api_key=cv_api_key)
        if ident is None:
            continue
        ident["comic_count"] = _count_comics(folder)

        mid = ident["metron_id"]
        if not mid:
            skipped.append(ident)
            continue

        existing = mapped_ids.get(mid)
        if existing and existing != nfolder:
            ident["reason"] = f"Series already mapped to {existing}"
            ident["conflict_with"] = existing
            review.append(ident)
            continue

        if mid in seen_ids and seen_ids[mid] != nfolder:
            ident["reason"] = f"Same series also found in {seen_ids[mid]}"
            ident["conflict_with"] = seen_ids[mid]
            review.append(ident)
            continue

        seen_ids[mid] = nfolder
        auto.append(ident)

    if progress_cb:
        progress_cb(total, total, None)

    return {
        "auto": auto,
        "review": review,
        "skipped": skipped,
        "total_candidates": total,
    }


def get_library_roots():
    """Thin wrapper so tests can monkeypatch roots without touching helpers."""
    from helpers.library import get_library_roots as _roots

    return _roots()


def _safe_cv_key():
    """Return the ComicVine API key, or None (tolerates missing app context)."""
    try:
        return comicvine.get_cv_api_key()
    except Exception:
        return None


def _strip_html(text):
    """Reduce a ComicVine HTML description to plain text."""
    if not text:
        return None
    import html
    import re

    return html.unescape(re.sub(r"<[^>]+>", "", str(text))).strip() or None


def _fetch_comicvine_series_dict(series_id, fallback):
    """Build a series payload from the ComicVine API for an offset (cv) id."""
    cv_id = cv_id_from_series_id(series_id)
    details = {}
    key = _safe_cv_key()
    if key:
        try:
            details = comicvine.get_volume_details(key, cv_id) or {}
        except Exception as e:
            app_logger.warning(f"automap: ComicVine volume {cv_id} fetch failed: {e}")

    name = details.get("name") or fallback.get("series_name") or f"ComicVine {cv_id}"
    return {
        "id": series_id,
        "name": name,
        "publisher_name": details.get("publisher_name") or fallback.get("publisher_name"),
        "status": fallback.get("status"),
        "year_began": details.get("start_year") or fallback.get("year"),
        "cv_id": cv_id,
        "desc": _strip_html(details.get("description")),
        "resource_url": f"https://comicvine.gamespot.com/volume/4050-{cv_id}/",
        "cover_image": details.get("image_url"),
    }


def _fetch_series_dict(api, metron_id, fallback):
    """Build a series payload for save_series_mapping.

    Prefer the authoritative Metron object (gives name/publisher/status/year);
    for a ComicVine-sourced id fetch from ComicVine; otherwise fall back to
    sidecar-derived fields so we can still write a valid (name-bearing) row.
    """
    if is_comicvine_series_id(metron_id):
        return _fetch_comicvine_series_dict(metron_id, fallback)

    if api:
        try:
            info = api.series(metron_id)
            if info:
                if hasattr(info, "model_dump"):
                    data = info.model_dump(mode="json")
                elif hasattr(info, "dict"):
                    data = info.dict()
                else:
                    data = {"id": metron_id, "name": getattr(info, "name", "")}
                data["id"] = metron_id
                return data
        except Exception as e:
            app_logger.warning(f"automap: api.series({metron_id}) failed: {e}")

    return {
        "id": metron_id,
        "name": fallback.get("series_name") or f"Series {metron_id}",
        "publisher_name": fallback.get("publisher_name"),
        "status": fallback.get("status"),
        "year_began": fallback.get("year"),
        "cv_id": fallback.get("cv_id"),
    }


def _backfill_sidecars(folder, series_dict, metron_id, api):
    """Write the Metron id into the folder's sidecars so future scans skip the API."""
    # A ComicVine-sourced series has no Metron id; its offset id must never be
    # written into a sidecar as a Metron series_id (a later scan would misread
    # it). The sidecar already carries the cv_id, so leave it untouched.
    if is_comicvine_series_id(metron_id):
        return

    try:
        existing = read_series_json(folder)
        if not _sidecar_metadata(existing).get("metron_id"):
            write_series_json(folder, series_dict, api=api)
    except Exception as e:
        app_logger.warning(f"automap: series.json backfill failed for {folder}: {e}")

    try:
        cvinfo_path = comicvine.find_cvinfo_in_folder(folder)
        if cvinfo_path:
            if metron.parse_cvinfo_for_metron_id(cvinfo_path) != metron_id:
                metron.update_cvinfo_with_metron_id(cvinfo_path, metron_id)
        else:
            publisher = series_dict.get("publisher")
            publisher_name = series_dict.get("publisher_name") or (
                publisher.get("name") if isinstance(publisher, dict) else None
            )
            metron.create_cvinfo_file(
                os.path.join(folder, "cvinfo"),
                cv_id=series_dict.get("cv_id"),
                series_id=metron_id,
                publisher_name=publisher_name,
                start_year=series_dict.get("year_began"),
            )
    except Exception as e:
        app_logger.warning(f"automap: cvinfo backfill failed for {folder}: {e}")


def apply_automap(items, api=None):
    """Map each item's folder to its Metron series and backfill sidecars.

    Args:
        items: iterable of dicts with at least ``folder`` and ``metron_id``.
        api: Optional Metron API client (fetched if None).

    Returns:
        dict with ``applied`` (int), ``failed`` (list of {folder,error}), and
        ``applied_ids`` (list of Metron series ids).
    """
    from core.database import (
        save_publisher,
        save_series_mapping,
        upsert_publisher_by_name,
    )

    if api is None:
        api = metron.get_flask_api()

    applied = 0
    failed = []
    applied_ids = []

    for item in items:
        folder = item.get("folder")
        metron_id = _to_int(item.get("metron_id"))
        if not folder or not metron_id:
            failed.append({"folder": folder, "error": "Missing folder or metron_id"})
            continue
        if not os.path.isdir(folder):
            failed.append({"folder": folder, "error": "Folder no longer exists"})
            continue

        try:
            series_dict = _fetch_series_dict(api, metron_id, item)

            # Publisher: prefer the Metron nested publisher (has an id); when
            # Metron is unavailable (fallback dict), resolve the sidecar
            # publisher name to an id so the Pull List publisher column fills in.
            publisher = series_dict.get("publisher")
            if isinstance(publisher, dict) and publisher.get("id"):
                save_publisher(publisher.get("id"), publisher.get("name"))
            elif not series_dict.get("publisher_id"):
                pub_name = series_dict.get("publisher_name") or item.get("publisher_name")
                if pub_name:
                    pub_id = upsert_publisher_by_name(pub_name)
                    if pub_id:
                        series_dict["publisher_id"] = pub_id

            # Status: fall back to the sidecar status when Metron didn't supply one.
            if not series_dict.get("status") and item.get("status"):
                series_dict["status"] = item.get("status")

            if not save_series_mapping(
                series_dict, folder, cover_image=series_dict.get("cover_image")
            ):
                failed.append({"folder": folder, "error": "Failed to save mapping"})
                continue

            _backfill_sidecars(folder, series_dict, metron_id, api)
            applied += 1
            applied_ids.append(metron_id)
        except Exception as e:
            app_logger.error(f"automap: apply failed for {folder}: {e}")
            failed.append({"folder": folder, "error": str(e)})

    return {"applied": applied, "failed": failed, "applied_ids": applied_ids}


def _sync_and_match(api, series_id):
    """Bring a mapped series up to date and compute its collection match.

    Mirrors what the per-series "Refresh"/sync button does on the backend:
    pull issues from Metron (only if none are cached yet), then run
    ``match_issues_to_collection`` so the found/missing status is populated and
    cached — otherwise a freshly mapped series shows no owned/wanted state until
    the user opens its page.
    """
    from core.database import (
        get_series_by_id,
        get_series_mapping,
        get_issues_for_series,
    )
    from helpers.collection import match_issues_to_collection

    try:
        mapped_path = get_series_mapping(series_id)
        if not mapped_path or not os.path.isdir(mapped_path):
            return

        issues = get_issues_for_series(series_id)
        if not issues:
            if is_comicvine_series_id(series_id):
                _sync_comicvine_issues(series_id)
            elif api:
                from sync import sync_series_from_api

                sync_series_from_api(api, series_id)
            issues = get_issues_for_series(series_id)

        series_info = get_series_by_id(series_id)
        if series_info and issues:
            match_issues_to_collection(
                mapped_path, issues, series_info, use_cache=False
            )
    except Exception as e:
        app_logger.warning(f"automap: sync+match failed for {series_id}: {e}")


def _sync_comicvine_issues(series_id):
    """Fetch a ComicVine volume's issue list into the issues cache."""
    from core.database import (
        delete_issues_for_series,
        save_issues_bulk,
        update_series_sync_time,
    )

    key = _safe_cv_key()
    if not key:
        app_logger.info(
            f"automap: ComicVine API not available; cannot sync issues for {series_id}"
        )
        return
    cv_issues = comicvine.get_all_issues_for_volume(key, cv_id_from_series_id(series_id))
    if not cv_issues:
        return
    delete_issues_for_series(series_id)
    save_issues_bulk(cv_issues, series_id)
    update_series_sync_time(series_id, len(cv_issues))


def _sync_and_match_ids(api, series_ids):
    for series_id in series_ids:
        _sync_and_match(api, series_id)


def match_unmatched_mapped_series(api):
    """Sync + match every mapped series that has no cached collection status.

    Covers the series just auto-mapped (which have none) plus any previously
    mapped series that were never matched.
    """
    from core.database import (
        get_all_mapped_series,
        get_collection_status_for_series,
    )

    for row in get_all_mapped_series():
        series_id = row.get("id")
        if not series_id:
            continue
        if get_collection_status_for_series(series_id):
            continue  # already matched — leave it (and its Metron budget) alone
        _sync_and_match(api, series_id)


def apply_and_sync(items, api=None):
    """Apply mappings, then background-sync + match the newly mapped series."""
    if api is None:
        api = metron.get_flask_api()
    result = apply_automap(items, api=api)
    if result["applied_ids"]:
        threading.Thread(
            target=_sync_and_match_ids,
            args=(api, list(result["applied_ids"])),
            daemon=True,
        ).start()
    return result


# ── Background scan jobs ───────────────────────────────────────────────────
# A small self-contained job store so the Pull List can poll a long scan and
# retrieve the review/skipped lists once it finishes. (app_state's operation
# registry prunes completed ops after 15s, too short to hand back results.)

_jobs = {}
_jobs_lock = threading.Lock()
_JOB_TTL = 900  # keep finished jobs 15 min for the UI to fetch


def _prune_jobs_locked():
    now = time.time()
    for op_id in [
        oid
        for oid, job in _jobs.items()
        if job["status"] in ("done", "error")
        and (now - job.get("finished_at", now)) > _JOB_TTL
    ]:
        del _jobs[op_id]


def _update_job(op_id, **fields):
    with _jobs_lock:
        job = _jobs.get(op_id)
        if job:
            job.update(fields)


def _run_scan_job(op_id, app):
    # Runs in a background thread with no request/app context of its own; push
    # one so metron.get_flask_api() (and any downstream current_app use) works.
    with app.app_context():
        _run_scan_job_inner(op_id, app)


def _run_scan_job_inner(op_id, app):
    try:
        api = metron.get_flask_api(app)
        cv_api_key = comicvine.get_cv_api_key(app)

        def progress(current, total, folder):
            _update_job(
                op_id,
                current=current,
                total=total,
                detail=os.path.basename(folder) if folder else "Finishing...",
            )

        scan = scan_library_for_automap(
            api=api, cv_api_key=cv_api_key, progress_cb=progress
        )
        _update_job(op_id, detail="Applying matches...")
        applied = apply_automap(scan["auto"], api=api)

        result = {
            "applied": applied["applied"],
            "applied_failed": applied["failed"],
            "review": scan["review"],
            "skipped": scan["skipped"],
            "total_candidates": scan["total_candidates"],
        }
        with _jobs_lock:
            job = _jobs.get(op_id)
            if job:
                job.update(
                    status="done",
                    result=result,
                    current=job.get("total", 0),
                    finished_at=time.time(),
                )

        # Result is already stored (UI can render), so this runs as a
        # background tail: sync + match every mapped series that isn't matched
        # yet, so the Pull List / Wanted lists reflect owned vs missing without
        # the user opening each series.
        match_unmatched_mapped_series(api)
    except Exception as e:
        app_logger.error(f"automap: scan job {op_id} failed: {e}")
        with _jobs_lock:
            job = _jobs.get(op_id)
            if job:
                job.update(status="error", error=str(e), finished_at=time.time())


def start_scan_job(app):
    """Start a background scan + auto-apply. Returns the job/op id to poll.

    ``app`` is the Flask application object; the worker pushes its context so
    credential/config lookups work off the request thread.
    """
    op_id = uuid.uuid4().hex
    with _jobs_lock:
        _prune_jobs_locked()
        _jobs[op_id] = {
            "id": op_id,
            "status": "running",
            "current": 0,
            "total": 0,
            "detail": "Starting...",
            "result": None,
            "error": None,
            "started_at": time.time(),
        }
    threading.Thread(target=_run_scan_job, args=(op_id, app), daemon=True).start()
    return op_id


def get_scan_job(op_id):
    """Return a copy of a scan job's state, or None if unknown/expired."""
    with _jobs_lock:
        _prune_jobs_locked()
        job = _jobs.get(op_id)
        return dict(job) if job else None
