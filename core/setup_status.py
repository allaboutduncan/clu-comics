"""Per-section status for the Guided Setup (``GET /api/setup/status``).

The wizard's progress rail and its Summary step show, for each section, one
of ``configured``, ``needs_attention`` or ``unconfigured``. On a re-run this
is what turns the wizard into a health check.

``SECTIONS`` is the canonical order from readme/guided-setup.md. A section
the wizard does not have a step for yet is listed with ``built: False`` and
no status: the Summary links it to its Settings tab instead of guessing.
"""

from core.app_logging import app_logger

CONFIGURED = "configured"
NEEDS_ATTENTION = "needs_attention"
UNCONFIGURED = "unconfigured"

# (id, label, built, Settings tab anchor or a page)
SECTIONS = (
    ("libraries", "Libraries", True, "libraries"),
    ("metadata", "Metadata", True, "metadata-providers"),
    ("mylar_import", "Mylar3 Import", True, None),
    ("pull_list_scan", "Pull List Scan", True, None),
    ("folder_monitoring", "Folder Monitoring", True, "file-processing"),
    ("downloads", "Downloads", True, "download-clients"),
    ("schedules", "Schedules", True, None),
    ("users", "Users", True, None),
    ("notifications", "Notifications", True, "notifications"),
    ("personalization", "Personalization", True, "personalization"),
    ("repair", "Repair Tour", True, None),
)

# Sections with no Settings tab whose Summary row links to a page instead
# (endpoint names, resolved per request so a subpath proxy is honoured).
LINKS = {
    "mylar_import": "mylar_import.mylar_import_page",
    "pull_list_scan": "series.pull_list",
    "repair": "problem_files.page",
}

# Providers reported as details of the Metadata section, besides Metron.
_OPTIONAL_PROVIDERS = ("comicvine", "comicvine_sqlite", "gcd", "gcd_api")


def libraries_status():
    """Configured when every enabled library is readable and writable."""
    from core.setup_preflight import _enabled_libraries, _run_one, check_library

    libraries = _enabled_libraries()
    if not libraries:
        return {"status": UNCONFIGURED, "message": "No libraries are configured.",
                "details": {"count": 0}}
    checks = [
        _run_one(f"library:{lib.get('id')}",
                 f"Library: {lib.get('name') or lib.get('path')}",
                 (lambda lib=lib: check_library(lib)))
        for lib in libraries
    ]
    bad = [c for c in checks if c["status"] != "ok"]
    if bad:
        return {"status": NEEDS_ATTENTION,
                "message": "; ".join(c["message"] for c in bad),
                "details": {"count": len(libraries)}}
    n = len(libraries)
    return {"status": CONFIGURED,
            "message": f"{n} librar{'y' if n == 1 else 'ies'} configured.",
            "details": {"count": n}}


def metadata_status():
    """Configured when Metron has credentials it has accepted.

    Metron is what the pull list, Wanted, subscriptions and the importers run
    on; ComicVine and GCD are optional and only reported.
    """
    from core.database import get_all_provider_credentials_status
    from models import metron as metron_module

    rows = {r["provider_type"]: r for r in get_all_provider_credentials_status()}
    optional = {p: p in rows for p in _OPTIONAL_PROVIDERS}
    metron = rows.get("metron")
    if not metron:
        return {"status": UNCONFIGURED,
                "message": "Metron is not set up. The pull list, Wanted and "
                           "subscriptions need it.",
                "details": {"metron": False, **optional}}
    if metron_module.auth_block_state().get("blocked"):
        return {"status": NEEDS_ATTENTION,
                "message": "Metron rejected the saved credentials, so CLU has "
                           "stopped calling it.",
                "details": {"metron": True, **optional}}
    if metron.get("is_valid") != 1:
        return {"status": NEEDS_ATTENTION,
                "message": "Metron credentials are saved but have not passed a "
                           "connection test.",
                "details": {"metron": True, **optional}}
    return {"status": CONFIGURED, "message": "Metron is connected.",
            "details": {"metron": True, **optional}}


