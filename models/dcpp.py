"""
DC++ download orchestration.

The DC++ counterpart to ``models/usenet.py``: search the user's AirDC++ hubs
for a wanted issue, score the hits with the existing GetComics scorer, queue
the winner in AirDC++, and — once the bundle finishes — move the comic
file(s) into the WATCH folder so the existing monitor pipeline imports them.

Two things differ from Usenet and drive the design here:

* **There are no indexers.** DC++ hubs are configured inside AirDC++ itself,
  so being "configured" means only that DC++ is an enabled source with an
  active client — there is no ``indexers`` table involvement.
* **Results are only addressable while their search instance lives.** AirDC++
  identifies a hit as ``(instance_id, result_id)``, so a manual search
  followed by the user clicking Download — two separate HTTP requests to CLU
  — needs the pairing kept somewhere. :data:`_result_tokens` is that cache:
  an opaque token per result, expiring after :data:`_TOKEN_TTL`. Unattended
  auto-download resolves its token in-process immediately and never depends
  on the TTL.

This module never touches ``api.py``. Completed files are landed in WATCH via
the movers in ``models/usenet.py`` (protocol-agnostic, and already carrying
the ``match_parent_permissions`` normalization) and the folder monitor takes
over from there.
"""
import threading
import time
import uuid

from core.app_logging import app_logger

# How often the completion poller runs while idle (winding down before exit).
_POLL_INTERVAL = 15
# Faster cadence while jobs are active, so cached progress stays fresh without
# hammering AirDC++.
_ACTIVE_POLL_INTERVAL = 5

# How long a manual search result stays grabbable. AirDC++ reports
# expires_in = 1800000ms (30 min) for a search instance, so this sits well
# inside the window while still bounding how long CLU holds the pairing.
_TOKEN_TTL = 900

# In-memory tracking of queued DC++ bundles, keyed by our download_id.
# Mirrors models.usenet.usenet_downloads so the status page renders both with
# the same code.
dcpp_downloads: dict = {}
_jobs_lock = threading.Lock()
_poller_thread = None
_poller_lock = threading.Lock()

# token -> {instance_id, result_id, name, size, expires}
_result_tokens: dict = {}
_tokens_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def dcpp_enabled_and_configured() -> bool:
    """True if DC++ is an enabled source AND a DC++ client is active.

    Deliberately does not check indexers — DC++ hubs live inside AirDC++.
    """
    from models.download_sources import source_enabled

    if not source_enabled("dcpp"):
        return False
    try:
        from core.database import get_active_download_client

        return bool(get_active_download_client(client_group="dcpp"))
    except Exception:
        return False


def _active_client():
    """Return the active DC++ client instance, or None."""
    from core.database import get_active_download_client
    from models.download_clients import (
        DownloadClientConfig,
        get_download_client_by_name,
    )

    active = get_active_download_client(client_group="dcpp")
    if not active:
        return None
    return get_download_client_by_name(
        active["client_type"], DownloadClientConfig.from_dict(active["config"] or {})
    )


# ---------------------------------------------------------------------------
# Search-result token cache
# ---------------------------------------------------------------------------

def _prune_tokens(now=None):
    """Drop expired tokens so the cache cannot grow without bound."""
    now = now if now is not None else time.time()
    with _tokens_lock:
        for token in [t for t, v in _result_tokens.items() if v["expires"] <= now]:
            del _result_tokens[token]


def _store_token(instance_id, result) -> str:
    """Cache one (instance, result) pairing and return its opaque token."""
    token = str(uuid.uuid4())
    with _tokens_lock:
        _result_tokens[token] = {
            "instance_id": instance_id,
            "result_id": result["result_id"],
            "name": result.get("name"),
            "size": result.get("size"),
            "expires": time.time() + _TOKEN_TTL,
        }
    return token


def _resolve_token(token):
    """Return the cached pairing for a token, or None if unknown/expired."""
    _prune_tokens()
    with _tokens_lock:
        entry = _result_tokens.get(token)
        return dict(entry) if entry else None


# ---------------------------------------------------------------------------
# Search + scoring
# ---------------------------------------------------------------------------

