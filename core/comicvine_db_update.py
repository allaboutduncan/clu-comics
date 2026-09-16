"""Keep the local ComicVine SQLite dump current.

The ``comicvine_sqlite`` provider reads a user-supplied SQLite export from a
path typed into Settings. Until now that file was entirely the user's problem:
find it, download it, remember to refresh it. A stale dump means missing issues
and missing credits, and nothing in the UI said how old it was.

A public mirror republishes that database alongside a 69-byte ``checksum`` file
in ``md5sum`` format, holding the MD5 of the **extracted** ``.db``. That gives
the probe/apply split ``core.reading_list_sync`` already uses::

    probe()      -- one 69-byte GET that yields a change token
    run_update() -- the ~541 MB download, run only when the token moved

and the same value doubles as the integrity check on the unpacked file, so
nothing else has to be invented to verify the download.

**The URL never reaches the UI.** It is a module constant here; the settings
page says "every 2 weeks" and nothing more.

Opt-in (``comicvine_sqlite_auto_update``, default off). A 541 MB download and a
multi-gigabyte disk write must not start unannounced on upgrade.
"""
import hashlib
import os
import re
import shutil
import sqlite3
import threading
import time
import zipfile
from datetime import datetime, timedelta, timezone

import requests

from core.app_logging import app_logger

# Not rendered anywhere. The settings page describes the cadence, not the host.
_BASE_URL = "https://localcv.nerdfirehurricane.com/"
_ZIP_URL = _BASE_URL + "localcv.zip"
_CHECKSUM_URL = _BASE_URL + "checksum"

UPDATE_INTERVAL_DAYS = 14

# Headroom. The real unpacked size is unknowable until the zip is here, so the
# pre-flight check is deliberately rough and the exact one runs after the
# download, off the archive's own central directory.
_ZIP_EXPANSION_ESTIMATE = 5
_FREE_SPACE_FACTOR = 1.15

# Preference keys. All global (``user_preferences``), category "metadata".
# NOT ``provider_credentials``: that store is AES-encrypted and exists for
# secrets, not for state the settings page renders on every load.
PREF_AUTO_UPDATE = "comicvine_sqlite_auto_update"
PREF_DB_TOKEN = "comicvine_sqlite_db_token"
PREF_LAST_CHECK = "comicvine_sqlite_last_check"
PREF_LAST_UPDATE = "comicvine_sqlite_last_update"
PREF_LAST_ERROR = "comicvine_sqlite_last_error"

# One run at a time. The scheduled sweep and the "Download now" button both
# drive this, and the button can be clicked twice; they walk the same
# destination directory. Whoever is second stands down -- the work is
# idempotent, so standing down costs nothing.
_run_lock = threading.Lock()

_MD5_RE = re.compile(r"\b([0-9a-fA-F]{32})\b")

_DOWNLOAD_CHUNK_LARGE = 4 * 1024 * 1024
_DOWNLOAD_CHUNK_MEDIUM = 1024 * 1024
_DOWNLOAD_CHUNK_SMALL = 256 * 1024
_WRITE_BUFFER = 8 * 1024 * 1024

# The operations registry flips an op with no update for STALE_TIMEOUT (300s)
# to "error". A 541 MB download on a slow line will exceed that between silent
# steps, so progress is reported on a timer, not only at stage boundaries.
_PROGRESS_INTERVAL = 1.0


class UpdateError(Exception):
    """A failure carrying a message fit to show the user."""


# ---------------------------------------------------------------------------
# Preferences and status
# ---------------------------------------------------------------------------

def _get_pref(key, default=None):
    try:
        from core.database import get_user_preference

        return get_user_preference(key, default=default)
    except Exception:
        return default


def _set_pref(key, value):
    try:
        from core.database import set_user_preference

        set_user_preference(key, value, category="metadata")
    except Exception as e:
        app_logger.warning(
            f"Could not save ComicVine DB update preference {key}: {e}"
        )


def is_auto_update_enabled():
    """True only when the user has explicitly opted in. Default OFF."""
    return _get_pref(PREF_AUTO_UPDATE, default=False) is True


def set_auto_update_enabled(enabled):
    _set_pref(PREF_AUTO_UPDATE, bool(enabled))
    return bool(enabled)


def get_database_path():
    """The configured local ComicVine database path, or None."""
    try:
        from models import comicvine_sqlite as cv_sqlite

        params = cv_sqlite.get_connection_params()
        return (params or {}).get("database_path") or None
    except Exception:
        return None


def is_running():
    return _run_lock.locked()


