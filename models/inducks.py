"""
INDUCKS integration for Disney comic metadata retrieval.

Reads a user-provided SQLite build of the INDUCKS database
(https://inducks.org/), the reference index of Disney comics worldwide. Like
the GCD and ComicVine SQLite providers, CLU never downloads or builds the file:
the user points a setting at it and CLU opens it read-only.

Two things make this material different from what the other providers handle,
and both shape the code below.

A Disney issue is an *anthology*: one album holds a dozen unrelated stories,
each with its own credits and cast. INDUCKS models that faithfully — issue ->
entry -> story version -> jobs/appearances — so the metadata has to be
flattened back to the one-book-one-record shape ComicInfo assumes. See
``_build_comicinfo`` for the mapping chosen and why.

The same title is also published in dozens of countries: ``it/TL`` is the
Italian *Topolino* and ``se/KA`` the Swedish *Kalle Anka*. Searching without a
country filter is therefore ambiguous by construction, which is what
``get_configured_countries`` exists to prevent.
"""
import os
import re
import sqlite3
import unicodedata
from datetime import datetime
from typing import Optional, Dict, Any, List, Set

from core.app_logging import app_logger

# =============================================================================
# Constants
# =============================================================================

# Every table CLU reads. All of them are required rather than split into core
# and optional sets the way models.gcd does it, and deliberately so: the GCD
# distinction exists because comics.org periodically publishes dumps with tables
# missing, whereas INDUCKS ships one tarball containing the whole database. A
# build missing any of these is a broken build, and saying so through
# test_connection is more useful than silently dropping every credit in the
# library.
INDUCKS_CORE_TABLES = frozenset({
    'inducks_publication', 'inducks_issue', 'inducks_issuedate',
    'inducks_entry', 'inducks_storyversion', 'inducks_story',
    'inducks_storyjob', 'inducks_person', 'inducks_publisher',
    'inducks_publishingjob', 'inducks_character', 'inducks_charactername',
    'inducks_appearance',
})

# ``inducks_storyjob.plotwritartink`` is a single character naming the job.
# "r" also occurs, for retouching, and has no ComicInfo equivalent.
_JOB_WRITER = ('p', 'w')
_JOB_PENCILLER = ('a',)
_JOB_INKER = ('i',)

# ``inducks_storyversion.kind`` codes that are actual comic or text stories, as
# opposed to covers ("c"), illustrations ("i") and other filler. Only these
# contribute to the table of contents and the merged credits.
STORY_KINDS = frozenset({'n', 't'})

# Italian series distinguish their runs with an ordinal, and a filename and
# INDUCKS rarely spell it the same way: a folder says "II Serie" or "2a serie"
# where INDUCKS writes "(Seconda Serie)". Only applied to the token immediately
# before "serie", so an issue titled "II" is untouched.
_SERIES_ORDINALS = {
    'i': 'prima', '1': 'prima', '1a': 'prima', 'prima': 'prima',
    'ii': 'seconda', '2': 'seconda', '2a': 'seconda', 'seconda': 'seconda',
    'iii': 'terza', '3': 'terza', '3a': 'terza', 'terza': 'terza',
    'iv': 'quarta', '4': 'quarta', '4a': 'quarta', 'quarta': 'quarta',
}

# Words carrying no identity in an Italian Disney title, dropped only by the
# last-resort token match. "walt" is here because INDUCKS and the scene
# disagree about it constantly -- "Le grandi storie di Walt Disney" against
# INDUCKS' "Le grandi storie Disney", and the reverse in the next folder along.
# "disney" itself is deliberately NOT here: it is the one word that still
# separates these titles from everything else in a library.
_TOKEN_STOPWORDS = frozenset({
    'di', 'del', 'della', 'dei', 'delle', 'degli', 'de', 'd',
    'la', 'il', 'lo', 'i', 'gli', 'le', 'l', 'e', 'a', 'walt',
})

# A trailing marker naming a slice of a run rather than the run itself:
# "Topolino anno 1975", "Anno 1986 vol 1571-1622", "Albo d'oro v2", the
# "Repack" a scene release adds. Folders are filed this way far more often than
# they are named after the publication, and the tail is pure noise for matching.
_RUN_MARKER_TAIL = re.compile(
    r"[\s_\-]*(?:"
    r"\b(?:anno|annata)\b[\s_\-]*\d{4}(?:[\s_\-]*\d{4})?"
    r"|\bv(?:ol(?:ume)?)?[.\s_\-]*\d{1,3}\b"
    r"|\b(?:numeri|num|vol|volumi|voilumi|n)\b[\s_\-]*\d{1,5}\s*-\s*\d{1,5}"
    r"|\b\d{1,5}\s*-\s*\d{1,5}\b"
    r"|\brepack\b"
    r")\s*$",
    re.I,
)

