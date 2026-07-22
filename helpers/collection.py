import os
import re
import logging
from core.config import config
from core.app_logging import app_logger


# Matches any year token variant ({volume_year}/{cover_year}/{issue_year}/
# {store_year}) and the legacy {year}.
_YEAR_TOKEN = r"\{(?:volume|cover|issue|store)?_?year\}"


def strip_year_token(pattern):
    """Remove every year token (and its surrounding ()/[] and spaces) from a
    rename pattern, producing a pattern suitable for year-agnostic matching.

    The year of a file can differ between metadata sources (or be absent), so
    wanted-issue matching deliberately ignores it. Handles all year variants,
    not just the legacy {volume_year}.
    """
    if not pattern:
        return pattern
    # " ({cover_year})" / " [{volume_year}]" -> ""
    pattern = re.sub(r"\s*[\(\[]\s*" + _YEAR_TOKEN + r"\s*[\)\]]", "", pattern)
    # remaining bare " {...year}" -> ""
    pattern = re.sub(r"\s*" + _YEAR_TOKEN, "", pattern)
    return pattern.strip()


_TITLE_TOKEN = r"\{issue_title\}"


def strip_title_token(pattern):
    """Remove the {issue_title} token (and its surrounding separators/brackets)
    from a rename pattern for title-agnostic matching.

    A file's title can differ between sources or be absent (the renamer drops the
    " - " separator for untitled issues), so wanted-issue matching must not
    require it.
    """
    if not pattern:
        return pattern
    # " ({issue_title})" / " [{issue_title}]" -> ""
    pattern = re.sub(r"\s*[\(\[]\s*" + _TITLE_TOKEN + r"\s*[\)\]]", "", pattern)
    # " - {issue_title}" / " : {issue_title}" / bare " {issue_title}" -> ""
    pattern = re.sub(r"\s*[-:_]*\s*" + _TITLE_TOKEN, "", pattern)
    return pattern.strip()


# Matches both month token variants: {issue_month_m} (2-digit) / {issue_month_M} (name).
_MONTH_TOKEN = r"\{issue_month_[mM]\}"


def strip_month_token(pattern):
    """Remove month tokens ({issue_month_m}/{issue_month_M}) for month-agnostic
    matching. Mirrors strip_year_token.

    A file's month can differ between sources or be absent (downloads are often
    named with only a year, e.g. "Black Cat - 012 (2026).cbz"), so wanted-issue
    matching must not require it.
    """
    if not pattern:
        return pattern
    # " ({issue_month_M})" / " [{issue_month_m}]" -> ""
    pattern = re.sub(r"\s*[\(\[]\s*" + _MONTH_TOKEN + r"\s*[\)\]]", "", pattern)
    # bare " {month}" with an optional leading -/:/, separator -> ""
    pattern = re.sub(r"\s*[-:,]?\s*" + _MONTH_TOKEN, "", pattern)
    return pattern.strip()


def strip_empty_groups(pattern):
    """Drop ()/[] groups left holding only separators after token removal.

    Stripping year/month/title tokens from a *shared* parenthetical (e.g.
    "({issue_month_M}, {issue_year})") leaves punctuation debris like "(, )" or
    "( )" that would otherwise compile into a literal, unmatchable requirement.
    A group still containing a real {token} is preserved (the char class
    excludes "{").
    """
    if not pattern:
        return pattern
    pattern = re.sub(r"\s*[\(\[][^{}()\[\]]*[\)\]]", "", pattern)
    return pattern.strip()


def build_series_match_names(series_name, aliases):
    """Ordered, de-duplicated list of names to match a series against.

    The primary ``series_name`` comes first, followed by any GetComics search
    aliases that are case-insensitively distinct from it and each other. This
    lets a wanted series match files stored under an alternative name (e.g.
    series "Thor" with alias "Mortal Thor" matching ``Mortal Thor 011.cbz``).

    Args:
        series_name: The primary series name (matched first).
        aliases: A comma-separated string or an iterable of alias names.

    Returns:
        List of names, ``series_name`` first, with empty/duplicate entries removed.
    """
    if isinstance(aliases, str):
        alias_list = aliases.split(",")
    else:
        alias_list = aliases or []

    names = [series_name]
    seen = {series_name.lower()}
    for alias in alias_list:
        alias = str(alias).strip()
        if alias and alias.lower() not in seen:
            names.append(alias)
            seen.add(alias.lower())
    return names


