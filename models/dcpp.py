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

# Cadence while jobs are active, so cached progress stays fresh without
# hammering AirDC++.
_ACTIVE_POLL_INTERVAL = 5
# Heartbeat cadence with no tracked jobs pending. The poller stays alive while
# DC++ is configured so the status page can show AirDC++'s real queue — one
# request every half minute is a fair price for that.
_IDLE_POLL_INTERVAL = 30

# How long a manual search result stays grabbable. AirDC++ reports
# expires_in = 1800000ms (30 min) for a search instance, so this sits well
# inside the window while still bounding how long CLU holds the pairing.
_TOKEN_TTL = 900

# A result page this full means the hub truncated the list, so a narrower
# follow-up search is worth its cost. Mirrors the client's result page size.
_TRUNCATED_RESULT_COUNT = 250

# In-memory tracking of queued DC++ bundles, keyed by our download_id.
# Mirrors models.usenet.usenet_downloads so the status page renders both with
# the same code.
dcpp_downloads: dict = {}
_jobs_lock = threading.Lock()
_poller_thread = None
_poller_lock = threading.Lock()

# Last bundle listing seen by the poller, as {client_id: DownloadStatus}. Lets
# get_dcpp_downloads() surface bundles queued directly in AirDC++ without
# putting a 10s-timeout HTTP call behind a 1s status-page poll.
_bundle_snapshot: dict = {}

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

    # One search on the series name, not a query per zero-padding variant.
    #
    # Usenet can afford `build_queries`' 2-/3-digit variants because a Newznab
    # call is a fast HTTP request. A hub search is not: AirDC++ paces searches
    # to respect hub flood limits, so each one costs tens of seconds, and three
    # per issue would both crawl and hammer the hub. Searching the series alone
    # returns every issue of it in one go — "83", "083" and "#83" all included
    # — and the scorer below picks the right one, which is more robust than
    # guessing the padding anyway.
    queries = [series_name.strip()]

    raw_results = []
    errors = []
    seen = set()
    tried = set()
    while queries:
        q = queries.pop(0)
        if not q or q in tried:
            continue
        tried.add(q)
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

        # A full page means the hub had more to say than it could return. Only
        # then is a second, narrower search worth its cost — a prolific series
        # can bury the wanted issue past the page limit.
        if len(results) >= _TRUNCATED_RESULT_COUNT and str(issue_num or "").isdigit():
            queries.append(f"{series_name.strip()} {str(int(issue_num)).zfill(3)}")

        for r in results:
            # De-dupe on TTH (the content hash) so the same file offered by
            # several users, or matched by several query variants, appears once.
            key = r.get("tth") or f"{instance_id}:{r['result_id']}"
            if key in seen:
                continue
            seen.add(key)
            raw_results.append((instance_id, r))

    app_logger.info(
        f"DC++ search {series_name} #{issue_num}: tried {sorted(tried)} -> "
        f"{len(raw_results)} unique result(s)"
        + (f"; errors: {errors}" if errors else "")
    )

    best_accept = None
    best_fallback = None
    single_found = False
    scored = []

    for instance_id, r in raw_results:
        title = r.get("name") or ""
        scored_title, file_volume = _normalize_hub_title(title, series_name)

        # Normalizing drops the vN token, so the scorer can no longer catch a
        # volume mismatch — do it here instead, before anything else.
        if (series_volume and file_volume
                and int(file_volume) != int(series_volume)):
            scored.append({
                "title": title,
                "result_token": _store_token(instance_id, r),
                "size": r.get("size"),
                "users": r.get("users"),
                "tth": r.get("tth"),
                "score": 0,
                "decision": "REJECT",
            })
            continue

        score, is_range, series_match, issue_matched = score_getcomics_result(
            scored_title, series_name, issue_num, issue_year,
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


def _normalize_hub_title(name, series_name):
    """Reshape a DC++ hub filename into the form the scorer understands.

    Hub back-catalogues are commonly named ``196103 Strange Tales v1 083.cbz``
    — a ``YYYYMM`` date prefix and a ``vN`` volume token. The shared scorer
    (``models.getcomics``) expects a scene-style ``Series 083 (1961)``: with
    the date in front the series no longer starts the title and matching fails
    outright, and ``v1 083`` defeats issue extraction. Neither shape ever
    reaches GetComics or Usenet, so the fix lives here rather than in the
    shared scorer.

    Returns ``(title, volume)`` where ``volume`` is the ``vN`` the filename
    declared (or None). The caller uses it to reject a wrong volume, since
    stripping the token removes the scorer's own chance to notice.

    Anything that doesn't match the pattern is returned untouched.
    """
    import re

    base = str(name or "").strip()
    series = str(series_name or "").strip()
    if not base or not series:
        return base, None

    # Drop a container extension — the scorer scores a bare title higher.
    base = re.sub(r"\.(cbz|cbr|cbt|pdf|zip|rar)$", "", base, flags=re.IGNORECASE)

    year = None
    m = re.match(r"^(\d{2,6})\s+(.*)$", base)
    # Only strip a leading number when the series name follows it. Otherwise a
    # series that legitimately opens with digits ("100 Bullets 001") would be
    # decapitated into "Bullets 001".
    if m and m.group(2).lower().startswith(series.lower()):
        prefix, remainder = m.group(1), m.group(2)
        if len(prefix) == 6 and 1900 <= int(prefix[:4]) <= 2100 \
                and 1 <= int(prefix[4:]) <= 12:
            year = prefix[:4]          # YYYYMM cover date
        elif len(prefix) == 4 and 1900 <= int(prefix) <= 2100:
            year = prefix              # YYYY
        # A short run (e.g. "07 ") is a sequence number — drop it, no year.
        base = remainder

    # "v1 083" -> "083": the volume token sits between series and issue and
    # stops the issue being read. Captured so the caller can still compare it.
    volume = None
    vm = re.search(r"\bv(\d{1,3})\b(?=\s*\d)", base)
    if vm:
        volume = int(vm.group(1))
        base = (base[:vm.start()] + base[vm.end():]).replace("  ", " ").strip()

    if year and not re.search(r"\((?:19|20)\d{2}\)", base):
        base = f"{base} ({year})"

    return base, volume


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


def grab_dcpp(result_token, filename, series=None, issue=None, errors=None):
    """Queue a previously-searched DC++ result and start tracking it.

    Returns the tracking ``download_id`` on success, or None on failure.

    ``errors`` is an optional list the reason for a failure is appended to.
    Some failures are user-fixable configuration (a Target Directory the
    AirDC++ host can't parse) and reading "something went wrong" in a toast
    while the real explanation sits in the log helps nobody.
    """
    def _fail(reason, level="error"):
        getattr(app_logger, level)(f"DC++ grab failed: {reason}")
        if errors is not None:
            errors.append(reason)
        return None

    entry = _resolve_token(result_token)
    if not entry:
        return _fail(
            "This search result has expired — re-run the search and try again",
            level="warning",
        )

    try:
        client = _active_client()
    except Exception as e:
        return _fail(f"Could not build the DC++ client: {e}")
    if client is None:
        return _fail("No active DC++ client is configured", level="warning")

    result = client.download_result(entry["instance_id"], entry["result_id"])
    if not result.success:
        return _fail(result.error or "The DC++ client rejected the download")

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
        "bytes_total": entry.get("size"),
        "bytes_downloaded": None,
        # Filled in by the poller from the bundle's target path.
        "target": None,
    }
    with _jobs_lock:
        dcpp_downloads[download_id] = job

    # Persist before starting the poller, for the same reason the weekly-pack
    # history row is written before the queue put: the bundle is already live
    # inside AirDC++, and a crash between here and the first poll must not lose
    # our only record of it.
    _persist_new(download_id, job)

    app_logger.info(
        f"Queued DC++ download for {filename} (bundle={result.client_id})"
    )
    _ensure_poller()
    return download_id