# INDUCKS writes this in a date column to mean "unknown" rather than leaving it
# empty, so it has to be filtered explicitly everywhere a date is read. It is
# also why the start-year aggregate cannot be a bare MIN() — see
# ``_start_year_and_count``.
_UNKNOWN_DATE_PREFIX = '9999'

# Cached set of INDUCKS tables actually present in the connected database.
_AVAILABLE_TABLES_CACHE: Optional[Set[str]] = None


# =============================================================================
# Helper Functions
# =============================================================================

def normalize_title(text: str) -> str:
    """Fold a title down to a comparable key.

    Accents, punctuation and case vary between filenames and INDUCKS titles, so
    matching happens on a stripped-down form: "Zio Paperone" and
    "zio  paperone!" both become "zio paperone".

    Deliberately not ``models.gcd.normalize_title``: that one drops every
    non-ASCII character, which for this material would fold "Paperinik" and
    "Paperìnik" together while turning a Nordic title into a row of spaces.
    """
    decomposed = unicodedata.normalize('NFKD', str(text or '').lower())
    stripped = ''.join(c for c in decomposed if not unicodedata.combining(c))
    kept = ''.join(c if c.isalnum() else ' ' for c in stripped)
    return ' '.join(kept.split())


def strip_qualifier(title: str) -> str:
    """Remove a trailing parenthetical from a publication title.

    INDUCKS disambiguates reused titles that way — "Topolino (libretto)" — but
    the bare name is what a filename usually carries.
    """
    head = str(title or '').split('(')[0].strip()
    return head or str(title or '').strip()


def _dict_factory(cursor, row):
    """Row factory returning plain dicts, matching models.gcd."""
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


# =============================================================================
# Database Connection
# =============================================================================

def _get_saved_credentials() -> Optional[Dict[str, Any]]:
    """Get INDUCKS credentials saved via the UI."""
    try:
        from core.database import get_provider_credentials
        return get_provider_credentials('inducks')
    except Exception:
        return None


def get_connection_params() -> Optional[Dict[str, Any]]:
    """
    Get INDUCKS SQLite connection parameters.

    Checks saved credentials first, then falls back to the
    INDUCKS_DATABASE_PATH environment variable.

    Returns:
        Dict with database_path, or None if not configured
    """
    saved_creds = _get_saved_credentials()
    if saved_creds and saved_creds.get('database_path'):
        return {'database_path': saved_creds.get('database_path')}

    env_path = os.environ.get('INDUCKS_DATABASE_PATH')
    if env_path:
        return {'database_path': env_path}

    return None


def is_database_available() -> bool:
    """Check whether a configured INDUCKS SQLite file exists on disk."""
    params = get_connection_params()
    path = params.get('database_path') if params else None
    return bool(path and os.path.exists(path))


def check_database_status() -> Dict[str, Any]:
    """Check if the INDUCKS SQLite database is configured and present on disk."""
    try:
        params = get_connection_params()
        path = params.get('database_path') if params else None
        available = bool(path and os.path.exists(path))

        return {
            "inducks_available": available,
            "inducks_path_configured": bool(path),
        }
    except Exception as e:
        return {
            "inducks_available": False,
            "inducks_path_configured": False,
            "error": str(e)
        }


def get_connection():
    """
    Open and return a read-only SQLite connection to the INDUCKS database.

    Returns:
        sqlite3.Connection (dict rows) or None on failure
    """
    try:
        params = get_connection_params()
        if not params or not params.get('database_path'):
            app_logger.error("INDUCKS database not configured (no saved path or INDUCKS_DATABASE_PATH)")
            return None

        path = params['database_path']
        if not os.path.exists(path):
            app_logger.error(f"INDUCKS database file not found: {path}")
            return None

        # Read-only URI: never creates an empty DB on a bad path and never
        # writes -wal/-shm/journal files next to the (700 MB) build.
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = _dict_factory
        return conn
    except sqlite3.Error as e:
        app_logger.error(f"Failed to open INDUCKS SQLite database: {e}")
        return None
    except Exception as e:
        app_logger.error(f"Failed to open INDUCKS SQLite database: {e}")
        return None


# =============================================================================
# Table Availability
# =============================================================================

def get_available_inducks_tables(conn=None, *, force_refresh: bool = False) -> Set[str]:
    """Return which of the required INDUCKS tables are present in the database."""
    global _AVAILABLE_TABLES_CACHE

    if _AVAILABLE_TABLES_CACHE is not None and not force_refresh:
        return _AVAILABLE_TABLES_CACHE

    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    if not conn:
        return set()

    try:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        present = {row['name'] for row in cursor.fetchall()}
        cursor.close()
        available = INDUCKS_CORE_TABLES & present
        _AVAILABLE_TABLES_CACHE = available
        missing = INDUCKS_CORE_TABLES - available
        if missing:
            app_logger.warning(f"INDUCKS database is missing tables: {sorted(missing)}")
        return available
    except Exception as e:
        app_logger.error(f"Could not inspect INDUCKS tables: {e}")
        return set()
    finally:
        if own_conn:
            conn.close()


