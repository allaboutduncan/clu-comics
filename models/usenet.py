"""
Usenet download orchestration (PR 2).

Wires the download-client and indexer building blocks into the download
flow: search enabled indexers for a wanted issue, score results with the
existing GetComics scorer, submit the winner to the active download
client, and — once the client reports completion — move the finished
comic file(s) into the WATCH folder so the existing monitor pipeline
imports them.

This module never touches ``api.py``. It reads the WATCH path via
``core.config`` (the same source ``api.py`` uses) and lands files there;
the folder monitor handles convert/rename/import from that point on.
"""
import json
import os
import shutil
import threading
import time
import uuid

from core.app_logging import app_logger

# Comic/container extensions we hand off to the WATCH pipeline.
_COMIC_EXTS = {".cbz", ".cbr", ".cbt", ".pdf", ".zip", ".rar"}

# How often the completion poller checks client history.
_POLL_INTERVAL = 15

# In-memory tracking of submitted Usenet jobs, keyed by our download_id.
# Each value: {client_type, client_id, filename, status, error, series, issue}.
usenet_downloads: dict = {}
_jobs_lock = threading.Lock()
_poller_thread = None
_poller_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def get_source_priority() -> list:
    """Return the ordered download-source list (default GetComics only).

    Stored as a JSON list under the ``download_source_priority`` preference.
    """
    try:
        from core.database import get_user_preference

        raw = get_user_preference("download_source_priority", default=None)
        if not raw:
            return ["getcomics"]
        value = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(value, list) and value:
            return [str(s) for s in value]
    except Exception:
        pass
    return ["getcomics"]


def usenet_enabled_and_configured() -> bool:
    """True if Usenet is a source AND a client is active AND an indexer is enabled."""
    if "usenet" not in get_source_priority():
        return False
    try:
        from core.database import get_active_download_client, get_enabled_indexers

        return bool(get_active_download_client()) and bool(get_enabled_indexers())
    except Exception:
        return False


def usenet_precedes_getcomics() -> bool:
    """True if 'usenet' ranks before 'getcomics' in the source priority."""
    order = get_source_priority()
    un = order.index("usenet") if "usenet" in order else 999
    gc = order.index("getcomics") if "getcomics" in order else 999
    return un < gc


def _watch_dir() -> str:
    """Resolve the WATCH staging path the same way api.py does."""
    try:
        from core.config import get_watch_dir

        watch = get_watch_dir()
        if watch:
            return watch
    except Exception:
        pass
    try:
        from core.config import config

        return config.get("SETTINGS", "WATCH", fallback="") or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Search + scoring
# ---------------------------------------------------------------------------

