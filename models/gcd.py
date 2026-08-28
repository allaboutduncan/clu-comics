"""
GCD (Grand Comics Database) integration for comic metadata retrieval.

Reads a user-provided SQLite export of the GCD database (downloaded from
https://www.comics.org/download/). The dump shares identical table/column names
with the historical MySQL schema, so only the connection + SQL-dialect layer is
SQLite-specific here.
"""
import os
import re
import sqlite3
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple, Set
from core.app_logging import app_logger

# =============================================================================
# Constants
# =============================================================================

STOPWORDS = {"the", "a", "an", "of", "and", "vol", "volume", "season", "series"}

# The last-resort search variations, which look up a single token as an
# unanchored substring. They are the only ones broad enough to need a guard.
MAIN_WORD_VARIATIONS = frozenset({"main_only", "main_with_year"})

# Variations that constrain results to series actually running in the parsed
# year, when the filename supplied one. `main_with_year` is included because
# that is what its name promises; `main_only` is what the same variation is
# called when no year is available.
#
# `tokenized` is deliberately absent, and not merely because it runs through
# the REGEXP branch -- that branch could take the same two bindings. It is the
# only tier that still matches when the filename's year falls outside the
# series' GCD run (a reprint year, a collected edition, a mis-parsed year),
# which is precisely when every variation above it has already failed for the
# same reason. It is not year-blind, though: where several series contain all
# the tokens, the parsed year picks the closest era rather than the newest.
# That is a ranking problem, not a filtering one, and it is solved as one --
# see `proximity_suffix` in search_series().
YEAR_CONSTRAINED_VARIATIONS = frozenset({
    "exact", "no_issue", "no_year", "no_dash", "main_with_year",
})

# How many candidate series the main-word fallback may match and still be
# treated as evidence. The loop only ever inspects the first row, so beyond a
# handful of candidates "first by year" is an arbitrary pick rather than a best
# match. Raising this does not improve matching -- it only makes wrong matches
# more likely.
MAIN_WORD_MAX_CANDIDATES = 10

# Full set of GCD tables CLU touches across all code paths. Used to detect
# which auxiliary tables a particular dump excludes — the public dump
# from comics.org periodically drops tables (e.g. gcd_creator, gcd_issue_credit)
# while GCD restructures schemas.
EXPECTED_GCD_TABLES = frozenset({
    'gcd_series', 'gcd_issue', 'gcd_story', 'gcd_publisher', 'gcd_creator',
    'gcd_indicia_publisher', 'gcd_story_credit', 'gcd_issue_credit',
    'gcd_credit_type', 'gcd_story_type', 'gcd_story_character',
    'gcd_character', 'stddata_language',
})

# Without these tables, GCD lookups are fundamentally broken. Surface a hard
# error to the user via the stats endpoint when any of these are missing.
GCD_CORE_TABLES = frozenset({
    'gcd_series', 'gcd_issue', 'gcd_publisher', 'stddata_language', 'gcd_story',
})

# Cached set of GCD tables actually present in the connected database.
# Populated lazily on first call to get_available_gcd_tables(). Invalidated
# via invalidate_gcd_table_cache() when credentials change.
_AVAILABLE_TABLES_CACHE: Optional[Set[str]] = None
_AVAILABLE_TABLES_LOGGED: bool = False

# =============================================================================
# Helper Functions
# =============================================================================

def normalize_title(s: str) -> str:
    """Normalize a title string for better matching."""
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)   # remove punctuation/hyphens
    s = " ".join(s.split())              # collapse spaces
    return s


def tokens_for_all_match(s: str):
    """Normalize and drop stopwords for 'all tokens present' matching."""
    norm = normalize_title(s)
    toks = [t for t in norm.split() if t not in STOPWORDS]
    return norm, toks