def invalidate_inducks_table_cache() -> None:
    """Drop the cached table set, after the configured path changes."""
    global _AVAILABLE_TABLES_CACHE
    _AVAILABLE_TABLES_CACHE = None


# =============================================================================
# Database Statistics
# =============================================================================

def get_database_stats() -> Optional[Dict[str, Any]]:
    """Row counts for the settings page, or None if the database is unusable."""
    conn = get_connection()
    if not conn:
        return None
    try:
        cursor = conn.cursor()
        stats: Dict[str, Any] = {}
        for label, table in (
            ('publications', 'inducks_publication'),
            ('issues', 'inducks_issue'),
            ('entries', 'inducks_entry'),
        ):
            try:
                cursor.execute(f"SELECT COUNT(*) AS n FROM {table}")
                stats[label] = cursor.fetchone()['n']
            except sqlite3.Error:
                stats[label] = None
        cursor.execute("SELECT COUNT(DISTINCT countrycode) AS n FROM inducks_publication")
        stats['countries'] = cursor.fetchone()['n']
        cursor.close()
        return stats
    except Exception as e:
        app_logger.error(f"INDUCKS get_database_stats failed: {e}")
        return None
    finally:
        conn.close()


# =============================================================================
# Preferences
# =============================================================================

def get_configured_countries() -> List[str]:
    """Country codes to filter INDUCKS publications by, from the user preference.

    The same title is published in dozens of countries, so an unfiltered search
    is ambiguous for nearly every Disney name there is. Mirrors
    ``models.gcd.get_configured_languages``: one accessor, read at call time, so
    that automatic and manual lookups can never drift apart.

    Falls back to ``['us']``, which keeps a freshly configured provider harmless
    rather than confidently Italian.
    """
    try:
        from core.database import get_user_preference
        raw = get_user_preference('inducks_countries', default='us')
    except Exception as e:
        app_logger.warning(f"Could not read inducks_countries preference: {e}")
        return ['us']

    codes = [code.strip().lower() for code in str(raw or '').split(',') if code.strip()]
    return codes or ['us']


# =============================================================================
# Series Search
# =============================================================================

def _publication_rows(conn, country_codes: List[str]) -> List[Dict[str, Any]]:
    """Every titled publication in the given countries."""
    placeholders = ','.join('?' for _ in country_codes)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT publicationcode, title, countrycode, languagecode "
        "FROM inducks_publication "
        f"WHERE countrycode IN ({placeholders}) AND title IS NOT NULL AND title <> ''",
        tuple(country_codes),
    )
    rows = cursor.fetchall()
    cursor.close()
    return rows


def _colliding_stripped_titles(conn) -> Set[str]:
    """Normalised qualifier-stripped titles claimed by more than one publication.

    Computed across every country, not just the configured ones: the qualifier
    is what tells "Topolino (libretto)" from "Topolino (giornale)", and dropping
    it from the series name would make a reader merge two unrelated runs into
    one shelf entry.
    """
    counts: Dict[str, int] = {}
    cursor = conn.cursor()
    cursor.execute("SELECT title FROM inducks_publication WHERE title IS NOT NULL AND title <> ''")
    for row in cursor.fetchall():
        key = normalize_title(strip_qualifier(row['title']))
        if key:
            counts[key] = counts.get(key, 0) + 1
    cursor.close()
    return {key for key, count in counts.items() if count > 1}


def series_name_for(conn, title: str, colliding: Optional[Set[str]] = None) -> str:
    """The name to write into ComicInfo's Series field.

    The parenthetical qualifier is dropped, so "Topolino (libretto)" becomes
    plain "Topolino" — except where dropping it would collide with another
    publication, in which case the qualifier is what keeps two unrelated runs
    apart and is kept.

    ``colliding`` lets a caller naming several publications compute the
    collision set once instead of rescanning the publication table per title.
    """
    stripped = strip_qualifier(title)
    if stripped == title:
        return title
    if colliding is None:
        colliding = _colliding_stripped_titles(conn)
    return title if normalize_title(stripped) in colliding else stripped


