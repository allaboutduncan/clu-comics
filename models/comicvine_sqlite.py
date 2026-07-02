"""
Local ComicVine SQLite integration for comic metadata retrieval.

Reads a user-provided SQLite export of ComicVine data (a file the user places on
a mapped path and points to in Settings). It produces ComicInfo output identical
to the ComicVine API provider by reusing ``models.comicvine.map_to_comicinfo`` —
this module only builds the intermediate ``issue_data`` dict (same keys as
``comicvine._issue_to_dict``) from the SQLite rows + JSON credit columns.

Schema (user-provided dump):
- cv_volume(id, name, aliases, start_year, publisher_id, count_of_issues,
            description, image_url, site_detail_url)   -- id IS the ComicVine volume id
- cv_issue(id, volume_id, name, issue_number, cover_date, store_date, description,
           image_url, site_detail_url, character_credits, person_credits,
           team_credits, location_credits, story_arc_credits, associated_images)
- cv_publisher(id, name)

The credit columns are ComicVine-API-style JSON:
- person_credits    = [{"id":.., "name":"..", "role":"writer, penciler"}, ...]
- character/team/location/story_arc_credits = [{"id":.., "name":".."}, ...]
"""
import os
import json
import sqlite3
from datetime import datetime
from typing import Optional, Dict, Any, List

from core.app_logging import app_logger
from models.comicvine import map_to_comicinfo, _extract_year_from_date

# Notes-field label so ComicInfo.xml written from the local DB is distinguishable
# from the ComicVine API (which uses the default "ComicVine CVDB").
SOURCE_LABEL = "ComicVine (Local DB)"


# =============================================================================
# Database Connection
# =============================================================================

def _dict_factory(cursor, row):
    """Row factory returning plain dicts (callers rely on ``row.get(...)``)."""
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def _get_saved_credentials() -> Optional[Dict[str, Any]]:
    """Get ComicVine-SQLite credentials saved via the UI."""
    try:
        from core.database import get_provider_credentials
        return get_provider_credentials('comicvine_sqlite')
    except Exception:
        return None


def get_connection_params() -> Optional[Dict[str, Any]]:
    """
    Get the local ComicVine SQLite connection parameters.
    Checks saved credentials first, then falls back to the
    COMICVINE_DATABASE_PATH environment variable.

    Returns:
        Dict with database_path, or None if not configured
    """
    saved_creds = _get_saved_credentials()
    if saved_creds and saved_creds.get('database_path'):
        return {'database_path': saved_creds.get('database_path')}

    env_path = os.environ.get('COMICVINE_DATABASE_PATH')
    if env_path:
        return {'database_path': env_path}

    return None


def is_database_available() -> bool:
    """Check whether a configured ComicVine SQLite file exists on disk."""
    params = get_connection_params()
    path = params.get('database_path') if params else None
    return bool(path and os.path.exists(path))


def check_database_status() -> Dict[str, Any]:
    """Check if the local ComicVine SQLite database is configured and present."""
    try:
        params = get_connection_params()
        path = params.get('database_path') if params else None
        available = bool(path and os.path.exists(path))
        return {
            "cv_sqlite_available": available,
            "cv_sqlite_path_configured": bool(path),
        }
    except Exception as e:
        return {
            "cv_sqlite_available": False,
            "cv_sqlite_path_configured": False,
            "error": str(e),
        }


def get_connection():
    """
    Open and return a read-only SQLite connection to the ComicVine database.
    Uses saved credentials from the UI first, falls back to COMICVINE_DATABASE_PATH.

    Returns:
        sqlite3.Connection (dict rows) or None on failure
    """
    try:
        params = get_connection_params()
        if not params or not params.get('database_path'):
            app_logger.error("ComicVine database not configured (no saved path or COMICVINE_DATABASE_PATH)")
            return None

        path = params['database_path']
        if not os.path.exists(path):
            app_logger.error(f"ComicVine database file not found: {path}")
            return None

        # Read-only URI: never creates an empty DB on a bad path and never writes
        # -wal/-shm/journal files next to the dump.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = _dict_factory
        return conn
    except sqlite3.Error as e:
        app_logger.error(f"Failed to open ComicVine SQLite database: {e}")
        return None
    except Exception as e:
        app_logger.error(f"Failed to open ComicVine SQLite database: {e}")
        return None