def main_word_token_too_broad(cursor, search_pattern: str, language_codes) -> bool:
    """True when a main-word LIKE pattern matches more series than can be ranked.

    The main-word fallback searches a single token as an unanchored substring,
    so a short or common token matches an enormous slice of the database --
    '%le%' matches 18,526 series in the current dump. Ordering those by year and
    taking the first does not pick the best candidate, it picks the most
    recently started one, which is how an Italian Disney part-work ends up
    tagged as a 2026 Harley Quinn book. When the token cannot narrow the field
    to something rankable, the caller must decline instead of guessing.

    The probe deliberately ignores any year filter the caller is about to apply.
    How discriminating a token is, is a property of the token: '%diabolik%'
    matches 15 series whichever year is asked for. Measuring the year-filtered
    set instead would let the year clause shrink an over-broad token under the
    cap and hand back an arbitrary pick from whatever happened to be running
    that year.

    Shared by both progressive-search loops -- ``search_series`` here and
    ``search_gcd_metadata`` in ``routes/metadata.py`` -- so the two cannot drift
    apart on the policy the way they did on the year constraint.
    """
    codes = list(language_codes or [])
    # Mirrors the callers' own empty-list handling: "IN (NULL)" matches nothing
    # rather than being a syntax error.
    in_clause = ','.join(['?'] * len(codes)) if codes else 'NULL'
    cursor.execute(
        'SELECT 1 FROM gcd_series s'
        ' JOIN stddata_language l ON s.language_id = l.id'
        ' WHERE s.name LIKE ? AND l.code IN (' + in_clause + ')'
        + f' LIMIT {MAIN_WORD_MAX_CANDIDATES + 1}',
        (search_pattern, *codes),
    )
    return len(cursor.fetchall()) > MAIN_WORD_MAX_CANDIDATES


def lookahead_regex(toks):
    """Build ^(?=.*\\bsuperman\\b)(?=.*\\bsecret\\b)(?=.*\\byears\\b).*$
    Works with MySQL REGEXP and is case-insensitive when we pass 'i' or pre-lowercase."""
    if not toks:
        return r".*"          # match-all fallback
    # The word boundary is a single backslash-b. Writing it as ``\\b`` inside a
    # *raw* string emits a literal backslash followed by 'b', which matches only
    # a series name containing a backslash -- so the variation never matched and
    # every multi-word title fell through to the one-token fallback below.
    parts = [rf"(?=.*\b{re.escape(t)}\b)" for t in toks]
    return "^" + "".join(parts) + ".*$"


def generate_search_variations(series_name: str, year: str = None):
    """Generate progressive search variations for a comic title."""
    variations = []

    # Original exact search (current behavior)
    variations.append(("exact", f"%{series_name}%"))

    # Remove issue number pattern from title for broader search
    clean_title = re.sub(r'\s+\d{3}\s*$', '', series_name)  # Remove trailing issue numbers like "001"
    clean_title = re.sub(r'\s+#\d+\s*$', '', clean_title)   # Remove trailing issue numbers like "#1"

    if clean_title != series_name:
        variations.append(("no_issue", f"%{clean_title}%"))

    # Remove year from title if present
    title_no_year = re.sub(r'\s*\(\d{4}\)\s*', '', clean_title)
    title_no_year = re.sub(r'\s+\d{4}\s*$', '', title_no_year)

    if title_no_year != clean_title:
        variations.append(("no_year", f"%{title_no_year}%"))

    # Normalize and tokenize for advanced matching
    norm, tokens = tokens_for_all_match(title_no_year)

    # Remove hyphens/dashes for matching (Superman - The Secret Years -> Superman The Secret Years)
    no_dash_title = re.sub(r'\s*-+\s*', ' ', title_no_year).strip()
    if no_dash_title != title_no_year:
        variations.append(("no_dash", f"%{no_dash_title}%"))

    # Remove articles and common words for broader matching
    if len(tokens) > 1:
        regex_pattern = lookahead_regex(tokens)
        variations.append(("tokenized", regex_pattern))

    # Just the main character/franchise name (first significant word)
    if len(tokens) > 0:
        main_word = tokens[0]
        if year:
            variations.append(("main_with_year", f"%{main_word}%"))
        else:
            variations.append(("main_only", f"%{main_word}%"))

    return variations


# =============================================================================
# Database Connection
# =============================================================================

def _regexp(pattern: Optional[str], value: Optional[str]) -> int:
    """SQLite REGEXP implementation.

    SQLite calls this as ``regexp(Y, X)`` for the expression ``X REGEXP Y``, so
    the pattern is the first argument. Returns 1/0 and treats NULL values as
    non-matching so the existing MySQL-style REGEXP SQL keeps working.
    """
    if value is None or pattern is None:
        return 0
    try:
        return 1 if re.search(pattern, str(value)) else 0
    except re.error:
        return 0