def get_status():
    """Everything the settings card renders. Never carries a URL."""
    path = get_database_path()
    size = None
    if path:
        try:
            size = os.path.getsize(path)
        except OSError:
            size = None
    return {
        "enabled": is_auto_update_enabled(),
        "path_configured": bool(path),
        "database_present": size is not None,
        "database_size": size,
        "database_size_human": _human(size) if size is not None else None,
        "last_check": _get_pref(PREF_LAST_CHECK),
        "last_update": _get_pref(PREF_LAST_UPDATE),
        "last_error": _get_pref(PREF_LAST_ERROR),
        "running": is_running(),
        "interval_days": UPDATE_INTERVAL_DAYS,
    }


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------

def _parse_checksum(text):
    """Pull the MD5 out of ``md5sum`` output. None when there isn't one."""
    if not text:
        return None
    match = _MD5_RE.search(text)
    return match.group(1).lower() if match else None


def probe():
    """One 69-byte request. Returns ``(token, error)``.

    Returns ``(None, message)`` on any failure and **never** an empty token for
    a reachable-but-unreadable response. Collapsing "I could not ask" into
    "nothing changed" would make every outage look like proof the database is
    current, and the sweep would skip it forever -- the same contract as
    ``models.metron.list_reading_lists_modified_since``.
    """
    try:
        resp = requests.get(_CHECKSUM_URL, timeout=(10, 30))
        resp.raise_for_status()
        text = resp.text
    except Exception as e:
        return None, f"Could not reach the update server: {e}"

    token = _parse_checksum(text)
    if not token:
        return None, "The update server returned an unreadable checksum."
    return token, None