def normalise_ordinals(key: str) -> str:
    """Rewrite an ordinal naming a series run into the word INDUCKS uses.

    "i grandi classici disney ii serie" becomes "... seconda serie", which is
    what INDUCKS calls it. Only the token immediately before "serie" is
    rewritten, so a title that merely contains a numeral is left alone.
    """
    tokens = key.split()
    out = []
    for index, token in enumerate(tokens):
        following = tokens[index + 1] if index + 1 < len(tokens) else ''
        out.append(_SERIES_ORDINALS[token]
                   if token in _SERIES_ORDINALS and following == 'serie'
                   else token)
    return ' '.join(out)


def significant_tokens(key: str) -> frozenset:
    """The words of a normalised title that actually identify it."""
    return frozenset(token for token in key.split() if token not in _TOKEN_STOPWORDS)


def strip_run_marker(name: str) -> str:
    """Drop a trailing marker naming a slice of a run rather than the run.

    Folders in a Disney library are filed by year far more often than by
    publication — "Topolino anno 1975", "Anno 1986 vol 1571-1622" — and the
    tail is noise for matching. Applied repeatedly, since the markers stack.
    """
    previous = None
    while previous != name:
        previous = name
        name = _RUN_MARKER_TAIL.sub('', name).strip(' _-')
    return name


def _search_keys(series_name: str) -> List[str]:
    """Lookup keys for a series name, most specific first."""
    keys: List[str] = []
    for variant in (series_name, strip_qualifier(series_name)):
        key = normalize_title(variant)
        for candidate in (key, normalise_ordinals(key)):
            if candidate and candidate not in keys:
                keys.append(candidate)
    return keys


def _start_year_and_count(conn, publicationcode: str) -> Dict[str, Any]:
    """The year a publication began, and how many issues it has.

    ``inducks_publication`` has no start-year column, so the year has to be
    derived from the issues — and the obvious ``MIN(SUBSTR(oldestdate, 1, 4))``
    is wrong twice over. ``oldestdate`` is frequently the empty string, which
    sorts before every real date, and INDUCKS writes ``9999-12-31`` for an
    unknown one. Both have to become NULL before MIN() sees them.

    This is not a cosmetic difference. Measured over the 664 Italian
    publications that have issues, the naive form yields a blank year for 44 of
    them and the corrected form for 13 — and among the 31 it recovers is
    ``it/TL``, *Topolino (libretto)*, whose real start year is 1949.

    The year matters more than it looks: ``core.bulk_metadata._years_match``
    refuses to auto-accept a series whose year is None, so a publication with no
    derivable start year goes to the review queue on every file forever.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT MIN(NULLIF(NULLIF(SUBSTR(oldestdate, 1, 4), ''), ?)) AS year_began, "
        "       COUNT(*) AS issue_count "
        "FROM inducks_issue WHERE publicationcode = ?",
        (_UNKNOWN_DATE_PREFIX, publicationcode),
    )
    row = cursor.fetchone() or {}
    cursor.close()

    year_began = None
    raw_year = row.get('year_began')
    if raw_year:
        try:
            year_began = int(raw_year)
        except (TypeError, ValueError):
            year_began = None

    return {'year_began': year_began, 'issue_count': row.get('issue_count') or 0}


def search_series(series_name: str, year: Optional[int] = None,
                  country_codes: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Find the INDUCKS publications a series name could refer to.

    Returns *every* publication the name resolves to rather than picking one.
    Disney titles collide constantly — "Almanacco Topolino" is a publication in
    its own right and a qualifier-stripped form of two others — and choosing
    arbitrarily between them is how a library ends up confidently mislabelled.
    Callers that need a single answer should require exactly one result; the
    batch path does, and an ambiguous name goes to the review queue instead.

    The name is looked up in two indexes, exact titles first and
    qualifier-stripped titles second, each fully resolved before falling back to
    the next key. That ordering is what stops a file named "Topolino
    (giornale)" being answered by the publication merely called "Topolino".

    ``year`` orders otherwise-equal candidates by closeness to the year the
    publication began; it never filters, because a folder year that disagrees is
    exactly the case the date check exists to catch.

    Args:
        series_name: Name of the series to search for
        year: Optional year parsed from the folder or filename
        country_codes: Optional country filter. Defaults to the configured
            ``inducks_countries`` preference.

    Returns:
        List of publication dicts, best first. Empty when nothing matched.
    """
    if not series_name or not str(series_name).strip():
        return []

    if country_codes is None:
        country_codes = get_configured_countries()
    if not country_codes:
        return []

    conn = get_connection()
    if not conn:
        return []

    try:
        exact: Dict[str, List[Dict[str, Any]]] = {}
        stripped: Dict[str, List[Dict[str, Any]]] = {}
        by_tokens: Dict[frozenset, List[Dict[str, Any]]] = {}

        for row in _publication_rows(conn, country_codes):
            title = row['title']
            for index, variant in ((exact, title), (stripped, strip_qualifier(title))):
                for key in {normalize_title(variant),
                            normalise_ordinals(normalize_title(variant))}:
                    if key:
                        index.setdefault(key, []).append(row)
            for variant in (title, strip_qualifier(title)):
                tokens = significant_tokens(normalize_title(variant))
                if tokens:
                    by_tokens.setdefault(tokens, []).append(row)

        # Keys run from the most specific form of the name to the least, and
        # the first that matches anything wins outright — a file that spells
        # out "Topolino (giornale)" must never be answered by the publication
        # merely called "Topolino".
        #
        # Within one key, though, both indexes are consulted. A run that
        # continues into a "(seconda serie)" is indexed under the same stripped
        # title as the first series, so taking only the exact match hides the
        # publication that actually holds the later issues. Which of the two is
        # right is settled afterwards by ``narrow_to_issue``, on evidence,
        # rather than by which index answered first.
        matches: List[Dict[str, Any]] = []
        exact_titles: Set[str] = set()
        seen: Set[str] = set()
        for key in _search_keys(series_name):
            for index, is_exact in ((exact, True), (stripped, False)):
                for row in index.get(key, []):
                    code = row['publicationcode']
                    if is_exact:
                        exact_titles.add(code)
                    if code not in seen:
                        seen.add(code)
                        matches.append(row)
            if matches:
                break

        # Last resort: the same significant words in any arrangement, ignoring
        # Italian articles and prepositions. This is what reaches "Le grandi
        # storie di Walt Disney - L'opera omnia di Romano Scarpa" from INDUCKS'
        # "Le grandi storie Disney - ...". Requires the whole token set to
        # match, not a subset, and at least two tokens, so it stays a spelling
        # difference rather than a fuzzy search.
        if not matches:
            tokens = significant_tokens(normalize_title(series_name))
            if len(tokens) >= 2:
                for row in by_tokens.get(tokens, []):
                    if row['publicationcode'] not in seen:
                        seen.add(row['publicationcode'])
                        matches.append(row)

        if not matches:
            return []

        colliding = _colliding_stripped_titles(conn)
        results = []
        for row in matches:
            derived = _start_year_and_count(conn, row['publicationcode'])
            results.append({
                'id': row['publicationcode'],
                'name': series_name_for(conn, row['title'], colliding),
                'title': row['title'],
                'year_began': derived['year_began'],
                'issue_count': derived['issue_count'],
                'country_code': row['countrycode'] or '',
                'language_code': row['languagecode'] or '',
                'exact_title': row['publicationcode'] in exact_titles,
            })

        # Closest start year first when the caller supplied one, so a name that
        # names two runs of the same publication is at least offered in a useful
        # order. Undated candidates sort last; they can never auto-accept.
        if year:
            results.sort(key=lambda r: abs(r['year_began'] - year) if r['year_began'] else 10_000)
        else:
            results.sort(key=lambda r: (r['year_began'] is None, -(r['issue_count'] or 0)))

        return results
    except sqlite3.Error as db_error:
        app_logger.error(f"Database error in INDUCKS search_series: {db_error}")
        return []
    except Exception as e:
        app_logger.error(f"Exception in INDUCKS search_series: {e}")
        return []
    finally:
        conn.close()


