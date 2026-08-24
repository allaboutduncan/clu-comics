"""Outbound push notifications via Apprise.

CLU has no other outbound channel: ``core.app_state.add_notification`` is an
in-app toast list that is process-local, expires after five minutes, and is lost
on restart. This module is what tells a user something happened when they are
not looking at the browser.

Apprise (https://github.com/caronc/apprise) takes URL-style service definitions
(``discord://...``, ``ntfy://...``, ``mailto://...``) and fans one message out to all
of them, so CLU never implements a per-service integration.

Design rules, all of which exist because a notification must never be able to
break the thing it is reporting on:

* ``import apprise`` is lazy. App import and the test suite must not depend on
  the package being installed, and a broken install degrades to a logged error.
* Every public function swallows its exceptions into ``app_logger``.
* Callers use :func:`notify_async`. The hook sites are a download worker thread
  and two single-threaded pollers that drive *every* tracked job - a hung
  webhook on any of them would stall unrelated downloads.
* Settings are read per call, so config changes take effect without a restart.
"""

import threading

from core.app_logging import app_logger

# Event ids. These are stored in user_preferences, so they are part of the
# on-disk format - renaming one silently resets that event to its default.
EVENT_DOWNLOAD_COMPLETE = "download_complete"
EVENT_DOWNLOAD_FAILED = "download_failed"
EVENT_WANTED_ADDED = "wanted_added"

# Single source of truth for the config UI, payload validation and defaults.
# The /config tab renders itself from this, so the two cannot drift.
EVENT_DEFS = {
    EVENT_DOWNLOAD_COMPLETE: {
        "label": "Download completed",
        "description": "A download finishes successfully.",
        "notify_type": "success",
        "default": True,
    },
    EVENT_DOWNLOAD_FAILED: {
        "label": "Download failed",
        "description": "A download fails after every mirror has been tried. "
                       "Cancelled downloads are never reported.",
        "notify_type": "failure",
        "default": True,
    },
    EVENT_WANTED_ADDED: {
        "label": "Wanted issues added",
        "description": "Missing issues from your wanted list are matched and "
                       "moved into the library. Sent as one digest per sweep.",
        "notify_type": "success",
        "default": True,
    },
}

# Preference keys (category "notifications" in user_preferences).
PREF_ENABLED = "notify_enabled"
PREF_URLS = "notify_urls"
PREF_EVENTS = "notify_events"

# A digest naming every issue in a large catch-up sweep is unreadable on a phone
# lock screen, so the body enumerates this many and then summarises the rest.
MAX_DIGEST_LINES = 20