def folder_monitoring_status():
    """Configured when the monitor is on and running and WATCH/TARGET pass.

    The path checks are the preflight's own (``watch_target_verdict`` plus a
    write probe), so the wizard and the System Check cannot disagree.
    """
    from core import monitor_supervisor
    from core.setup_preflight import _run_one, check_target_dir, check_watch_dir

    monitor = monitor_supervisor.status()
    details = {"enabled": bool(monitor.get("enabled")),
               "running": bool(monitor.get("running"))}
    if not details["enabled"]:
        return {"status": UNCONFIGURED,
                "message": "Folder monitoring is off; downloads are not "
                           "processed automatically.",
                "details": details}
    checks = [_run_one("watch_dir", "WATCH folder", check_watch_dir),
              _run_one("target_dir", "TARGET folder", check_target_dir)]
    bad = [c for c in checks if c["status"] != "ok"]
    if bad:
        return {"status": NEEDS_ATTENTION,
                "message": "; ".join(c["message"] for c in bad),
                "details": details}
    if not details["running"]:
        return {"status": NEEDS_ATTENTION,
                "message": "Folder monitoring is on but the monitor is not "
                           "running. See monitor.log.",
                "details": details}
    return {"status": CONFIGURED, "message": "Watching " + monitor.get("watch", "") + ".",
            "details": details}


_SOURCE_LABELS = {"getcomics": "GetComics", "usenet": "Usenet", "dcpp": "DC++"}


def _client_problem(group, label, rows):
    """Why the active client of ``group`` cannot be used, or None."""
    active = [r for r in rows if r.get("client_group") == group and r.get("is_active") == 1]
    if not active:
        return f"{label} is on, but no {label} client is active."
    row = active[0]
    # is_valid is 0 until the first test too; only a test that ran and failed counts.
    if row.get("last_tested") and row.get("is_valid") != 1:
        return f"The {label} client failed its last connection test."
    return None


def downloads_status():
    """Configured when every source in the priority list can actually be used.

    ``download_source_priority`` is the on/off switch, so a source listed there
    without its client (or, for Usenet, without an indexer) is skipped by
    Auto-download without a word. That is what needs attention here.
    GetComics needs nothing.
    """
    from core.database import get_all_download_clients_status, get_enabled_indexers
    from models.download_sources import get_source_priority

    order = get_source_priority()
    rows = get_all_download_clients_status()
    indexers = len(get_enabled_indexers()) if "usenet" in order else None

    def active(group):
        return next((r["client_type"] for r in rows
                     if r.get("client_group") == group and r.get("is_active") == 1), None)

    details = {"order": order,
               "usenet": {"client": active("usenet"), "indexers": indexers},
               "dcpp": {"client": active("dcpp")}}
    if not order:
        return {"status": UNCONFIGURED, "message": "No download source is on.",
                "details": details}
    problems = []
    if "usenet" in order:
        problems.append(_client_problem("usenet", "Usenet", rows))
        if not indexers:
            problems.append("Usenet is on, but no indexer is enabled.")
    if "dcpp" in order:
        problems.append(_client_problem("dcpp", "DC++", rows))
    problems = [p for p in problems if p]
    if problems:
        return {"status": NEEDS_ATTENTION, "message": " ".join(problems), "details": details}
    return {"status": CONFIGURED,
            "message": "Sources, in order: " +
                       ", ".join(_SOURCE_LABELS.get(s, s) for s in order) + ".",
            "details": details}


# The schedules the wizard recommends turning on (readme/guided-setup.md §11).
RECOMMENDED_SCHEDULES = (("rebuild", "file index rebuild"), ("sync", "series sync"))