def get_series_name_from_files(mapped_path, db_series_name):
    """
    Extract actual series name used in existing files.
    Falls back to database series name if no files exist.

    This helps match files when the database has "The Ultimates" but
    files are named "Ultimates 001.cbz".
    """
    if not mapped_path or not os.path.exists(mapped_path):
        app_logger.debug(
            f"get_series_name_from_files: path doesn't exist: {mapped_path}"
        )
        return db_series_name

    comic_extensions = (".cbz", ".cbr", ".zip", ".rar")
    try:
        files = [
            f for f in os.listdir(mapped_path) if f.lower().endswith(comic_extensions)
        ]
    except Exception:
        return db_series_name

    if not files:
        app_logger.debug(
            f"get_series_name_from_files: no files in {mapped_path}, using DB name: {db_series_name}"
        )
        return db_series_name

    # Try to extract series name from first file
    # Pattern: "Series Name 001 (2024).cbz" -> "Series Name"
    first_file = files[0]
    # Remove extension
    name = os.path.splitext(first_file)[0]
    # Remove all parenthetical groups: "(2024)", "(1)", "(digital)", etc.
    name = re.sub(r"\s*\([^)]*\)", "", name)
    # Remove issue number at end, including "001 of 5" and "#001" forms.
    # Renamed files can use a "NNN of M" count (see cbz_ops/rename.py); without
    # the "of M" branch only " M" is stripped, leaving "Series 001 of" as the name.
    name = re.sub(r"\s+#?\d+(?:\s+of\s+\d+)?\s*$", "", name, flags=re.IGNORECASE)
    # Drop a trailing separator left by a "Series - NNN" naming style, so the
    # derived name is "Black Cat" not "Black Cat -". Guarded below so a name
    # that is *only* separators falls back to the DB name.
    stripped = re.sub(r"[\s\-_:;,]+$", "", name)
    if stripped:
        name = stripped

    if name:
        extracted = name.strip()
        if extracted != db_series_name:
            app_logger.info(
                f"get_series_name_from_files: extracted '{extracted}' from '{first_file}' (DB: '{db_series_name}')"
            )
        return extracted

    return db_series_name


