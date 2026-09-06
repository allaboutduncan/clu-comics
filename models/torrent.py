"""
Torrent download orchestration.

Wires the Torznab-indexer and qBittorrent-client building blocks into the
download flow: search enabled Torznab indexers for a wanted issue, score
results with the existing GetComics scorer, submit the winner to the active
torrent client, and — once qBittorrent reports completion — move the
finished comic file(s) into the WATCH folder so the existing monitor
pipeline imports them.

Structurally this is a merge of the other two source modules: the
indexer-driven search is ``models/usenet.py``'s shape, and the job ledger +
continuous poller are ``models/dcpp.py``'s shape — qBittorrent, like
AirDC++, is a separate long-running process whose torrents survive a CLU
restart, so a crash-recovery ledger is needed the way it isn't for
SABnzbd/NZBGet.

This module never touches ``api.py``. It reads the WATCH path via
``models.usenet._watch_dir`` (the same source ``api.py`` uses) and lands
files there via the shared mover; the folder monitor handles convert/rename/
import from that point on.
"""
import threading
import time
import uuid

from core.app_logging import app_logger

# Cadence while jobs are active, so cached progress stays fresh without
# hammering qBittorrent.
_ACTIVE_POLL_INTERVAL = 5
# Heartbeat cadence with no tracked jobs pending. The poller stays alive while
# Torrent is configured so the status page can show qBittorrent's real queue —
# mirrors models.dcpp's rationale for not exiting when idle.
_IDLE_POLL_INTERVAL = 30

# A torrent with no seeders will never complete — drop it before scoring
# rather than let it win on title/year alone and then stall forever.
_MIN_SEEDERS = 1

# In-memory tracking of submitted torrent jobs, keyed by our download_id.
# Mirrors models.usenet.usenet_downloads / models.dcpp.dcpp_downloads so the
# status page renders all three with the same shape.
torrent_downloads: dict = {}
_jobs_lock = threading.Lock()
_poller_thread = None
_poller_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

def torrent_enabled_and_configured() -> bool:
    """True if Torrent is a source AND a client is active AND a Torznab indexer is enabled."""
    from models.download_sources import enabled_indexers_of_type, source_enabled

    if not source_enabled("torrent"):
        return False
    try:
        from core.database import get_active_download_client

        return bool(get_active_download_client(client_group="torrent")) and bool(
            enabled_indexers_of_type("torznab")
        )
    except Exception:
        return False


def _active_client():
    """Return the active torrent client instance, or None."""
    from core.database import get_active_download_client
    from models.download_clients import (
        DownloadClientConfig,
        get_download_client_by_name,
    )

    active = get_active_download_client(client_group="torrent")
    if not active:
        return None
    return get_download_client_by_name(
        active["client_type"], DownloadClientConfig.from_dict(active["config"] or {})
    )


# ---------------------------------------------------------------------------
# Search + scoring
# ---------------------------------------------------------------------------