def schedules_status():
    """Configured when the recommended schedules are enabled."""
    from core.database import get_schedule

    enabled = {}
    for name, _label in RECOMMENDED_SCHEDULES:
        row = get_schedule(name) or {}
        enabled[name] = (row.get("frequency") or "disabled") != "disabled"
    off = [label for name, label in RECOMMENDED_SCHEDULES if not enabled[name]]
    if off:
        return {"status": UNCONFIGURED,
                "message": "Not scheduled: " + ", ".join(off) + ".",
                "details": enabled}
    return {"status": CONFIGURED,
            "message": "The file index and series sync run on a schedule.",
            "details": enabled}


def users_status():
    """Configured once the owner has a password.

    Login-free with a passwordless owner is a valid single-user setup, so it
    is ``unconfigured`` rather than a problem; it only needs attention when
    login is required and the owner still cannot log in.
    """
    from core.auth import is_login_required
    from core.database import count_users, get_owner_user

    owner = get_owner_user() or {}
    login_required = is_login_required()
    n = count_users()
    details = {"needs_setup": bool(owner.get("needs_setup")),
               "login_required": login_required, "count": n,
               "username": owner.get("username")}
    if not owner:
        return {"status": NEEDS_ATTENTION, "message": "No owner account exists.",
                "details": details}
    if owner.get("needs_setup"):
        if login_required:
            return {"status": NEEDS_ATTENTION,
                    "message": "Login is required but the owner has no password yet.",
                    "details": details}
        return {"status": UNCONFIGURED,
                "message": "Single user, login-free. No owner password is set.",
                "details": details}
    if login_required:
        message = f"{n} account{'' if n == 1 else 's'}; login is required."
    else:
        message = "Single user, login-free. The owner has a password."
    return {"status": CONFIGURED, "message": message, "details": details}


def notifications_status():
    """Configured when notifications are on with at least one URL."""
    from core.notifications import get_settings

    settings = get_settings()
    details = {"enabled": settings["enabled"], "url_count": len(settings["urls"])}
    if settings["enabled"] and settings["urls"]:
        n = len(settings["urls"])
        return {"status": CONFIGURED,
                "message": f"Sending to {n} target{'' if n == 1 else 's'}.",
                "details": details}
    return {"status": UNCONFIGURED, "message": "Notifications are off.",
            "details": details}


def personalization_status():
    """Always configured: some theme is always in effect."""
    from core.settings import get_setting
    from core.themes import BOOTSWATCH_THEMES

    theme = get_setting("BOOTSTRAP_THEME") or "default"
    label = dict(BOOTSWATCH_THEMES).get(theme, theme)
    return {"status": CONFIGURED, "message": f"Theme: {label}.",
            "details": {"theme": theme}}


def _plural(n, word):
    """``1 hour``, ``2 hours``; "series" is its own plural."""
    return f"{n} {word}{'' if n == 1 or word.endswith('series') else 's'}"


def mylar_import_status():
    """From the last import's recorded counts; the job itself expires.

    Deferred rows are the ones a rate-limited or blocked Metron held back, and
    a re-run picks them up, so they need attention rather than a re-import.
    """
    from models.mylar_import import read_last_run

    last = read_last_run()
    if not last:
        return {"status": UNCONFIGURED, "message": "No Mylar3 import has run.",
                "details": {}}
    if last.get("status") == "error":
        return {"status": NEEDS_ATTENTION,
                "message": f"The last import failed: {last.get('error') or 'unknown error'}",
                "details": last}
    problems = []
    if last.get("deferred"):
        problems.append(f"{_plural(last['deferred'], 'series')} deferred while Metron "
                        "was unavailable; run the import again to finish them")
    if last.get("unresolved"):
        problems.append(f"{_plural(last['unresolved'], 'series')} could not be identified")
    if last.get("failed"):
        problems.append(f"{_plural(last['failed'], 'series')} failed")
    mapped = f"{_plural(last.get('mapped') or 0, 'series')} imported"
    if problems:
        return {"status": NEEDS_ATTENTION,
                "message": mapped + "; " + "; ".join(problems) + ".",
                "details": last}
    return {"status": CONFIGURED, "message": mapped + ".", "details": last}