def get_dcpp_downloads() -> list:
    """Return tracked DC++ downloads, plus AirDC++'s own untracked bundles.

    Untracked entries are bundles queued directly in AirDC++ that CLU never
    submitted. They are shown so the panel reflects the client's real queue,
    and are flagged ``untracked`` so the UI can render them read-only: CLU has
    no idea what series or issue they are, and they may have nothing to do with
    comics, so it must never move their files.
    """
    with _jobs_lock:
        tracked = [
            dict(download_id=k, untracked=False, **v)
            for k, v in dcpp_downloads.items()
        ]
        known = {str(v.get("client_id")) for v in dcpp_downloads.values()}
        untracked = [
            dict(v) for cid, v in _bundle_snapshot.items() if cid not in known
        ]
    return tracked + untracked


# ---------------------------------------------------------------------------
# Persistence — the dcpp_jobs ledger
# ---------------------------------------------------------------------------
#
# The ledger exists purely so a CLU restart is a non-event. AirDC++ is a
# separate process and keeps downloading regardless, but the bundle id and the
# last-known target path used to live only in ``dcpp_downloads`` — so a restart
# orphaned the job and the finished file was never imported into WATCH.
#
# Every write is best-effort: losing a ledger row is bad, but failing a live
# download because SQLite hiccuped would be worse.