def search_usenet_for_issue(
    series_name,
    issue_num,
    issue_year=None,
    series_volume=None,
    series_year=None,
    publisher_name=None,
    search_variants=None,
    series_aliases=None,
    limit=100,
):
    """Search enabled indexers for an issue and score the results.

    Returns a dict with ``chosen`` ((result, score) or None), ``tier``,
    ``best_accept``, ``best_fallback`` and ``all_results`` (scored dicts),
    mirroring the GetComics engine's decision shape.
    """
    from core.database import get_enabled_indexers
    from models.getcomics import score_getcomics_result, accept_result
    from models.indexers import IndexerConfig, IndexerType, get_indexer_impl

    queries = _build_queries(series_name, issue_num)

    raw_results = []
    errors = []
    seen = set()
    for idx in get_enabled_indexers():
        name = idx.get("name", "")
        try:
            cfg = IndexerConfig(
                name=name,
                url=idx.get("url", ""),
                api_key=idx.get("api_key"),
                categories=idx.get("categories"),
                enabled=idx.get("enabled", True),
            )
            impl = get_indexer_impl(
                IndexerType(idx.get("indexer_type", "newznab")), cfg
            )
            found_any = False
            for q in queries:
                found = impl.search(q, limit=limit, indexer_id=idx.get("id", 0))
                for r in found:
                    key = r.nzb_url or r.guid or r.title
                    if key in seen:
                        continue
                    seen.add(key)
                    raw_results.append(r)
                    found_any = True
            if not found_any and getattr(impl, "last_error", None):
                errors.append(f"{name}: {impl.last_error}")
        except Exception as e:
            app_logger.error(f"Indexer search failed for {name}: {e}")
            errors.append(f"{name}: {e}")

    app_logger.info(
        f"Usenet search {series_name} #{issue_num}: tried {queries} -> "
        f"{len(raw_results)} unique result(s)"
        + (f"; errors: {errors}" if errors else "")
    )

    best_accept = None
    best_fallback = None
    single_found = False
    scored = []

    for r in raw_results:
        score, is_range, series_match, issue_matched = score_getcomics_result(
            r.title, series_name, issue_num, issue_year,
            accept_variants=search_variants,
            series_volume=series_volume,
            volume_year=series_year,
            publisher_name=publisher_name,
            series_aliases=series_aliases,
            return_issue_matched=True,
        )
        decision = accept_result(
            score, is_range, series_match, single_issue_found=single_found
        )
        # Usenet releases rarely carry a '#' issue marker, so a wrong single
        # issue can still clear the score threshold on series+year alone. Only
        # auto-accept a direct match when the target issue was positively
        # confirmed; range packs (FALLBACK) legitimately have no single confirm.
        if decision == "ACCEPT" and not issue_matched:
            decision = "REJECT"
        scored.append({
            "title": r.title,
            "nzb_url": r.nzb_url,
            "indexer_id": r.indexer_id,
            "indexer_name": r.indexer_name,
            "size": r.size,
            "score": score,
            "decision": decision,
        })
        if decision == "ACCEPT":
            if best_accept is None or score > best_accept[1]:
                best_accept = (r, score)
            single_found = True
        elif decision == "FALLBACK" and best_fallback is None:
            best_fallback = (r, score)

    chosen = best_accept or best_fallback
    return {
        "chosen": chosen,
        "tier": "direct match" if best_accept else ("range fallback" if best_fallback else None),
        "best_accept": best_accept,
        "best_fallback": best_fallback,
        "all_results": scored,
        "errors": errors,
    }


def _build_queries(series_name, issue_num) -> list:
    """Build indexer query variants for a series/issue.

    Releases commonly zero-pad the issue number (e.g. "004", "#4"), so we try
    the raw number plus 2- and 3-digit padded forms. Order matters only for
    logging; results are de-duplicated by NZB URL.
    """
    s = str(issue_num or "").strip()
    variants = []
    if s:
        variants.append(s)
        if s.isdigit():
            n = int(s)
            for width in (2, 3):
                p = str(n).zfill(width)
                if p not in variants:
                    variants.append(p)
    else:
        variants.append("")
    base = series_name.strip()
    return [f"{base} {v}".strip() for v in variants]


# ---------------------------------------------------------------------------
# Submission + completion tracking
# ---------------------------------------------------------------------------

def _fetch_nzb(url):
    """Download an NZB from ``url``; return bytes if it looks like an NZB, else None."""
    try:
        import requests

        resp = requests.get(url, timeout=30)
        if resp.status_code != 200 or not resp.content:
            app_logger.warning(f"Usenet: NZB fetch HTTP {resp.status_code} for {url}")
            return None
        head = resp.content.lstrip()[:256].lower()
        if b"<?xml" in head or b"<nzb" in head:
            return resp.content
        app_logger.warning(
            f"Usenet: fetched URL did not return an NZB (looks like a page): {url}"
        )
        return None
    except Exception as e:
        app_logger.error(f"Usenet: NZB fetch failed for {url}: {e}")
        return None


def _make_filename(series_name, issue_num, chosen_result, tier) -> str:
    """Build the destination filename (range packs keep the release title)."""
    if tier == "range fallback":
        raw = chosen_result.title
    else:
        raw = f"{series_name} {issue_num}"
    return raw.replace("/", "-").replace("\\", "-").replace("#", "").strip() + ".cbz"


