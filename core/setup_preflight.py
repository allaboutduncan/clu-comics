"""Preflight checks for Guided Setup (``GET /api/setup/preflight``).

Before this, nothing in the app checked whether its volumes were usable:
``entrypoint.sh`` touch-tests a few mounts at start and reports only to the
container log, and ``/health`` always says healthy. This module answers the
same questions in-app, read-only, as a list the wizard can render.

Every check returns one dict::

    {id, label, status, blocking, message, fix, docs, details}

``status`` is ``ok``, ``warn``, ``fail`` or ``skip``. ``skip`` means the check
cannot run here (no ``os.getuid`` on Windows, an unreadable mount table) --
reporting that as ``ok`` would claim something nobody verified. Only the
database check is ``blocking``: everything else is a warning with a fix hint,
because the wizard can still be useful on a half-configured install.

Things that look arbitrary and are not:

- **No ``quick_check``.** It is a full scan of the database, which is why
  ``/api/database/health`` does not run one either. The latched integrity state
  and a ``SELECT 1`` are what this reports; Settings -> Database runs the full
  check.
- **Every filesystem check is time-bounded** (``_bounded``). A hung CIFS/NFS
  mount is exactly what this endpoint exists to find, and one ``stat`` against
  it would otherwise hold the request until gunicorn's 120s timeout.
- **The write probe is a dot-file** (``.clu_preflight_*``). ``is_hidden``
  treats it as junk, so neither the WATCH monitor nor the library file watcher
  will act on it in the moment it exists.
- **``run_preflight`` never raises.** A check that throws becomes that check's
  ``fail``; the rest still run.
"""
import os
import tempfile
import threading

from core.app_logging import app_logger

DOCS_BASE = "https://clucomics.org"
DOCS_SETUP = f"{DOCS_BASE}/getting-started/setup-walkthrough/"
DOCS_SETTINGS = f"{DOCS_BASE}/features/app-settings/"
DOCS_MONITORING = f"{DOCS_BASE}/features/folder-monitoring/setup/"
DOCS_USERS = f"{DOCS_BASE}/features/users/"

STATUSES = ("ok", "warn", "fail", "skip")

# The fallbacks get_protected_roots() uses when WATCH/TARGET are unset.
DEFAULT_WATCH = "/downloads/temp"
DEFAULT_TARGET = "/downloads/processed"

CHECK_TIMEOUT_SECONDS = 5
MAX_LISTED_SUBFOLDERS = 200
PROBE_PREFIX = ".clu_preflight_"