def search_dcpp_for_issue(
    series_name,
    issue_num,
    issue_year=None,
    series_volume=None,
    series_year=None,
    publisher_name=None,
    search_variants=None,
    series_aliases=None,
):
    """Search the active client's hubs for an issue and score the results.

    Returns the same shape as ``models.usenet.search_usenet_for_issue``:
    ``chosen`` ((result, score) or None), ``tier``, ``best_accept``,
    ``best_fallback``, ``all_results`` (scored dicts) and ``errors``.
    """
    from models.download_sources import build_queries
    from models.getcomics import score_getcomics_result, accept_result

    empty = {
        "chosen": None, "tier": None, "best_accept": None,
        "best_fallback": None, "all_results": [], "errors": [],
    }

    try:
        client = _active_client()
    except Exception as e:
        app_logger.error(f"DC++ search: could not build client: {e}")
        return {**empty, "errors": [str(e)]}
    if client is None:
        return {**empty, "errors": ["No active DC++ client"]}

    queries = build_queries(series_name, issue_num)

    raw_results = []
    errors = []
    seen = set()
    for q in queries:
        try:
            instance_id, results = client.search(q)
        except Exception as e:
            app_logger.error(f"DC++ search failed for '{q}': {e}")
            errors.append(str(e))
            continue
        if not instance_id:
            if getattr(client, "last_error", None):
                errors.append(client.last_error)
            continue
        for r in results:
            # De-dupe on TTH (the content hash) so the same file offered by
            # several users, or matched by several query variants, appears once.
            key = r.get("tth") or f"{instance_id}:{r['result_id']}"
            if key in seen:
                continue
            seen.add(key)
            raw_results.append((instance_id, r))

    app_logger.info(
        f"DC++ search {series_name} #{issue_num}: tried {queries} -> "
        f"{len(raw_results)} unique result(s)"
        + (f"; errors: {errors}" if errors else "")
    )

    best_accept = None
    best_fallback = None
    single_found = False
    scored = []

    for instance_id, r in raw_results:
        title = r.get("name") or ""
        score, is_range, series_match, issue_matched = score_getcomics_result(
            title, series_name, issue_num, issue_year,
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
        # Same guard as Usenet: DC++ filenames rarely carry a '#' issue marker,
        # so a wrong single issue can clear the threshold on series+year alone.
        # Only auto-accept when the target issue was positively confirmed.
        if decision == "ACCEPT" and not issue_matched:
            decision = "REJECT"

        token = _store_token(instance_id, r)
        entry = {
            "title": title,
            "result_token": token,
            "size": r.get("size"),
            "users": r.get("users"),
            "tth": r.get("tth"),
            "score": score,
            "decision": decision,
        }
        scored.append(entry)
        if decision == "ACCEPT":
            if best_accept is None or score > best_accept[1]:
                best_accept = (entry, score)
            single_found = True
        elif decision == "FALLBACK" and best_fallback is None:
            best_fallback = (entry, score)

    chosen = best_accept or best_fallback
    return {
        "chosen": chosen,
        "tier": "direct match" if best_accept else ("range fallback" if best_fallback else None),
        "best_accept": best_accept,
        "best_fallback": best_fallback,
        "all_results": scored,
        "errors": errors,
    }


def _make_filename(series_name, issue_num, chosen, tier) -> str:
    """Build the destination filename (range packs keep the release title)."""
    if tier == "range fallback":
        raw = chosen.get("title") or f"{series_name} {issue_num}"
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
    """Search DC++ for an issue and (unless dry_run) queue the best match.

    Signature and return contract match
    ``models.usenet.try_download_for_issue`` so the auto-download loop can
    treat every non-GetComics source uniformly.
    """
    res = search_dcpp_for_issue(
        series_name, issue_num,
        issue_year=issue_year,
        series_volume=series_volume,
        series_year=series_year,
        publisher_name=publisher_name,
        search_variants=search_variants,
        series_aliases=series_aliases,
    )
    out = {
        "source": "dcpp",
        "chosen": None,
        "submitted": False,
        "download_id": None,
        "all_results": res["all_results"],
        "status": "no_results" if not res["all_results"] else "no_match",
    }
    chosen = res["chosen"]
    if not chosen:
        return out

    entry, score = chosen
    filename = _make_filename(series_name, issue_num, entry, res["tier"])
    out["chosen"] = {
        "title": entry["title"],
        "result_token": entry["result_token"],
        "size": entry.get("size"),
        "score": score,
        "tier": res["tier"],
        "filename": filename,
    }
    out["status"] = "match_found"
    if dry_run:
        return out

    download_id = grab_dcpp(
        entry["result_token"], filename, series=series_name, issue=issue_num
    )
    out["submitted"] = bool(download_id)
    out["download_id"] = download_id
    out["status"] = "submitted" if download_id else "submit_failed"
    return out


def grab_dcpp(result_token, filename, series=None, issue=None):
    """Queue a previously-searched DC++ result and start tracking it.

    Returns the tracking ``download_id`` on success, or None on failure.
    """
    entry = _resolve_token(result_token)
    if not entry:
        app_logger.warning(
            "DC++ grab: search result expired — re-run the search and try again"
        )
        return None

    try:
        client = _active_client()
    except Exception as e:
        app_logger.error(f"DC++ grab: could not build client: {e}")
        return None
    if client is None:
        app_logger.warning("DC++ grab requested but no active DC++ client")
        return None

    result = client.download_result(entry["instance_id"], entry["result_id"])
    if not result.success:
        app_logger.error(f"DC++ grab failed: {result.error}")
        return None

    download_id = str(uuid.uuid4())
    with _jobs_lock:
        dcpp_downloads[download_id] = {
            "client_type": client.client_type.value,
            "client_id": result.client_id,
            "filename": filename,
            "status": "downloading",
            "error": None,
            "series": series,
            "issue": issue,
            "percent": 0,
            "stage": "Queued",
            "bytes_total": entry.get("size"),
            "bytes_downloaded": None,
            # Filled in by the poller from the bundle's target path.
            "target": None,
        }
    app_logger.info(
        f"Queued DC++ download for {filename} (bundle={result.client_id})"
    )
    _ensure_poller()
    return download_id


def get_dcpp_downloads() -> list:
    """Return a snapshot of tracked DC++ downloads (for status display)."""
    with _jobs_lock:
        return [dict(download_id=k, **v) for k, v in dcpp_downloads.items()]


# ---------------------------------------------------------------------------
# Completion polling
# ---------------------------------------------------------------------------

def _ensure_poller():
    """Start the completion poller thread if it isn't already running."""
    global _poller_thread
    with _poller_lock:
        if _poller_thread is not None and _poller_thread.is_alive():
            return
        _poller_thread = threading.Thread(
            target=_poll_loop, name="dcpp-poller", daemon=True
        )
        _poller_thread.start()


def _pending_jobs():
    with _jobs_lock:
        return {k: dict(v) for k, v in dcpp_downloads.items()
                if v["status"] == "downloading"}


def _poll_loop():
    """Poll AirDC++ until all tracked bundles reach a terminal state."""
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

        try:
            client = _active_client()
        except Exception as e:
            app_logger.error(f"DC++ poll: could not build client: {e}")
            client = None

        if client is not None:
            for download_id, job in pending.items():
                _poll_one(client, download_id, job)

        time.sleep(_ACTIVE_POLL_INTERVAL)


def _poll_one(client, download_id, job):
    """Advance a single tracked bundle by one poll."""
    try:
        state, status = client.get_bundle_state(job["client_id"])
    except Exception as e:
        app_logger.error(f"DC++ status fetch failed for {job['filename']}: {e}")
        return

    if state == "error":
        return  # transient; try again next round

    if state == "gone":
        # AirDC++ no longer holds the bundle. With "remove finished bundles"
        # enabled that is the *only* signal a download completed, so treat it
        # as done and import from the target we cached while it was running.
        # A user who cancelled by hand lands here too; the import simply finds
        # nothing and the job records complete_no_move.
        imported = _import_completed(job.get("target"), job["filename"])
        _set_status(download_id, "complete" if imported else "complete_no_move",
                    percent=100)
        return

    if status.status == "complete":
        imported = _import_completed(status.storage_path, job["filename"])
        _set_status(download_id, "complete" if imported else "complete_no_move",
                    percent=100)
    elif status.status == "failed":
        _set_status(download_id, "failed", error="Download failed at client")
    else:
        _update_progress(download_id, status)


def _update_progress(download_id, status):
    """Cache live percent/stage/bytes from a DownloadStatus onto a job.

    ``target`` is cached too: once AirDC++ drops a finished bundle from the
    queue there is nothing left to ask where the file landed, so the last
    value seen while it was running is what the WATCH import has to use.
    """
    with _jobs_lock:
        job = dcpp_downloads.get(download_id)
        if job is None:
            return
        job["percent"] = status.percent
        job["stage"] = status.stage
        job["bytes_total"] = status.bytes_total
        job["bytes_downloaded"] = status.bytes_downloaded
        if status.storage_path:
            job["target"] = status.storage_path


def _set_status(download_id, status, error=None, percent=None):
    with _jobs_lock:
        job = dcpp_downloads.get(download_id)
        if job is not None:
            job["status"] = status
            job["error"] = error
            if percent is not None:
                job["percent"] = percent


def _import_completed(storage_path, filename) -> bool:
    """Move completed comic file(s) into WATCH.

    Delegates to the Usenet mover — landing a finished file in WATCH is
    protocol-agnostic, and that implementation already normalizes permissions
    via ``match_parent_permissions``. Keeping one copy means DC++ and Usenet
    can never drift on how files enter the pipeline.
    """
    from models.usenet import _import_completed as _shared_import

    return _shared_import(storage_path, filename, source="DC++")
