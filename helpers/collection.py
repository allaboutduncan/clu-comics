import os
import re
from config import config
from app_logging import app_logger


def generate_filename_pattern(custom_pattern, series_name, issue_number):
    """
    Convert CUSTOM_RENAME_PATTERN to a precise regex for matching a specific issue.

    Pattern placeholders:
    - {series_name} -> matches the series name (flexible whitespace/case)
    - {issue_number} -> matches the issue number (with optional leading zeros)
    - {year} -> matches any 4-digit year

    Args:
        custom_pattern: The rename pattern from config (e.g., "{series_name} {issue_number} ({year})")
        series_name: The series name to match
        issue_number: The issue number to match

    Returns:
        Compiled regex pattern or None if pattern is invalid
    """

    if not custom_pattern or not series_name:
        return None

    try:
        # First, escape literal parentheses in the custom pattern BEFORE substituting
        # This handles patterns like "{series_name} {issue_number} ({year})"
        # The ( ) around {year} should become \( \) in the final regex

        # Use placeholders to protect our variable markers
        pattern = custom_pattern
        pattern = pattern.replace('{series_name}', '<<<SERIES>>>')
        pattern = pattern.replace('{issue_number}', '<<<ISSUE>>>')
        pattern = pattern.replace('{year}', '<<<YEAR>>>')

        # Now escape any remaining literal parentheses
        pattern = pattern.replace('(', r'\(').replace(')', r'\)')

        # Handle "The " prefix - make it optional for matching
        # DB might have "The Ultimates" but files might be "Ultimates"
        working_name = series_name
        the_prefix = ''
        if series_name.lower().startswith('the '):
            the_prefix = r'(?:The[\s\-_]+)?'
            working_name = series_name[4:]  # Remove "The " from name

        # Remove apostrophes and ampersands entirely first
        # Handles possessives: "Night's" -> "Nights"
        # Handles ampersands: "Black & White" -> "Black White" (files often omit &)
        temp_name = working_name.replace("'", "").replace("&", "")
        # Then normalize other punctuation - replace :, -, etc. with space for consistent handling
        # This allows "Nemesis: Forever", "Nemesis - Forever", "Nemesis Forever" to all match
        normalized_name = re.sub(r'[\s\-_:;,\.]+', ' ', temp_name).strip()

        # Build series pattern word-by-word, making common connecting words optional
        # Files often omit words like "and", "of", "the" (e.g., "Magik Colossus" for "Magik and Colossus")
        OPTIONAL_WORDS = {'and', 'the', 'of', 'or', 'vs', 'versus'}
        sep = r"[\s\-_:'\.&]*"
        words = normalized_name.split()
        pattern_parts = []
        for i, word in enumerate(words):
            escaped_word = re.escape(word)
            if word.lower() in OPTIONAL_WORDS:
                pattern_parts.append(f"(?:{escaped_word}{sep})?")
            else:
                pattern_parts.append(escaped_word)
                if i < len(words) - 1:
                    pattern_parts.append(sep)
        series_pattern = the_prefix + ''.join(pattern_parts)

        # Normalize issue number - handle leading zeros (1, 01, 001 all match)
        issue_num_clean = str(issue_number).strip().lstrip('0') or '0'
        # Match issue number with optional leading zeros
        issue_pattern = r'0*' + re.escape(issue_num_clean)

        # Now substitute our patterns back in
        pattern = pattern.replace('<<<SERIES>>>', f'(?:{series_pattern})')
        pattern = pattern.replace('<<<ISSUE>>>', f'({issue_pattern})')
        pattern = pattern.replace('<<<YEAR>>>', r'\d{4}')

        # Make spaces between components flexible (allow punctuation like trailing periods)
        # This handles cases like "K.O. 003" where there's punctuation before the space
        pattern = pattern.replace(') (', r")[\s\-_:'\.&]+(" )

        # Add file extension matching at the end
        pattern += r'.*\.(?:cbz|cbr|zip|rar)$'

        return re.compile(pattern, re.IGNORECASE)

    except Exception as e:
        app_logger.debug(f"Failed to generate filename pattern: {e}")
        return None


def extract_comicinfo(file_path):
    """
    Extract ComicInfo.xml from a CBZ file.

    Args:
        file_path: Path to the CBZ file

    Returns:
        Dict with series, number, volume, year or None
    """
    import zipfile
    import xml.etree.ElementTree as ET

    if not file_path.lower().endswith(('.cbz', '.zip')):
        return None

    try:
        with zipfile.ZipFile(file_path, 'r') as zf:
            if 'ComicInfo.xml' in zf.namelist():
                with zf.open('ComicInfo.xml') as ci:
                    tree = ET.parse(ci)
                    root = tree.getroot()
                    return {
                        'series': root.findtext('Series', ''),
                        'number': root.findtext('Number', ''),
                        'volume': root.findtext('Volume', ''),
                        'year': root.findtext('Year', '')
                    }
    except Exception:
        pass

    return None


