"""
ComicVine API integration for comic metadata retrieval.

This module provides functions to search for and retrieve comic metadata from ComicVine API,
including volume (series) search, issue search, and metadata mapping to ComicInfo.xml format.
"""

from app_logging import app_logger
from datetime import datetime, date
from typing import Optional, Dict, List, Any
import os
import shutil
import re
from cbz_ops.rename import rename_comic_from_metadata

try:
    from simyan.comicvine import Comicvine
    from simyan.sqlite_cache import SQLiteCache
    from simyan.comicvine import ComicvineResource
    SIMYAN_AVAILABLE = True
except ImportError:
    SIMYAN_AVAILABLE = False

def is_simyan_available() -> bool:
    """Check if the Simyan library is available."""
    return SIMYAN_AVAILABLE


def search_volumes(api_key: str, series_name: str, year: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    Search for comic volumes (series) on ComicVine.

    Args:
        api_key: ComicVine API key
        series_name: Name of the series to search for
        year: Optional year to filter/rank results

    Returns:
        List of volume dictionaries with id, name, start_year, publisher info

    Raises:
        Exception: If API request fails
    """
    if not SIMYAN_AVAILABLE:
        raise Exception("Simyan library not installed. Install with: pip install simyan")

    try:
        app_logger.info(f"Searching ComicVine for volume: '{series_name}' (year: {year})")

        # Initialize ComicVine API client
        cv = Comicvine(api_key=api_key)

        # Search for volumes using fuzzy search
        volumes = cv.search(resource=ComicvineResource.VOLUME, query=series_name)

        if not volumes:
            app_logger.info(f"No volumes found for '{series_name}'")
            return []

        # Convert to simple dict format
        results = []
        for vol in volumes:
            # Get image URL and convert to string (Pydantic HttpUrl isn't JSON serializable)
            image_url = None
            if hasattr(vol, 'image') and vol.image and hasattr(vol.image, 'thumbnail'):
                image_url = str(vol.image.thumbnail) if vol.image.thumbnail else None

            vol_dict = {
                "id": vol.id,
                "name": vol.name,
                "start_year": getattr(vol, 'start_year', None),
                "publisher_name": vol.publisher.name if hasattr(vol, 'publisher') and vol.publisher else None,
                "count_of_issues": getattr(vol, 'count_of_issues', None),
                "image_url": image_url,
                "description": getattr(vol, 'description', None)
            }
            # Truncate description if present
            if vol_dict["description"] and len(vol_dict["description"]) > 200:
                vol_dict["description"] = vol_dict["description"][:200] + "..."
            results.append(vol_dict)

        app_logger.info(f"Found {len(results)} volumes")

        # If year is provided, sort by closest year match
        if year:
            results = _rank_volumes_by_year(results, year)

        return results

    except Exception as e:
        app_logger.error(f"Error searching ComicVine volumes: {str(e)}")
        raise


def _rank_volumes_by_year(volumes: List[Dict[str, Any]], target_year: int) -> List[Dict[str, Any]]:
    """
    Rank volumes by how close their start_year is to the target year.

    Args:
        volumes: List of volume dictionaries
        target_year: Target year to match

    Returns:
        Sorted list of volumes (closest year first)
    """
    def year_distance(vol):
        if not vol.get('start_year'):
            return 9999  # Put volumes without year at the end
        return abs(vol['start_year'] - target_year)

    return sorted(volumes, key=year_distance)


def get_issue_by_number(api_key: str, volume_id: int, issue_number: str, year: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Get a specific issue from a volume by issue number.

    Args:
        api_key: ComicVine API key
        volume_id: ComicVine volume ID
        issue_number: Issue number (can be "1", "12.1", etc.)
        year: Optional publication year for filtering

    Returns:
        Issue dictionary with metadata, or None if not found

    Raises:
        Exception: If API request fails
    """
    if not SIMYAN_AVAILABLE:
        raise Exception("Simyan library not installed. Install with: pip install simyan")

    try:
        app_logger.info(f"Searching for issue #{issue_number} in volume {volume_id} (year: {year})")

        # Initialize ComicVine API client
        cv = Comicvine(api_key=api_key)

        # Get issues from the volume
        # Build filter string
        filter_str = f"volume:{volume_id},issue_number:{issue_number}"

        issues = cv.list_issues(params={"filter": filter_str})

        if not issues:
            app_logger.info(f"No issues found for volume {volume_id}, issue #{issue_number}")
            return None

        # If year is provided and multiple issues found, filter by year
        if year and len(issues) > 1:
            issues = [issue for issue in issues if _extract_year_from_date(issue.cover_date) == year]

        # If still multiple issues, take the first one
        if not issues:
            app_logger.info(f"No issues found matching year {year}")
            return None

        basic_issue = issues[0]

        # Fetch full issue details to get all metadata (credits, characters, etc.)
        issue = cv.get_issue(basic_issue.id)

        # Convert to dict format
        issue_dict = _issue_to_dict(issue)

        app_logger.info(f"Found issue: {issue_dict['name']} (ID: {issue_dict['id']})")

        return issue_dict

    except Exception as e:
        app_logger.error(f"Error getting ComicVine issue: {str(e)}")
        raise


def _extract_year_from_date(date_str: Optional[str]) -> Optional[int]:
    """
    Extract year from a date string.

    Args:
        date_str: Date string in format "YYYY-MM-DD"

    Returns:
        Year as integer, or None if parsing fails
    """
    if not date_str:
        return None

    try:
        # Try parsing as YYYY-MM-DD
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        return date_obj.year
    except (ValueError, TypeError):
        # Try extracting just the year
        try:
            return int(date_str.split("-")[0])
        except (ValueError, IndexError, AttributeError):
            return None


def _issue_to_dict(issue: Any) -> Dict[str, Any]:
    """
    Convert a Simyan Issue object to a dictionary.

    Args:
        issue: Simyan Issue object

    Returns:
        Dictionary with issue metadata
    """
    # Parse cover date or store date (prefer cover_date, fallback to store_date)
    year = None
    month = None
    day = None
    date_str = None

    # Try cover_date first (preferred), then store_date as fallback
    date_value = getattr(issue, 'cover_date', None)
    if not date_value:
        date_value = getattr(issue, 'store_date', None)

    if date_value:
        # Handle datetime objects, date objects, and strings
        if isinstance(date_value, (datetime, date)):
            # Already a datetime or date object
            year = date_value.year
            month = date_value.month
            day = date_value.day
            date_str = date_value.strftime("%Y-%m-%d")
            app_logger.info(f"DEBUG _issue_to_dict: date/datetime object - year={year}, month={month}, day={day}")
        elif isinstance(date_value, str):
            # String format
            date_str = date_value
            year = _extract_year_from_date(date_str)
            app_logger.info(f"DEBUG _issue_to_dict: date_str={date_str}, extracted year={year}")
            try:
                date_obj = datetime.strptime(date_str, "%Y-%m-%d")
                month = date_obj.month
                day = date_obj.day
                app_logger.info(f"DEBUG _issue_to_dict: parsed month={month}, day={day}")
            except (ValueError, TypeError):
                app_logger.warning(f"DEBUG _issue_to_dict: Failed to parse date_str={date_str}")
                pass
        else:
            # Unknown type - try converting to string
            date_str = str(date_value)
            year = _extract_year_from_date(date_str)
            app_logger.info(f"DEBUG _issue_to_dict: unknown type {type(date_value)}, converted to string={date_str}, year={year}")

    # Extract person credits (creators)
    writers = []
    pencillers = []
    inkers = []
    colorists = []
    letterers = []
    cover_artists = []

    creators = getattr(issue, 'creators', None)
    if creators:
        for credit in creators:
            name = credit.name if hasattr(credit, 'name') else str(credit)
            role = credit.roles.lower() if hasattr(credit, 'roles') else ""

            if "writer" in role or "script" in role:
                writers.append(name)
            elif "pencil" in role:
                pencillers.append(name)
            elif "ink" in role:
                inkers.append(name)
            elif "color" in role:
                colorists.append(name)
            elif "letter" in role:
                letterers.append(name)
            elif "cover" in role:
                cover_artists.append(name)

    # Extract character names
    character_list = []
    characters = getattr(issue, 'characters', None)
    if characters:
        character_list = [char.name if hasattr(char, 'name') else str(char) for char in characters]

    # Extract teams
    team_list = []
    teams = getattr(issue, 'teams', None)
    if teams:
        team_list = [team.name if hasattr(team, 'name') else str(team) for team in teams]

    # Extract locations
    location_list = []
    locations = getattr(issue, 'locations', None)
    if locations:
        location_list = [loc.name if hasattr(loc, 'name') else str(loc) for loc in locations]

    # Extract story arc
    story_arc = None
    story_arcs = getattr(issue, 'story_arcs', None)
    if story_arcs and len(story_arcs) > 0:
        story_arc = story_arcs[0].name if hasattr(story_arcs[0], 'name') else None

    # Get volume info
    volume = getattr(issue, 'volume', None)
    volume_name = volume.name if volume and hasattr(volume, 'name') else None
    volume_id = volume.id if volume and hasattr(volume, 'id') else None
    publisher = None
    if volume and hasattr(volume, 'publisher') and volume.publisher:
        publisher = volume.publisher.name if hasattr(volume.publisher, 'name') else None

    # Get image URL (convert HttpUrl to string for JSON serialization)
    image = getattr(issue, 'image', None)
    image_url = str(image.thumbnail) if image and hasattr(image, 'thumbnail') and image.thumbnail else None

    return {
        "id": issue.id,
        "name": getattr(issue, 'name', None),  # BasicIssue.name -> Title
        "issue_number": getattr(issue, 'number', None),  # BasicIssue.number -> Number
        "volume_name": volume_name,  # BasicIssue.volume.name -> Series
        "volume_id": volume_id,
        "publisher": publisher,
        "cover_date": date_str,  # BasicIssue.cover_date or store_date
        "year": year,  # Parsed from cover_date or store_date -> Year
        "month": month,  # Parsed from cover_date or store_date -> Month
        "day": day,  # Parsed from cover_date or store_date -> Day
        "description": getattr(issue, 'description', None),  # BasicIssue.description -> Summary
        "image_url": image_url,
        "page_count": None,  # ComicVine doesn't always provide page count
        "writers": writers,
        "pencillers": pencillers,
        "inkers": inkers,
        "colorists": colorists,
        "letterers": letterers,
        "cover_artists": cover_artists,
        "characters": character_list,
        "teams": team_list,
        "locations": location_list,
        "story_arc": story_arc,
    }


def map_to_comicinfo(issue_data: Dict[str, Any], volume_data: Optional[Dict[str, Any]] = None, start_year: Optional[int] = None) -> Dict[str, Any]:
    """
    Map ComicVine issue data to ComicInfo.xml format.

    Args:
        issue_data: Issue data from ComicVine
        volume_data: Optional volume data for additional context
        start_year: Optional series start year to use for Volume field (preferred over publication year)

    Returns:
        Dictionary in ComicInfo.xml format
    """
    # Use volume name from volume_data if available, otherwise from issue_data
    series_name = volume_data.get('name') if volume_data else issue_data.get('volume_name')

    # Get publisher - prefer volume_data (from search results), fallback to issue_data
    publisher = None
    if volume_data and volume_data.get('publisher_name'):
        publisher = volume_data.get('publisher_name')
    else:
        publisher = issue_data.get('publisher')

    # Get volume ID for Notes field
    from datetime import datetime
    volume_id = volume_data.get('id') if volume_data else issue_data.get('volume_id')
    current_date = datetime.now().strftime('%Y-%m-%d')
    if volume_id:
        notes = f'Metadata from ComicVine CVDB. Volume ID: {volume_id} — retrieved {current_date}.'
    else:
        notes = f'Metadata from ComicVine CVDB — retrieved {current_date}.'

    # Append cover_date or store_date if available
    if issue_data.get('cover_date'):
        notes += f' Cover/Store Date: {issue_data.get("cover_date")}.'

    comicinfo = {
        'Series': series_name,
        'Number': issue_data.get('issue_number'),
        # Use start_year (series start year) for Volume field, fallback to publication year
        'Volume': start_year if start_year else (volume_data.get('start_year') if volume_data else issue_data.get('year')),
        'Title': issue_data.get('name'),
        'Publisher': publisher,
        'Summary': issue_data.get('description'),
        'Year': issue_data.get('year'),
        'Month': issue_data.get('month'),
        'Day': issue_data.get('day'),
        'Writer': ', '.join(issue_data.get('writers', [])) if issue_data.get('writers') else None,
        'Penciller': ', '.join(issue_data.get('pencillers', [])) if issue_data.get('pencillers') else None,
        'Inker': ', '.join(issue_data.get('inkers', [])) if issue_data.get('inkers') else None,
        'Colorist': ', '.join(issue_data.get('colorists', [])) if issue_data.get('colorists') else None,
        'Letterer': ', '.join(issue_data.get('letterers', [])) if issue_data.get('letterers') else None,
        'CoverArtist': ', '.join(issue_data.get('cover_artists', [])) if issue_data.get('cover_artists') else None,
        'Characters': ', '.join(issue_data.get('characters', [])) if issue_data.get('characters') else None,
        'Teams': ', '.join(issue_data.get('teams', [])) if issue_data.get('teams') else None,
        'Locations': ', '.join(issue_data.get('locations', [])) if issue_data.get('locations') else None,
        'StoryArc': issue_data.get('story_arc'),
        'PageCount': issue_data.get('page_count'),
        'LanguageISO': 'en',  # ComicVine is primarily English content
        'Notes': notes,
        'Count': None,  # Not needed per requirements
    }

    # Debug logging for date fields
    app_logger.info(f"DEBUG map_to_comicinfo: cover_date={issue_data.get('cover_date')}, year={issue_data.get('year')}, month={issue_data.get('month')}, day={issue_data.get('day')}")

    # Remove None values
    return {k: v for k, v in comicinfo.items() if v is not None}


def search_and_get_metadata(api_key: str, series_name: str, issue_number: str, year: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    High-level function to search for a series and get issue metadata.

    Args:
        api_key: ComicVine API key
        series_name: Name of the series
        issue_number: Issue number
        year: Optional year for better matching

    Returns:
        Dictionary with metadata in ComicInfo.xml format, or None if not found

    Raises:
        Exception: If API request fails
    """
    try:
        # Search for volumes
        volumes = search_volumes(api_key, series_name, year)

        if not volumes:
            app_logger.info(f"No volumes found for '{series_name}'")
            return None

        # Auto-select first volume (already sorted by year if provided)
        selected_volume = volumes[0]
        app_logger.info(f"Auto-selected volume: {selected_volume['name']} ({selected_volume['start_year']})")

        # Get the issue
        issue_data = get_issue_by_number(api_key, selected_volume['id'], issue_number, year)

        if not issue_data:
            logger.info(f"Issue #{issue_number} not found in volume {selected_volume['name']}")
            return None

        # Map to ComicInfo format
        comicinfo = map_to_comicinfo(issue_data, selected_volume)

        # Add image URL for UI display
        comicinfo['_image_url'] = issue_data.get('image_url')
        comicinfo['_volume_matches'] = volumes  # For showing alternatives if needed

        return comicinfo

    except Exception as e:
        app_logger.error(f"Error in search_and_get_metadata: {str(e)}")
        raise


def auto_move_file(file_path: str, volume_data: Dict[str, Any], config: Dict[str, Any]) -> Optional[str]:
    """
    Automatically move a file to an organized location based on the custom move pattern.

    Args:
        file_path: Current absolute path to the file
        volume_data: Volume data containing 'name', 'start_year', 'publisher_name'
        config: Flask app config with ENABLE_AUTO_MOVE and CUSTOM_MOVE_PATTERN

    Returns:
        New file path if moved successfully, None if not moved

    Raises:
        Exception: If file move fails
    """
    try:
        # Check if auto-move is enabled
        if not config.get("ENABLE_AUTO_MOVE", False):
            app_logger.debug("Auto-move is disabled in config")
            return None

        # Get the custom move pattern
        move_pattern = config.get("CUSTOM_MOVE_PATTERN", "")
        if not move_pattern:
            app_logger.warning("CUSTOM_MOVE_PATTERN is empty, skipping auto-move")
            return None

        # Extract metadata values for pattern replacement
        series_name = volume_data.get('name', '')
        start_year = str(volume_data.get('start_year', '')) if volume_data.get('start_year') else ''
        publisher = volume_data.get('publisher_name', '')

        # Log the metadata values
        app_logger.info(f"📦 Auto-move preparation - series_name: '{series_name}', start_year: '{start_year}', publisher: '{publisher}'")

        # Replace pattern placeholders with actual values
        folder_structure = move_pattern
        folder_structure = folder_structure.replace('{series_name}', series_name)
        folder_structure = folder_structure.replace('{start_year}', start_year)
        folder_structure = folder_structure.replace('{publisher}', publisher)

        # Handle other optional placeholders that might be in the pattern
        # These won't have values from volume data, so we'll just remove them or keep them as-is
        folder_structure = folder_structure.replace('{volume_number}', '')
        folder_structure = folder_structure.replace('{issue_number}', '')

        # Clean up any double slashes or trailing slashes
        folder_structure = folder_structure.replace('//', '/').strip('/')

        app_logger.info(f"📂 Computed folder structure: '{folder_structure}'")

        # Construct the target directory path
        # Base directory is /data
        base_dir = '/data'
        target_dir = os.path.join(base_dir, folder_structure)

        app_logger.info(f"🎯 Target directory: '{target_dir}'")

        # Create target directory if it doesn't exist
        os.makedirs(target_dir, exist_ok=True)
        app_logger.info(f"✅ Target directory created/verified: '{target_dir}'")

        # Get the filename from the current path
        filename = os.path.basename(file_path)

        # Construct the new file path
        new_file_path = os.path.join(target_dir, filename)

        # Check if source file exists
        if not os.path.exists(file_path):
            app_logger.error(f"❌ Source file does not exist: '{file_path}'")
            return None

        # Check if a file with the same name already exists at the target
        if os.path.exists(new_file_path):
            app_logger.warning(f"⚠️ File already exists at target location: '{new_file_path}', skipping move")
            return None

        # Move the file
        app_logger.info(f"🚚 Moving file from '{file_path}' to '{new_file_path}'")
        shutil.move(file_path, new_file_path)
        app_logger.info(f"✅ File successfully moved to: '{new_file_path}'")

        return new_file_path

    except Exception as e:
        app_logger.error(f"❌ Error during auto-move: {str(e)}")
        import traceback
        app_logger.error(f"Traceback: {traceback.format_exc()}")
        raise


def get_metadata_by_volume_id(api_key: str, volume_id: int, issue_number: str, year: Optional[int] = None, start_year: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    Get issue metadata using a known volume ID (from cvinfo file).

    Args:
        api_key: ComicVine API key
        volume_id: ComicVine volume ID (extracted from cvinfo URL)
        issue_number: Issue number to look up
        year: Optional year for filtering
        start_year: Optional series start year to use for Volume field

    Returns:
        Dictionary with metadata in ComicInfo.xml format, or None if not found
    """
    try:
        # Get the issue directly using volume_id
        issue_data = get_issue_by_number(api_key, volume_id, issue_number, year)

        if not issue_data:
            return None

        # Map to ComicInfo format (volume_data=None since we don't have full volume info)
        comicinfo = map_to_comicinfo(issue_data, None, start_year=start_year)
        comicinfo['_image_url'] = issue_data.get('image_url')

        return comicinfo
    except Exception as e:
        app_logger.error(f"Error in get_metadata_by_volume_id: {str(e)}")
        return None


def parse_cvinfo_volume_id(cvinfo_path: str) -> Optional[int]:
    """
    Parse a cvinfo file and extract the ComicVine volume ID.

    cvinfo file contains a URL like: https://comicvine.gamespot.com/avengers/4050-150431/
    The volume ID is the number after '4050-' (e.g., 150431)

    Args:
        cvinfo_path: Path to the cvinfo file

    Returns:
        Volume ID as integer, or None if not found/parseable
    """
    import re

    try:
        with open(cvinfo_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()

        # Match pattern: 4050-{volume_id}
        match = re.search(r'/4050-(\d+)', content)
        if match:
            return int(match.group(1))

        return None
    except Exception as e:
        app_logger.error(f"Error parsing cvinfo file {cvinfo_path}: {e}")
        return None


def find_cvinfo_in_folder(folder_path: str) -> Optional[str]:
    """
    Look for a cvinfo file in a folder (case-insensitive).

    Args:
        folder_path: Path to the folder to search

    Returns:
        Full path to cvinfo file if found, None otherwise
    """
    try:
        for item in os.listdir(folder_path):
            if item.lower() == 'cvinfo':
                return os.path.join(folder_path, item)
        return None
    except Exception as e:
        app_logger.error(f"Error searching for cvinfo in {folder_path}: {e}")
        return None


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
        # Read existing content
        existing = read_cvinfo_fields(cvinfo_path)

        # Determine what needs to be added
        lines_to_add = []
        if publisher_name and not existing['publisher_name']:
            lines_to_add.append(f"publisher_name: {publisher_name}")
        if start_year and not existing['start_year']:
            lines_to_add.append(f"start_year: {start_year}")

        if not lines_to_add:
            return True  # Nothing to add

        # Append to file
        with open(cvinfo_path, 'a', encoding='utf-8') as f:
            for line in lines_to_add:
                f.write(f"\n{line}")

        app_logger.debug(f"Added to cvinfo: {', '.join(lines_to_add)}")
        return True

    except Exception as e:
        app_logger.error(f"Error writing cvinfo fields to {cvinfo_path}: {e}")
        return False


def get_volume_details(api_key: str, volume_id: int) -> Dict[str, Any]:
    """
    Fetch volume details from ComicVine including publisher and start_year.

    Args:
        api_key: ComicVine API key
        volume_id: ComicVine volume ID

    Returns:
        Dict with 'publisher_name' and 'start_year' keys
    """
    result = {'publisher_name': None, 'start_year': None}

    if not SIMYAN_AVAILABLE:
        app_logger.warning("Simyan library not available for volume details lookup")
        return result

    try:
        app_logger.info(f"Fetching volume details for volume ID: {volume_id}")
        cv = Comicvine(api_key=api_key)
        volume = cv.get_volume(volume_id)

        if volume:
            result['start_year'] = getattr(volume, 'start_year', None)
            if hasattr(volume, 'publisher') and volume.publisher:
                result['publisher_name'] = getattr(volume.publisher, 'name', None)

            app_logger.info(f"Volume details: publisher={result['publisher_name']}, start_year={result['start_year']}")

        return result

    except Exception as e:
        app_logger.error(f"Error fetching volume details for {volume_id}: {e}")
        return result


# Re-export from shared provider base for backward compatibility
from models.providers.base import extract_issue_number  # noqa: F811


def add_comicinfo_to_archive(file_path: str, xml_content) -> bool:
    """
    Add or update ComicInfo.xml in a CBZ archive.

    Args:
        file_path: Path to the CBZ file
        xml_content: XML content to add (str or bytes)

    Returns:
        True on success, False on failure
    """
    import zipfile
    import tempfile

    temp_path = None
    try:
        # Create temp file
        temp_fd, temp_path = tempfile.mkstemp(suffix='.cbz')
        os.close(temp_fd)

        with zipfile.ZipFile(file_path, 'r') as zin:
            with zipfile.ZipFile(temp_path, 'w', zipfile.ZIP_DEFLATED) as zout:
                for item in zin.infolist():
                    # Skip existing ComicInfo.xml
                    if item.filename.lower() == 'comicinfo.xml':
                        continue
                    zout.writestr(item, zin.read(item.filename))

                # Add new ComicInfo.xml - handle both str and bytes
                if isinstance(xml_content, bytes):
                    zout.writestr('ComicInfo.xml', xml_content)
                else:
                    zout.writestr('ComicInfo.xml', xml_content.encode('utf-8'))

        # Replace original with temp
        shutil.move(temp_path, file_path)
        return True

    except Exception as e:
        app_logger.error(f"Error adding ComicInfo.xml to {file_path}: {e}")
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
        return False


def generate_comicinfo_xml(issue_data: Dict[str, Any]) -> bytes:
    """
    Generate ComicInfo.xml content from issue metadata.

    Args:
        issue_data: Dictionary with ComicInfo fields

    Returns:
        XML content as bytes
    """
    import xml.etree.ElementTree as ET
    import io

    root = ET.Element("ComicInfo")

    def add(tag, value):
        if value is not None and str(value).strip():
            ET.SubElement(root, tag).text = str(value)

    # Basic fields
    add("Title", issue_data.get("Title"))
    add("Series", issue_data.get("Series"))

    # Number field
    num = issue_data.get("Number")
    if num is not None and str(num).strip():
        try:
            # Format as integer for whole numbers, preserve original string for decimals
            num_str = str(num).strip()
            if num_str.replace(".", "", 1).isdigit():
                if "." in num_str:
                    num_val = float(num_str)
                    if num_val == int(num_val):
                        add("Number", str(int(num_val)))
                    else:
                        add("Number", num_str)
                else:
                    add("Number", str(int(num_str)))
            else:
                add("Number", num_str)
        except (ValueError, TypeError):
            add("Number", str(num))

    # Volume
    vol = issue_data.get("Volume")
    if vol is not None and str(vol).strip():
        try:
            add("Volume", str(int(vol)))
        except (ValueError, TypeError):
            add("Volume", str(vol))

    add("Summary", issue_data.get("Summary"))

    # Dates
    if issue_data.get("Year"):
        try:
            add("Year", str(int(issue_data["Year"])))
        except (ValueError, TypeError):
            pass
    if issue_data.get("Month"):
        try:
            m = int(issue_data["Month"])
            if 1 <= m <= 12:
                add("Month", str(m))
        except (ValueError, TypeError):
            pass
    if issue_data.get("Day"):
        try:
            d = int(issue_data["Day"])
            if 1 <= d <= 31:
                add("Day", str(d))
        except (ValueError, TypeError):
            pass

    # Credits
    add("Writer", issue_data.get("Writer"))
    add("Penciller", issue_data.get("Penciller"))
    add("Inker", issue_data.get("Inker"))
    add("Colorist", issue_data.get("Colorist"))
    add("Letterer", issue_data.get("Letterer"))
    add("CoverArtist", issue_data.get("CoverArtist"))

    # Publisher
    add("Publisher", issue_data.get("Publisher"))

    # Characters/Teams/Locations
    add("Characters", issue_data.get("Characters"))
    add("Teams", issue_data.get("Teams"))
    add("Locations", issue_data.get("Locations"))
    add("StoryArc", issue_data.get("StoryArc"))

    # Language
    add("LanguageISO", issue_data.get("LanguageISO") or "en")

    # Page count
    if issue_data.get("PageCount"):
        try:
            add("PageCount", str(int(issue_data["PageCount"])))
        except (ValueError, TypeError):
            pass

    # Notes
    add("Notes", issue_data.get("Notes"))

    # Metron ID (for scrobble support)
    add("MetronId", issue_data.get("MetronId"))

    # Serialize as UTF-8 bytes
    ET.indent(root)
    tree = ET.ElementTree(root)
    buf = io.BytesIO()
    tree.write(buf, encoding="utf-8", xml_declaration=True)
    return buf.getvalue()


def auto_fetch_metadata_for_folder(folder_path: str, api_key: str, target_file: str = None) -> Dict[str, Any]:
    """
    Auto-fetch ComicVine metadata for comics in a folder using cvinfo.

    Processes all comic files in the folder that don't have meaningful comicinfo.xml.
    Files are processed one at a time consecutively (API rate limiting).

    Args:
        folder_path: Path to the folder containing cvinfo and comic files
        api_key: ComicVine API key
        target_file: Optional specific file to prioritize (just moved)

    Returns:
        Dict with 'processed', 'skipped', 'errors' counts and 'details' list
    """
    from comicinfo import read_comicinfo_from_zip
    import time

    result = {'processed': 0, 'skipped': 0, 'errors': 0, 'details': []}

    # Find cvinfo file
    cvinfo_path = find_cvinfo_in_folder(folder_path)
    if not cvinfo_path:
        app_logger.debug(f"No cvinfo file found in {folder_path}")
        return result

    # Parse volume ID
    volume_id = parse_cvinfo_volume_id(cvinfo_path)
    if not volume_id:
        app_logger.warning(f"Could not extract volume ID from {cvinfo_path}")
        return result

    app_logger.info(f"Found cvinfo with volume ID: {volume_id}")

    # Read cvinfo fields for publisher_name and start_year
    cvinfo_fields = read_cvinfo_fields(cvinfo_path)
    start_year = cvinfo_fields.get('start_year')
    publisher_name = cvinfo_fields.get('publisher_name')

    # If not present in cvinfo, fetch from ComicVine and save
    if not start_year or not publisher_name:
        app_logger.info("Fetching volume details from ComicVine to get publisher/start_year")
        volume_details = get_volume_details(api_key, volume_id)

        if volume_details.get('start_year') or volume_details.get('publisher_name'):
            # Update local vars
            if not start_year and volume_details.get('start_year'):
                start_year = volume_details['start_year']
            if not publisher_name and volume_details.get('publisher_name'):
                publisher_name = volume_details['publisher_name']

            # Save to cvinfo for future use
            write_cvinfo_fields(cvinfo_path, publisher_name, start_year)

    # Get list of comic files to process
    comic_files = []

    # If target_file is specified, only process that file (not all files in folder)
    if target_file:
        if os.path.isfile(target_file) and target_file.lower().endswith(('.cbz', '.cbr')):
            comic_files = [target_file]
        else:
            app_logger.debug(f"Target file not a valid comic: {target_file}")
            return result
    else:
        # No target file - process all comics in folder (e.g., manual batch operation)
        for item in os.listdir(folder_path):
            item_path = os.path.join(folder_path, item)
            if os.path.isfile(item_path) and item.lower().endswith(('.cbz', '.cbr')):
                comic_files.append(item_path)

    for file_path in comic_files:
        try:
            # Check if file already has meaningful metadata
            existing = read_comicinfo_from_zip(file_path)
            existing_notes = existing.get('Notes', '').strip()

            # Skip if has metadata, unless it's just Amazon scraped data
            if existing_notes and 'Scraped metadata from Amazon' not in existing_notes:
                app_logger.debug(f"Skipping {file_path} - already has metadata")
                result['skipped'] += 1
                result['details'].append({'file': file_path, 'status': 'skipped', 'reason': 'has metadata'})
                continue

            # Extract issue number from filename
            issue_number = extract_issue_number(os.path.basename(file_path))
            if not issue_number:
                app_logger.warning(f"Could not extract issue number from {file_path}")
                result['errors'] += 1
                result['details'].append({'file': file_path, 'status': 'error', 'reason': 'no issue number'})
                continue

            # Fetch metadata from ComicVine (pass start_year for Volume field)
            metadata = get_metadata_by_volume_id(api_key, volume_id, issue_number, start_year=start_year)

            if not metadata:
                app_logger.warning(f"No metadata found for {file_path}, issue #{issue_number}")
                result['errors'] += 1
                result['details'].append({'file': file_path, 'status': 'error', 'reason': 'not found on ComicVine'})
                continue

            # Generate and add ComicInfo.xml to the file
            xml_content = generate_comicinfo_xml(metadata)
            if add_comicinfo_to_archive(file_path, xml_content):
                app_logger.debug(f"Added metadata to {file_path}")
                result['processed'] += 1

                # Auto-rename if enabled
                from cbz_ops.rename import rename_comic_from_metadata
                new_path, was_renamed = rename_comic_from_metadata(file_path, metadata)
                if was_renamed:
                    result['details'].append({'file': file_path, 'status': 'success', 'renamed_to': new_path})
                else:
                    result['details'].append({'file': file_path, 'status': 'success'})
            else:
                result['errors'] += 1
                result['details'].append({'file': file_path, 'status': 'error', 'reason': 'failed to add XML'})

            # Rate limiting - wait between API calls
            time.sleep(1)

        except Exception as e:
            app_logger.error(f"Error processing {file_path}: {e}")
            result['errors'] += 1
            result['details'].append({'file': file_path, 'status': 'error', 'reason': str(e)})

    return result
