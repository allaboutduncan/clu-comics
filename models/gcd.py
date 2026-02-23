"""
GCD (Grand Comics Database) integration for comic metadata retrieval.
"""
import os
import re
from datetime import datetime
from typing import Optional, Dict, Any, List, Tuple
from app_logging import app_logger

# Check if mysql.connector is available
try:
    import mysql.connector
    MYSQL_AVAILABLE = True
except ImportError:
    MYSQL_AVAILABLE = False

# =============================================================================
# Constants
# =============================================================================

STOPWORDS = {"the", "a", "an", "of", "and", "vol", "volume", "season", "series"}

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


def lookahead_regex(toks):
    """Build ^(?=.*\\bsuperman\\b)(?=.*\\bsecret\\b)(?=.*\\byears\\b).*$
    Works with MySQL REGEXP and is case-insensitive when we pass 'i' or pre-lowercase."""
    if not toks:
        return r".*"          # match-all fallback
    parts = [rf"(?=.*\\b{re.escape(t)}\\b)" for t in toks]
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

def is_mysql_available() -> bool:
    """Check if MySQL connector is available."""
    return MYSQL_AVAILABLE


def _get_saved_credentials() -> Optional[Dict[str, Any]]:
    """Get GCD credentials saved via the UI."""
    try:
        from database import get_provider_credentials
        return get_provider_credentials('gcd')
    except Exception:
        return None


def get_connection_params() -> Optional[Dict[str, Any]]:
    """
    Get GCD MySQL connection parameters.
    Checks saved credentials first, then falls back to environment variables.

    Returns:
        Dict with host, port, database, username, password or None if not configured
    """
    # First try saved credentials from UI
    saved_creds = _get_saved_credentials()
    if saved_creds and saved_creds.get('host'):
        return {
            'host': saved_creds.get('host'),
            'port': int(saved_creds.get('port', 3306)),
            'database': saved_creds.get('database'),
            'username': saved_creds.get('username'),
            'password': saved_creds.get('password', '')
        }

    # Fall back to environment variables
    gcd_host = os.environ.get('GCD_MYSQL_HOST')
    if gcd_host:
        return {
            'host': gcd_host,
            'port': int(os.environ.get('GCD_MYSQL_PORT', 3306)),
            'database': os.environ.get('GCD_MYSQL_DATABASE'),
            'username': os.environ.get('GCD_MYSQL_USER'),
            'password': os.environ.get('GCD_MYSQL_PASSWORD', '')
        }

    return None


def check_mysql_status() -> Dict[str, Any]:
    """Check if GCD MySQL database is configured."""
    try:
        params = get_connection_params()
        gcd_available = params is not None and bool(params.get('host'))

        return {
            "gcd_mysql_available": gcd_available,
            "gcd_host_configured": gcd_available
        }
    except Exception as e:
        return {
            "gcd_mysql_available": False,
            "gcd_host_configured": False,
            "error": str(e)
        }