def parse_urls(raw):
    """Normalise Apprise URLs from a textarea (or an already-parsed list).

    Accepts one URL per line. Blank lines and ``#`` comments are dropped so a
    user can annotate or temporarily disable a target without deleting it.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = raw.splitlines()
    if not isinstance(raw, (list, tuple)):
        return []

    urls = []
    for entry in raw:
        if not isinstance(entry, str):
            continue
        entry = entry.strip()
        if not entry or entry.startswith("#"):
            continue
        urls.append(entry)
    return urls


def sanitize_events(raw):
    """Coerce an events payload to ``{known_event_id: bool}``.

    Unknown ids are dropped and missing ones fall back to their default, so a
    stale client cannot store junk or silently turn off an event it never knew
    about.
    """
    raw = raw if isinstance(raw, dict) else {}
    return {
        event_id: bool(raw.get(event_id, defn["default"]))
        for event_id, defn in EVENT_DEFS.items()
    }


def get_settings():
    """Read the owner-global notification settings.

    Returns ``{'enabled': bool, 'urls': [str], 'events': {id: bool}}``. On any
    read failure this returns a disabled config rather than raising - a database
    hiccup must not propagate into a download worker.
    """
    try:
        from core.database import get_user_preference

        return {
            "enabled": bool(get_user_preference(PREF_ENABLED, default=False)),
            "urls": parse_urls(get_user_preference(PREF_URLS, default=[])),
            "events": sanitize_events(get_user_preference(PREF_EVENTS, default={})),
        }
    except Exception as e:
        app_logger.error(f"Failed to read notification settings: {e}")
        return {"enabled": False, "urls": [], "events": sanitize_events({})}


def is_event_enabled(event, settings=None):
    """True when ``event`` should be delivered right now.

    Requires the master switch on, at least one target URL, and the event
    itself ticked.
    """
    settings = settings if settings is not None else get_settings()
    if not settings.get("enabled") or not settings.get("urls"):
        return False
    return bool(settings.get("events", {}).get(event))


def _send(urls, title, body, notify_type):
    """Deliver one message to ``urls``. Returns ``(ok, error_message)``."""
    try:
        import apprise
    except Exception as e:
        # Not installed, or installed broken. Nothing else in CLU imports
        # apprise, so this is contained to notifications being unavailable.
        msg = f"Apprise is not available: {e}"
        app_logger.error(msg)
        return False, msg

    try:
        client = apprise.Apprise()
        rejected = [url for url in urls if not client.add(url)]
        if rejected:
            # add() rejects a malformed or unknown-scheme URL. Report it, but
            # still deliver to whatever else parsed.
            app_logger.error(
                f"Apprise rejected {len(rejected)} notification URL(s): "
                f"{', '.join(_redact(u) for u in rejected)}"
            )
        if not len(client):
            return False, "No valid Apprise URLs configured"

        ok = client.notify(title=title, body=body, notify_type=notify_type)
        if not ok:
            # notify() returns False when a service refused the message; the
            # detail is on apprise's own logger.
            return False, "Apprise reported a delivery failure"
        return True, None
    except Exception as e:
        app_logger.error(f"Failed to send notification: {e}")
        return False, str(e)


def _redact(url):
    """Strip credentials from an Apprise URL so it is safe to log."""
    try:
        scheme, _, rest = url.partition("://")
        if not rest:
            return url
        host = rest.split("/", 1)[0]
        if "@" in host:
            host = "***@" + host.rsplit("@", 1)[1]
        return f"{scheme}://{host}/..."
    except Exception:
        return "<unparseable url>"


def notify(event, title, body):
    """Send ``event`` synchronously. Returns True only if it was delivered.

    Returns False (without raising) when notifications are off, the event is
    unticked, no URLs are set, or delivery failed.
    """
    try:
        if event not in EVENT_DEFS:
            app_logger.error(f"Unknown notification event: {event}")
            return False

        settings = get_settings()
        if not is_event_enabled(event, settings):
            return False

        ok, error = _send(
            settings["urls"], title, body, EVENT_DEFS[event]["notify_type"]
        )
        if ok:
            app_logger.info(f"Notification sent ({event}): {title}")
        else:
            app_logger.error(f"Notification failed ({event}): {error}")
        return ok
    except Exception as e:
        app_logger.error(f"Notification dispatch failed ({event}): {e}")
        return False


def notify_async(event, title, body):
    """Fire :func:`notify` on a daemon thread and return immediately.

    Use this from every hook site. The callers are a download worker and two
    pollers that advance every tracked job in one loop, so a slow endpoint must
    not be able to hold them up.
    """
    try:
        # Cheap gate on the calling thread: skip spawning a thread at all when
        # notifications are off, which is the common case.
        if not is_event_enabled(event):
            return
        threading.Thread(
            target=notify,
            args=(event, title, body),
            daemon=True,
        ).start()
    except Exception as e:
        app_logger.error(f"Failed to dispatch notification ({event}): {e}")


def notify_download_terminal(status, filename, error=None, source=None):
    """Notify for a terminal download status from a client-backed poller.

    Shared by the Usenet and DC++ pollers (models/usenet.py, models/dcpp.py),
    which each settle jobs through their own ``_set_status``. Call it outside
    their job locks.

    ``complete_no_move`` is a partial success: the client finished but CLU could
    not reach its storage path, so the file still needs moving by hand. Say so
    rather than reporting a clean completion.

    Swallows everything. The Usenet poll loop has no per-round guard, so a raise
    here would kill the poller and strand every tracked job.
    """
    try:
        lines = [filename or "Unknown file"]
        if source:
            lines.append(f"Source: {source}")

        if status in ("complete", "complete_no_move"):
            if status == "complete_no_move":
                lines.append(
                    "Finished at the client, but CLU could not access the "
                    "file - it has not been imported."
                )
            notify_async(
                EVENT_DOWNLOAD_COMPLETE, "Download complete", "\n".join(lines)
            )
        elif status == "failed":
            if error:
                lines.append(f"Error: {error}")
            notify_async(
                EVENT_DOWNLOAD_FAILED, "Download failed", "\n".join(lines)
            )
    except Exception as e:
        app_logger.error(f"Failed to notify for {source or 'download'}: {e}")


def send_test(urls):
    """Send a test message to ``urls``, bypassing the enable/event gates.

    Used by the /config "Send Test Notification" button so an owner can verify a
    target before saving it. Returns ``(ok, error_message)``.
    """
    urls = parse_urls(urls)
    if not urls:
        return False, "No notification URLs provided"
    return _send(
        urls,
        "Comic Library Utilities",
        "Test notification - your CLU notifications are working.",
        "info",
    )


def format_digest(header, lines):
    """Build a digest body, truncating a long list to a readable length."""
    lines = [str(line) for line in (lines or [])]
    shown = lines[:MAX_DIGEST_LINES]
    body = "\n".join(shown)
    remaining = len(lines) - len(shown)
    if remaining > 0:
        body += f"\n...and {remaining} more"
    return f"{header}\n\n{body}" if body else header