# =============================================================================
# JSON credit parsing
# =============================================================================

def _load_json_list(value) -> List[Dict[str, Any]]:
    """Parse a JSON-array credit column, returning [] on anything unexpected."""
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except (ValueError, TypeError):
        return []


def _names_from(value) -> List[str]:
    """Extract the 'name' of each object in a JSON credit array."""
    names = []
    for entry in _load_json_list(value):
        if isinstance(entry, dict):
            name = entry.get('name')
            if name:
                names.append(name)
    return names


def _parse_person_credits(value):
    """Map person_credits JSON into ComicInfo role buckets.

    Replicates comicvine._issue_to_dict exactly: the role string is lowercased
    and matched with a first-match-wins if/elif chain (NOT split on commas), so
    each creator lands in exactly one bucket. Roles matching none are dropped.
    """
    writers, pencillers, inkers, colorists, letterers, cover_artists = [], [], [], [], [], []
    for credit in _load_json_list(value):
        if not isinstance(credit, dict):
            continue
        name = credit.get('name')
        if not name:
            continue
        role = (credit.get('role') or '').lower()
        if "writer" in role or "script" in role:
            writers.append(name)
        elif "pencil" in role or "illustrat" in role:
            pencillers.append(name)
        elif "ink" in role:
            inkers.append(name)
        elif "color" in role:
            colorists.append(name)
        elif "letter" in role:
            letterers.append(name)
        elif "cover" in role:
            cover_artists.append(name)
    return writers, pencillers, inkers, colorists, letterers, cover_artists


# =============================================================================
# Volume / Issue queries
# =============================================================================

_VOLUME_SELECT = (
    "SELECT v.id, v.name, v.start_year, v.count_of_issues, v.description,"
    "       v.image_url, p.name AS publisher_name"
    " FROM cv_volume v"
    " LEFT JOIN cv_publisher p ON p.id = v.publisher_id"
)


def search_volumes(series_name: str, year: Optional[int] = None) -> List[Dict[str, Any]]:
    """Search cv_volume by name (optionally constrained by start_year).

    Returns dicts shaped identically to comicvine.search_volumes so the same
    selection modal and map_to_comicinfo path can consume them.
    """
    if not series_name:
        return []
    conn = get_connection()
    if not conn:
        return []
    try:
        cursor = conn.cursor()
        # Match on name OR aliases (ComicVine searches aliases too). Alias-only
        # matches deliberately won't satisfy the name-based "confident match"
        # check in the cascade, so they surface a selection prompt.
        like = f"%{series_name}%"
        query = _VOLUME_SELECT + " WHERE (v.name LIKE ? OR v.aliases LIKE ?)"
        params: List[Any] = [like, like]
        if year:
            query += " AND v.start_year = ?"
            params.append(year)
        query += " ORDER BY v.count_of_issues DESC LIMIT 50"
        cursor.execute(query, params)
        return cursor.fetchall()
    except sqlite3.Error as e:
        app_logger.error(f"ComicVine SQLite search_volumes failed: {e}")
        return []
    finally:
        conn.close()


def get_volume_details(volume_id: int) -> Optional[Dict[str, Any]]:
    """Fetch a single volume by ComicVine volume id."""
    conn = get_connection()
    if not conn:
        return None
    try:
        cursor = conn.cursor()
        cursor.execute(_VOLUME_SELECT + " WHERE v.id = ?", (int(volume_id),))
        return cursor.fetchone()
    except (sqlite3.Error, ValueError) as e:
        app_logger.error(f"ComicVine SQLite get_volume_details failed: {e}")
        return None
    finally:
        conn.close()


