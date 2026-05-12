"""
Mylar3-compatible series.json file support.

Writes a `series.json` file alongside each subscribed/mapped series folder.
The format mirrors Mylar3's so external tools (readers, sync agents) can
consume it without API access. See:
https://github.com/mylar3/mylar3/wiki/The-series.json-file

Public API:
    write_series_json(folder_path, series, issues=None, api=None,
                      preserve_existing=True) -> bool
    read_series_json(folder_path) -> dict | None
    build_metadata(series, issues=None, api=None) -> dict

`series` may be either a dict (e.g. from `get_series_by_id`) or a Mokkari
pydantic model (e.g. from `api.series(id)`).
"""

import json
import os
import tempfile

from core.app_logging import app_logger

SERIES_JSON_FILENAME = "series.json"

# Fields a user may have hand-edited; preserved across refreshes when a
# valid series.json already exists. Matches Mylar3 behavior.
PRESERVED_FIELDS = (
    "description_text",
    "description_formatted",
    "volume",
    "booktype",
    "status",
)

_ENDED_STATUS_VALUES = {"ended", "cancelled", "canceled", "completed", "finished"}


def _get(obj, key, default=None):
    """Read `key` from a dict or attribute from an object."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _normalize_status(status):
    """Map Metron/DB status values to Mylar's 'Continuing' or 'Ended'."""
    if status is None:
        return "Continuing"
    if isinstance(status, dict):
        status = status.get("name", "")
    text = str(status).strip().lower()
    if not text:
        return "Continuing"
    if text in _ENDED_STATUS_VALUES:
        return "Ended"
    return "Continuing"


def _format_publication_run(year_began, year_end, status):
    """Format the publication_run string.

    Mylar uses 'Month YYYY - Month YYYY'; we only track years, so we emit
    'YYYY - YYYY' for ended series and 'YYYY - Present' for continuing ones.
    """
    if not year_began:
        return ""
    if _normalize_status(status) == "Ended":
        if year_end:
            return f"{year_began} - {year_end}"
        return str(year_began)
    return f"{year_began} - Present"


def _resolve_publisher(series):
    """Return the publisher name, handling dict/object/joined-column shapes."""
    publisher = _get(series, "publisher")
    if isinstance(publisher, dict):
        name = publisher.get("name")
        if name:
            return name
    elif publisher is not None and not isinstance(publisher, str):
        name = getattr(publisher, "name", None)
        if name:
            return name
    elif isinstance(publisher, str) and publisher:
        return publisher
    return _get(series, "publisher_name")


def _resolve_imprint(series):
    imprint = _get(series, "imprint")
    if imprint is None:
        return None
    if isinstance(imprint, dict):
        return imprint.get("name")
    if not isinstance(imprint, str):
        return getattr(imprint, "name", None)
    return imprint or None


def _resolve_year_began(series):
    return _get(series, "year_began") or _get(series, "volume_year")


def _resolve_cover_image(series):
    return _get(series, "cover_image") or _get(series, "image")


def _backfill_cv_id(series, api):
    """If cv_id is missing but we have a Metron id and API, try to fetch it."""
    if _get(series, "cv_id"):
        return _get(series, "cv_id")
    metron_id = _get(series, "id")
    if not api or not metron_id:
        return None
    try:
        fresh = api.series(metron_id)
        return _get(fresh, "cv_id")
    except Exception as e:
        app_logger.warning(f"series.json cv_id backfill failed for {metron_id}: {e}")
        return None


def build_metadata(series, issues=None, api=None):
    """Build the Mylar-compatible metadata dict for a series."""
    metron_id = _get(series, "id")
    cv_id = _backfill_cv_id(series, api)
    year_began = _resolve_year_began(series)
    year_end = _get(series, "year_end")
    raw_status = _get(series, "status")
    status = _normalize_status(raw_status)
    description = _get(series, "desc") or _get(series, "description")

    if issues is not None:
        total_issues = len(issues)
    else:
        total_issues = _get(series, "issue_count") or 0

    return {
        "type": "comicSeries",
        "publisher": _resolve_publisher(series),
        "imprint": _resolve_imprint(series),
        "name": _get(series, "name"),
        "comicid": cv_id,
        "metron_id": metron_id,
        "year": year_began,
        "description_text": description,
        "description_formatted": None,
        "volume": _get(series, "volume"),
        "booktype": "Print",
        "collects": None,
        "comic_image": _resolve_cover_image(series),
        "total_issues": total_issues,
        "publication_run": _format_publication_run(year_began, year_end, raw_status),
        "status": status,
    }


def read_series_json(folder_path):
    """Read and parse an existing series.json. Returns dict or None."""
    if not folder_path:
        return None
    path = os.path.join(folder_path, SERIES_JSON_FILENAME)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        app_logger.warning(f"Failed to parse existing {path}: {e}")
        return None


def _merge_preserved(new_metadata, existing_metadata):
    """Copy non-empty preserved fields from existing into new."""
    for field in PRESERVED_FIELDS:
        if field not in existing_metadata:
            continue
        old_value = existing_metadata[field]
        if old_value is None:
            continue
        if isinstance(old_value, str) and not old_value.strip():
            continue
        new_metadata[field] = old_value
    return new_metadata


def _atomic_write(path, payload):
    """Write JSON atomically via temp file + os.replace."""
    folder = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".series.json.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=4, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def write_series_json(folder_path, series, issues=None, api=None, preserve_existing=True):
    """Create or update the series.json file in `folder_path`.

    Args:
        folder_path: Target folder (must exist).
        series: Dict or Mokkari object with series data.
        issues: Optional iterable of issues used to compute total_issues.
        api: Optional Metron API client; used to backfill missing cv_id.
        preserve_existing: When True, copy user-editable fields from any
            existing series.json into the new one (Mylar behavior).

    Returns:
        True on success, False otherwise.
    """
    if not folder_path or not os.path.isdir(folder_path):
        app_logger.warning(
            f"Cannot write series.json: folder does not exist: {folder_path}"
        )
        return False

    try:
        metadata = build_metadata(series, issues=issues, api=api)

        if preserve_existing:
            existing = read_series_json(folder_path)
            existing_metadata = (existing or {}).get("metadata")
            if isinstance(existing_metadata, dict):
                _merge_preserved(metadata, existing_metadata)

        target = os.path.join(folder_path, SERIES_JSON_FILENAME)
        _atomic_write(target, {"metadata": metadata})
        app_logger.info(f"Wrote series.json at {target}")
        return True

    except Exception as e:
        app_logger.error(f"Failed to write series.json in {folder_path}: {e}")
        return False