def needs_update(remote_token, force=False):
    """True when ``remote_token`` differs from what is installed.

    Also true when the file itself has gone: a matching token over a missing
    database would leave the provider permanently broken with nothing to
    dislodge it.
    """
    if force or not remote_token:
        return True
    path = get_database_path()
    if not path or not os.path.exists(path):
        return True
    return remote_token != _get_pref(PREF_DB_TOKEN)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _human(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(stamp):
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when


def _interval_elapsed():
    """True when the 2-week window since the last *probe* has passed."""
    when = _parse_iso(_get_pref(PREF_LAST_CHECK))
    if when is None:
        return True
    return datetime.now(timezone.utc) - when >= timedelta(days=UPDATE_INTERVAL_DAYS)


def _require_space(directory, needed, what):
    try:
        free = shutil.disk_usage(directory).free
    except OSError:
        return  # Cannot tell; let the write fail with a real error instead.
    if needed and free < needed:
        raise UpdateError(
            f"Not enough free space: {what} needs about {_human(needed)} "
            f"and only {_human(free)} is available in {directory}."
        )


def _discard(path):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            app_logger.warning(f"Could not remove staged file {path}: {e}")


def _clear_sidecars(db_path):
    """Drop a stale ``-wal``/``-shm`` left beside the old database.

    CLU opens this file with ``mode=ro`` and never creates sidecars, so
    anything here came with the user's own copy. A ``-wal`` belonging to the
    previous database, sitting beside a freshly installed one, is corruption.
    """
    for suffix in ("-wal", "-shm"):
        side = db_path + suffix
        if os.path.exists(side):
            try:
                os.remove(side)
                app_logger.info(
                    f"Removed stale sidecar {os.path.basename(side)}"
                )
            except OSError as e:
                app_logger.warning(f"Could not remove stale sidecar {side}: {e}")


def _noop_progress(detail, current=None, total=None):
    pass


# ---------------------------------------------------------------------------
# Download, extract, verify
# ---------------------------------------------------------------------------

def _download_zip(dest, say):
    """Stream the archive to ``dest``. Returns the byte count."""
    with requests.get(_ZIP_URL, stream=True, timeout=(30, 300)) as resp:
        # A 403/404 is an answer, not a hiccup -- retrying only burns time.
        if resp.status_code in (403, 404):
            raise UpdateError(
                f"The update server refused the download (HTTP {resp.status_code})."
            )
        resp.raise_for_status()

        content_type = (resp.headers.get("content-type") or "").lower()
        if content_type.startswith("text/html"):
            raise UpdateError(
                "The update server returned a web page instead of the database."
            )

        try:
            total = int(resp.headers.get("content-length") or 0)
        except (TypeError, ValueError):
            total = 0

        if total:
            _require_space(
                os.path.dirname(dest),
                total * (1 + _ZIP_EXPANSION_ESTIMATE),
                "downloading and unpacking the update",
            )
            chunk = _DOWNLOAD_CHUNK_LARGE if total > 1024 ** 3 else (
                _DOWNLOAD_CHUNK_MEDIUM if total > 100 * 1024 ** 2
                else _DOWNLOAD_CHUNK_SMALL
            )
        else:
            chunk = _DOWNLOAD_CHUNK_MEDIUM

        done = 0
        last_report = 0.0
        with open(dest, "wb", buffering=_WRITE_BUFFER) as fh:
            for block in resp.iter_content(chunk_size=chunk):
                if not block:
                    continue
                fh.write(block)
                done += len(block)
                now = time.monotonic()
                if now - last_report >= _PROGRESS_INTERVAL:
                    last_report = now
                    suffix = f" of {_human(total)}" if total else ""
                    say(
                        f"downloading {_human(done)}{suffix}",
                        current=done,
                        total=total or None,
                    )

    if total and done < total:
        raise UpdateError(
            f"The download stopped early ({_human(done)} of {_human(total)})."
        )
    return done


def _extract_db(zip_path, dest, say):
    """Unpack the single ``.db`` member to ``dest``, returning its MD5.

    Hashed while it is written: one pass over several gigabytes instead of a
    write followed by a full re-read.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        members = [
            info for info in zf.infolist()
            if not info.is_dir() and info.filename.lower().endswith(".db")
        ]
        if len(members) != 1:
            raise UpdateError(
                "The downloaded archive does not contain exactly one database "
                f"file (found {len(members)})."
            )
        member = members[0]

        # Now the real size is knowable, so the rough pre-flight check can be
        # replaced with an exact one before anything large is written.
        _require_space(
            os.path.dirname(dest),
            member.file_size * _FREE_SPACE_FACTOR,
            "unpacking the update",
        )

        digest = hashlib.md5(usedforsecurity=False)
        done = 0
        last_report = 0.0
        with zf.open(member, "r") as src:
            with open(dest, "wb", buffering=_WRITE_BUFFER) as out:
                while True:
                    block = src.read(_DOWNLOAD_CHUNK_LARGE)
                    if not block:
                        break
                    out.write(block)
                    digest.update(block)
                    done += len(block)
                    now = time.monotonic()
                    if now - last_report >= _PROGRESS_INTERVAL:
                        last_report = now
                        say(
                            f"unpacking {_human(done)} of "
                            f"{_human(member.file_size)}",
                            current=done,
                            total=member.file_size or None,
                        )
    return digest.hexdigest()


def verify_database(path):
    """Cheap structural check on a staged database. Returns ``(ok, message)``.

    Deliberately **not** ``check_integrity()``: ``quick_check`` reads every page
    of a multi-gigabyte file, and a byte-exact MD5 match against the
    publisher's own hash is stronger evidence than a structural scan of bytes
    already proven correct. What that hash cannot prove is that the file is the
    database this provider expects, which is what this checks -- the same
    tables ``ComicVineSqliteProvider.test_connection`` looks for.
    """
    conn = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('cv_volume', 'cv_issue')"
        ).fetchall()
        present = {row[0] for row in rows}
        missing = {"cv_volume", "cv_issue"} - present
        if missing:
            return False, (
                "the downloaded file is missing the "
                + ", ".join(sorted(missing))
                + " table(s)"
            )
        conn.execute("SELECT id FROM cv_volume LIMIT 1").fetchall()
        conn.execute("SELECT id FROM cv_issue LIMIT 1").fetchall()
        return True, "ok"
    except sqlite3.DatabaseError as e:
        return False, str(e)
    except Exception as e:
        return False, str(e)
    finally:
        if conn is not None:
            conn.close()


# ---------------------------------------------------------------------------
# The apply
# ---------------------------------------------------------------------------

def _fail(message):
    _set_pref(PREF_LAST_ERROR, message)
    return {"success": False, "updated": False, "error": message}


def run_update(progress=None, force=False):
    """Fetch, verify and install the database. Returns a result dict.

    ``progress(detail, current=None, total=None)`` is called as the work
    proceeds. ``force`` re-installs even when the token has not moved.
    """
    if not _run_lock.acquire(blocking=False):
        return {
            "success": False,
            "updated": False,
            "error": "A ComicVine database update is already running.",
        }
    try:
        return _run_update_locked(progress or _noop_progress, force)
    finally:
        _run_lock.release()


def _run_update_locked(say, force):
    path = get_database_path()
    if not path:
        return _fail(
            "No local ComicVine database path is configured. Set one on the "
            "ComicVine (Local DB) provider first."
        )

    path = os.path.abspath(path)
    dest_dir = os.path.dirname(path)
    if not os.path.isdir(dest_dir):
        return _fail(
            f"The folder for the configured database path does not exist: {dest_dir}"
        )

    say("checking for a new database")
    token, error = probe()
    # Stamp the probe whether or not it succeeded: the 2-week window is about
    # how often we *ask*, and a server that is down should not be re-asked on
    # every heartbeat.
    _set_pref(PREF_LAST_CHECK, _now_iso())
    if token is None:
        return _fail(error or "Could not reach the update server.")

    if not needs_update(token, force=force):
        _set_pref(PREF_LAST_ERROR, None)
        return {
            "success": True,
            "updated": False,
            "message": "The local ComicVine database is already up to date.",
        }

    # Staged beside the destination, so the final step is a rename within one
    # directory. The hidden ".clu_incoming" shape matches
    # core/problem_replacements.py, so nothing treats these as real files.
    zip_staged = os.path.join(dest_dir, ".localcv.zip.clu_incoming")
    db_staged = os.path.join(dest_dir, "." + os.path.basename(path) + ".clu_incoming")

    try:
        _discard(zip_staged)
        _discard(db_staged)

        say("downloading the database")
        _download_zip(zip_staged, say)

        say("unpacking the database")
        digest = _extract_db(zip_staged, db_staged, say)

        # Before the swap, not after: peak disk is then old + new, rather than
        # old + new + a 541 MB archive nothing needs any more.
        _discard(zip_staged)

        say("verifying the database")
        if digest != token:
            raise UpdateError(
                "The downloaded database did not match the published checksum. "
                "Nothing was changed."
            )
        ok, why = verify_database(db_staged)
        if not ok:
            raise UpdateError(f"The downloaded database failed its check: {why}")

        # A file CLU writes lands with the process umask, and on a root-fallback
        # start it lands root:0600 -- unreadable to the gosu'd process that then
        # opens it on every lookup. Same failure as the root-owned thumbnails.
        try:
            from helpers import match_parent_permissions

            match_parent_permissions(db_staged)
        except Exception as e:
            app_logger.warning(
                f"Could not normalise permissions on the staged database: {e}"
            )

        say("installing the database")
        # Shares core.database's body rather than growing a second copy: the
        # gc.collect() in there is the actual remedy for a leaked handle on
        # Windows, not a superstition, and a naive os.replace would fail with
        # EACCES against an in-flight read connection.
        from core.database import _replace_with_retry

        _replace_with_retry(db_staged, path, "the ComicVine database update")
        _clear_sidecars(path)

        # Stamped only now. Writing the token any earlier would make the next
        # sweep skip a database that was never installed.
        _set_pref(PREF_DB_TOKEN, digest)
        _set_pref(PREF_LAST_UPDATE, _now_iso())
        _set_pref(PREF_LAST_ERROR, None)

        say("done")
        app_logger.info(
            f"Local ComicVine database updated at {path} ({_human(os.path.getsize(path))})"
        )
        return {
            "success": True,
            "updated": True,
            "message": "The local ComicVine database was updated.",
            "size": os.path.getsize(path),
        }

    except UpdateError as e:
        return _fail(str(e))
    except Exception as e:
        app_logger.error(f"ComicVine database update failed: {e}", exc_info=True)
        return _fail(f"The update failed: {e}")
    finally:
        # A failed run has to be a no-op. The destination is untouched until the
        # replace, and every staged file goes whatever happened -- after a
        # successful swap these are already gone, so this is a no-op there.
        _discard(zip_staged)
        _discard(db_staged)


def run_scheduled_update():
    """The scheduler's body. Never raises -- it runs on an APScheduler thread.

    The 2-week cadence lives here rather than in the trigger. An
    ``IntervalTrigger``'s clock restarts at process start and this scheduler
    has no jobstore, so on a ``restart: always`` container restarted more often
    than every two weeks a two-week trigger would never fire at all.
    """
    try:
        if not is_auto_update_enabled():
            return
        if not get_database_path():
            return
        if not _interval_elapsed():
            return

        result = run_update()
        if result.get("updated"):
            app_logger.info(
                f"Scheduled ComicVine DB update: {result.get('message')}"
            )
        elif not result.get("success"):
            app_logger.warning(
                f"Scheduled ComicVine DB update failed: {result.get('error')}"
            )
    except Exception as e:
        app_logger.error(
            f"Scheduled ComicVine DB update failed: {e}", exc_info=True
        )