def issue_dates(publicationcode: str, issue_numbers: List[str],
                conn=None) -> Dict[str, Optional[str]]:
    """The publication date of each of these issue numbers, where it has one.

    Leading zeros are stripped on both sides, since INDUCKS stores an
    unpadded number and CLU parses a padded one. A number absent from the
    result is absent from the publication.
    """
    wanted = {(str(n or '').strip().lstrip('0') or '0') for n in issue_numbers if str(n or '').strip()}
    if not publicationcode or not wanted:
        return {}

    own_conn = conn is None
    if own_conn:
        conn = get_connection()
    if not conn:
        return {}
    try:
        cursor = conn.cursor()
        # Matched in SQL on the number as stored and as zero-padded to the four
        # widths INDUCKS and CLU between them produce, rather than by scanning
        # the publication: Topolino alone has 3,775 issues, and this runs once
        # per candidate per file.
        placeholders = ','.join('?' for _ in range(len(wanted) * 5))
        variants = [v for n in wanted
                    for v in (n, n.zfill(2), n.zfill(3), n.zfill(4), n.zfill(5))]
        cursor.execute(
            "SELECT issuenumber, "
            "       NULLIF(NULLIF(oldestdate, ''), '9999-12-31') AS oldestdate "
            f"FROM inducks_issue WHERE publicationcode = ? AND issuenumber IN ({placeholders})",
            (publicationcode, *variants),
        )
        found = {}
        for row in cursor.fetchall():
            key = (row['issuenumber'] or '').strip().lstrip('0') or '0'
            if key in wanted and key not in found:
                found[key] = row['oldestdate']
        cursor.close()
        return found
    except Exception as e:
        app_logger.error(f"INDUCKS issue_dates failed: {e}")
        return {}
    finally:
        if own_conn:
            conn.close()