def match_issues_to_collection(mapped_path, issues, series_info, use_cache=True):
    """
    Match Metron issues to local files in the mapped directory with caching.

    Strategy:
    1. Check database cache first (if use_cache=True)
    2. For uncached issues, use CUSTOM_RENAME_PATTERN to generate precise regex
    3. Fall back to ComicInfo.xml matching
    4. Cache results in database

    Args:
        mapped_path: Path to the series directory
        issues: List of issue objects from Metron
        series_info: Series info object
        use_cache: Whether to use cached results (default True)

    Returns:
        Dict mapping issue_number -> {'found': bool, 'file_path': str or None}
    """
    from database import (
        get_collection_status_for_series,
        save_collection_status_bulk,
    )

    results = {}
    comic_extensions = ('.cbz', '.cbr', '.zip', '.rar')

    # Get series info
    series_id = getattr(series_info, 'id', None) or (series_info.get('id') if isinstance(series_info, dict) else None)
    series_name = getattr(series_info, 'name', '') or (series_info.get('name', '') if isinstance(series_info, dict) else '')

    # Step 1: Check cache first
    if use_cache and series_id:
        cached = get_collection_status_for_series(series_id)
        if cached:
            # Validate cache by checking file existence and mtime
            valid_cache = True
            for entry in cached:
                if entry['file_path']:
                    if not os.path.exists(entry['file_path']):
                        valid_cache = False
                        app_logger.debug(f"Cache invalid: file no longer exists {entry['file_path']}")
                        break
                    try:
                        current_mtime = os.path.getmtime(entry['file_path'])
                        if entry['file_mtime'] and abs(current_mtime - entry['file_mtime']) > 1:
                            valid_cache = False
                            app_logger.debug(f"Cache invalid: mtime changed for {entry['file_path']}")
                            break
                    except OSError:
                        valid_cache = False
                        break

            if valid_cache:
                # Return cached results
                for entry in cached:
                    results[entry['issue_number']] = {
                        'found': bool(entry['found']),
                        'file_path': entry['file_path']
                    }
                app_logger.debug(f"Using cached collection status for series {series_id} ({len(results)} issues)")
                return results
            else:
                app_logger.debug(f"Cache invalid for series {series_id}, re-scanning")

    # Step 2: Scan directory and build file metadata
    local_files = []
    file_metadata = {}

    try:
        for filename in os.listdir(mapped_path):
            if filename.lower().endswith(comic_extensions):
                file_path = os.path.join(mapped_path, filename)
                local_files.append(file_path)
                try:
                    mtime = os.path.getmtime(file_path)
                except OSError:
                    mtime = None
                file_metadata[file_path] = {
                    'filename': filename,
                    'path': file_path,
                    'mtime': mtime,
                    'comicinfo': None  # Lazy-loaded
                }
    except Exception as e:
        app_logger.error(f"Error scanning directory {mapped_path}: {e}")
        return results

    # Step 3: Get custom rename pattern from DB
    from database import get_user_preference
    custom_pattern = get_user_preference('custom_rename_pattern', default='') or ''

    # Step 4: Match each issue
    cache_entries = []

    for issue in issues:
        issue_num = str(getattr(issue, 'number', '') or (issue.get('number', '') if isinstance(issue, dict) else ''))
        issue_id = getattr(issue, 'id', None) or (issue.get('id') if isinstance(issue, dict) else None)

        if not issue_num:
            continue

        match_found = False
        matched_file = None
        matched_via = None

        # 4a: Try CUSTOM_RENAME_PATTERN matching first (most reliable for user's files)
        if custom_pattern and series_name:
            pattern_regex = generate_filename_pattern(custom_pattern, series_name, issue_num)
            if pattern_regex:
                for file_path, metadata in file_metadata.items():
                    if pattern_regex.search(metadata['filename']):
                        match_found = True
                        matched_file = file_path
                        matched_via = 'pattern'
                        break

        # 4b: Fallback to ComicInfo.xml matching
        if not match_found:
            for file_path, metadata in file_metadata.items():
                # Lazy-load ComicInfo.xml only when needed
                if metadata['comicinfo'] is None:
                    metadata['comicinfo'] = extract_comicinfo(file_path) or {}

                ci = metadata['comicinfo']
                if ci.get('number'):
                    # Normalize issue numbers for comparison
                    meta_num = str(ci['number']).strip().lstrip('0') or '0'
                    check_num = issue_num.strip().lstrip('0') or '0'

                    if meta_num == check_num:
                        # Check series name matches (loose match)
                        meta_series = ci.get('series', '').lower()
                        if not meta_series or series_name.lower() in meta_series or meta_series in series_name.lower():
                            match_found = True
                            matched_file = file_path
                            matched_via = 'comicinfo'
                            break

        # 4c: Final fallback to generic filename patterns
        if not match_found:
            check_num = issue_num.strip().lstrip('0') or '0'
            patterns = [
                rf'[\s\-_]0*{re.escape(check_num)}(?:[\s\-_\.\(]|$)',  # space/dash/underscore + number + delimiter
                rf'#0*{re.escape(check_num)}(?:\D|$)',  # #1, #01, #001
            ]

            for file_path, metadata in file_metadata.items():
                filename = metadata['filename']
                for pattern in patterns:
                    if re.search(pattern, filename, re.IGNORECASE):
                        match_found = True
                        matched_file = file_path
                        matched_via = 'filename'
                        break
                if match_found:
                    break

        results[issue_num] = {
            'found': match_found,
            'file_path': matched_file
        }

        # Prepare cache entry
        if series_id and issue_id:
            cache_entries.append({
                'series_id': series_id,
                'issue_id': issue_id,
                'issue_number': issue_num,
                'found': 1 if match_found else 0,
                'file_path': matched_file,
                'file_mtime': file_metadata.get(matched_file, {}).get('mtime') if matched_file else None,
                'matched_via': matched_via
            })

    # Step 5: Save to cache
    if cache_entries:
        save_collection_status_bulk(cache_entries)
        app_logger.debug(f"Cached collection status for series {series_id} ({len(cache_entries)} issues)")

    return results