def _dict_factory(cursor, row):
    """Row factory returning plain dicts.

    Used instead of sqlite3.Row because callers rely on ``row.get(...)`` which
    sqlite3.Row does not provide.
    """
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


def is_database_available() -> bool:
    """Check whether a configured GCD SQLite file exists on disk."""
    params = get_connection_params()
    path = params.get('database_path') if params else None
    return bool(path and os.path.exists(path))


def _get_saved_credentials() -> Optional[Dict[str, Any]]:
    """Get GCD credentials saved via the UI."""
    try:
        from core.database import get_provider_credentials
        return get_provider_credentials('gcd')
    except Exception:
        return None


def get_connection_params() -> Optional[Dict[str, Any]]:
    """
    Get GCD SQLite connection parameters.
    Checks saved credentials first, then falls back to the GCD_DATABASE_PATH
    environment variable.

    Returns:
        Dict with database_path, or None if not configured
    """
    # First try saved credentials from UI
    saved_creds = _get_saved_credentials()
    if saved_creds and saved_creds.get('database_path'):
        return {'database_path': saved_creds.get('database_path')}

    # Fall back to environment variable (Docker/headless setups)
    env_path = os.environ.get('GCD_DATABASE_PATH')
    if env_path:
        return {'database_path': env_path}

    return None


def check_database_status() -> Dict[str, Any]:
    """Check if the GCD SQLite database is configured and present on disk."""
    try:
        params = get_connection_params()
        path = params.get('database_path') if params else None
        available = bool(path and os.path.exists(path))

        return {
            "gcd_available": available,
            "gcd_path_configured": bool(path),
        }
    except Exception as e:
        return {
            "gcd_available": False,
            "gcd_path_configured": False,
            "error": str(e)
        }


def get_connection():
    """
    Open and return a read-only SQLite connection to the GCD database.
    Uses saved credentials from the UI first, falls back to GCD_DATABASE_PATH.

    Returns:
        sqlite3.Connection (dict rows, REGEXP registered) or None on failure
    """
    try:
        params = get_connection_params()
        if not params or not params.get('database_path'):
            app_logger.error("GCD database not configured (no saved path or GCD_DATABASE_PATH)")
            return None

        path = params['database_path']
        if not os.path.exists(path):
            app_logger.error(f"GCD database file not found: {path}")
            return None

        # Read-only URI: never creates an empty DB on a bad path and never writes
        # -wal/-shm/journal files next to the (large) dump.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = _dict_factory
        conn.create_function("regexp", 2, _regexp)
        return conn
    except sqlite3.Error as e:
        app_logger.error(f"Failed to open GCD SQLite database: {e}")
        return None
    except Exception as e:
        app_logger.error(f"Failed to open GCD SQLite database: {e}")
        return None


# =============================================================================
# Table Availability
# =============================================================================

def get_available_gcd_tables(conn=None, *, force_refresh: bool = False) -> Set[str]:
    """
    Return the set of expected GCD tables actually present in the connected database.

    Caches at module level — first call queries sqlite_master, subsequent
    calls return the cached set. Pass force_refresh=True to re-query (e.g. after
    credentials change). If `conn` is provided, reuses it; otherwise opens a
    short-lived connection. Returns an empty set on any failure so callers fall
    back to the safest behavior (skip optional joins, emit empty results).
    """
    global _AVAILABLE_TABLES_CACHE, _AVAILABLE_TABLES_LOGGED

    if _AVAILABLE_TABLES_CACHE is not None and not force_refresh:
        return _AVAILABLE_TABLES_CACHE

    owned_conn = False
    try:
        if conn is None:
            conn = get_connection()
            owned_conn = True
        if conn is None:
            return set()

        cursor = conn.cursor()
        expected = sorted(EXPECTED_GCD_TABLES)
        placeholders = ','.join(['?'] * len(expected))
        cursor.execute(
            f"SELECT name FROM sqlite_master "
            f"WHERE type = 'table' AND name IN ({placeholders})",
            expected,
        )
        present = {row['name'] for row in cursor.fetchall()}
        cursor.close()

        _AVAILABLE_TABLES_CACHE = present

        if not _AVAILABLE_TABLES_LOGGED:
            missing = EXPECTED_GCD_TABLES - present
            if missing:
                app_logger.warning(
                    "GCD: %d expected table(s) missing from dump — credits/characters/story types will be partial: %s",
                    len(missing),
                    sorted(missing),
                )
            _AVAILABLE_TABLES_LOGGED = True

        return present
    except Exception as e:
        app_logger.error(f"Failed to enumerate GCD tables: {e}")
        return set()
    finally:
        if owned_conn and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def invalidate_gcd_table_cache() -> None:
    """Reset the cached set so the next call re-queries sqlite_master."""
    global _AVAILABLE_TABLES_CACHE, _AVAILABLE_TABLES_LOGGED
    _AVAILABLE_TABLES_CACHE = None
    _AVAILABLE_TABLES_LOGGED = False