def get_issue_by_number(volume_id: int, issue_number: str, year: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Look up an issue within a volume and build the intermediate issue_data dict.

    The returned dict mirrors comicvine._issue_to_dict's keys so it can be passed
    straight to comicvine.map_to_comicinfo. ``year`` is accepted for signature
    parity with the API path but is unused (volume_id + issue_number is unique).
    """
    conn = get_connection()
    if not conn:
        return None
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT i.id, i.volume_id, i.name, i.issue_number, i.cover_date,"
            "       i.store_date, i.description, i.image_url,"
            "       i.character_credits, i.person_credits, i.team_credits,"
            "       i.location_credits, i.story_arc_credits,"
            "       v.name AS volume_name, v.start_year AS volume_start_year,"
            "       p.name AS publisher_name"
            " FROM cv_issue i"
            " JOIN cv_volume v ON v.id = i.volume_id"
            " LEFT JOIN cv_publisher p ON p.id = v.publisher_id"
            " WHERE i.volume_id = ? AND i.issue_number = ?"
            " LIMIT 1",
            (int(volume_id), str(issue_number)),
        )
        row = cursor.fetchone()
        if not row:
            return None
        return _row_to_issue_data(row)
    except (sqlite3.Error, ValueError) as e:
        app_logger.error(f"ComicVine SQLite get_issue_by_number failed: {e}")
        return None
    finally:
        conn.close()


def _row_to_issue_data(row: Dict[str, Any]) -> Dict[str, Any]:
    """Turn a joined cv_issue row into the comicvine._issue_to_dict-shaped dict."""
    # Dates: prefer cover_date, fall back to store_date (mirrors _issue_to_dict).
    cover_date = row.get('cover_date') or None
    store_date = row.get('store_date') or None
    date_value = cover_date or store_date
    year = month = day = None
    if date_value:
        year = _extract_year_from_date(date_value)
        try:
            date_obj = datetime.strptime(date_value, "%Y-%m-%d")
            month = date_obj.month
            day = date_obj.day
        except (ValueError, TypeError):
            pass

    writers, pencillers, inkers, colorists, letterers, cover_artists = \
        _parse_person_credits(row.get('person_credits'))

    story_arcs = _names_from(row.get('story_arc_credits'))
    story_arc = story_arcs[0] if story_arcs else None

    return {
        "id": row.get('id'),
        "name": row.get('name'),
        "issue_number": row.get('issue_number'),
        "volume_name": row.get('volume_name'),
        "volume_id": row.get('volume_id'),
        "volume_start_year": row.get('volume_start_year'),
        "publisher": row.get('publisher_name'),
        "cover_date": cover_date,
        "store_date": store_date,
        "year": year,
        "month": month,
        "day": day,
        "description": row.get('description'),
        "image_url": row.get('image_url'),
        "page_count": None,
        "writers": writers,
        "pencillers": pencillers,
        "inkers": inkers,
        "colorists": colorists,
        "letterers": letterers,
        "cover_artists": cover_artists,
        "characters": _names_from(row.get('character_credits')),
        "teams": _names_from(row.get('team_credits')),
        "locations": _names_from(row.get('location_credits')),
        "story_arc": story_arc,
    }


def _volume_data_from_issue(issue_data: Dict[str, Any]) -> Dict[str, Any]:
    """Build the volume_data dict map_to_comicinfo expects from issue_data."""
    return {
        'id': issue_data.get('volume_id'),
        'name': issue_data.get('volume_name'),
        'publisher_name': issue_data.get('publisher'),
        'start_year': issue_data.get('volume_start_year'),
    }


def get_issue_metadata(volume_id: int, issue_number: str, start_year: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Return a fully-mapped ComicInfo dict for an issue, or None if not found.

    Reuses comicvine.map_to_comicinfo for byte-for-byte parity with the API path,
    and attaches ``_image_url`` (stripped by callers) like get_metadata_by_volume_id.
    """
    issue_data = get_issue_by_number(volume_id, issue_number)
    if not issue_data:
        return None
    volume_data = _volume_data_from_issue(issue_data)
    metadata = map_to_comicinfo(issue_data, volume_data, start_year=start_year, source_label=SOURCE_LABEL)
    metadata['_image_url'] = issue_data.get('image_url')
    return metadata