def generate_filename_pattern(custom_pattern, series_name, issue_number):
    """
    Convert CUSTOM_RENAME_PATTERN to a precise regex for matching a specific issue.

    Pattern placeholders:
    - {series_name} -> matches the series name (flexible whitespace/case)
    - {issue_number} -> matches the issue number (with optional leading zeros)
    - {volume_year}/{issue_year} (and legacy {year}) -> matches any 4-digit year
    - {issue_month_m} -> matches a 2-digit month
    - {issue_month_M} -> matches a month name
    Any other (unrecognized) {token} is stripped defensively so it never leaks
    into the compiled regex as a literal requirement.

    Args:
        custom_pattern: The rename pattern from config (e.g., "{series_name} {issue_number} ({volume_year})")
        series_name: The series name to match
        issue_number: The issue number to match

    Returns:
        Compiled regex pattern or None if pattern is invalid
    """

    if not custom_pattern or not series_name:
        return None

    try:
        # First, escape literal parentheses in the custom pattern BEFORE substituting
        # This handles patterns like "{series_name} {issue_number} ({volume_year})"
        # The ( ) around {volume_year} should become \( \) in the final regex

        # Use placeholders to protect our variable markers
        pattern = custom_pattern
        pattern = pattern.replace('{series_name}', '<<<SERIES>>>')
        pattern = pattern.replace('{issue_number}', '<<<ISSUE>>>')
        # Year variants — all match any 4-digit year
        for tok in ('{volume_year}', '{issue_year}', '{year}'):  # {year} is a legacy fallback
            pattern = pattern.replace(tok, '<<<YEAR>>>')
        # Month variants — numeric (2-digit) and name
        pattern = pattern.replace('{issue_month_m}', '<<<MONTHNUM>>>')
        pattern = pattern.replace('{issue_month_M}', '<<<MONTHNAME>>>')
        pattern = pattern.replace('{volume_number}', '<<<VOLUME>>>')
        pattern = pattern.replace('{issue_title}', '<<<TITLE>>>')

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
        # Include Unicode dashes: en dash \u2013, em dash \u2014, horizontal bar \u2015
        normalized_name = re.sub(r'[\s\-_:;,\.\u2010-\u2015\u2212]+', ' ', temp_name).strip()

        # Build series pattern word-by-word, making common connecting words optional
        # Files often omit words like "and", "of", "the" (e.g., "Magik Colossus" for "Magik and Colossus")
        OPTIONAL_WORDS = {'and', 'the', 'of', 'or', 'vs', 'versus'}
        sep = r"[\s\-_:'\.&\u2010-\u2015\u2212]*"
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
        issue_pattern = r'0*' + re.escape(issue_num_clean) + r'(?!\d)'

        # Now substitute our patterns back in
        pattern = pattern.replace('<<<SERIES>>>', f'(?:{series_pattern})')
        pattern = pattern.replace('<<<ISSUE>>>', f'({issue_pattern})')
        pattern = pattern.replace('<<<YEAR>>>', r'\d{4}')
        pattern = pattern.replace('<<<MONTHNUM>>>', r'\d{2}')
        pattern = pattern.replace('<<<MONTHNAME>>>', r'[A-Za-z]+')
        pattern = pattern.replace('<<<VOLUME>>>', r'\d+')
        pattern = pattern.replace('<<<TITLE>>>', r'[^()]*?')

        # Make spaces between components flexible (allow punctuation like trailing periods)
        # This handles cases like "K.O. 003" where there's punctuation before the space
        pattern = pattern.replace(') (', r").+?(" )

        # Defensive: drop any unrecognized {token} (and an empty "()" it may
        # leave behind) so a stray placeholder never becomes a literal regex
        # requirement that no real filename can satisfy. Match only placeholder
        # tokens (names start with a letter/underscore) so we never clobber a
        # regex quantifier like \d{4} or \d{1,4} produced by substitution above.
        _tok = r'\{[A-Za-z_][^}]*\}'
        pattern = re.sub(r'\s*\\\(\s*' + _tok + r'\s*\\\)', '', pattern)  # " ({token})" -> ""
        pattern = re.sub(r'\s*' + _tok, '', pattern)                     # bare " {token}" -> ""

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
    import defusedxml.ElementTree as SafeET

    if not file_path.lower().endswith(('.cbz', '.zip')):
        return None

    try:
        with zipfile.ZipFile(file_path, 'r') as zf:
            from core.comicinfo import find_comicinfo_in_zip
            comicinfo_path = find_comicinfo_in_zip(zf)
            if comicinfo_path:
                with zf.open(comicinfo_path) as ci:
                    tree = SafeET.parse(ci)
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


def extract_comicinfo_cached(file_path, cache):
    """Return the ComicInfo dict for ``file_path``, opening the archive at most once.

    ``cache`` is a caller-owned dict mapping path -> parsed dict (``{}`` when the
    file has no readable ComicInfo.xml). Non-CBZ/ZIP paths resolve to ``{}``
    without a disk read. This lets a scan open each archive a single time
    instead of once per wanted issue (the previous O(issues x files) cost).
    """
    cached = cache.get(file_path)
    if cached is not None:
        return cached
    if not file_path.lower().endswith((".cbz", ".zip")):
        cache[file_path] = {}
        return cache[file_path]
    cache[file_path] = extract_comicinfo(file_path) or {}
    return cache[file_path]