def try_download_for_issue(
    series_name,
    issue_num,
    *,
    issue_year=None,
    series_volume=None,
    series_year=None,
    publisher_name=None,
    search_variants=None,
    series_aliases=None,
    dry_run=False,
):
    """Search Usenet for an issue and (unless dry_run) submit the best match.

    Returns a dict describing the outcome: ``status`` (no_results/no_match/
    match_found/submitted/submit_failed), ``chosen`` (None or a summary dict),
    ``all_results`` (scored), and ``download_id`` when submitted.
    """
    res = search_usenet_for_issue(
        series_name, issue_num,
        issue_year=issue_year,
        series_volume=series_volume,
        series_year=series_year,
        publisher_name=publisher_name,
        search_variants=search_variants,
        series_aliases=series_aliases,
    )
    out = {
        "source": "usenet",
        "chosen": None,
        "submitted": False,
        "download_id": None,
        "all_results": res["all_results"],
        "status": "no_results" if not res["all_results"] else "no_match",
    }
    chosen = res["chosen"]
    if not chosen:
        return out

    result, score = chosen
    filename = _make_filename(series_name, issue_num, result, res["tier"])
    out["chosen"] = {
        "title": result.title,
        "nzb_url": result.nzb_url,
        "indexer_name": result.indexer_name,
        "score": score,
        "tier": res["tier"],
        "filename": filename,
    }
    out["status"] = "match_found"
    if dry_run:
        return out

    download_id = grab_nzb(result.nzb_url, filename, series=series_name, issue=issue_num)
    out["submitted"] = bool(download_id)
    out["download_id"] = download_id
    out["status"] = "submitted" if download_id else "submit_failed"
    return out


def grab_nzb(nzb_url, filename, series=None, issue=None):
    """Submit an NZB URL to the active download client and start tracking.

    Returns the tracking ``download_id`` on success, or None on failure.
    """
    from core.database import get_active_download_client
    from models.download_clients import get_download_client_by_name, DownloadClientConfig

    active = get_active_download_client()
    if not active:
        app_logger.warning("Usenet grab requested but no active download client")
        return None

    client_type = active["client_type"]
    cfg = active["config"] or {}
    client = get_download_client_by_name(client_type, DownloadClientConfig.from_dict(cfg))

    # Fetch the NZB ourselves and submit the real content. This is more reliable
    # than handing the client a URL to fetch (some clients/versions fetch poorly,
    # and an indexer <link> can be a details page rather than the NZB). Falls back
    # to the URL if the fetch doesn't yield an NZB.
    payload = _fetch_nzb(nzb_url) or nzb_url

    result = client.add_nzb(payload, filename)
    if not result.success:
        app_logger.error(f"Usenet grab failed ({client_type}): {result.error}")
        return None

    download_id = str(uuid.uuid4())
    with _jobs_lock:
        usenet_downloads[download_id] = {
            "client_type": client_type,
            "client_id": result.client_id,
            "filename": filename,
            "status": "downloading",
            "error": None,
            "series": series,
            "issue": issue,
        }
    app_logger.info(
        f"Submitted NZB to {client_type} for {filename} (client_id={result.client_id})"
    )
    _ensure_poller()
    return download_id


def get_usenet_downloads() -> list:
    """Return a snapshot of tracked Usenet downloads (for status display)."""
    with _jobs_lock:
        return [dict(download_id=k, **v) for k, v in usenet_downloads.items()]


def _ensure_poller():
    """Start the completion poller thread if it isn't already running."""
    global _poller_thread
    with _poller_lock:
        if _poller_thread is not None and _poller_thread.is_alive():
            return
        _poller_thread = threading.Thread(
            target=_poll_loop, name="usenet-poller", daemon=True
        )
        _poller_thread.start()


def _pending_jobs():
    with _jobs_lock:
        return {k: dict(v) for k, v in usenet_downloads.items()
                if v["status"] == "downloading"}