def get_connection():
    """
    Create and return a MySQL connection to the GCD database.
    Uses saved credentials from UI first, falls back to environment variables.

    Returns:
        MySQL connection object or None if connection fails
    """
    if not MYSQL_AVAILABLE:
        app_logger.error("MySQL connector not available")
        return None

    try:
        params = get_connection_params()
        if not params:
            app_logger.error("GCD MySQL not configured (no saved credentials or environment variables)")
            return None

        if not all([params.get('host'), params.get('database'), params.get('username')]):
            app_logger.error("GCD MySQL configuration incomplete (missing host, database, or username)")
            return None

        conn = mysql.connector.connect(
            host=params['host'],
            port=params['port'],
            database=params['database'],
            user=params['username'],
            password=params['password'],
            charset='utf8mb4',
            collation='utf8mb4_unicode_ci'
        )
        return conn
    except Exception as e:
        app_logger.error(f"Failed to connect to GCD MySQL database: {e}")
        return None


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

    if not MYSQL_AVAILABLE:
        return {
            "success": False,
            "error": "MySQL connector not available"
        }

    try:
        conn = get_connection()
        if not conn:
            return {
                "success": False,
                "error": "Failed to connect to GCD database"
            }

        cursor = conn.cursor(dictionary=True)

        # Query to find the issue
        validation_query = """
            SELECT id, title, number
            FROM gcd_issue
            WHERE series_id = %s
            AND (number = %s OR number = CONCAT('[', %s, ']') OR number LIKE CONCAT(%s, ' (%'))
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

    except mysql.connector.Error as db_error:
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


def search_series(series_name: str, year: int = None, language_codes: List[str] = None) -> Optional[Dict[str, Any]]:
    """
    Search for a series in GCD and auto-select the best match.

    Args:
        series_name: Name of the series to search for
        year: Optional year to filter/rank results
        language_codes: Optional list of language codes (default: ['en'])

    Returns:
        Best matching series dict with id, name, year_began, publisher_name, or None if not found
    """
    if not MYSQL_AVAILABLE:
        return None

    if language_codes is None:
        language_codes = ['en']

    try:
        conn = get_connection()
        if not conn:
            return None

        cursor = conn.cursor(dictionary=True)

        # Build language IN clause
        lang_placeholders = ','.join(['%s'] * len(language_codes))

        # Generate search variations
        variations = generate_search_variations(series_name, str(year) if year else None)

        series_result = None

        for search_type, search_pattern in variations:
            try:
                # lang_placeholders is validated above (only %s tokens)
                base_select = (
                    'SELECT s.id, s.name, s.year_began, s.year_ended,'
                    '       p.name AS publisher_name'
                    ' FROM gcd_series s'
                    ' JOIN stddata_language l ON s.language_id = l.id'
                    ' LEFT JOIN gcd_publisher p ON s.publisher_id = p.id'
                )
                lang_filter = ' AND l.code IN (' + lang_placeholders + ')'
                order_suffix = ' ORDER BY s.year_began DESC LIMIT 10'

                if search_type == "tokenized":
                    # REGEXP search
                    query = (base_select
                             + ' WHERE LOWER(s.name) REGEXP %s'
                             + lang_filter + order_suffix)
                    cursor.execute(query, (search_pattern.lower(), *language_codes))
                elif year and search_type in ["exact", "no_issue", "no_year", "no_dash"]:
                    # Year-constrained LIKE search
                    query = (base_select
                             + ' WHERE s.name LIKE %s'
                             + ' AND s.year_began <= %s'
                             + ' AND (s.year_ended IS NULL OR s.year_ended >= %s)'
                             + lang_filter + order_suffix)
                    cursor.execute(query, (search_pattern, year, year, *language_codes))
                else:
                    # Regular LIKE search
                    query = (base_select
                             + ' WHERE s.name LIKE %s'
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
    if not MYSQL_AVAILABLE:
        return None

    try:
        conn = get_connection()
        if not conn:
            return None

        cursor = conn.cursor(dictionary=True)

        # Get series info first
        series_query = """
            SELECT s.id, s.name, s.year_began, p.name as publisher_name
            FROM gcd_series s
            LEFT JOIN gcd_publisher p ON s.publisher_id = p.id
            WHERE s.id = %s
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
                    THEN CAST(SUBSTRING(COALESCE(i.key_date, i.on_sale_date), 1, 4) AS UNSIGNED)
                END AS year,
                CASE
                    WHEN COALESCE(i.key_date, i.on_sale_date) IS NOT NULL
                         AND LENGTH(COALESCE(i.key_date, i.on_sale_date)) >= 7
                    THEN CAST(SUBSTRING(COALESCE(i.key_date, i.on_sale_date), 6, 2) AS UNSIGNED)
                END AS month
            FROM gcd_issue i
            WHERE i.series_id = %s
              AND i.deleted = 0
              AND (i.number = %s OR i.number = CONCAT('[', %s, ']') OR i.number LIKE CONCAT(%s, ' (%%'))
            LIMIT 1
        """
        cursor.execute(issue_query, (series_id, issue_number, issue_number, issue_number))
        issue = cursor.fetchone()

        if not issue:
            cursor.close()
            conn.close()
            app_logger.debug(f"GCD get_issue_metadata: Issue #{issue_number} not found in series {series_id}")
            return None

        # Get credits (simplified query)
        credits_query = """
            SELECT DISTINCT
                ct.name as credit_type,
                TRIM(COALESCE(NULLIF(sc.credited_as,''), NULLIF(sc.credit_name,''), c.gcd_official_name)) as creator_name
            FROM gcd_story s
            JOIN gcd_story_credit sc ON sc.story_id = s.id
            JOIN gcd_credit_type ct ON ct.id = sc.credit_type_id
            LEFT JOIN gcd_creator c ON c.id = sc.creator_id
            WHERE s.issue_id = %s
              AND (s.sequence_number IS NULL OR s.sequence_number <> 0)
              AND (sc.deleted = 0 OR sc.deleted IS NULL)
              AND TRIM(COALESCE(NULLIF(sc.credited_as,''), NULLIF(sc.credit_name,''), c.gcd_official_name)) != ''
        """
        cursor.execute(credits_query, (issue['id'],))
        credits = cursor.fetchall()

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
            elif 'pencil' in credit_type:
                if name not in pencillers:
                    pencillers.append(name)
            elif 'ink' in credit_type:
                if name not in inkers:
                    inkers.append(name)
            elif 'color' in credit_type:
                if name not in colorists:
                    colorists.append(name)
            elif 'letter' in credit_type:
                if name not in letterers:
                    letterers.append(name)
            elif 'cover' in credit_type:
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
            'LanguageISO': 'en',
            'Notes': f'Metadata from GCD (Grand Comics Database). Series ID: {series_id} — retrieved {current_date}.'
        }

        # Remove None values
        metadata = {k: v for k, v in metadata.items() if v is not None}

        app_logger.info(f"GCD get_issue_metadata: Found metadata for {series['name']} #{issue_number}")
        return metadata

    except mysql.connector.Error as db_error:
        app_logger.error(f"Database error in get_issue_metadata: {db_error}")
        return None
    except Exception as e:
        app_logger.error(f"Exception in get_issue_metadata: {e}")
        return None
