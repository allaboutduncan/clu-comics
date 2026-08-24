"""Small, dependency-light helpers for the download pipeline.

Kept separate from ``api.py`` so the pure logic can be imported and tested
without triggering api.py's import-time side effects (worker threads, DB
connection, cloudscraper). Nothing here imports ``api`` or ``core.database`` at
module scope; the weekly-pack reconciler reaches for both lazily.
"""

import os
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


# ---------------------------------------------------------------------------
# Cooperative cancellation
#
# /cancel_download only sets a flag on the download's progress entry; nothing
# can interrupt a worker thread mid-transfer. Every download loop therefore has
# to poll for that flag and stop itself — between chunks, between retries and
# between failover mirrors — otherwise "Cancelled" is only a label and the file
# keeps downloading into WATCH, where the monitor happily imports it.
# ---------------------------------------------------------------------------

def is_cancel_requested(progress, download_id) -> bool:
    """True when the download has been flagged for cancellation.

    Tolerates a missing/cleared entry (a dismissed download must read as "not
    cancelled" rather than raise inside a tight chunk loop).
    """
    if not isinstance(progress, dict):
        return False
    entry = progress.get(download_id)
    return bool(isinstance(entry, dict) and entry.get('cancelled'))


def cleanup_partial_files(paths, logger=None) -> list:
    """Delete half-written temp files, returning the ones actually removed.

    A cancelled download must leave nothing behind: ``.part`` files are resume
    state for the next attempt, and a ``.crdownload`` left in WATCH is picked up
    once its extension stops being ignored.
    """
    removed = []
    for path in paths or ():
        if not path or not os.path.exists(path):
            continue
        try:
            os.remove(path)
            removed.append(path)
        except Exception as e:
            if logger:
                logger.warning(f"Failed to remove temp file on cancel: {path} — {e}")
    return removed


def mark_cancelled(progress, download_id, temp_paths=(), logger=None) -> None:
    """Settle a download as cancelled and discard its partial output."""
    cleanup_partial_files(temp_paths, logger)
    entry = progress.get(download_id) if isinstance(progress, dict) else None
    if isinstance(entry, dict):
        entry['status'] = 'cancelled'


def set_error_status(progress, download_id, error=None) -> None:
    """Record a failure, unless the user already cancelled this download.

    Aborting a transfer is how a cancel usually surfaces — as a read error,
    exhausted retries, or a truncated body. Without this guard that self-
    inflicted failure overwrites 'cancelled', and the download the user just
    stopped comes back as 'Failed' with a Retry button.
    """
    entry = progress.get(download_id) if isinstance(progress, dict) else None
    if not isinstance(entry, dict):
        return
    if entry.get('cancelled'):
        entry['status'] = 'cancelled'
        return
    entry['status'] = 'error'
    if error is not None:
        entry['error'] = str(error)


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


# ---------------------------------------------------------------------------
# Post-download ownership of WATCH
#
# WATCH has two independent writers: this app's download workers (threads in the
# Gunicorn process) and monitor.py (a separate OS process, started by app.py when
# MONITOR=yes). monitor.py's _processing_lock/_in_flight guard is per-process, so
# neither side can see the other's claim. Left to race, both convert the file and
# both move it, so TARGET gains a "Foo.cbz" *and* a "Foo (1).cbz" -- with loose
# extraction pages alongside them, because the monitor's recursive sweep descends
# into the conversion scratch dir while it is still being filled.
#
# The resolution is the contract DC++ and Usenet already follow: put the finished
# file in WATCH and stop (models/dcpp.py, models/usenet.py). The one wrinkle is
# that the monitor refuses some extensions outright, so "hand it over" is only
# correct for files it will actually claim.
# ---------------------------------------------------------------------------

DEFER_TO_MONITOR = "defer"           # leave in WATCH; monitor renames/converts/moves
CONVERT_IN_PLACE = "convert_only"    # convert only, so the monitor will claim the result
FULL_POST_PROCESS = "full"           # no monitor: convert + move to TARGET + rename here

_CONVERTIBLE_EXTS = ('.cbr', '.rar')


def monitor_enabled(env=None) -> bool:
    """True when monitor.py owns WATCH. Mirrors app.py's MONITOR env check."""
    env = os.environ if env is None else env
    return (env.get("MONITOR", "") or "").strip().lower() == "yes"


def parse_ignored_extensions(value) -> set:
    """IGNORED_EXTENSIONS config string -> {'.ext', ...}.

    Tolerates spacing, casing and a missing leading dot, so a hand-edited
    config.ini can't silently desync this from monitor.py's own parse.
    """
    out = set()
    for part in (value or "").split(","):
        part = part.strip().lower()
        if part:
            out.add(part if part.startswith(".") else "." + part)
    return out


def monitor_claims(file_path, ignored_extensions, auto_unpack=False) -> bool:
    """Whether monitor.py would import *file_path* as-is.

    Mirror of ``DownloadCompleteHandler._handle_file_if_complete``: everything
    except an ignored extension, plus ``.zip`` when AUTO_UNPACK is on. Note that
    ``.rar`` is in the shipped IGNORED_EXTENSIONS default -- which is exactly why
    deferring every file unconditionally would strand RAR downloads in WATCH.
    Kept in lockstep by tests/unit/test_monitor.py::test_monitor_ignores_rar_extension.
    """
    ext = os.path.splitext(file_path or "")[1].lower()
    if not ext:
        return False
    if ext not in parse_ignored_extensions(ignored_extensions):
        return True
    return ext == ".zip" and bool(auto_unpack)


def post_download_action(file_path, *, monitor_running,
                         ignored_extensions, auto_unpack=False) -> str:
    """Decide who finishes a completed download sitting in WATCH."""
    if not file_path:
        return DEFER_TO_MONITOR
    if not monitor_running:
        # Nothing else drains WATCH in this configuration.
        return FULL_POST_PROCESS
    if monitor_claims(file_path, ignored_extensions, auto_unpack):
        return DEFER_TO_MONITOR
    # The monitor will never touch this extension (.rar by default). Convert it
    # into something it does claim, but still leave the rename and the move to it
    # so only one process ever writes into TARGET.
    if os.path.splitext(file_path)[1].lower() in _CONVERTIBLE_EXTS:
        return CONVERT_IN_PLACE
    return DEFER_TO_MONITOR


def watch_drain_timeout_seconds(reconcile_interval_minutes=5) -> int:
    """How long to wait for monitor.py to drain WATCH before giving up.

    Must cover a fully missed watchdog event, where the file waits for the next
    reconciliation sweep: two sweep intervals plus conversion headroom, never
    less than the historical 5 minutes. Capped at an hour so a long sweep
    interval can't leave a poller thread alive per download indefinitely -- this
    only gates an opportunistic wanted-issue check, which is also scheduled.
    """
    try:
        minutes = int(reconcile_interval_minutes)
    except (TypeError, ValueError):
        minutes = 5
    return max(300, min(3600, minutes * 60 * 2 + 120))


def download_notification_body(dest_filename, file_path=None, provider=None,
                               error=None):
    """Build the body of a download-complete/failed notification.

    Lives here rather than in api.py so it can be tested without triggering
    api.py's import-time side effects (worker threads, cloudscraper, DB).

    ``dest_filename`` is absent for browser-extension grabs, so fall back to the
    resolved path before giving up and calling it "Unknown file".
    """
    name = dest_filename
    if not name and file_path:
        name = os.path.basename(file_path)
    lines = [name or "Unknown file"]
    if provider:
        lines.append(f"Source: {provider}")
    if error:
        lines.append(f"Error: {error}")
    return "\n".join(lines)