def narrow_to_issue(candidates: List[Dict[str, Any]], issue_number: str,
                    year: Optional[int] = None,
                    tolerance: int = 2) -> List[Dict[str, Any]]:
    """Reduce candidate publications to those that can hold this issue.

    A Disney title names several runs, and the title alone cannot say which.
    The issue number can: of the eleven Italian publications whose name reduces
    to "Topolino", only ``it/TL`` has an issue 1500 at all. This is evidence
    the caller already has, not a heuristic — and it can only remove candidates,
    never introduce one the name did not produce.

    Applied in order, each step only when more than one candidate survives the
    last:

    1. the publication actually contains that issue number;
    2. that issue's own date is within ``tolerance`` of the year the filename
       claims, where the filename claims one;
    3. the title matched exactly rather than with its qualifier stripped.

    Returns every survivor. Two candidates that all three tests cannot separate
    are genuinely ambiguous and the caller should decline rather than pick.
    """
    raw = str(issue_number or '').strip()
    if not candidates or not raw:
        return list(candidates)
    # "000" is issue 0, not the absence of one, so the default only applies
    # after we know a number was supplied at all.
    number = raw.lstrip('0') or '0'

    conn = get_connection()
    if not conn:
        return list(candidates)
    try:
        dates = {c['id']: issue_dates(c['id'], [number], conn=conn).get(number, ...)
                 for c in candidates}
    finally:
        conn.close()

    holding = [c for c in candidates if dates.get(c['id']) is not ...]
    if len(holding) <= 1:
        return holding

    if year:
        dated = []
        for candidate in holding:
            date = dates.get(candidate['id']) or ''
            if len(date) >= 4 and date[:4].isdigit() and abs(int(date[:4]) - year) <= tolerance:
                dated.append(candidate)
        if dated:
            holding = dated
    if len(holding) <= 1:
        return holding

    titled = [c for c in holding if c.get('exact_title')]
    return titled if len(titled) == 1 else holding


def get_series(publicationcode: str) -> Optional[Dict[str, Any]]:
    """One publication by its INDUCKS code, in the shape search_series returns."""
    if not publicationcode:
        return None

    conn = get_connection()
    if not conn:
        return None
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT publicationcode, title, countrycode, languagecode "
            "FROM inducks_publication WHERE publicationcode = ?",
            (publicationcode,),
        )
        row = cursor.fetchone()
        cursor.close()
        if not row:
            return None

        derived = _start_year_and_count(conn, publicationcode)
        return {
            'id': row['publicationcode'],
            'name': series_name_for(conn, row['title'] or ''),
            'title': row['title'] or '',
            'year_began': derived['year_began'],
            'issue_count': derived['issue_count'],
            'country_code': row['countrycode'] or '',
            'language_code': row['languagecode'] or '',
        }
    except Exception as e:
        app_logger.error(f"INDUCKS get_series failed: {e}")
        return None
    finally:
        conn.close()


def get_issues(publicationcode: str) -> List[Dict[str, Any]]:
    """Every issue of a publication, oldest first."""
    if not publicationcode:
        return []

    conn = get_connection()
    if not conn:
        return []
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT issuecode, issuenumber, title, "
            "       NULLIF(NULLIF(oldestdate, ''), '9999-12-31') AS oldestdate "
            "FROM inducks_issue WHERE publicationcode = ? "
            "ORDER BY CASE WHEN issuenumber GLOB '[0-9]*' "
            "              THEN printf('%010d', CAST(issuenumber AS INTEGER)) "
            "              ELSE issuenumber END",
            (publicationcode,),
        )
        rows = cursor.fetchall()
        cursor.close()
        return [{
            'id': row['issuecode'],
            'issue_number': (row['issuenumber'] or '').strip(),
            'title': (row['title'] or '').strip() or None,
            'cover_date': row['oldestdate'],
        } for row in rows]
    except Exception as e:
        app_logger.error(f"INDUCKS get_issues failed: {e}")
        return []
    finally:
        conn.close()


# =============================================================================
# Issue Details
# =============================================================================

