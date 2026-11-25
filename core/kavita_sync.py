"""Import reading progress from Kavita, one CLU user at a time.

One-way: Kavita is read, never written. Each CLU user links their own Kavita
auth key (``core/kavita_store.py``), because Kavita's API reports progress only
for the user a key belongs to.

**Kavita only ever adds.** A read CLU already has is left as it is, a position
is moved only forward (or when Kavita's is the more recent of the two), and
time spent is carried over. A read imported once and then un-marked in CLU is
not imported again; the import log is what remembers that.

The body lives here rather than in app.py so it can be tested: app.py's
``scheduled_kavita_sync`` is a wrapper.
"""
import threading
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone

import requests

from core import kavita_store
from core.app_logging import app_logger
from core.external_library_paths import build_mappings, resolve_external_path
from models.kavita import (
    KavitaAuthError,
    KavitaClient,
    extract_chapter_info,
    iter_chapters,
    normalize_timestamp,
)

# How many unmatched chapters a run names at INFO; the rest are DEBUG.
UNMATCHED_LOG_LIMIT = 5

# A series is skipped when Kavita says it was last read before the previous
# sync. The margin covers clock differences between the two servers.
INCREMENTAL_MARGIN = timedelta(days=1)

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"

# One run at a time, whoever asks: the schedule, the owner's "Sync all" and
# every user's own "Sync now" write the same tables. A second caller stands
# down; the routes answer 409.
_run_lock = threading.Lock()


def is_running():
    return _run_lock.locked()


def _utc_now():
    return datetime.now(timezone.utc)


def _series_unchanged(series, last_sync):
    """True only on positive evidence that nothing was read since `last_sync`."""
    if not last_sync:
        return False
    latest = normalize_timestamp(series.get("latestReadDate"))
    if not latest:
        return False
    try:
        cutoff = datetime.strptime(str(last_sync)[:19], _TS_FORMAT) - INCREMENTAL_MARGIN
        return datetime.strptime(latest, _TS_FORMAT) < cutoff
    except ValueError:
        return False


def _position_should_move(info, position):
    """Whether Kavita's position replaces the one CLU has."""
    if not position:
        return True
    current = position.get("page_number") or 0
    if info["current_page"] > current:
        return True
    if info["current_page"] == current:
        return False
    # Kavita is behind CLU. That is a re-read only if Kavita's progress is the
    # more recent of the two; otherwise it is simply stale.
    theirs = info.get("read_date")
    ours = str(position.get("updated_at") or "")[:19]
    return bool(theirs and ours and theirs > ours)


def _apply_chapter(user, info, clu_path, synced_reads, counts):
    """Write one matched chapter for one user. Never regresses CLU's state."""
    from core.database import (
        get_reading_position,
        is_issue_read,
        save_reading_position,
    )

    uid = user["id"]
    chapter_id = str(info["chapter_id"])

    if info["completed"]:
        if chapter_id in synced_reads:
            return
        from core.database import is_issue_read

        # Credits are only looked up for a read that is about to be added.
        credits = None if is_issue_read(clu_path, user_id=uid) else kavita_store.credits_for(clu_path)
        added = kavita_store.import_read(
            clu_path, uid, read_at=info["read_date"], page_count=info["page_count"],
            credits=credits,
        )
        if added is None:
            counts["errors"] += 1
            return
        # Logged whether or not a row was added: CLU having the read already
        # settles this chapter just the same.
        kavita_store.log_synced(uid, chapter_id, info["file_path"], clu_path, "read")
        synced_reads.add(chapter_id)
        if added:
            counts["read"] += 1
        return

    if info["pages_read"] <= 0:
        return
    if is_issue_read(clu_path, user_id=uid):
        return
    position = get_reading_position(clu_path, user_id=uid)
    if not _position_should_move(info, position):
        return
    saved = save_reading_position(
        comic_path=clu_path,
        page_number=info["current_page"],
        total_pages=info["page_count"] or None,
        time_spent=(position or {}).get("time_spent") or 0,
        user_id=uid,
    )
    if saved:
        counts["progress"] += 1
    else:
        counts["errors"] += 1