def _poll_loop():
    """Poll the active client's history until all jobs reach a terminal state."""
    idle_rounds = 0
    while True:
        pending = _pending_jobs()
        if not pending:
            idle_rounds += 1
            if idle_rounds >= 2:
                return
            time.sleep(_POLL_INTERVAL)
            continue
        idle_rounds = 0

        # Group pending jobs by client type; fetch each client's history once.
        histories = {}
        for job in pending.values():
            ct = job["client_type"]
            if ct not in histories:
                histories[ct] = _history_for(ct)

        for download_id, job in pending.items():
            hist = histories.get(job["client_type"]) or {}
            status = hist.get(str(job["client_id"]))
            if status is None:
                continue  # still downloading / not yet in history
            if status.status == "complete":
                imported = _import_completed(status.storage_path, job["filename"])
                _set_status(download_id, "complete" if imported else "complete_no_move")
            elif status.status == "failed":
                _set_status(download_id, "failed", error="Download failed at client")

        time.sleep(_POLL_INTERVAL)


def _history_for(client_type):
    """Return {client_id: DownloadStatus} for a client type, or {} on error."""
    try:
        from core.database import get_download_client_config
        from models.download_clients import (
            get_download_client_by_name, DownloadClientConfig,
        )

        cfg = get_download_client_config(client_type)
        if not cfg:
            return {}
        client = get_download_client_by_name(
            client_type, DownloadClientConfig.from_dict(cfg)
        )
        return {h.client_id: h for h in client.get_history()}
    except Exception as e:
        app_logger.error(f"Usenet history fetch failed for {client_type}: {e}")
        return {}


def _set_status(download_id, status, error=None):
    with _jobs_lock:
        if download_id in usenet_downloads:
            usenet_downloads[download_id]["status"] = status
            usenet_downloads[download_id]["error"] = error


def _import_completed(storage_path, filename) -> bool:
    """Move completed comic file(s) from the client's storage into WATCH.

    Returns True if the file(s) are (or already are) in the pipeline. Returns
    False when the completed path is not accessible to CLU — in that case the
    client must be configured to complete directly into a monitored folder.
    """
    watch = _watch_dir()
    if not watch or not os.path.isdir(watch):
        app_logger.error("Usenet import: WATCH folder is not configured/accessible")
        return False
    if not storage_path:
        app_logger.warning(
            f"Usenet import: no storage path for {filename}; relying on the client's "
            f"completed folder being a monitored path"
        )
        return False

    watch_abs = os.path.abspath(watch)
    src_abs = os.path.abspath(storage_path)

    if not os.path.exists(src_abs):
        app_logger.warning(
            f"Usenet import: completed path {storage_path} is not accessible from CLU "
            f"(is it on a shared volume?) — relying on the monitored folder"
        )
        return False

    # Already inside WATCH -> the folder monitor will pick it up.
    if src_abs == watch_abs or src_abs.startswith(watch_abs + os.sep):
        return True

    moved = 0
    if os.path.isfile(src_abs):
        if os.path.splitext(src_abs)[1].lower() in _COMIC_EXTS:
            moved += _move_into_watch(src_abs, watch)
    else:
        for root, _dirs, files in os.walk(src_abs):
            for f in files:
                if os.path.splitext(f)[1].lower() in _COMIC_EXTS:
                    moved += _move_into_watch(os.path.join(root, f), watch)

    if moved:
        app_logger.info(f"Usenet import: moved {moved} file(s) into WATCH for {filename}")
        return True
    app_logger.warning(f"Usenet import: no comic files found under {storage_path}")
    return False


def _move_into_watch(src_file, watch) -> int:
    """Move a single file into WATCH with a unique name; return 1/0."""
    try:
        dest = _unique_path(os.path.join(watch, os.path.basename(src_file)))
        shutil.move(src_file, dest)
        try:
            from helpers import match_parent_permissions
            match_parent_permissions(dest)
        except Exception:
            pass
        return 1
    except Exception as e:
        app_logger.error(f"Usenet import: failed to move {src_file}: {e}")
        return 0


def _unique_path(path) -> str:
    """Return ``path`` or a ' (n)'-suffixed variant that does not exist."""
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"
