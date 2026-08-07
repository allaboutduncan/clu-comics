"""Unified access to ComicVine data from whichever source is configured.

CLU can reach ComicVine two ways: the web API (Simyan, needs
``COMICVINE_API_KEY``) or a user-supplied local SQLite dump (needs
``database_path``). Callers that just want "the volume" or "the issues"
shouldn't have to know which is set up, and the library sweep in particular
must not be gated on the API key alone -- a user with only the local dump was
previously treated as having no ComicVine at all.

**Local first, API fallback.** The dump costs no requests and no rate-limit
budget, and the per-volume issue listing is the single most expensive call in a
sweep (one full pagination per series). A volume the dump doesn't know about
falls through to the API, so coverage is the union of both. The trade-off: for
a volume that *is* in the dump, recently published issues won't appear until the
dump is refreshed.

Metron remains the preferred provider overall -- this is only consulted once a
sidecar's ComicVine id has failed to resolve to a Metron series.
"""

from typing import Any, Dict, List, Optional

from core.app_logging import app_logger


def _local_available() -> bool:
    """True when a usable local ComicVine SQLite dump is configured."""
    try:
        from models import comicvine_sqlite as cv_sqlite

        return bool(
            cv_sqlite.check_database_status().get("cv_sqlite_available", False)
        )
    except Exception:
        return False


def _api_key(app=None) -> Optional[str]:
    """The ComicVine API key, or None (tolerates a missing app context)."""
    try:
        from models import comicvine

        return comicvine.get_cv_api_key(app)
    except Exception:
        return None


def is_available(app=None) -> bool:
    """True when ComicVine data can be read from either source.

    This is the gate for ComicVine-sourced mapping. Checking the API key alone
    silently disabled ComicVine for local-dump-only users.
    """
    return _local_available() or bool(_api_key(app))


def describe(app=None) -> str:
    """Human-readable summary of the configured sources, for logs."""
    parts = []
    if _local_available():
        parts.append("local DB")
    if _api_key(app):
        parts.append("API")
    return " + ".join(parts) if parts else "none"


def get_volume_details(cv_id: int, app=None) -> Dict[str, Any]:
    """Volume metadata for a ComicVine volume id, from whichever source has it.

    Returns ``{}`` when neither source can answer, so callers can fall back to
    sidecar-derived data.
    """
    if _local_available():
        try:
            from models import comicvine_sqlite as cv_sqlite

            details = cv_sqlite.get_volume_details(cv_id)
            if details and details.get("name"):
                return dict(details)
        except Exception as e:
            app_logger.warning(
                f"ComicVine local DB volume {cv_id} lookup failed: {e}"
            )

    key = _api_key(app)
    if key:
        try:
            from models import comicvine

            return comicvine.get_volume_details(key, cv_id) or {}
        except Exception as e:
            app_logger.warning(f"ComicVine API volume {cv_id} fetch failed: {e}")

    return {}


def get_all_issues_for_volume(cv_id: int, app=None) -> List[Dict[str, Any]]:
    """Every issue in a ComicVine volume, from whichever source has it.

    Both sources return the same record shape (ids offset into the ComicVine
    range), so callers can treat them interchangeably.
    """
    if _local_available():
        try:
            from models import comicvine_sqlite as cv_sqlite

            issues = cv_sqlite.get_all_issues_for_volume(cv_id)
            if issues:
                app_logger.info(
                    f"ComicVine local DB returned {len(issues)} issues for "
                    f"volume {cv_id}"
                )
                return issues
        except Exception as e:
            app_logger.warning(
                f"ComicVine local DB issue list for volume {cv_id} failed: {e}"
            )

    key = _api_key(app)
    if key:
        try:
            from models import comicvine

            return comicvine.get_all_issues_for_volume(key, cv_id) or []
        except Exception as e:
            app_logger.warning(
                f"ComicVine API issue list for volume {cv_id} failed: {e}"
            )

    return []