def match_wanted_issues_to_files(wanted, files, match_pattern, alias_lookup=None):
    """Match wanted issues against a pool of files without touching the filesystem.

    Replaces the old ``for each wanted issue: for each file`` scan (which
    rebuilt the folder-derived series name, reloaded aliases, recompiled the
    regex, and re-opened every archive on every iteration). Here all per-file
    work (ComicInfo.xml) happens at most once, and all per-series work
    (folder-derived name, aliases, compiled regexes) is memoized, so the cost
    drops to roughly O(issues + files) archive/directory reads.

    Matching semantics are identical to the previous inline loop: a filename
    regex built from ``match_pattern`` (year/month/title already stripped by the
    caller), with a ComicInfo.xml fallback that compares the normalized issue
    number plus a loose series-name substring. Each file is matched to at most
    one wanted issue, wanted issues are considered in order, and for each the
    first matching remaining file wins.

    Args:
        wanted: Iterable of dicts with ``series_name``, ``number``, ``mapped_path``
            (and any extra keys the caller needs, e.g. ``series_id``).
        files: Iterable of ``(filename, full_path)`` tuples.
        match_pattern: Rename pattern with year/month/title tokens removed.
        alias_lookup: Callable ``name -> comma-separated aliases``; defaults to
            ``models.getcomics.get_series_aliases``. Injectable for tests.

    Returns:
        List of dicts ``{"issue", "series_name", "filename", "src"}`` — one per
        matched file, in the order matches were found.
    """
    if alias_lookup is None:
        from models.getcomics import get_series_aliases
        alias_lookup = get_series_aliases

    debug = app_logger.isEnabledFor(logging.DEBUG)

    # Per-series memoization (many wanted issues share one folder / name).
    series_name_cache = {}   # mapped_path -> folder-derived series name
    alias_cache = {}         # db_series_name -> [alias, ...]
    match_names_cache = {}   # (mapped_path, db_series_name) -> [name, ...]
    regex_cache = {}         # (name, issue_number) -> compiled regex or None
    # Per-file ComicInfo memo (archive opened at most once).
    comicinfo_cache = {}

    remaining = list(files)  # (filename, src) tuples; matched files removed
    matches = []

    for issue in wanted:
        db_series_name = issue["series_name"]
        issue_number = issue["number"]
        mapped_path = issue["mapped_path"]

        # Folder-derived name — once per folder, not once per issue.
        if mapped_path not in series_name_cache:
            series_name_cache[mapped_path] = get_series_name_from_files(
                mapped_path, db_series_name
            )
        actual_series_name = series_name_cache[mapped_path]

        # Aliases — once per DB series name.
        if db_series_name not in alias_cache:
            try:
                raw_aliases = alias_lookup(db_series_name) or ""
            except Exception as e:
                app_logger.debug(f"Failed to load aliases for '{db_series_name}': {e}")
                raw_aliases = ""
            alias_cache[db_series_name] = [
                a.strip() for a in raw_aliases.split(",") if a.strip()
            ]

        mn_key = (mapped_path, db_series_name)
        if mn_key not in match_names_cache:
            match_names_cache[mn_key] = build_series_match_names(
                actual_series_name, alias_cache[db_series_name]
            )
        match_names = match_names_cache[mn_key]

        # Compiled regexes — cached per (name, issue) so a repeated series/issue
        # (e.g. via aliases) doesn't recompile.
        regexes = []
        for name in match_names:
            rk = (name, str(issue_number))
            if rk not in regex_cache:
                regex_cache[rk] = generate_filename_pattern(
                    match_pattern, name, issue_number
                )
            r = regex_cache[rk]
            if r:
                regexes.append(r)
        if not regexes:
            app_logger.debug(
                f"Failed to generate pattern for: {actual_series_name} #{issue_number}"
            )
            continue

        if debug:
            app_logger.debug(f"Checking: '{actual_series_name}' #{issue_number}")

        check_num = str(issue_number).strip().lstrip("0") or "0"
        matched = None
        for filename, src in remaining:
            match_result = any(r.match(filename) for r in regexes)

            # Fallback: ComicInfo.xml (archive opened at most once per file).
            if not match_result:
                ci = extract_comicinfo_cached(src, comicinfo_cache)
                if ci and ci.get("number"):
                    meta_num = str(ci["number"]).strip().lstrip("0") or "0"
                    if meta_num == check_num:
                        meta_series = (ci.get("series") or "").lower()
                        if meta_series and any(
                            n.lower() in meta_series or meta_series in n.lower()
                            for n in match_names
                        ):
                            match_result = True

            if debug:
                app_logger.debug(
                    f"  Testing '{filename}' -> {'MATCH' if match_result else 'no match'}"
                )
            if match_result:
                matched = (filename, src)
                break

        if matched:
            remaining.remove(matched)
            matches.append(
                {
                    "issue": issue,
                    "series_name": actual_series_name,
                    "filename": matched[0],
                    "src": matched[1],
                }
            )

    return matches


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
    from core.database import (
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

            # Detect newly-added files that could satisfy a still-missing issue.
            # The existence/mtime loop above only re-validates issues that were
            # cached as *found* (their file_path is set). Issues cached as
            # not-found have file_path=None, so a file added after the last scan
            # would never invalidate the cache — the issue would stay "missing"
            # forever, keep showing on the wanted page, and get re-downloaded
            # every night. If the folder now holds more comic files than the
            # cache matched and something is still missing, force a re-scan.
            if valid_cache and any(not entry['found'] for entry in cached):
                found_paths = {e['file_path'] for e in cached if e['file_path']}
                try:
                    current_comic_count = sum(
                        1 for f in os.listdir(mapped_path)
                        if f.lower().endswith(comic_extensions)
                    )
                    if current_comic_count > len(found_paths):
                        valid_cache = False
                        app_logger.debug(
                            f"Cache invalid for series {series_id}: {current_comic_count} "
                            f"comic file(s) on disk > {len(found_paths)} matched, missing issues present"
                        )
                except OSError:
                    pass

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
                }
    except Exception as e:
        app_logger.error(f"Error scanning directory {mapped_path}: {e}")
        return results

    # Step 3: Get custom rename pattern from DB
    from core.database import get_user_preference
    custom_pattern = get_user_preference('custom_rename_pattern', default='') or ''

    # Step 4: Match each issue
    cache_entries = []
    # ComicInfo.xml is read at most once per file across all issues (shared with
    # the scan path via extract_comicinfo_cached).
    comicinfo_cache = {}

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
                # Lazy-load ComicInfo.xml only when needed (once per file).
                ci = extract_comicinfo_cached(file_path, comicinfo_cache)
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