# =============================================================================
# Database Stats
# =============================================================================

def get_database_stats() -> Optional[Dict[str, Any]]:
    """Get row counts for key GCD database tables via COUNT(*)."""
    conn = get_connection()
    if not conn:
        return None
    try:
        available = get_available_gcd_tables(conn=conn)

        mapping = {
            'gcd_series': 'series',
            'gcd_issue': 'issues',
            'gcd_story': 'stories',
            'gcd_publisher': 'publishers',
            'gcd_creator': 'creators',
        }

        # Count only the tables we care about AND that exist. Table names come
        # from the trusted mapping above (never user input), so inlining them is
        # safe — SQLite cannot parameterize table names anyway.
        stats = {friendly: 0 for friendly in mapping.values()}
        wanted = [t for t in mapping.keys() if t in available]

        if wanted:
            cursor = conn.cursor()
            for table_name in wanted:
                cursor.execute(f"SELECT COUNT(*) AS c FROM {table_name}")
                row = cursor.fetchone()
                stats[mapping[table_name]] = (row['c'] if row else 0) or 0
            cursor.close()

        stats['table_count'] = len(available)
        stats['available_tables'] = sorted(available)
        stats['missing_tables'] = sorted(EXPECTED_GCD_TABLES - available)
        stats['core_ok'] = GCD_CORE_TABLES.issubset(available)
        return stats
    except Exception as e:
        app_logger.error(f"Failed to get GCD database stats: {e}")
        return None
    finally:
        conn.close()


# =============================================================================
# Issue Validation
# =============================================================================

def validate_issue(series_id: int, issue_number: str) -> Dict[str, Any]:
    """
    Validate if an issue exists in a series.

    Args:
        series_id: GCD series ID
        issue_number: Issue number to validate

    Returns:
        Dict with success status and issue data or error
    """
    if not series_id or not issue_number:
        return {
            "success": False,
            "error": "Missing series_id or issue_number"
        }

    try:
        conn = get_connection()
        if not conn:
            return {
                "success": False,
                "error": "Failed to connect to GCD database"
            }

        cursor = conn.cursor()

        # Query to find the issue
        validation_query = """
            SELECT id, title, number
            FROM gcd_issue
            WHERE series_id = ?
            AND (number = ? OR number = '[' || ? || ']' OR number LIKE ? || ' (%')
            AND deleted = 0
            LIMIT 1
        """
        cursor.execute(validation_query, (series_id, issue_number, issue_number, issue_number))
        issue = cursor.fetchone()

        cursor.close()
        conn.close()

        if issue:
            return {
                "success": True,
                "valid": True,
                "issue": {
                    "id": issue['id'],
                    "title": issue['title'],
                    "number": issue['number']
                }
            }
        else:
            return {
                "success": True,
                "valid": False,
                "message": f"Issue #{issue_number} not found in series {series_id}"
            }

    except sqlite3.Error as db_error:
        app_logger.error(f"Database error in validate_issue: {db_error}")
        return {
            "success": False,
            "error": f"Database error: {str(db_error)}"
        }
    except Exception as e:
        app_logger.error(f"Exception in validate_issue: {e}")
        return {
            "success": False,
            "error": str(e)
        }


def get_configured_languages() -> List[str]:
    """Language codes to filter GCD series by, from the user preference.

    Reads the same ``gcd_metadata_languages`` preference the manual search
    endpoint uses, so automatic and manual lookups agree. Falls back to
    ``['en']`` when the preference is unset or unreadable, which was the
    historical behaviour.
    """
    try:
        from core.database import get_user_preference
        raw = get_user_preference('gcd_metadata_languages', default='en')
    except Exception as e:
        app_logger.warning(f"Could not read gcd_metadata_languages preference: {e}")
        return ['en']

    codes = [code.strip().lower() for code in str(raw or '').split(',') if code.strip()]
    return codes or ['en']