def _sync_user_locked(user_id, server_url, mappings, full=False, client_factory=KavitaClient):
    """One user's run. The caller holds ``_run_lock``."""
    from core.auth import user_can_access_path
    from core.database import clear_stats_cache_prefix, get_user_by_id

    link = kavita_store.get_link(user_id)
    if not link or not link.get("auth_key"):
        return {"status": "not_linked", "user_id": user_id}
    if not link["connection_valid"]:
        return {"status": "key_rejected", "user_id": user_id}
    user = get_user_by_id(user_id)
    if not user:
        return {"status": "not_linked", "user_id": user_id}

    synced_reads = kavita_store.synced_chapter_ids(user_id, "read")
    if synced_reads is None:
        # Not knowing what was imported is not the same as nothing imported.
        kavita_store.record_sync_error(user_id, "Could not read the import log")
        return {"status": "error", "user_id": user_id, "error": "Could not read the import log"}

    started = _utc_now()
    counts = {
        "read": 0, "progress": 0, "errors": 0, "series": 0, "series_skipped": 0,
        "chapters": 0, "matched": 0, "already": 0,
        "no_match": 0, "ambiguous": 0, "no_access": 0, "unusable": 0,
    }
    last_sync = None if full else link.get("last_sync")
    client = client_factory(server_url, link["auth_key"])

    try:
        for series in client.iter_series_with_progress():
            if _series_unchanged(series, last_sync):
                counts["series_skipped"] += 1
                continue
            counts["series"] += 1
            for chapter in iter_chapters(client.get_volumes(series.get("id"))):
                if (chapter.get("pagesRead") or 0) <= 0:
                    continue
                counts["chapters"] += 1
                info = extract_chapter_info(chapter)
                if info is None:
                    counts["unusable"] += 1
                    continue
                clu_path, reason = resolve_external_path(
                    info["file_path"], mappings, file_name=info["file_name"]
                )
                if not clu_path:
                    counts[reason] = counts.get(reason, 0) + 1
                    unmatched = counts["no_match"] + counts["ambiguous"]
                    log = app_logger.info if unmatched <= UNMATCHED_LOG_LIMIT else app_logger.debug
                    log(
                        f"Kavita sync: {reason} for {info['file_path']!r} "
                        f"(user {user['username']})"
                    )
                    continue
                if not user_can_access_path(user, clu_path):
                    counts["no_access"] += 1
                    continue
                counts["matched"] += 1
                _apply_chapter(user, info, clu_path, synced_reads, counts)
    except KavitaAuthError as e:
        kavita_store.set_link_invalid(user_id, str(e))
        app_logger.warning(
            f"Kavita sync: the auth key for {user['username']} was rejected; "
            "this user is skipped until they save a key again"
        )
        return {"status": "key_rejected", "user_id": user_id, "error": str(e), **counts}
    except (requests.RequestException, ValueError) as e:
        # Nothing is stamped: what was written is kept (it is idempotent) and
        # the next run starts from the same last_sync.
        kavita_store.record_sync_error(user_id, str(e))
        app_logger.error(f"Kavita sync failed for {user['username']}: {e}")
        return {"status": "error", "user_id": user_id, "error": str(e), **counts}

    # Matched, and CLU needed nothing from it: read here already, or at least
    # as far along.
    counts["already"] = max(
        0, counts["matched"] - counts["read"] - counts["progress"] - counts["errors"]
    )
    kavita_store.record_sync_result(
        user_id, counts["read"], counts["progress"], started.strftime(_TS_FORMAT),
        detail=counts,
    )
    filled = kavita_store.fill_blank_credits(user_id)
    if counts["read"] or counts["progress"] or filled:
        clear_stats_cache_prefix(f"u{user_id}:")

    elapsed = (_utc_now() - started).total_seconds()
    app_logger.info(
        f"Kavita sync for {user['username']}: {counts['read']} read, "
        f"{counts['progress']} in progress, {counts['already']} already in CLU, "
        f"of {counts['chapters']} issues with progress in {counts['series']} "
        f"series ({counts['series_skipped']} series unchanged); "
        f"{counts['no_match']} not found, {counts['ambiguous']} ambiguous, "
        f"{counts['no_access']} outside this user's folders, "
        f"{counts['unusable']} without a single comic file, "
        f"{counts['errors']} errors ({elapsed:.1f}s)"
    )
    return {"status": "ok", "user_id": user_id, "elapsed": round(elapsed, 2), **counts}


def _run(user_ids, full, app, client_factory):
    """Claim the run, then sync each user. None when a run is already going."""
    if not _run_lock.acquire(blocking=False):
        return None
    try:
        # user_can_access_path and the settings read are safe outside a request,
        # but a caller on a bare thread hands its app over so anything that
        # reads current_app still works.
        with (app.app_context() if app is not None else nullcontext()):
            server_url = kavita_store.get_server_url()
            if not server_url:
                return {"status": "not_configured", "users": []}
            mappings = build_mappings(
                kavita_store.get_library_mappings(), "kavita_path_prefix"
            )
            if user_ids is None:
                user_ids = [
                    link["user_id"]
                    for link in kavita_store.list_links()
                    if link["connection_valid"]
                ]
            results = []
            for user_id in user_ids:
                try:
                    results.append(
                        _sync_user_locked(
                            user_id, server_url, mappings,
                            full=full, client_factory=client_factory,
                        )
                    )
                except Exception as e:
                    # One user's failure must not cost the others their run.
                    app_logger.error(f"Kavita sync failed for user {user_id}: {e}")
                    kavita_store.record_sync_error(user_id, str(e))
                    results.append({"status": "error", "user_id": user_id, "error": str(e)})
            return {"status": "ok", "users": results}
    finally:
        _run_lock.release()


def sync_user(user_id, full=False, app=None, client_factory=KavitaClient):
    """Import one user's progress. Returns None when a run is already going."""
    return _run([user_id], full, app, client_factory)


def sync_all(full=False, app=None, client_factory=KavitaClient):
    """Import every linked user's progress. Returns None when a run is going."""
    return _run(None, full, app, client_factory)