def _persist_new(download_id, job):
    try:
        from core.database import save_dcpp_job

        save_dcpp_job(download_id, job)
    except Exception as e:
        app_logger.error(f"Could not persist DC++ job {download_id}: {e}")


def _persist_update(download_id, **fields):
    try:
        from core.database import update_dcpp_job

        update_dcpp_job(download_id, **fields)
    except Exception as e:
        app_logger.error(f"Could not update DC++ job {download_id}: {e}")


def _persist_delete(download_id) -> bool:
    """Delete a ledger row. Returns True if one was actually removed."""
    try:
        from core.database import delete_dcpp_job

        return delete_dcpp_job(download_id)
    except Exception as e:
        app_logger.error(f"Could not delete DC++ job {download_id}: {e}")
        return False


def recover_dcpp_jobs() -> int:
    """Re-adopt DC++ bundles left in flight by a previous CLU process.

    Deliberately DB-only. This runs from ``start_background_services()``, which
    executes at import time under Gunicorn, and a per-bundle HTTP call with a
    10s timeout would stall boot.

    The network reconcile happens on the poller's first round instead, where
    :func:`_poll_one` already handles every case — including a bundle AirDC++
    finished and dropped from its queue while CLU was down, which it sees as
    "gone" and imports using the ``target`` path restored here. That path is the
    whole reason it is persisted: once the bundle is gone there is nothing left
    to ask where the file landed.

    Returns the number of jobs re-adopted.
    """
    try:
        from core.database import get_active_dcpp_jobs

        rows = get_active_dcpp_jobs()
    except Exception as e:
        app_logger.error(f"DC++ recovery could not read the job ledger: {e}")
        return 0

    recovered = 0
    with _jobs_lock:
        for row in rows:
            download_id = row.get("download_id")
            if not download_id or download_id in dcpp_downloads:
                continue
            dcpp_downloads[download_id] = {
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
        app_logger.info(f"Recovered {recovered} DC++ download(s) from the ledger")
    if recovered or dcpp_enabled_and_configured():
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
            target=_poll_loop, name="dcpp-poller", daemon=True
        )
        _poller_thread.start()


def _pending_jobs():
    with _jobs_lock:
        return {k: dict(v) for k, v in dcpp_downloads.items()
                if v["status"] == "downloading"}


def _poll_loop():
    """Poll AirDC++ for as long as DC++ is configured.

    This used to exit after two idle rounds, which meant the poller existed
    only between a grab and the last completion. It now runs continuously at a
    slow heartbeat when nothing is pending, for two reasons: the idle rounds
    refresh the bundle snapshot backing the status page's view of AirDC++'s own
    queue, and a job recovered from the ledger no longer has to wait for a new
    grab before anything looks at it.
    """
    while True:
        pending = {}
        client = None
        try:
            pending = _pending_jobs()
            try:
                client = _active_client()
            except Exception as e:
                app_logger.error(f"DC++ poll: could not build client: {e}")
                client = None

            if client is None:
                _set_bundle_snapshot({})
                # Nothing to poll with. Exit only if DC++ is gone entirely —
                # a client that merely failed to build is worth retrying, but
                # a deconfigured one never resolves, and holding the thread on
                # a pending job would keep it alive for the process's life.
                # grab_dcpp() and recover_dcpp_jobs() both restart it.
                if not dcpp_enabled_and_configured():
                    return
            else:
                _refresh_bundle_snapshot(client)
                for download_id, job in pending.items():
                    _poll_one(client, download_id, job)
        except Exception as e:
            # A bad round must never kill the thread: nothing restarts it, and
            # every tracked bundle would silently stall for the process's life.
            app_logger.error(f"DC++ poll round failed: {e}")

        # Only hurry when there is something to hurry for. With no client, a
        # job stuck pending would otherwise spin two DB reads every 5s forever.
        hurry = pending and client is not None
        time.sleep(_ACTIVE_POLL_INTERVAL if hurry else _IDLE_POLL_INTERVAL)


def _set_bundle_snapshot(snapshot: dict):
    global _bundle_snapshot
    with _jobs_lock:
        _bundle_snapshot = snapshot


