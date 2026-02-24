"""
Metron API integration for comic metadata retrieval using Mokkari library.
"""
from app_logging import app_logger
from typing import Optional, Dict, Any, List
import re
from datetime import datetime, timedelta
from version import __version__

# Check if mokkari is available
try:
    from mokkari.session import Session as MokkariSession
    from mokkari.exceptions import RateLimitError
    MOKKARI_AVAILABLE = True
except ImportError:
    MOKKARI_AVAILABLE = False

    class RateLimitError(Exception):
        """Placeholder when mokkari is not installed."""
        pass

# User agent for Metron API requests
CLU_USER_AGENT = f"CLU/{__version__}"


def is_mokkari_available() -> bool:
    """Check if the Mokkari library is available."""
    return MOKKARI_AVAILABLE


def is_connection_error(exc: Exception) -> bool:
    """Check if an exception is a Metron connectivity/timeout error."""
    msg = str(exc).lower()
    return any(kw in msg for kw in (
        'timeout', 'connection error', 'connection refused',
        'unreachable', 'name resolution', 'nodename nor servname',
        'temporary failure', 'gaierror', 'readtimeout',
        'connecttimeout', 'connectionpool',
    ))


def get_api(username: str, password: str):
    """
    Initialize and return a Metron API client using Mokkari Session.

    Args:
        username: Metron username
        password: Metron password

    Returns:
        Mokkari Session client or None if unavailable
    """
    if not MOKKARI_AVAILABLE:
        app_logger.warning("Mokkari library not available. Install with: pip install mokkari")
        return None
    if not username or not password:
        app_logger.warning("Metron credentials not configured")
        return None
    try:
        return MokkariSession(username=username, passwd=password, user_agent=CLU_USER_AGENT)
    except Exception as e:
        app_logger.error(f"Failed to initialize Metron API: {e}")
        return None


