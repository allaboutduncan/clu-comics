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
    ("folder_monitoring", "Folder Monitoring", False, "file-processing"),
    ("downloads", "Downloads", False, "download-clients"),
    ("schedules", "Schedules", False, None),
    ("users", "Users", False, None),
    ("notifications", "Notifications", False, "notifications"),
    ("personalization", "Personalization", False, "personalization"),
)

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


_STATUS_FNS = {
    "libraries": libraries_status,
    "metadata": metadata_status,
}


def get_setup_status():
    """The endpoint's payload. A section whose check raises needs attention."""
    sections = []
    for section_id, label, built, settings_tab in SECTIONS:
        entry = {"id": section_id, "label": label, "built": built,
                 "settings_tab": settings_tab, "status": None,
                 "message": "", "details": {}}
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