def _publisher(conn, issuecode: str) -> str:
    """The publisher of an issue.

    INDUCKS may attach several publishers to one issue, listed oldest first —
    Topolino carries Mondadori, Disney Italia and Panini Comics. The most recent
    one is the imprint that actually produced a modern issue, so the last row
    wins.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COALESCE(p.publishername, j.publisherid) AS name "
        "FROM inducks_publishingjob j "
        "LEFT JOIN inducks_publisher p ON p.publisherid = j.publisherid "
        "WHERE j.issuecode = ?",
        (issuecode,),
    )
    names = [row['name'] for row in cursor.fetchall() if row['name']]
    cursor.close()
    return names[-1] if names else ''


def _issue_date(conn, issuecode: str, fallback: str) -> str:
    """The publication date of an issue as 'YYYY-MM-DD', or '' if unknown.

    ``inducks_issuedate`` carries the precise date where one is known and
    ``inducks_issue.oldestdate`` is the fallback. Either may be the 9999
    sentinel, which must become empty rather than a year 9999 that the date
    check would read as a nine-thousand-year conflict.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT date FROM inducks_issuedate WHERE issuecode = ? ORDER BY date LIMIT 1",
        (issuecode,),
    )
    row = cursor.fetchone()
    cursor.close()
    date = (row['date'] if row else '') or fallback or ''
    return '' if date.startswith(_UNKNOWN_DATE_PREFIX) else date


def _story_credits(conn, storyversioncode: str) -> Dict[str, List[str]]:
    """Writer/penciller/inker names for one story version, in INDUCKS order."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT j.plotwritartink AS job, COALESCE(p.fullname, j.personcode) AS name "
        "FROM inducks_storyjob j "
        "LEFT JOIN inducks_person p ON p.personcode = j.personcode "
        "WHERE j.storyversioncode = ?",
        (storyversioncode,),
    )
    rows = cursor.fetchall()
    cursor.close()

    credits: Dict[str, List[str]] = {'writers': [], 'pencillers': [], 'inkers': []}
    for row in rows:
        name, job = row['name'], row['job']
        if not name:
            continue
        if job in _JOB_WRITER and name not in credits['writers']:
            credits['writers'].append(name)
        elif job in _JOB_PENCILLER and name not in credits['pencillers']:
            credits['pencillers'].append(name)
        elif job in _JOB_INKER and name not in credits['inkers']:
            credits['inkers'].append(name)
    return credits


def _story_characters(conn, storyversioncode: str, language: str) -> List[str]:
    """Character names for one story version, preferring the localised name.

    A character may carry several names in one language, so the join is
    restricted to the preferred one; without that restriction the row multiplies
    and the same character appears two or three times over.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COALESCE(NULLIF(cn.charactername, ''), c.charactername) AS name "
        "FROM inducks_appearance a "
        "LEFT JOIN inducks_character c ON c.charactercode = a.charactercode "
        "LEFT JOIN inducks_charactername cn "
        "       ON cn.charactercode = a.charactercode "
        "      AND cn.languagecode = ? AND cn.preferred = 'Y' "
        "WHERE a.storyversioncode = ?",
        (language or 'en', storyversioncode),
    )
    rows = cursor.fetchall()
    cursor.close()

    seen: List[str] = []
    for row in rows:
        if row['name'] and row['name'] not in seen:
            seen.append(row['name'])
    return seen


def _stories(conn, issuecode: str, language: str) -> List[Dict[str, Any]]:
    """Everything printed inside an issue, in printed order."""
    cursor = conn.cursor()
    cursor.execute(
        "SELECT e.position, e.title, e.storyversioncode, "
        "       COALESCE(sv.kind, '') AS kind, COALESCE(s.title, '') AS storytitle "
        "FROM inducks_entry e "
        "LEFT JOIN inducks_storyversion sv ON sv.storyversioncode = e.storyversioncode "
        "LEFT JOIN inducks_story s ON s.storycode = sv.storycode "
        "WHERE e.issuecode = ? ORDER BY e.position",
        (issuecode,),
    )
    rows = cursor.fetchall()
    cursor.close()

    stories = []
    for row in rows:
        story = {
            'position': row['position'] or '',
            'title': (row['title'] or row['storytitle'] or '').strip(),
            'kind': row['kind'],
            'writers': [],
            'pencillers': [],
            'inkers': [],
            'characters': [],
        }
        code = row['storyversioncode']
        if code:
            story.update(_story_credits(conn, code))
            story['characters'] = _story_characters(conn, code, language)
        stories.append(story)
    return stories


def _merge(groups: List[List[str]]) -> Optional[str]:
    """Flatten per-story credit lists into one comma-separated string.

    Deduplicated, preserving first-appearance order, so the writer of the lead
    story is named first.
    """
    seen: List[str] = []
    for group in groups:
        for value in group:
            if value not in seen:
                seen.append(value)
    return ', '.join(seen) if seen else None


def _summary(stories: List[Dict[str, Any]]) -> Optional[str]:
    """The album's table of contents, one line per story.

    ComicInfo has no representation for "one album, twelve unrelated stories",
    so a mapping has to be chosen. This one keeps the album as the unit —
    ``Series`` and ``Number`` name the album — and puts the contents in
    ``Summary``, because that is what the file on disk actually is and what a
    reader such as Komga displays.
    """
    lines = [s['title'] for s in stories if s['kind'] in STORY_KINDS and s['title']]
    return '\n'.join(f'- {title}' for title in lines) if lines else None