def _check(check_id, label, status, message, fix=None, docs=None,
           blocking=False, details=None):
    return {
        "id": check_id,
        "label": label,
        "status": status,
        "blocking": bool(blocking),
        "message": message,
        "fix": fix,
        "docs": docs,
        "details": details or {},
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _probe_write(path):
    """Create and delete a hidden temp file in ``path``.

    Returns ``(ok, error)``; ``error`` is None on success.
    """
    if not os.path.exists(path):
        return False, "does not exist"
    if not os.path.isdir(path):
        return False, "is not a directory"
    try:
        fd, probe = tempfile.mkstemp(prefix=PROBE_PREFIX, dir=path)
    except OSError as e:
        return False, e.strerror or str(e)
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.remove(probe)
    except OSError as e:
        return False, (
            f"could create a file but not delete it ({e.strerror or e}); "
            f"{os.path.basename(probe)} was left behind"
        )
    return True, None


def _bounded(fn, timeout):
    """Run ``fn()`` on a daemon thread and wait at most ``timeout`` seconds.

    Returns ``(finished, result, error)``. A thread stuck on a dead mount is
    left behind as a daemon; there is no way to cancel a blocked syscall.
    """
    box = {}

    def _runner():
        try:
            box["result"] = fn()
        except Exception as e:
            box["error"] = e

    t = threading.Thread(target=_runner, name="setup-preflight", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return False, None, None
    return True, box.get("result"), box.get("error")


def _enabled_libraries():
    from core.database import get_libraries
    return get_libraries(enabled_only=True) or []


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_database():
    """Database reachable, and not latched as corrupt."""
    import core.app_state as app_state
    from core.database import db_conn
    from core.db_health import error_summary

    label = "Database"
    reachable = False
    with db_conn() as conn:
        if conn is not None:
            conn.execute("SELECT 1").fetchone()
            reachable = True
    if not reachable:
        return _check(
            "database", label, "fail",
            "The database could not be opened.",
            fix="Check that /config is mounted and writable, then restart the "
                "container. The container log shows the underlying error.",
            docs=DOCS_SETTINGS, blocking=True,
        )

    integrity = app_state.get_db_integrity()
    summary = error_summary()
    details = {
        "last_integrity_ok": integrity.get("ok"),
        "last_integrity_checked_at": integrity.get("checked_at") or None,
        "corruption_errors": summary.get("corruption", 0),
    }
    if summary.get("latched"):
        return _check(
            "database", label, "fail",
            "The database has reported corruption since it was last checked.",
            fix="Open Settings -> Database to run a full check and repair or "
                "restore it.",
            docs=DOCS_SETTINGS, blocking=True, details=details,
        )
    if integrity.get("ok") is False:
        return _check(
            "database", label, "fail",
            f"The last integrity check failed: {integrity.get('error') or 'unknown error'}.",
            fix="Open Settings -> Database to run a full check and repair or "
                "restore it.",
            docs=DOCS_SETTINGS, blocking=True, details=details,
        )
    return _check(
        "database", label, "ok",
        "The database is reachable and no corruption has been reported. "
        "Settings -> Database runs a full integrity check.",
        docs=DOCS_SETTINGS, blocking=True, details=details,
    )


def check_config_storage():
    """/config is on a real, local volume."""
    from core.database import get_db_path
    from core.db_health import describe_storage

    label = "Database storage"
    info = describe_storage(os.path.dirname(get_db_path()))
    details = {
        "fstype": info.get("fstype"),
        "mountpoint": info.get("mountpoint"),
        "source": info.get("source"),
    }
    risk = info.get("risk")
    note = info.get("note") or ""
    message = note[:1].upper() + note[1:]
    fix = ("Mount /config on local storage or a Docker named volume "
           "(see docker-compose.yaml).")
    if risk == "ok":
        return _check("config_storage", label, "ok", message,
                      docs=DOCS_SETUP, details=details)
    if risk == "warn":
        return _check("config_storage", label, "warn", message,
                      fix=fix, docs=DOCS_SETUP, details=details)
    if risk == "danger":
        return _check("config_storage", label, "fail", message,
                      fix=fix, docs=DOCS_SETUP, details=details)
    return _check("config_storage", label, "skip",
                  "Could not determine the filesystem holding /config on this "
                  "platform.", docs=DOCS_SETUP, details=details)


def _check_writable_dir(check_id, label, path, purpose):
    ok, error = _probe_write(path)
    details = {"path": path}
    if ok:
        return _check(check_id, label, "ok", f"{path} is writable.",
                      docs=DOCS_SETUP, details=details)
    return _check(
        check_id, label, "fail", f"{path} {error}. {purpose}",
        fix=f"Map a volume onto {path} that the container user (PUID:PGID) "
            f"can write to.",
        docs=DOCS_SETUP, details=details,
    )


def check_config_writable():
    from core.config import get_config_dir
    return _check_writable_dir(
        "config_writable", "/config is writable", get_config_dir(),
        "The database, its backups and the logs live here.",
    )


def check_cache_writable():
    from core.config import get_cache_dir
    return _check_writable_dir(
        "cache_writable", "/cache is writable", get_cache_dir(),
        "Thumbnails and the trash live here.",
    )


def check_data_mount(libraries=None):
    """/data, or another mapped volume, holds folders that can become libraries.

    Libraries may live anywhere (``/comics`` is documented), so a volume mapped
    outside /data satisfies this check too; ``details.other_mounts`` lists
    them for step 5.1 alongside ``details.subfolders``.
    """
    from core.config import get_data_dir
    from core.setup_browse import extra_mounts

    label = "Comic folders under /data"
    data_dir = get_data_dir()
    if libraries is None:
        libraries = _enabled_libraries()
    other_mounts = extra_mounts()
    fix = ("Map your comics folder into the container under /data in "
           "docker-compose.yaml (for example /mnt/comics:/data/comics), then "
           "recreate the container.")
    also = ("Also mapped: " + ", ".join(other_mounts) + "."
            if other_mounts else "")

    def _elsewhere(reason, details, count_libraries=True):
        """``reason`` would warn, but comics are mapped somewhere else."""
        if libraries and count_libraries:
            return _check(
                "data_mount", label, "ok",
                f"{reason}; your libraries are configured elsewhere.",
                docs=DOCS_SETUP, details=details,
            )
        if other_mounts:
            return _check(
                "data_mount", label, "ok",
                f"{reason}, but {', '.join(other_mounts)} is mapped and can be "
                f"added as a library.",
                docs=DOCS_SETUP, details=details,
            )
        return None

    if not os.path.isdir(data_dir):
        details = {"path": data_dir, "subfolders": [],
                   "other_mounts": other_mounts}
        return _elsewhere(f"{data_dir} is not mounted", details) or _check(
            "data_mount", label, "warn",
            f"{data_dir} does not exist, so no comics volume is mapped.",
            fix=fix, docs=DOCS_SETUP, details=details,
        )

    from helpers import is_hidden

    subfolders = []
    for entry in sorted(os.scandir(data_dir), key=lambda e: e.name.lower()):
        try:
            if entry.is_dir() and not is_hidden(entry.path):
                subfolders.append(entry.path)
        except OSError:
            continue
    truncated = len(subfolders) > MAX_LISTED_SUBFOLDERS
    details = {
        "path": data_dir,
        "subfolders": subfolders[:MAX_LISTED_SUBFOLDERS],
        "subfolder_count": len(subfolders),
        "truncated": truncated,
        "other_mounts": other_mounts,
    }
    if not subfolders:
        # A library can be /data itself, so an empty /data with libraries
        # still warns; only a volume mapped elsewhere settles it.
        return _elsewhere(f"{data_dir} has no folders in it", details,
                          count_libraries=False) or _check(
            "data_mount", label, "warn",
            f"{data_dir} has no folders in it, so no comics volume appears to "
            f"be mapped.",
            fix=fix, docs=DOCS_SETUP, details=details,
        )
    noun = "folder" if len(subfolders) == 1 else "folders"
    return _check(
        "data_mount", label, "ok",
        f"Found {len(subfolders)} {noun} under {data_dir}. {also}".rstrip(),
        docs=DOCS_SETUP, details=details,
    )


def _check_staging_dir(check_id, name, configured, default):
    from helpers.library import watch_target_verdict

    label = f"{name} folder"
    path = configured or default
    details = {"path": path, "configured": bool(configured)}
    fix = (f"Set {name} in Settings -> File Processing to a folder mapped into "
           f"the container outside /data.")

    blocked, verdict = watch_target_verdict(name, path)
    if blocked:
        return _check(check_id, label, "fail", verdict, fix=fix,
                      docs=DOCS_MONITORING, details=details)

    ok, error = _probe_write(path)
    if not ok:
        return _check(
            check_id, label, "warn", f"{name} ({path}) {error}.",
            fix=fix + " The container user (PUID:PGID) must be able to write "
                      "to it.",
            docs=DOCS_MONITORING, details=details,
        )
    if verdict:
        return _check(check_id, label, "warn", verdict, docs=DOCS_MONITORING,
                      details=details)
    source = "" if configured else " (the default; not set in Settings)"
    return _check(check_id, label, "ok", f"{path}{source} is writable.",
                  docs=DOCS_MONITORING, details=details)


def check_watch_dir():
    from core.config import get_watch_dir
    return _check_staging_dir("watch_dir", "WATCH", get_watch_dir(), DEFAULT_WATCH)


def check_target_dir():
    from core.config import get_target_dir
    return _check_staging_dir("target_dir", "TARGET", get_target_dir(), DEFAULT_TARGET)


def check_no_libraries():
    return _check(
        "libraries", "Libraries", "warn",
        "No libraries are configured yet.",
        fix="Add one in Settings -> Libraries (or in the next step).",
        docs=DOCS_SETTINGS,
    )


def check_library(lib):
    """One library folder: exists, readable, writable."""
    path = lib.get("path") or ""
    name = lib.get("name") or path
    check_id = f"library:{lib.get('id')}"
    label = f"Library: {name}"
    details = {"library_id": lib.get("id"), "path": path}

    if not os.path.exists(path):
        return _check(
            check_id, label, "fail", f"{path} does not exist.",
            fix="Check the volume mapping in docker-compose.yaml, or edit the "
                "library's path in Settings -> Libraries.",
            docs=DOCS_SETTINGS, details=details,
        )
    if not os.path.isdir(path):
        return _check(check_id, label, "fail", f"{path} is not a directory.",
                      docs=DOCS_SETTINGS, details=details)
    try:
        os.listdir(path)
    except OSError as e:
        return _check(
            check_id, label, "fail",
            f"{path} cannot be read: {e.strerror or e}.",
            fix="Give the container user (PUID:PGID) read access to it.",
            docs=DOCS_SETTINGS, details=details,
        )
    ok, error = _probe_write(path)
    if not ok:
        return _check(
            check_id, label, "warn",
            f"{path} can be read but not written ({error}). Renaming, "
            f"metadata writes and conversion will fail.",
            fix="Give the container user (PUID:PGID) write access to it, or "
                "set PUID/PGID to the folder's owner.",
            docs=DOCS_SETTINGS, details=details,
        )
    return _check(check_id, label, "ok", f"{path} is readable and writable.",
                  docs=DOCS_SETTINGS, details=details)


def check_owner_account():
    from core.auth import is_login_required
    from core.database import get_owner_user

    label = "Owner account"
    owner = get_owner_user()
    if not owner:
        return _check(
            "owner_account", label, "fail", "No owner account exists.",
            fix="Restart the container: an owner account is created at "
                "startup.",
            docs=DOCS_USERS,
        )
    login_required = is_login_required()
    details = {
        "username": owner.get("username"),
        "needs_setup": bool(owner.get("needs_setup")),
        "login_required": login_required,
    }
    if not owner.get("needs_setup"):
        return _check("owner_account", label, "ok",
                      "The owner account has a password.",
                      docs=DOCS_USERS, details=details)
    if not login_required:
        return _check(
            "owner_account", label, "ok",
            "Single-user and login-free. Set an owner password before adding "
            "other users.",
            docs=DOCS_USERS, details=details,
        )
    return _check(
        "owner_account", label, "warn",
        "Login is required but the owner account has no password yet.",
        fix="Set the owner's username and password from the Users page.",
        docs=DOCS_USERS, details=details,
    )


def check_run_as_user(libraries=None):
    """The process runs as PUID, not as root, and owns the comics."""
    label = "Container user"
    if not hasattr(os, "getuid"):
        return _check("run_as_user", label, "skip",
                      "Not applicable on this platform.", docs=DOCS_SETUP)

    from core.config import get_data_dir

    uid = os.getuid()
    gid = os.getgid() if hasattr(os, "getgid") else None
    details = {"uid": uid, "gid": gid}
    fix = ("Make the mounted folders writable by PUID:PGID (fix the volume's "
           "ownership, or set PUID/PGID to match it) and recreate the "
           "container.")

    if uid == 0:
        return _check(
            "run_as_user", label, "warn",
            "CLU is running as root. The entrypoint falls back to root when a "
            "mount is not writable by PUID:PGID, so new files will be "
            "root-owned.",
            fix=fix, docs=DOCS_SETUP, details=details,
        )

    puid = os.environ.get("PUID", "").strip()
    if puid.isdigit():
        details["puid"] = int(puid)
        if int(puid) != uid:
            return _check(
                "run_as_user", label, "warn",
                f"CLU is running as uid {uid}, but PUID is {puid}.",
                fix=fix, docs=DOCS_SETUP, details=details,
            )

    if libraries is None:
        libraries = _enabled_libraries()
    root = libraries[0]["path"] if libraries else get_data_dir()
    try:
        owner_uid = os.stat(root).st_uid
    except OSError:
        owner_uid = None
    details["checked_path"] = root
    details["path_owner_uid"] = owner_uid
    if owner_uid is not None and owner_uid != uid:
        return _check(
            "run_as_user", label, "warn",
            f"{root} is owned by uid {owner_uid}, but CLU runs as uid {uid}.",
            fix=fix, docs=DOCS_SETUP, details=details,
        )
    return _check("run_as_user", label, "ok", f"CLU is running as uid {uid}.",
                  docs=DOCS_SETUP, details=details)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _run_one(check_id, label, fn, blocking=False):
    """Run one check; a raise or an overrun becomes that check's result."""
    finished, result, error = _bounded(fn, CHECK_TIMEOUT_SECONDS)
    if not finished:
        return _check(
            check_id, label, "warn",
            f"Did not respond within {CHECK_TIMEOUT_SECONDS}s. A network "
            f"mount (CIFS/NFS) that has stopped responding is the usual cause.",
            docs=DOCS_SETUP, blocking=False,
        )
    if error is not None:
        app_logger.debug(f"Preflight check {check_id} raised: {error}")
        return _check(check_id, label, "fail", f"The check failed: {error}",
                      docs=DOCS_SETUP, blocking=blocking)
    return result


def run_preflight():
    """Run every check and return the endpoint's payload. Never raises."""
    try:
        libraries = _enabled_libraries()
    except Exception as e:
        app_logger.debug(f"Preflight could not list libraries: {e}")
        libraries = []

    jobs = [
        ("database", "Database", check_database, True),
        ("config_storage", "Database storage", check_config_storage, False),
        ("config_writable", "/config is writable", check_config_writable, False),
        ("cache_writable", "/cache is writable", check_cache_writable, False),
        ("data_mount", "Comic folders under /data",
         lambda: check_data_mount(libraries), False),
        ("watch_dir", "WATCH folder", check_watch_dir, False),
        ("target_dir", "TARGET folder", check_target_dir, False),
    ]
    if libraries:
        for lib in libraries:
            jobs.append((f"library:{lib.get('id')}",
                         f"Library: {lib.get('name') or lib.get('path')}",
                         (lambda lib=lib: check_library(lib)), False))
    else:
        jobs.append(("libraries", "Libraries", check_no_libraries, False))
    jobs.append(("owner_account", "Owner account", check_owner_account, False))
    jobs.append(("run_as_user", "Container user",
                 lambda: check_run_as_user(libraries), False))

    checks = []
    for check_id, label, fn, blocking in jobs:
        result = _run_one(check_id, label, fn, blocking=blocking)
        app_logger.debug(
            f"Preflight {check_id}: {result['status']} - {result['message']}"
        )
        checks.append(result)

    summary = {s: 0 for s in STATUSES}
    for c in checks:
        summary[c["status"]] = summary.get(c["status"], 0) + 1
    blocking = any(c["blocking"] and c["status"] == "fail" for c in checks)

    line = ("Setup preflight: " + ", ".join(f"{n} {s}" for s, n in summary.items())
            + (" (blocking)" if blocking else ""))
    if summary["fail"]:
        failed = ", ".join(c["id"] for c in checks if c["status"] == "fail")
        app_logger.info(f"{line}; failed: {failed}")
    else:
        app_logger.debug(line)

    return {
        "success": True,
        "blocking": blocking,
        "summary": summary,
        "checks": checks,
    }