def search_series(series_name: str, year: int = None, language_codes: List[str] = None) -> Optional[Dict[str, Any]]:
    """
    Search for a series in GCD and auto-select the best match.

    Args:
        series_name: Name of the series to search for
        year: Optional year to filter/rank results
        language_codes: Optional list of language codes. Defaults to the
            configured ``gcd_metadata_languages`` preference.

    Returns:
        Best matching series dict with id, name, year_began, publisher_name, or None if not found
    """
    if language_codes is None:
        language_codes = get_configured_languages()

    try:
        conn = get_connection()
        if not conn:
            return None

        cursor = conn.cursor()

        # Build language IN clause
        lang_placeholders = ','.join(['?'] * len(language_codes))

        # Generate search variations
        variations = generate_search_variations(series_name, str(year) if year else None)

        series_result = None

        for search_type, search_pattern in variations:
            try:
                # lang_placeholders is validated above (only ? tokens)
                base_select = (
                    'SELECT s.id, s.name, s.year_began, s.year_ended,'
                    '       p.name AS publisher_name'
                    ' FROM gcd_series s'
                    ' JOIN stddata_language l ON s.language_id = l.id'
                    ' LEFT JOIN gcd_publisher p ON s.publisher_id = p.id'
                )
                lang_filter = ' AND l.code IN (' + lang_placeholders + ')'
                order_suffix = ' ORDER BY s.year_began DESC LIMIT 10'
                # `tokenized` is not year-*filtered* (see
                # YEAR_CONSTRAINED_VARIATIONS), but when the filename supplied a
                # year there is no reason to break its ties by recency. Ranking
                # by distance from the parsed year only decides which of several
                # all-token matches wins -- it can never drop one. COALESCE
                # keeps a NULL year_began sorting last: ABS(NULL - y) is NULL,
                # and SQLite sorts NULLs first on ASC.
                proximity_suffix = (
                    ' ORDER BY ABS(COALESCE(s.year_began, 9999) - ?) ASC,'
                    ' s.year_began DESC LIMIT 10'
                )

                # Decline the one-token fallback when it cannot narrow the
                # field to something rankable. See main_word_token_too_broad().
                if search_type in MAIN_WORD_VARIATIONS and main_word_token_too_broad(
                    cursor, search_pattern, language_codes
                ):
                    app_logger.info(
                        f"GCD search_series: Skipping {search_type} for "
                        f"'{series_name}' -- '{search_pattern}' matches more "
                        f"than {MAIN_WORD_MAX_CANDIDATES} series, too broad "
                        f"to rank"
                    )
                    continue

                if search_type == "tokenized":
                    # REGEXP search, ranked by year proximity when we have one.
                    if year:
                        query = (base_select
                                 + ' WHERE LOWER(s.name) REGEXP ?'
                                 + lang_filter + proximity_suffix)
                        cursor.execute(query,
                                       (search_pattern.lower(), *language_codes, year))
                    else:
                        query = (base_select
                                 + ' WHERE LOWER(s.name) REGEXP ?'
                                 + lang_filter + order_suffix)
                        cursor.execute(query, (search_pattern.lower(), *language_codes))
                elif year and search_type in YEAR_CONSTRAINED_VARIATIONS:
                    # Year-constrained LIKE search. `main_with_year` belongs here:
                    # it was named for a year it never applied, because it was
                    # missing from this list and fell through to the plain LIKE
                    # below -- which is how a file dated 1987 matched a 2026
                    # series while the log line read "using main_with_year".
                    query = (base_select
                             + ' WHERE s.name LIKE ?'
                             + ' AND s.year_began <= ?'
                             + ' AND (s.year_ended IS NULL OR s.year_ended >= ?)'
                             + lang_filter + order_suffix)
                    cursor.execute(query, (search_pattern, year, year, *language_codes))
                else:
                    # Regular LIKE search
                    query = (base_select
                             + ' WHERE s.name LIKE ?'
                             + lang_filter + order_suffix)
                    cursor.execute(query, (search_pattern, *language_codes))

                results = cursor.fetchall()
                if results:
                    # Auto-select the best match (first result, sorted by year)
                    series_result = results[0]
                    app_logger.info(f"GCD search_series: Found '{series_result['name']}' ({series_result['year_began']}) using {search_type}")
                    break

            except Exception as e:
                app_logger.debug(f"GCD search_series: Error in {search_type} search: {e}")
                continue

        cursor.close()
        conn.close()

        if series_result is None:
            # Logged because a language-filtered miss is otherwise invisible:
            # the caller just sees "no match" with nothing to explain why.
            app_logger.info(
                f"GCD search_series: No match for '{series_name}' "
                f"in languages {language_codes}"
            )

        return series_result

    except Exception as e:
        app_logger.error(f"Exception in search_series: {e}")
        return None