def mapped_series_counts():
    """``(mapped, monitored)`` Pull List series. Unreadable reads as ``(0, 0)``."""
    from core.database import get_all_mapped_series

    try:
        rows = get_all_mapped_series()
    except Exception:
        return 0, 0
    # NULL `monitored` on legacy rows means on, as in the simulation.
    return len(rows), sum(1 for s in rows if s.get("monitored") != 0)


def pull_list_scan_status():
    """Configured once the Pull List has series, however they got there."""
    from models import comicvine_source
    from models.metron import is_metron_configured

    mapped, _monitored = mapped_series_counts()
    details = {"mapped": mapped}
    if mapped:
        return {"status": CONFIGURED,
                "message": f"{_plural(mapped, 'series')} on the Pull List.",
                "details": details}
    if not is_metron_configured() and not comicvine_source.is_available():
        return {"status": NEEDS_ATTENTION,
                "message": "The scan needs Metron (or a ComicVine key) to identify "
                           "series.",
                "details": details}
    return {"status": UNCONFIGURED, "message": "The Pull List is empty.",
            "details": details}


def repair_status():
    """Whether the library health scan is set up, not how healthy the library is.

    Open problems are reported in the message only: a damaged file is the
    library's state, not a gap in the setup.
    """
    from core.library_health import get_summary

    summary = get_summary()
    details = {key: summary.get(key) for key in (
        "enabled", "last_run_at", "integrity_open", "orphans_open", "other_open",
        "duplicates", "level")}
    if not summary.get("enabled") and not summary.get("last_run_at"):
        return {"status": UNCONFIGURED, "message": "The library health scan is off.",
                "details": details}
    message = ("The health scan runs every "
               f"{_plural(summary.get('interval_hours') or 0, 'hour')}."
               if summary.get("enabled") else "The health scan has run but is not scheduled.")
    if summary.get("last_run_at"):
        dups = summary.get("duplicates") or {}
        open_problems = (summary.get("integrity_open", 0) + summary.get("orphans_open", 0)
                         + summary.get("other_open", 0))
        message += (f" Open: {_plural(open_problems, 'problem')}, "
                    f"{_plural(dups.get('exact', 0) + dups.get('likely', 0), 'duplicate group')}.")
    return {"status": CONFIGURED, "message": message, "details": details}


_STATUS_FNS = {
    "libraries": libraries_status,
    "metadata": metadata_status,
    "mylar_import": mylar_import_status,
    "pull_list_scan": pull_list_scan_status,
    "folder_monitoring": folder_monitoring_status,
    "downloads": downloads_status,
    "schedules": schedules_status,
    "users": users_status,
    "notifications": notifications_status,
    "personalization": personalization_status,
    "repair": repair_status,
}


def _link(section_id):
    endpoint = LINKS.get(section_id)
    if not endpoint:
        return None
    try:
        from flask import url_for

        return url_for(endpoint)
    except Exception:
        return None


def get_setup_status():
    """The endpoint's payload. A section whose check raises needs attention."""
    sections = []
    for section_id, label, built, settings_tab in SECTIONS:
        entry = {"id": section_id, "label": label, "built": built,
                 "settings_tab": settings_tab, "link": _link(section_id),
                 "status": None, "message": "", "details": {}}
        fn = _STATUS_FNS.get(section_id)
        if fn is not None:
            try:
                entry.update(fn())
            except Exception as e:
                app_logger.debug(f"Setup status {section_id} raised: {e}")
                entry.update(status=NEEDS_ATTENTION,
                             message=f"Could not check this section: {e}")
        sections.append(entry)
    return {"success": True, "sections": sections}