def search_torrent_for_issue(
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
    """Search enabled Torznab indexers for an issue and score the results.

    Returns a dict with ``chosen`` ((result, score) or None), ``tier``,
    ``best_accept``, ``best_fallback`` and ``all_results`` (scored dicts),
    mirroring ``models.usenet.search_usenet_for_issue``'s shape.
    """
    from models.download_sources import build_queries, enabled_indexers_of_type
    from models.getcomics import score_getcomics_result, accept_result
    from models.indexers import IndexerConfig, IndexerType, get_indexer_impl

    queries = build_queries(series_name, issue_num)

    raw_results = []
    errors = []
    seen = set()
    # Torznab only — a Newznab-configured indexer belongs to the Usenet
    # search (models.usenet), never here.
    for idx in enabled_indexers_of_type("torznab"):
        name = idx.get("name", "")
        try:
            cfg = IndexerConfig(
                name=name,
                url=idx.get("url", ""),
                api_key=idx.get("api_key"),
                categories=idx.get("categories"),
                enabled=idx.get("enabled", True),
            )
            impl = get_indexer_impl(IndexerType.TORZNAB, cfg)
            found_any = False
            for q in queries:
                found = impl.search(q, limit=limit, indexer_id=idx.get("id", 0))
                for r in found:
                    key = r.download_url or r.guid or r.title
                    if key in seen:
                        continue
                    seen.add(key)
                    # A dead torrent (no seeders) will never complete. Drop it
                    # here rather than let it win on title/year alone.
                    if r.seeders is not None and r.seeders < _MIN_SEEDERS:
                        continue
                    raw_results.append(r)
                    found_any = True
            if not found_any and getattr(impl, "last_error", None):
                errors.append(f"{name}: {impl.last_error}")
        except Exception as e:
            app_logger.error(f"Torznab search failed for {name}: {e}")
            errors.append(f"{name}: {e}")

    app_logger.info(
        f"Torrent search {series_name} #{issue_num}: tried {queries} -> "
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
        # Same guard as Usenet/DC++: torrent release names rarely carry a '#'
        # issue marker, so a wrong single issue can still clear the score
        # threshold on series+year alone. Only auto-accept a direct match when
        # the target issue was positively confirmed.
        if decision == "ACCEPT" and not issue_matched:
            decision = "REJECT"
        scored.append({
            "title": r.title,
            "download_url": r.download_url,
            "indexer_id": r.indexer_id,
            "indexer_name": r.indexer_name,
            "size": r.size,
            "seeders": r.seeders,
            "peers": r.peers,
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
    """Search Torrent for an issue and (unless dry_run) submit the best match.

    Signature and return contract match
    ``models.usenet.try_download_for_issue`` / ``models.dcpp.try_download_for_issue``
    so the auto-download loop can treat every source uniformly.
    """
    res = search_torrent_for_issue(
        series_name, issue_num,
        issue_year=issue_year,
        series_volume=series_volume,
        series_year=series_year,
        publisher_name=publisher_name,
        search_variants=search_variants,
        series_aliases=series_aliases,
    )
    out = {
        "source": "torrent",
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
        "download_url": result.download_url,
        "indexer_name": result.indexer_name,
        "score": score,
        "tier": res["tier"],
        "filename": filename,
    }
    out["status"] = "match_found"
    if dry_run:
        return out

    download_id = grab_torrent(
        result.download_url, filename, series=series_name, issue=issue_num
    )
    out["submitted"] = bool(download_id)
    out["download_id"] = download_id
    out["status"] = "submitted" if download_id else "submit_failed"
    return out


# ---------------------------------------------------------------------------
# Submission + completion tracking
# ---------------------------------------------------------------------------

def _job_name(filename) -> str:
    """Strip the comic extension off a filename, mirroring models.usenet._job_name.

    qBittorrent names the torrent after what the tracker reports, not what we
    pass, so this mostly keeps the job dict's ``filename`` consistent with the
    other sources — but a torrent submitted with an extension-bearing name
    could still confuse a tracker that echoes it back as a suggested save name.
    """
    import os

    base, ext = os.path.splitext(filename or "")
    if base and ext.lower() in {".cbz", ".cbr", ".cbt", ".pdf", ".zip", ".rar", ".torrent"}:
        return base
    return filename or ""


def grab_torrent(download_url, filename, series=None, issue=None, errors=None):
    """Submit a magnet/.torrent URL to the active torrent client and start tracking.

    Returns the tracking ``download_id`` on success, or None on failure.

    ``errors`` is an optional list the reason for a failure is appended to
    (mirrors ``models.dcpp.grab_dcpp``'s contract) — a rejected torrent is
    often a configuration problem (auth, category) worth surfacing verbatim.
    """
    def _fail(reason, level="error"):
        getattr(app_logger, level)(f"Torrent grab failed: {reason}")
        if errors is not None:
            errors.append(reason)
        return None

    try:
        client = _active_client()
    except Exception as e:
        return _fail(f"Could not build the torrent client: {e}")
    if client is None:
        return _fail("No active torrent client is configured", level="warning")

    result = client.add_torrent(download_url, _job_name(filename))
    if not result.success:
        return _fail(result.error or "The torrent client rejected the download")

    download_id = str(uuid.uuid4())
    job = {
        "client_type": client.client_type.value,
        "client_id": result.client_id,
        "filename": filename,
        "status": "downloading",
        "error": None,
        "series": series,
        "issue": issue,
        "percent": 0,
        "stage": "Queued",
        "bytes_total": None,
        "bytes_downloaded": None,
        # Filled in by the poller from the torrent's storage path.
        "target": None,
    }
    with _jobs_lock:
        torrent_downloads[download_id] = job

    # Persist before starting the poller, for the same reason the DC++ ledger
    # row is written before the queue put: the torrent is already live inside
    # qBittorrent, and a crash between here and the first poll must not lose
    # our only record of it.
    _persist_new(download_id, job)

    app_logger.info(
        f"Submitted torrent to {client.client_type.value} for {filename} "
        f"(hash={result.client_id})"
    )
    _ensure_poller()
    return download_id


def get_torrent_downloads() -> list:
    """Return tracked torrent downloads (for the status page)."""
    with _jobs_lock:
        return [dict(download_id=k, **v) for k, v in torrent_downloads.items()]


def dismiss_torrent_job(download_id: str) -> bool:
    """Drop a resolved-but-unimported torrent job from the ledger and memory.

    Returns False if the job isn't tracked, so the route can 404. Mirrors
    ``models.dcpp.dismiss_dcpp_job``.
    """
    with _jobs_lock:
        known = download_id in torrent_downloads
        torrent_downloads.pop(download_id, None)
    deleted = _persist_delete(download_id)
    return known or deleted


# ---------------------------------------------------------------------------
# Persistence — the torrent_jobs ledger
# ---------------------------------------------------------------------------
#
# Exists purely so a CLU restart is a non-event. qBittorrent is a separate
# process and keeps downloading/seeding regardless, but the torrent hash and
# last-known storage path used to live only in ``torrent_downloads`` — so a
# restart orphaned the job and a finished download was never imported into
# WATCH. Mirrors models.dcpp's ledger functions exactly.

def _persist_new(download_id, job):
    try:
        from core.database import save_torrent_job

        save_torrent_job(download_id, job)
    except Exception as e:
        app_logger.error(f"Could not persist torrent job {download_id}: {e}")


def _persist_update(download_id, **fields):
    try:
        from core.database import update_torrent_job

        update_torrent_job(download_id, **fields)
    except Exception as e:
        app_logger.error(f"Could not update torrent job {download_id}: {e}")


def _persist_delete(download_id) -> bool:
    """Delete a ledger row. Returns True if one was actually removed."""
    try:
        from core.database import delete_torrent_job

        return delete_torrent_job(download_id)
    except Exception as e:
        app_logger.error(f"Could not delete torrent job {download_id}: {e}")
        return False


def recover_torrent_jobs() -> int:
    """Re-adopt torrents left in flight by a previous CLU process.

    Deliberately DB-only — see ``models.dcpp.recover_dcpp_jobs`` for why (this
    runs at import time under Gunicorn, and a per-torrent HTTP call with a 10s
    timeout would stall boot). The poller's first round reconciles with
    qBittorrent, including a torrent that finished (or was removed) while CLU
    was down, using the ``target`` path restored here.

    Returns the number of jobs re-adopted.
    """
    try:
        from core.database import get_active_torrent_jobs

        rows = get_active_torrent_jobs()
    except Exception as e:
        app_logger.error(f"Torrent recovery could not read the job ledger: {e}")
        return 0

    recovered = 0
    with _jobs_lock:
        for row in rows:
            download_id = row.get("download_id")
            if not download_id or download_id in torrent_downloads:
                continue
            torrent_downloads[download_id] = {
                "client_type": row.get("client_type"),
                "client_id": row.get("client_id"),
                "filename": row.get("filename"),
                "status": row.get("status") or "downloading",
                "error": row.get("error"),
                "series": row.get("series"),
                "issue": row.get("issue"),
                "percent": row.get("percent") or 0,
                "stage": row.get("stage"),
                "bytes_total": row.get("bytes_total"),
                "bytes_downloaded": row.get("bytes_downloaded"),
                "target": row.get("target"),
            }
            recovered += 1

    if recovered:
        app_logger.info(f"Recovered {recovered} torrent job(s) from the ledger")
    if recovered or torrent_enabled_and_configured():
        _ensure_poller()
    return recovered


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
            target=_poll_loop, name="torrent-poller", daemon=True
        )
        _poller_thread.start()


def _pending_jobs():
    with _jobs_lock:
        return {k: dict(v) for k, v in torrent_downloads.items()
                if v["status"] == "downloading"}


def _poll_loop():
    """Poll qBittorrent for as long as Torrent is configured.

    Runs continuously at a slow heartbeat when nothing is pending — same
    rationale as models.dcpp._poll_loop: a torrent recovered from the ledger
    shouldn't have to wait for a new grab before anything polls it, and the
    idle rounds keep the status page's queue view fresh.
    """
    while True:
        pending = {}
        client = None
        try:
            pending = _pending_jobs()
            try:
                client = _active_client()
            except Exception as e:
                app_logger.error(f"Torrent poll: could not build client: {e}")
                client = None

            if client is None:
                # Nothing to poll with. Exit only if Torrent is gone entirely —
                # a client that merely failed to build is worth retrying, but a
                # deconfigured one never resolves, and holding the thread on a
                # pending job would keep it alive for the process's life.
                # grab_torrent() and recover_torrent_jobs() both restart it.
                if not torrent_enabled_and_configured():
                    return
            else:
                for download_id, job in pending.items():
                    _poll_one(client, download_id, job)
        except Exception as e:
            # A bad round must never kill the thread: nothing restarts it, and
            # every tracked torrent would silently stall for the process's life.
            app_logger.error(f"Torrent poll round failed: {e}")

        hurry = pending and client is not None
        time.sleep(_ACTIVE_POLL_INTERVAL if hurry else _IDLE_POLL_INTERVAL)


def _poll_one(client, download_id, job):
    """Advance a single tracked torrent by one poll."""
    try:
        status = client.get_status(job["client_id"])
    except Exception as e:
        app_logger.error(f"Torrent status fetch failed for {job['filename']}: {e}")
        return

    if status is None:
        # qBittorrent no longer holds the torrent — either the user removed it
        # by hand, or it was auto-removed on completion (a "remove torrent
        # after completion" rule). Either way there is nothing left to poll,
        # so this is the only remaining chance to import it, using the last
        # storage path seen while it was active. Mirrors models.dcpp's "gone"
        # handling of a bundle AirDC++ has dropped from its queue.
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

    ``target`` is cached too, for the same reason models.dcpp caches it: if
    qBittorrent is ever configured to remove a finished torrent, the last
    value seen while it was active is what the WATCH import has to use.
    """
    with _jobs_lock:
        job = torrent_downloads.get(download_id)
        if job is None:
            return
        changed = (
            int(status.percent or 0) != int(job.get("percent") or 0)
            or status.stage != job.get("stage")
            or (status.storage_path and status.storage_path != job.get("target"))
        )
        job["percent"] = status.percent
        job["stage"] = status.stage
        job["bytes_total"] = status.bytes_total
        job["bytes_downloaded"] = status.bytes_downloaded
        if status.storage_path:
            job["target"] = status.storage_path
        current = dict(job)

    if changed:
        _persist_update(
            download_id,
            percent=current.get("percent"),
            stage=current.get("stage"),
            bytes_total=current.get("bytes_total"),
            bytes_downloaded=current.get("bytes_downloaded"),
            target=current.get("target"),
        )


def _set_status(download_id, status, error=None, percent=None):
    with _jobs_lock:
        job = torrent_downloads.get(download_id)
        # Unlike models.usenet (no ledger), torrent jobs are backed by the
        # torrent_jobs table — writing a ledger row, or notifying, for a job
        # nothing tracks in memory would either touch a nonexistent row or
        # send a filename-less notification. Mirrors models.dcpp._set_status.
        if job is None:
            return
        job["status"] = status
        job["error"] = error
        if percent is not None:
            job["percent"] = percent
        filename = job.get("filename")
        current_percent = job["percent"]

    # Retention: the ledger holds unresolved work only. A clean completion is
    # resolved — the file is in WATCH and monitor.py owns it from here. A
    # failure, or a completion whose file could not be found, still needs a
    # human, so its row survives a restart until the user dismisses it.
    if status == "complete":
        _persist_delete(download_id)
    else:
        _persist_update(
            download_id, status=status, error=error, percent=current_percent
        )

    # Outside the lock, as with the persistence calls above. _set_status is
    # only ever called with a terminal status, so this fires exactly once per
    # job.
    _notify_terminal_status(status, filename, error, "Torrent")


def _notify_terminal_status(status, filename, error, source):
    """Thin wrapper over the shared notifier (see core.notifications).

    Imported lazily so this module keeps no import-time dependency on the
    notification stack.
    """
    from core.notifications import notify_download_terminal

    notify_download_terminal(status, filename, error=error, source=source)


def _import_completed(storage_path, filename) -> bool:
    """Move completed comic file(s) into WATCH.

    Delegates to the Usenet mover — landing a finished file in WATCH is
    protocol-agnostic, and that implementation already normalizes permissions
    via ``match_parent_permissions``. Keeping one copy means Torrent, DC++ and
    Usenet can never drift on how files enter the pipeline.
    """
    from models.usenet import _import_completed as _shared_import

    return _shared_import(storage_path, filename, source="Torrent")