def reconcile_wanted_for_series(series_id):
    """
    Re-check a mapped series against the files on disk and prune any wanted-cache
    rows that are now satisfied.

    This is the single reconciliation path used whenever a file lands in a
    subscribed series folder. It invalidates the collection-status cache by
    series id (robust, unlike path-based invalidation), re-runs the canonical
    matcher, removes the now-found issues from the wanted cache, and — as a side
    effect of the fresh collection-status scan — stops the nightly GetComics
    auto-download from re-downloading issues that already exist.

    Args:
        series_id: Metron series ID

    Returns:
        Number of wanted rows removed.
    """
    from core.database import (
        get_series_by_id,
        get_issues_for_series,
        invalidate_collection_status_for_series,
        remove_wanted_issues,
    )
    from models.issue import IssueObj, SeriesObj

    if not series_id:
        return 0

    try:
        series = get_series_by_id(series_id)
        if not series:
            return 0

        mapped_path = series.get("mapped_path")
        if not mapped_path or not os.path.exists(mapped_path):
            return 0

        issues = get_issues_for_series(series_id)
        if not issues:
            return 0

        # Force a fresh scan of the folder so newly-added files are recognized.
        invalidate_collection_status_for_series(series_id)

        issue_objs = [IssueObj(i) for i in issues]
        series_obj = SeriesObj(series)
        status = match_issues_to_collection(mapped_path, issue_objs, series_obj)

        found_numbers = [num for num, s in status.items() if s.get("found")]
        removed = remove_wanted_issues(series_id, found_numbers)
        if removed:
            app_logger.info(
                f"Reconciled series {series_id}: removed {removed} satisfied wanted issue(s)"
            )
        return removed
    except Exception as e:
        app_logger.error(f"Failed to reconcile wanted for series {series_id}: {e}")
        return 0


def _series_id_for_path(file_path):
    """
    Resolve the mapped series that owns a file (or directory) path.

    Matches by normalized path prefix so subfolders, trailing slashes, and
    case-insensitive filesystems (e.g. /data) all resolve — unlike the exact
    `mapped_path = ?` equality used elsewhere. When a file sits under nested
    mapped folders, the longest (most specific) mapped_path wins.

    Returns:
        The series id, or None if the path is not under any mapped series.
    """
    from core.database import get_all_mapped_series

    try:
        directory = file_path if os.path.isdir(file_path) else os.path.dirname(file_path)
        norm_dir = os.path.normcase(os.path.normpath(directory))

        best_id = None
        best_len = -1
        for series in get_all_mapped_series():
            mapped_path = series.get("mapped_path")
            if not mapped_path:
                continue
            norm_mapped = os.path.normcase(os.path.normpath(mapped_path))
            if norm_dir == norm_mapped or norm_dir.startswith(norm_mapped + os.sep):
                if len(norm_mapped) > best_len:
                    best_len = len(norm_mapped)
                    best_id = series.get("id")
        return best_id
    except Exception as e:
        app_logger.error(f"Failed to resolve series for path {file_path}: {e}")
        return None


def reconcile_wanted_for_path(file_path):
    """
    Resolve the series that owns file_path and reconcile its wanted list.

    Called when a comic file is added, moved, or removed under the collection so
    the wanted cache and collection-status cache stay in sync without waiting for
    a full manual refresh.

    Returns:
        Number of wanted rows removed (0 if the path is not under a mapped series).
    """
    series_id = _series_id_for_path(file_path)
    if not series_id:
        return 0
    return reconcile_wanted_for_series(series_id)