def parse_cvinfo_for_metron_id(cvinfo_path: str) -> Optional[int]:
    """
    Parse a cvinfo file for series_id.

    cvinfo format:
        https://comicvine.gamespot.com/series-name/4050-123456/
        series_id: 10354

    Args:
        cvinfo_path: Path to the cvinfo file

    Returns:
        Metron series ID as integer, or None if not found
    """
    try:
        with open(cvinfo_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Look for series_id: <number>
        match = re.search(r'series_id:\s*(\d+)', content, re.IGNORECASE)
        if match:
            return int(match.group(1))
        return None
    except Exception as e:
        app_logger.error(f"Error parsing cvinfo for Metron ID: {e}")
        return None


def parse_cvinfo_for_comicvine_id(cvinfo_path: str) -> Optional[int]:
    """
    Parse a cvinfo file for ComicVine series ID.

    URL format: https://comicvine.gamespot.com/series-name/4050-123456/
    The CV series ID is 123456 (after 4050-)

    Args:
        cvinfo_path: Path to the cvinfo file

    Returns:
        ComicVine series ID as integer, or None if not found
    """
    try:
        with open(cvinfo_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Match pattern: 4050-{volume_id}
        match = re.search(r'/4050-(\d+)', content)
        if match:
            return int(match.group(1))
        return None
    except Exception as e:
        app_logger.error(f"Error parsing cvinfo for ComicVine ID: {e}")
        return None


def get_series_id_by_comicvine_id(api, cv_series_id: int) -> Optional[int]:
    """
    Look up Metron series ID using ComicVine series ID.

    Searches Metron for series with matching cv_id.

    Args:
        api: Mokkari API client
        cv_series_id: ComicVine series/volume ID

    Returns:
        Metron series ID, or None if not found
    """
    try:
        # Search for series by cv_id
        params = {"cv_id": cv_series_id}
        results = api.series_list(params)

        if results:
            series_id = results[0].id
            app_logger.info(f"Found Metron series {series_id} for CV ID {cv_series_id}")
            return series_id

        app_logger.warning(f"No Metron series found for ComicVine ID {cv_series_id}")
        return None
    except RateLimitError:
        raise
    except Exception as e:
        app_logger.error(f"Error looking up Metron series by CV ID {cv_series_id}: {e}")
        return None


def update_cvinfo_with_metron_id(cvinfo_path: str, series_id: int) -> bool:
    """
    Update cvinfo file to include series_id.

    Args:
        cvinfo_path: Path to the cvinfo file
        series_id: Metron series ID to add

    Returns:
        True if successful, False otherwise
    """
    try:
        with open(cvinfo_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Check if series_id already exists
        if re.search(r'series_id:', content, re.IGNORECASE):
            # Update existing
            content = re.sub(
                r'series_id:\s*\d+',
                f'series_id: {series_id}',
                content,
                flags=re.IGNORECASE
            )
        else:
            # Append new line
            content = content.rstrip() + f'\nseries_id: {series_id}\n'

        with open(cvinfo_path, 'w', encoding='utf-8') as f:
            f.write(content)

        app_logger.info(f"Updated cvinfo with series_id: {series_id}")
        return True
    except Exception as e:
        app_logger.error(f"Error updating cvinfo with Metron ID: {e}")
        return False


def read_cvinfo_fields(cvinfo_path: str) -> Dict[str, Any]:
    """
    Read publisher_name and start_year from cvinfo file if present.

    Args:
        cvinfo_path: Path to the cvinfo file

    Returns:
        Dict with 'publisher_name' and 'start_year' keys (values may be None)
    """
    result = {'publisher_name': None, 'start_year': None}
    try:
        with open(cvinfo_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line.startswith('publisher_name:'):
                    result['publisher_name'] = line.split(':', 1)[1].strip()
                elif line.startswith('start_year:'):
                    try:
                        result['start_year'] = int(line.split(':', 1)[1].strip())
                    except ValueError:
                        pass
    except Exception as e:
        app_logger.error(f"Error reading cvinfo fields from {cvinfo_path}: {e}")
    return result


def write_cvinfo_fields(cvinfo_path: str, publisher_name: Optional[str], start_year: Optional[int]) -> bool:
    """
    Append publisher_name and start_year to cvinfo file if not already present.

    Args:
        cvinfo_path: Path to the cvinfo file
        publisher_name: Publisher name to save
        start_year: Series start year to save

    Returns:
        True if successful, False otherwise
    """
    try:
        existing = read_cvinfo_fields(cvinfo_path)
        lines_to_add = []

        if publisher_name and not existing['publisher_name']:
            lines_to_add.append(f"publisher_name: {publisher_name}")
        if start_year and not existing['start_year']:
            lines_to_add.append(f"start_year: {start_year}")

        if not lines_to_add:
            return True  # Nothing to add

        with open(cvinfo_path, 'a', encoding='utf-8') as f:
            for line in lines_to_add:
                f.write(f"\n{line}")

        app_logger.debug(f"Added to cvinfo: {', '.join(lines_to_add)}")
        return True
    except Exception as e:
        app_logger.error(f"Error writing cvinfo fields to {cvinfo_path}: {e}")
        return False


def get_issue_metadata(api, series_id: int, issue_number: str) -> Optional[Dict[str, Any]]:
    """
    Fetch issue metadata from Metron.

    Uses the "double fetch" pattern: first search for issue, then get full details.

    Args:
        api: Mokkari API client
        series_id: Metron series ID
        issue_number: Issue number (string to handle "10.1", "Annual 1", etc.)

    Returns:
        Full issue data dict, or None if not found
    """
    try:
        # Search for the issue within the series
        params = {
            "series_id": series_id,
            "number": issue_number
        }
        issues = api.issues_list(params)

        if not issues:
            app_logger.warning(f"Issue {issue_number} not found in Metron series {series_id}")
            return None

        # Get the full detailed metadata
        metron_issue_id = issues[0].id
        app_logger.info(f"Found Metron issue ID {metron_issue_id}, fetching full details...")
        details = api.issue(metron_issue_id)

        # Convert schema object to dict - try multiple methods
        # Pydantic v2 uses model_dump(), v1 uses dict()
        result = None
        if hasattr(details, 'model_dump'):
            app_logger.debug("Converting Metron response using model_dump()")
            result = details.model_dump()
        elif hasattr(details, 'dict'):
            app_logger.debug("Converting Metron response using dict()")
            result = details.dict()
        elif hasattr(details, 'json'):
            import json
            app_logger.debug("Converting Metron response using json()")
            result = json.loads(details.json())
        elif hasattr(details, '__dict__'):
            app_logger.debug("Converting Metron response using vars()")
            result = vars(details)
        else:
            app_logger.debug(f"Metron response type: {type(details)}")
            result = details

        # Log key fields to verify conversion
        if result and isinstance(result, dict):
            app_logger.debug(f"Metron data keys: {list(result.keys())}")
            app_logger.debug(f"Series: {result.get('series')}, Number: {result.get('number')}")

        return result

    except RateLimitError:
        raise
    except Exception as e:
        app_logger.error(f"Error fetching issue metadata from Metron: {e}")
        return None


def _get_attr(obj, key, default=None):
    """Helper to get attribute from dict or object."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def extract_credits_by_role(credits: List, role_names: List[str]) -> str:
    """
    Extract creator names for specific roles from credits list.

    Args:
        credits: List of credit dicts or objects with 'creator' and 'role' fields
        role_names: List of role names to match (e.g., ['Writer'])

    Returns:
        Comma-separated string of creator names
    """
    creators = []
    for credit in credits:
        roles = _get_attr(credit, 'role', [])
        if roles is None:
            roles = []
        for role in roles:
            role_name = _get_attr(role, 'name', '')
            if role_name is None:
                role_name = str(role)
            if role_name in role_names:
                creator_name = _get_attr(credit, 'creator', '')
                if creator_name and creator_name not in creators:
                    creators.append(creator_name)
    return ', '.join(creators)


def map_to_comicinfo(issue_data) -> Dict[str, Any]:
    """
    Map Metron issue data to ComicInfo.xml format.

    Args:
        issue_data: Issue data from Metron API (dict or object)

    Returns:
        Dictionary in ComicInfo.xml format
    """
    from datetime import datetime

    # Debug: log what we received
    app_logger.info(f"map_to_comicinfo received type: {type(issue_data)}")
    if isinstance(issue_data, dict):
        app_logger.info(f"map_to_comicinfo keys: {list(issue_data.keys())[:10]}...")

    # Parse cover_date for Year/Month/Day
    cover_date = _get_attr(issue_data, 'cover_date', '')
    year = None
    month = None
    day = None
    if cover_date:
        try:
            dt = datetime.strptime(str(cover_date), '%Y-%m-%d')
            year = dt.year
            month = dt.month
            day = dt.day
        except ValueError:
            # Try parsing just year
            try:
                year = int(str(cover_date)[:4])
            except (ValueError, TypeError):
                pass

    # Extract series info
    series = _get_attr(issue_data, 'series', {}) or {}
    series_name = _get_attr(series, 'name', '') or ''
    # Use year_began for Volume field (series start year, not volume number)
    year_began = _get_attr(series, 'year_began', None)

    # Extract genres from series
    genres = _get_attr(series, 'genres', []) or []
    genre_names = []
    for g in genres:
        name = _get_attr(g, 'name', '')
        if name:
            genre_names.append(name)
    genre_str = ', '.join(genre_names) if genre_names else None

    # Extract publisher
    publisher = _get_attr(issue_data, 'publisher', {}) or {}
    publisher_name = _get_attr(publisher, 'name', '') or ''

    # Extract credits
    credits = _get_attr(issue_data, 'credits', []) or []
    writer = extract_credits_by_role(credits, ['Writer'])
    penciller = extract_credits_by_role(credits, ['Penciller', 'Artist'])
    inker = extract_credits_by_role(credits, ['Inker'])
    colorist = extract_credits_by_role(credits, ['Colorist'])
    letterer = extract_credits_by_role(credits, ['Letterer'])
    cover_artist = extract_credits_by_role(credits, ['Cover'])

    # Extract characters
    characters = _get_attr(issue_data, 'characters', []) or []
    char_names = []
    for c in characters:
        name = _get_attr(c, 'name', '')
        if name:
            char_names.append(name)
    characters_str = ', '.join(char_names) if char_names else None

    # Extract teams
    teams = _get_attr(issue_data, 'teams', []) or []
    team_names = []
    for t in teams:
        name = _get_attr(t, 'name', '')
        if name:
            team_names.append(name)
    teams_str = ', '.join(team_names) if team_names else None

    # Get title from story_titles/name array (first element)
    # Mokkari model_dump() renames API "name" -> "story_titles" and "title" -> "collection_title"
    names = _get_attr(issue_data, 'story_titles', None) or _get_attr(issue_data, 'name', [])
    if isinstance(names, list) and names:
        title = names[0]
    elif isinstance(names, str):
        title = names
    else:
        title = None

    # Fall back to collection_title/title if story_titles is empty
    if not title:
        title = _get_attr(issue_data, 'collection_title', None) or _get_attr(issue_data, 'title', None) or None

    # Rating
    rating = _get_attr(issue_data, 'rating', {})
    age_rating = _get_attr(rating, 'name', None) if rating else None

    # Build notes
    resource_url = _get_attr(issue_data, 'resource_url', 'Unknown')
    modified = _get_attr(issue_data, 'modified', 'Unknown')
    notes = f"Metadata from Metron. Resource URL: {resource_url} — modified {modified}."

    comicinfo = {
        'Series': series_name,
        'Number': _get_attr(issue_data, 'number', None),
        'Volume': year_began,
        'Title': title,
        'Summary': _get_attr(issue_data, 'desc', None),
        'Publisher': publisher_name,
        'Year': year,
        'Month': month,
        'Day': day,
        'Writer': writer or None,
        'Penciller': penciller or None,
        'Inker': inker or None,
        'Colorist': colorist or None,
        'Letterer': letterer or None,
        'CoverArtist': cover_artist or None,
        'Characters': characters_str,
        'Teams': teams_str,
        'Genre': genre_str,
        'AgeRating': age_rating,
        'LanguageISO': 'en',
        'Manga': 'No',
        'Notes': notes,
        'PageCount': _get_attr(issue_data, 'page_count', None) or _get_attr(issue_data, 'page', None),
        'MetronId': _get_attr(issue_data, 'id', None),
    }

    # Remove None values
    result = {k: v for k, v in comicinfo.items() if v is not None}
    app_logger.info(f"map_to_comicinfo returning {len(result)} fields: {list(result.keys())}")
    return result


def get_series_id(cvinfo_path: str, api) -> Optional[int]:
    """
    Get Metron series ID from cvinfo, looking up by CV ID if needed.

    This is a convenience function that:
    1. Checks cvinfo for existing series_id
    2. If not found, extracts CV ID and looks up Metron series
    3. Updates cvinfo with the found Metron series ID

    Args:
        cvinfo_path: Path to cvinfo file
        api: Mokkari API client

    Returns:
        Metron series ID, or None if not found
    """
    # First, check if series_id already exists
    metron_id = parse_cvinfo_for_metron_id(cvinfo_path)
    if metron_id:
        app_logger.debug(f"Found existing series_id: {metron_id}")
        return metron_id

    # Not found, try to look up by ComicVine ID
    cv_id = parse_cvinfo_for_comicvine_id(cvinfo_path)
    if not cv_id:
        app_logger.warning("No ComicVine ID found in cvinfo")
        return None

    app_logger.info(f"Looking up Metron series by ComicVine ID: {cv_id}")
    metron_id = get_series_id_by_comicvine_id(api, cv_id)

    if metron_id:
        # Save to cvinfo for future use
        update_cvinfo_with_metron_id(cvinfo_path, metron_id)
        return metron_id

    return None


def fetch_and_map_issue(api, cvinfo_path: str, issue_number: str) -> Optional[Dict[str, Any]]:
    """
    Convenience function to fetch issue metadata and map to ComicInfo format.

    This combines get_series_id, get_issue_metadata, and map_to_comicinfo.
    Also saves publisher_name and start_year to cvinfo for future use.

    Args:
        api: Mokkari API client
        cvinfo_path: Path to cvinfo file
        issue_number: Issue number to fetch

    Returns:
        ComicInfo-formatted dict, or None if not found
    """
    # Get the Metron series ID
    series_id = get_series_id(cvinfo_path, api)
    if not series_id:
        app_logger.warning("Could not determine Metron series ID")
        return None

    # Fetch issue metadata
    issue_data = get_issue_metadata(api, series_id, issue_number)
    if not issue_data:
        return None

    # Extract publisher_name and start_year for cvinfo
    publisher = _get_attr(issue_data, 'publisher', {}) or {}
    publisher_name = _get_attr(publisher, 'name', None)
    series = _get_attr(issue_data, 'series', {}) or {}
    year_began = _get_attr(series, 'year_began', None)

    # Save to cvinfo for future use
    if publisher_name or year_began:
        write_cvinfo_fields(cvinfo_path, publisher_name, year_began)

    # Map to ComicInfo format
    return map_to_comicinfo(issue_data)


def calculate_comic_week(date_obj=None):
    """
    Calculate the comic week (Sunday to Saturday) for a given date.

    Args:
        date_obj: datetime object (defaults to now)

    Returns:
        tuple of (start_date_obj, end_date_obj)
    """
    if date_obj is None:
        date_obj = datetime.now()

    # If date_obj is a string, parse it
    if isinstance(date_obj, str):
        try:
            date_obj = datetime.strptime(date_obj, '%Y-%m-%d')
        except ValueError:
            app_logger.error(f"Invalid date string format: {date_obj}")
            date_obj = datetime.now()

    # Calculate start of week (Sunday)
    # Weekday: Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
    # To get Sunday: (weekday + 1) % 7 gives days since Sunday
    days_since_sunday = (date_obj.weekday() + 1) % 7
    start_of_week = date_obj - timedelta(days=days_since_sunday)

    # End of week is Saturday (6 days later)
    end_of_week = start_of_week + timedelta(days=6)

    return start_of_week, end_of_week


def get_releases(api, date_after: str, date_before: Optional[str] = None) -> List[Any]:
    """
    Fetch releases from Metron API within a date range.

    Args:
        api: Mokkari API client
        date_after: Start date (YYYY-MM-DD)
        date_before: End date (YYYY-MM-DD), optional. If None, fetches everything after start date.

    Returns:
        List of issue objects
    """
    try:
        if not api:
            return []

        params = {
            "store_date_range_after": date_after
        }
        if date_before:
            params["store_date_range_before"] = date_before

        app_logger.info(f"Fetching releases with params: {params}")

        # Note: Using issues_list matching existing patterns in this file
        results = api.issues_list(params)
        return results

    except RateLimitError:
        raise
    except Exception as e:
        app_logger.error(f"Error getting releases: {e}")
        return []

def get_all_issues_for_series(api, series_id):
    """
    Retrieves all issues associated with a specific series ID.
    """
    try:
        # Pass the series ID as a filter in the params dictionary
        params = {
            "series_id": series_id
        }

        app_logger.info(f"Fetching issues for series_id: {series_id} with params: {params}")
        series_issues = api.issues_list(params)

        return series_issues

    except RateLimitError:
        raise
    except Exception as e:
        app_logger.error(f"Error retrieving issues for series {series_id}: {e}")
        return []

def search_series_by_name(api, series_name: str, year: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Search Metron for a series by name, optionally filtering by year.

    Args:
        api: Mokkari API client
        series_name: Series name to search for
        year: Optional year to filter/rank results by year_began

    Returns:
        Dict with id, name, cv_id, publisher_name, year_began, or None if not found
    """
    try:
        if not api or not series_name:
            return None

        app_logger.info(f"Searching Metron for series: '{series_name}' (year: {year})")
        results = api.series_list({'name': series_name})

        if not results:
            app_logger.info(f"No Metron series found for '{series_name}'")
            return None

        # Convert results to list for sorting
        series_list = list(results)
        app_logger.info(f"Found {len(series_list)} Metron series matches")

        # If year provided, sort by closest year_began match
        if year and len(series_list) > 1:
            def year_distance(s):
                s_year = getattr(s, 'year_began', None)
                if s_year is None:
                    return 9999
                return abs(s_year - year)
            series_list = sorted(series_list, key=year_distance)

        # Take best match
        series = series_list[0]

        # Extract publisher info
        publisher = getattr(series, 'publisher', None)
        publisher_name = None
        if publisher:
            publisher_name = getattr(publisher, 'name', None)

        result = {
            'id': getattr(series, 'id', None),
            'name': getattr(series, 'name', '') or getattr(series, 'display_name', ''),
            'cv_id': getattr(series, 'cv_id', None),
            'publisher_name': publisher_name,
            'year_began': getattr(series, 'year_began', None)
        }

        app_logger.info(f"Best Metron match: {result['name']} ({result['year_began']}) - cv_id: {result['cv_id']}")
        return result

    except RateLimitError:
        raise
    except Exception as e:
        app_logger.error(f"Error searching Metron for series '{series_name}': {e}")
        return None


def get_series_details(api, series_id: int) -> Optional[Dict[str, Any]]:
    """
    Get full details for a Metron series including cv_id, publisher, year_began.

    Args:
        api: Mokkari API client
        series_id: Metron series ID

    Returns:
        Dict with id, cv_id, publisher_name, year_began, or None if not found
    """
    try:
        if not api or not series_id:
            return None

        # Get series details
        series = api.series(series_id)
        if series:
            publisher = getattr(series, 'publisher', None)
            publisher_name = None
            if publisher:
                publisher_name = getattr(publisher, 'name', None)

            result = {
                'id': series_id,
                'cv_id': getattr(series, 'cv_id', None),
                'publisher_name': publisher_name,
                'year_began': getattr(series, 'year_began', None)
            }
            app_logger.info(f"Metron series details: cv_id={result['cv_id']}, publisher={result['publisher_name']}, year={result['year_began']}")
            return result

        return None
    except RateLimitError:
        raise
    except Exception as e:
        app_logger.error(f"Error getting details for series {series_id}: {e}")
        return None


def get_series_cv_id(api, series_id: int) -> Optional[int]:
    """
    Get the ComicVine ID for a Metron series.

    Args:
        api: Mokkari API client
        series_id: Metron series ID

    Returns:
        ComicVine volume ID, or None if not found
    """
    details = get_series_details(api, series_id)
    return details.get('cv_id') if details else None


def add_cvinfo_url(cvinfo_path: str, cv_id: int) -> bool:
    """
    Add or update the ComicVine URL as the first line of a cvinfo file.

    Args:
        cvinfo_path: Path to the cvinfo file
        cv_id: ComicVine volume ID

    Returns:
        True if successful, False otherwise
    """
    try:
        cv_url = f"https://comicvine.gamespot.com/volume/4050-{cv_id}/"

        # Read existing content
        with open(cvinfo_path, 'r', encoding='utf-8') as f:
            content = f.read()

        # Check if URL already exists
        if f"4050-{cv_id}" in content:
            app_logger.debug(f"CV URL already exists in {cvinfo_path}")
            return True

        # Check if any CV URL exists (different ID)
        if "comicvine.gamespot.com/volume/4050-" in content:
            app_logger.warning(f"Different CV URL exists in {cvinfo_path}, not overwriting")
            return False

        # Prepend the URL to the content
        new_content = cv_url + '\n' + content

        with open(cvinfo_path, 'w', encoding='utf-8') as f:
            f.write(new_content)

        app_logger.info(f"Added CV URL to cvinfo: {cv_url}")
        return True

    except Exception as e:
        app_logger.error(f"Error adding CV URL to {cvinfo_path}: {e}")
        return False


def create_cvinfo_file(cvinfo_path: str, cv_id: Optional[int], series_id: int,
                       publisher_name: Optional[str] = None, start_year: Optional[int] = None) -> bool:
    """
    Create a cvinfo file with all available fields.

    Args:
        cvinfo_path: Path to create the cvinfo file
        cv_id: ComicVine volume ID (for URL)
        series_id: Metron series ID
        publisher_name: Publisher name
        start_year: Series start year (year_began)

    Returns:
        True if successful, False otherwise
    """
    try:
        lines = []

        # Add ComicVine URL if cv_id is available
        if cv_id:
            lines.append(f"https://comicvine.gamespot.com/volume/4050-{cv_id}/")

        # Add Metron series_id
        lines.append(f"series_id: {series_id}")

        # Add optional fields
        if publisher_name:
            lines.append(f"publisher_name: {publisher_name}")
        if start_year:
            lines.append(f"start_year: {start_year}")

        # Write to file
        with open(cvinfo_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

        app_logger.info(f"Created cvinfo file: {cvinfo_path}")
        return True

    except Exception as e:
        app_logger.error(f"Error creating cvinfo file {cvinfo_path}: {e}")
        return False


def scrobble_issue(api, metron_issue_id: int, date_read: str = None) -> bool:
    """
    Scrobble (mark as read) an issue on Metron.

    Args:
        api: Mokkari API client
        metron_issue_id: Metron issue ID to mark as read
        date_read: Optional ISO timestamp for when the issue was read

    Returns:
        True if scrobble succeeded, False otherwise
    """
    try:
        from mokkari.schemas.collection import ScrobbleRequest
    except ImportError:
        app_logger.warning("ScrobbleRequest not available in installed mokkari version")
        return False

    try:
        request = ScrobbleRequest(issue_id=metron_issue_id, date_read=date_read)
        response = api.collection_scrobble(request)
        return response is not None
    except RateLimitError:
        raise
    except Exception as e:
        app_logger.error(f"Failed to scrobble issue {metron_issue_id}: {e}")
        return False


def resolve_metron_issue_id(api, comic_path: str, issue_number: str = None) -> Optional[int]:
    """
    Get Metron issue ID from ComicInfo.xml or by looking up via series.

    Strategy:
    1. Check ComicInfo.xml for <MetronId> tag
    2. Find cvinfo in parent folder to get series_id
    2.5. If no series_id, search Metron by series name from ComicInfo.xml
         and create/update cvinfo for future lookups
    3. Use get_all_issues_for_series() and match by issue number

    Args:
        api: Mokkari API client
        comic_path: Path to the comic file (CBZ)
        issue_number: Optional issue number (from ComicInfo.xml or filename)

    Returns:
        Metron issue ID, or None if not resolved
    """
    import os

    comic_info = None
    parent_folder = os.path.dirname(comic_path)

    # Step 1: Check ComicInfo.xml for MetronId
    try:
        from comicinfo import read_comicinfo_from_zip
        if os.path.exists(comic_path) and comic_path.lower().endswith(('.cbz', '.zip')):
            comic_info = read_comicinfo_from_zip(comic_path)
            if comic_info:
                metron_id = comic_info.get('MetronId')
                if metron_id:
                    try:
                        return int(metron_id)
                    except (ValueError, TypeError):
                        pass
                # Also grab issue number from XML if not provided
                if not issue_number:
                    issue_number = comic_info.get('Number')
    except Exception as e:
        app_logger.warning(f"Could not read ComicInfo.xml for MetronId: {e}")

    if not issue_number:
        # Try extracting from filename as last resort
        from models.providers.base import extract_issue_number
        issue_number = extract_issue_number(os.path.basename(comic_path))

    if not issue_number:
        app_logger.debug(f"Cannot resolve Metron issue ID: no issue number for {comic_path}")
        return None

    try:
        # Step 2: Find cvinfo in parent folder to get series_id
        from models.comicvine import find_cvinfo_in_folder
        cvinfo_path = find_cvinfo_in_folder(parent_folder)
        series_id = None

        if cvinfo_path:
            series_id = parse_cvinfo_for_metron_id(cvinfo_path)

        # Step 2.5: Search Metron by series name from ComicInfo.xml
        if not series_id and comic_info:
            series_name = comic_info.get('Series')
            volume_year = comic_info.get('Volume')
            if series_name:
                try:
                    year = int(volume_year) if volume_year else None
                except (ValueError, TypeError):
                    year = None
                search_result = search_series_by_name(api, series_name, year)
                if search_result:
                    series_id = search_result['id']
                    # Persist to cvinfo for future lookups
                    if cvinfo_path:
                        update_cvinfo_with_metron_id(cvinfo_path, series_id)
                    else:
                        create_cvinfo_file(
                            os.path.join(parent_folder, 'cvinfo'),
                            search_result.get('cv_id'),
                            series_id,
                            search_result.get('publisher_name'),
                            search_result.get('year_began')
                        )
                    app_logger.info(f"Found Metron series {series_id} via name search for '{series_name}'")

        if not series_id:
            app_logger.debug(f"Could not resolve series_id for {comic_path}")
            return None

        # Step 3: Fetch all issues for the series and match by number
        all_issues = get_all_issues_for_series(api, series_id)
        if not all_issues:
            return None

        # Normalize issue number for comparison (strip leading zeros)
        target = str(issue_number).strip().lstrip('0') or '0'

        for issue in all_issues:
            issue_num = getattr(issue, 'number', None) or (issue.get('number') if isinstance(issue, dict) else None)
            if issue_num is not None:
                candidate = str(issue_num).strip().lstrip('0') or '0'
                if candidate == target:
                    issue_id = getattr(issue, 'id', None) or (issue.get('id') if isinstance(issue, dict) else None)
                    if issue_id:
                        app_logger.info(f"Resolved Metron issue ID {issue_id} for #{issue_number} in series {series_id}")
                        return int(issue_id)

        app_logger.debug(f"Could not match issue #{issue_number} in series {series_id}")
        return None

    except RateLimitError:
        raise
    except Exception as e:
        app_logger.warning(f"Error resolving Metron issue ID: {e}")
        return None