def get_issue_metadata(publicationcode: str, issue_number: str) -> Optional[Dict[str, Any]]:
    """
    Get ComicInfo-compatible metadata for one issue of an INDUCKS publication.

    The lookup deliberately goes through ``publicationcode`` plus
    ``issuenumber`` rather than composing an ``issuecode``: INDUCKS pads the
    number inside the code to a variable width ("it/TL 3200" but "ae/DC    0"),
    so composing it by hand is fragile.

    Args:
        publicationcode: INDUCKS publication code, e.g. "it/TL"
        issue_number: Issue number as printed, e.g. "3200"

    Returns:
        Dict of ComicInfo fields, or None if the issue is not in the database
    """
    if not publicationcode or issue_number is None or str(issue_number).strip() == '':
        return None

    conn = get_connection()
    if not conn:
        return None

    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT issuecode, publicationcode, issuenumber, title, pages, oldestdate "
            "FROM inducks_issue WHERE publicationcode = ? AND issuenumber = ?",
            (publicationcode, str(issue_number).strip()),
        )
        issue = cursor.fetchone()
        cursor.close()

        if not issue:
            app_logger.debug(
                f"INDUCKS get_issue_metadata: issue #{issue_number} not found in {publicationcode}"
            )
            return None

        cursor = conn.cursor()
        cursor.execute(
            "SELECT title, countrycode, languagecode FROM inducks_publication "
            "WHERE publicationcode = ?",
            (publicationcode,),
        )
        publication = cursor.fetchone() or {}
        cursor.close()

        language = (publication.get('languagecode') or '').strip()
        issuecode = issue['issuecode']
        stories = _stories(conn, issuecode, language)

        return _build_comicinfo(
            issuecode=issuecode,
            series=series_name_for(conn, publication.get('title') or publicationcode),
            number=(issue['issuenumber'] or str(issue_number)).strip(),
            title=(issue['title'] or '').strip(),
            publisher=_publisher(conn, issuecode),
            pages=issue['pages'],
            language=language,
            date=_issue_date(conn, issuecode, issue['oldestdate'] or ''),
            stories=stories,
        )
    except sqlite3.Error as db_error:
        app_logger.error(f"Database error in INDUCKS get_issue_metadata: {db_error}")
        return None
    except Exception as e:
        app_logger.error(f"Exception in INDUCKS get_issue_metadata: {e}")
        return None
    finally:
        conn.close()


def issue_url(issuecode: str) -> str:
    """The inducks.org page for an issue, for ComicInfo's Web field."""
    return f"https://inducks.org/issue.php?c={str(issuecode).replace(' ', '+')}"


def _build_comicinfo(*, issuecode: str, series: str, number: str, title: str,
                     publisher: str, pages: Any, language: str, date: str,
                     stories: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Flatten one anthology issue into the ComicInfo fields CLU writes.

    Credits and characters are merged across the stories rather than reported
    per story, because ComicInfo has one Writer field and not twelve. Covers and
    illustrations are excluded from the merge — a cover artist is not the
    penciller of the book — but the issue keeps them in the database.

    Every key here survives ``core.comicinfo.generate_comicinfo_xml``'s
    allowlist; a key added that is not in that allowlist would be computed and
    silently dropped.
    """
    told = [s for s in stories if s['kind'] in STORY_KINDS]
    retrieved = datetime.now().strftime('%Y-%m-%d')

    metadata = {
        'Series': series or None,
        'Number': number or None,
        'Title': title or None,
        'Summary': _summary(stories),
        'Publisher': publisher or None,
        'Year': int(date[:4]) if len(date) >= 4 and date[:4].isdigit() else None,
        'Month': int(date[5:7]) if len(date) >= 7 and date[5:7].isdigit() else None,
        'Day': int(date[8:10]) if len(date) >= 10 and date[8:10].isdigit() else None,
        'PageCount': pages or None,
        'LanguageISO': language or None,
        'Writer': _merge([s['writers'] for s in told]),
        'Penciller': _merge([s['pencillers'] for s in told]),
        'Inker': _merge([s['inkers'] for s in told]),
        'Characters': _merge([s['characters'] for s in told]),
        'Web': issue_url(issuecode),
        # Notes has to start with a recognisable source string: #524 centralised
        # the "already tagged, skip this file" check on it.
        'Notes': f'Metadata from INDUCKS. Issue: {issuecode} — retrieved {retrieved}.',
    }

    return {k: v for k, v in metadata.items() if v is not None}