def get_issue_metadata(series_id: int, issue_number: str) -> Optional[Dict[str, Any]]:
    """
    Get metadata for a specific issue from GCD.

    Args:
        series_id: GCD series ID
        issue_number: Issue number (string to handle variants like "1A")

    Returns:
        Dict with ComicInfo-compatible metadata, or None if not found
    """
    try:
        conn = get_connection()
        if not conn:
            return None

        cursor = conn.cursor()

        # Get series info first. stddata_language is a core table (see
        # GCD_CORE_TABLES), so the join is safe on any usable dump.
        series_query = """
            SELECT s.id, s.name, s.year_began, p.name as publisher_name,
                   l.code as language_code
            FROM gcd_series s
            LEFT JOIN gcd_publisher p ON s.publisher_id = p.id
            LEFT JOIN stddata_language l ON s.language_id = l.id
            WHERE s.id = ?
        """
        cursor.execute(series_query, (series_id,))
        series = cursor.fetchone()

        if not series:
            cursor.close()
            conn.close()
            return None

        # Search for the issue with flexible matching
        issue_query = """
            SELECT
                i.id,
                i.number,
                i.volume,
                COALESCE(NULLIF(TRIM(i.title), ''),
                    (SELECT NULLIF(TRIM(s.title), '')
                     FROM gcd_story s
                     WHERE s.issue_id = i.id AND (s.sequence_number IS NULL OR s.sequence_number <> 0)
                     ORDER BY s.sequence_number LIMIT 1)
                ) AS title,
                (SELECT COALESCE(NULLIF(TRIM(s.synopsis), ''), NULLIF(TRIM(s.notes), ''))
                 FROM gcd_story s
                 WHERE s.issue_id = i.id
                   AND COALESCE(NULLIF(TRIM(s.synopsis), ''), NULLIF(TRIM(s.notes), '')) IS NOT NULL
                 ORDER BY CASE WHEN s.sequence_number = 0 THEN 1 ELSE 0 END, s.sequence_number
                 LIMIT 1
                ) AS summary,
                CASE
                    WHEN COALESCE(i.key_date, i.on_sale_date) IS NOT NULL
                         AND LENGTH(COALESCE(i.key_date, i.on_sale_date)) >= 4
                    THEN CAST(substr(COALESCE(i.key_date, i.on_sale_date), 1, 4) AS INTEGER)
                END AS year,
                CASE
                    WHEN COALESCE(i.key_date, i.on_sale_date) IS NOT NULL
                         AND LENGTH(COALESCE(i.key_date, i.on_sale_date)) >= 7
                    THEN CAST(substr(COALESCE(i.key_date, i.on_sale_date), 6, 2) AS INTEGER)
                END AS month
            FROM gcd_issue i
            WHERE i.series_id = ?
              AND i.deleted = 0
              AND (i.number = ? OR i.number = '[' || ? || ']' OR i.number LIKE ? || ' (%')
            LIMIT 1
        """
        cursor.execute(issue_query, (series_id, issue_number, issue_number, issue_number))
        issue = cursor.fetchone()

        if not issue:
            cursor.close()
            conn.close()
            app_logger.debug(f"GCD get_issue_metadata: Issue #{issue_number} not found in series {series_id}")
            return None

        # Get credits from normalized gcd_story_credit table — but only if the
        # tables it needs are actually present in this dump.
        available = get_available_gcd_tables(conn=conn)
        normalized_ok = {
            'gcd_story', 'gcd_story_credit', 'gcd_credit_type', 'gcd_creator'
        }.issubset(available)

        credits = []
        if normalized_ok:
            credits_query = """
                SELECT DISTINCT
                    ct.name as credit_type,
                    TRIM(COALESCE(
                        NULLIF(TRIM(sc.credited_as),''),
                        NULLIF(TRIM(sc.credit_name),''),
                        NULLIF(TRIM(c.gcd_official_name),''),
                        NULLIF(TRIM(c.sort_name),'')
                    )) as creator_name
                FROM gcd_story s
                JOIN gcd_story_credit sc ON sc.story_id = s.id
                JOIN gcd_credit_type ct ON ct.id = sc.credit_type_id
                LEFT JOIN gcd_creator c ON c.id = sc.creator_id
                WHERE s.issue_id = ?
                  AND (sc.deleted = 0 OR sc.deleted IS NULL)
            """
            cursor.execute(credits_query, (issue['id'],))
            credits = cursor.fetchall()
            app_logger.debug(f"GCD credits for issue {issue['id']}: {credits}")

        # Fallback: older issues store credits as text columns on gcd_story.
        # Also runs when gcd_story_credit is missing from the dump entirely.
        if not credits and 'gcd_story' in available:
            legacy_query = """
                SELECT script, pencils, inks, colors, letters, editing
                FROM gcd_story
                WHERE issue_id = ?
                LIMIT 1
            """
            cursor.execute(legacy_query, (issue['id'],))
            legacy = cursor.fetchone()
            if legacy:
                field_map = {
                    'script': 'script',
                    'pencils': 'pencils',
                    'inks': 'inks',
                    'colors': 'colors',
                    'letters': 'letters',
                    'editing': 'editing',
                }
                for field, credit_type in field_map.items():
                    val = (legacy.get(field) or '').strip()
                    if val and val != '?':
                        for name in re.split(r';\s*', val):
                            name = name.strip()
                            if name and name != '?':
                                credits.append({'credit_type': credit_type, 'creator_name': name})

        cursor.close()
        conn.close()

        # Build ComicInfo-compatible metadata
        writers = []
        pencillers = []
        inkers = []
        colorists = []
        letterers = []
        cover_artists = []

        for credit in credits:
            credit_type = credit['credit_type'].lower() if credit['credit_type'] else ''
            name = credit['creator_name']
            if not name:
                continue

            if 'script' in credit_type or 'writer' in credit_type or 'plot' in credit_type:
                if name not in writers:
                    writers.append(name)
            if 'pencil' in credit_type or 'illustrat' in credit_type:
                if name not in pencillers:
                    pencillers.append(name)
            if 'ink' in credit_type or 'illustrat' in credit_type:
                if name not in inkers:
                    inkers.append(name)
            if 'color' in credit_type or 'paint' in credit_type:
                if name not in colorists:
                    colorists.append(name)
            if 'letter' in credit_type:
                if name not in letterers:
                    letterers.append(name)
            if 'cover' in credit_type:
                if name not in cover_artists:
                    cover_artists.append(name)

        from datetime import datetime
        current_date = datetime.now().strftime('%Y-%m-%d')

        metadata = {
            'Series': series['name'],
            'Number': issue['number'],
            'Volume': issue['volume'] if issue['volume'] else None,
            'Title': issue['title'],
            'Summary': issue['summary'],
            'Publisher': series['publisher_name'],
            'Year': issue['year'],
            'Month': issue['month'],
            'Writer': ', '.join(writers) if writers else None,
            'Penciller': ', '.join(pencillers) if pencillers else None,
            'Inker': ', '.join(inkers) if inkers else None,
            'Colorist': ', '.join(colorists) if colorists else None,
            'Letterer': ', '.join(letterers) if letterers else None,
            'CoverArtist': ', '.join(cover_artists) if cover_artists else None,
            'LanguageISO': series['language_code'] or 'en',
            'Notes': f'Metadata from GCD (Grand Comics Database). Series ID: {series_id} — retrieved {current_date}.'
        }

        # Remove None values
        metadata = {k: v for k, v in metadata.items() if v is not None}

        app_logger.info(f"GCD get_issue_metadata: Found metadata for {series['name']} #{issue_number}")
        return metadata

    except sqlite3.Error as db_error:
        app_logger.error(f"Database error in get_issue_metadata: {db_error}")
        return None
    except Exception as e:
        app_logger.error(f"Exception in get_issue_metadata: {e}")
        return None