def _refresh_bundle_snapshot(client):
    """Cache AirDC++'s live queue as display-ready untracked-job dicts.

    Built here, where the client type is known, so serving it costs
    ``/api/dcpp/downloads`` nothing — the status page polls that endpoint every
    second and cannot afford a 10s-timeout HTTP call per request.
    """
    try:
        queue = client.get_queue()
    except Exception as e:
        app_logger.error(f"DC++ queue listing failed: {e}")
        return

    client_type = client.client_type.value
    _set_bundle_snapshot({
        str(s.client_id): {
            "download_id": None,
            "untracked": True,
            "client_type": client_type,
            "client_id": str(s.client_id),
            "filename": s.name or str(s.client_id),
            "status": "downloading",
            "error": None,
            "series": None,
            "issue": None,
            "percent": s.percent,
            "stage": s.stage,
            "bytes_total": s.bytes_total,
            "bytes_downloaded": s.bytes_downloaded,
            "target": None,
        }
        for s in queue if s.client_id
    })


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
        imported = _import_completed(
            _to_local_path(client, job.get("target")), job["filename"]
        )
        _set_status(download_id, "complete" if imported else "complete_no_move",
                    percent=100)
        return

    if status.status == "complete":
        imported = _import_completed(
            _to_local_path(client, status.storage_path), job["filename"]
        )
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
        # Only write to the ledger when something a restart would need has
        # actually moved. This runs every few seconds per bundle, and an
        # unconditional write would be pure churn.
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
        job = dcpp_downloads.get(download_id)
        if job is None:
            return
        job["status"] = status
        job["error"] = error
        if percent is not None:
            job["percent"] = percent
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


def dismiss_dcpp_job(download_id: str) -> bool:
    """Drop a resolved-but-unimported DC++ job from the ledger and memory.

    Returns False if the job isn't tracked, so the route can 404.
    """
    with _jobs_lock:
        known = download_id in dcpp_downloads
        dcpp_downloads.pop(download_id, None)
    deleted = _persist_delete(download_id)
    return known or deleted


def translate_remote_path(path, remote_root, local_root):
    """Rewrite a path AirDC++ reported into CLU's own view of it.

    AirDC++ is usually not in CLU's filesystem namespace, and in more than one
    way:

    * a native Windows install reports ``F:\\downloads\\temp\\x.cbz`` — not a
      path a Linux container can open, and not even the same separator;
    * an AirDC++ *container* reports its own mount point (``/downloads/x.cbz``)
      which CLU may mount elsewhere (``/data/downloads/x.cbz``).

    Both are the same problem: a shared folder with two names. When the client
    config records both views (``target_directory`` and
    ``local_target_directory``), swap the prefix and convert separators.
    Anything that doesn't sit under the remote root — or a client with no
    local path configured, which is the correct setup when both sides agree —
    is returned untouched, leaving the existing monitored-folder fallback in
    ``_import_completed`` to handle it.
    """
    if not path or not remote_root or not local_root:
        return path

    remote_sep = "\\" if "\\" in remote_root else "/"
    local_sep = "\\" if "\\" in local_root else "/"

    r_root = str(remote_root).rstrip("/\\")
    if not r_root:
        return path

    # Windows paths compare case-insensitively; POSIX ones must not.
    if remote_sep == "\\":
        matched = path.lower().startswith(r_root.lower())
    else:
        matched = path.startswith(r_root)
    if not matched:
        return path

    remainder = path[len(r_root):].lstrip("/\\")
    l_root = str(local_root).rstrip("/\\")
    if not remainder:
        return l_root or local_sep
    remainder = remainder.replace(remote_sep, local_sep)
    return (l_root + local_sep + remainder) if l_root else (local_sep + remainder)


def _to_local_path(client, path):
    """Apply :func:`translate_remote_path` using the active client's config."""
    cfg = getattr(client, "config", None)
    return translate_remote_path(
        path,
        getattr(cfg, "target_directory", None),
        getattr(cfg, "local_target_directory", None),
    )


def _import_completed(storage_path, filename) -> bool:
    """Move completed comic file(s) into WATCH.

    Delegates to the Usenet mover — landing a finished file in WATCH is
    protocol-agnostic, and that implementation already normalizes permissions
    via ``match_parent_permissions``. Keeping one copy means DC++ and Usenet
    can never drift on how files enter the pipeline.
    """
    from models.usenet import _import_completed as _shared_import

    return _shared_import(storage_path, filename, source="DC++")
