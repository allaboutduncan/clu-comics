"""Small, dependency-light helpers for the download pipeline.

Kept separate from ``api.py`` so the pure logic can be imported and tested
without triggering api.py's import-time side effects (worker threads, DB
connection, cloudscraper). Nothing here imports ``api`` or ``core.database`` at
module scope; the weekly-pack reconciler reaches for both lazily.
"""

import sys

# Live api.download_progress status -> weekly_packs_history status. 'queued'
# maps to itself and is filtered out before any write, so a genuinely queued
# download never causes DB churn.
_LIVE_TO_HISTORY_STATUS = {
    'queued': 'queued',
    'in_progress': 'downloading',
    'complete': 'completed',
    'error': 'failed',
    'cancelled': 'cancelled',
}


def issue_number_to_int(issue_num) -> int | None:
    """Best-effort parse of an issue-number string to a whole number.

    Used by the auto-download scheduler to test whether an issue falls inside a
    already-downloaded range pack. Returns ``None`` for values that aren't a
    whole number so callers skip the numeric comparison instead of raising:

    - ``"0"`` / ``"00"`` -> ``0`` (leading zeros stripped, but a bare "0" that
      strips to ``''`` must NOT blow up ``int('')``)
    - ``""`` / ``None`` -> ``None``
    - ``"1.MU"`` / ``"½"`` / ``"Annual"`` -> ``None`` (not a whole number)
    - ``"007"`` -> ``7``
    """
    s = str(issue_num).strip()
    if not s:
        return None
    try:
        return int(s.lstrip('0') or '0')
    except (ValueError, TypeError):
        return None


def is_cloudflare_challenge(response) -> bool:
    """Detect a Cloudflare managed / JS "Just a moment..." challenge response.

    These challenges cannot be solved by any automated HTTP client (requests,
    cloudscraper, curl_cffi) or even headless/scripted browsers — only a real,
    manually-driven browser passes them. When we see one there is no point
    retrying; the caller surfaces a clear "download manually" error instead.

    Accepts anything with ``.headers`` (mapping) and ``.content`` (bytes),
    e.g. a ``requests.Response``.
    """
    try:
        headers = response.headers
        # Most reliable signal: Cloudflare stamps this on challenge responses.
        if 'challenge' in headers.get('cf-mitigated', '').lower():
            return True
        if 'cloudflare' not in headers.get('Server', '').lower():
            return False
        if 'text/html' not in headers.get('Content-Type', '').lower():
            return False
        # Fall back to sniffing the (small) challenge page body for markers.
        snippet = response.content[:4096].lower()
        return (b'just a moment' in snippet
                or b'__cf_chl' in snippet
                or b'challenge-platform' in snippet)
    except Exception:
        return False


def _live_download_progress():
    """Return ``api.download_progress`` if api is already imported, else None.

    Deliberately reads ``sys.modules`` instead of importing: the monitor process
    (and any test that hasn't loaded api) must get a safe ``None`` rather than
    trigger api.py's import-time worker threads. Also rejects non-dict values so
    a ``MagicMock`` stand-in can't produce nondeterministic lookups.
    """
    mod = sys.modules.get('api')
    progress = getattr(mod, 'download_progress', None) if mod is not None else None
    return progress if isinstance(progress, dict) else None


def apply_live_weekly_pack_status(history, progress=None):
    """Overlay live download state onto weekly-pack history rows, in place.

    Reconciliation heals the stored status, but only on rows old enough to have
    been swept; this makes the UI agree with the Status page immediately, and
    adds a ``progress`` percentage for downloads that are actually running.

    Args:
        history: rows from ``get_weekly_packs_history()`` (each with download_id)
        progress: live download-progress mapping; defaults to the live dict.

    Returns the same list.
    """
    if progress is None:
        progress = _live_download_progress() or {}

    for item in history:
        live = progress.get(item.get('download_id'))
        if not isinstance(live, dict):
            continue
        item['status'] = _LIVE_TO_HISTORY_STATUS.get(live.get('status'), item.get('status'))
        if item['status'] == 'downloading':
            item['progress'] = int(live.get('progress') or 0)

    return history


def reconcile_weekly_pack_history(progress=None, stale_after_seconds=900):
    """Sync stuck weekly-pack history rows against live download progress.

    ``weekly_packs_history.status`` is only a mirror — the authoritative state
    lives in the in-memory ``api.download_progress``. Restarts, cancellations,
    cleared entries and crashed workers all leave rows frozen at 'queued' or
    'downloading' forever, which also blocks re-download because
    ``is_weekly_pack_downloaded()`` counts those as done. This resolves each
    frozen row against live state, or marks it 'interrupted' when the download
    it referred to no longer exists.

    Args:
        progress: live download-progress mapping. Defaults to
            ``api.download_progress`` when api is already loaded; when neither is
            available this is a no-op (zero reads of live state, zero writes).
        stale_after_seconds: grace period before a row with no ``download_id``
            is treated as orphaned. api.py's status writes carry no id, so a
            young id-less row may belong to a download that is actually running.
            Callers that know the progress dict is empty (app startup) pass 0.

    Returns:
        Number of rows whose status changed. Performs no writes at rest.

    Assumes a single web worker (``gunicorn -w 1``): the progress dict lives in
    one process. Under multiple workers this would need a DB-backed progress
    store instead.
    """
    from core.app_logging import app_logger
    from core.database import get_stale_weekly_pack_rows, update_weekly_pack_status

    rows = get_stale_weekly_pack_rows(stale_after_seconds)
    if not rows:
        return 0

    if progress is None:
        progress = _live_download_progress()
    if progress is None:
        return 0

    changed = 0
    for row in rows:
        download_id = row.get('download_id')
        live = progress.get(download_id) if download_id else None
        if not isinstance(live, dict):
            live = None

        if live is not None:
            new_status = _LIVE_TO_HISTORY_STATUS.get(live.get('status'))
            if not new_status or new_status == row.get('status'):
                # Already agrees with live state, still queued, or a status we
                # don't model — leave it. Keeps the steady state write-free.
                continue
        elif download_id or row.get('is_stale'):
            # The download this row pointed at is gone (restart, cleared,
            # dismissed), or it predates download_id tracking and has aged out.
            new_status = 'interrupted'
        else:
            # Young row with no id: may belong to an in-flight download whose
            # only writer is api.py (which never passes an id). Leave it alone.
            continue

        if update_weekly_pack_status(
            row['pack_date'], row['publisher'], row['format'], new_status,
            touch_timestamp=False,
        ):
            changed += 1
            app_logger.info(
                f"Reconciled weekly pack {row['pack_date']} {row['publisher']} "
                f"{row['format']}: -> {new_status}"
            )

    return changed
