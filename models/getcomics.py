"""
GetComics.org search and download functionality.
Uses cloudscraper to bypass Cloudflare protection.
"""
import cloudscraper
from bs4 import BeautifulSoup
from dataclasses import dataclass, field
import logging
import re
import sqlite3

# ── Config caching ────────────────────────────────────────────────────────────
# Cached config sets — loaded once per process, invalidated on config change.
# Access via _get_format_variants(), _get_crossover_keywords(), etc.
_cached_pub_types: set[str] | None = None
_cached_var_types: list[str] | None = None
_cached_fmt_variants: list[str] | None = None
_cached_crossover_kws: list[str] | None = None
_cache_valid = False


def _load_config_caches():
    """Load all config caches. Called lazily on first access."""
    global _cache_valid, _cached_pub_types, _cached_var_types, _cached_fmt_variants, _cached_crossover_kws
    if _cache_valid:
        return
    _cache_valid = True
    try:
        from core.config import config
        pub_str = config.get("SETTINGS", "PUBLICATION_TYPES", fallback="annual,quarterly")
        _cached_pub_types = {v.strip().lower() for v in pub_str.split(",") if v.strip()}
        var_str = config.get(
            "SETTINGS", "VARIANT_TYPES",
            fallback="annual,quarterly,tpB,oneshot,one-shot,o.s.,os,trade paperback,trade-paperback,omni,omnibus,omb,hardcover,deluxe,prestige,gallery,absolute"
        )
        _cached_var_types = [v.strip().lower() for v in var_str.split(",") if v.strip()]
        _cached_fmt_variants = [v for v in _cached_var_types if v not in _cached_pub_types]
        kw_str = config.get(
            "SETTINGS", "CROSSOVER_KEYWORDS", fallback="meets,vs,versus,x-over,crossover"
        )
        _cached_crossover_kws = [v.strip().lower() for v in kw_str.split(",") if v.strip()]
    except Exception:
        _cache_valid = False


def _clear_config_caches():
    """Clear config caches — call after config changes."""
    global _cache_valid, _cached_pub_types, _cached_var_types, _cached_fmt_variants, _cached_crossover_kws
    _cache_valid = False
    _cached_pub_types = None
    _cached_var_types = None
    _cached_fmt_variants = None
    _cached_crossover_kws = None


def get_publication_types():
    _load_config_caches()
    return list(_cached_pub_types) if _cached_pub_types else ["annual", "quarterly"]


def get_variant_types():
    _load_config_caches()
    return list(_cached_var_types) if _cached_var_types else [
        'annual', 'quarterly', 'tpb', 'oneshot', 'one-shot', 'o.s.', 'os',
        'trade paperback', 'trade-paperback', 'omni', 'omnibus', 'omb',
        'hardcover', 'deluxe', 'prestige', 'gallery', 'absolute'
    ]


def get_format_variants():
    _load_config_caches()
    return list(_cached_fmt_variants) if _cached_fmt_variants else [
        'tpb', 'oneshot', 'one-shot', 'o.s.', 'os',
        'trade paperback', 'trade-paperback', 'omni', 'omnibus', 'omb',
        'hardcover', 'deluxe', 'prestige', 'gallery', 'absolute'
    ]


def get_crossover_keywords():
    _load_config_caches()
    return list(_cached_crossover_kws) if _cached_crossover_kws else [
        'meets', 'vs', 'versus', 'x-over', 'crossover'
    ]
import time
from core.app_logging import app_logger

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# DATA CLASSES — Structured representations for parsing and scoring
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ComicTitle:
    """Parsed structure of a GetComics result title."""
    name: str = ""
    issue: str | None = None
    issue_range: tuple[int, int] | None = None  # (start, end)
    year: int | None = None
    publication_year: int | None = None
    volume: int | None = None
    is_annual: bool = False
    is_quarterly: bool = False
    is_arc: bool = False
    arc_name: str | None = None
    format_variants: list[str] = field(default_factory=list)
    is_multi_series: bool = False
    is_range_pack: bool = False
    has_tpb_in_pack: bool = False
    is_bulk_pack: bool = False
    remaining: str = ""  # text after series name (populated during scoring)


@dataclass
class ComicScore:
    """Result of scoring a ComicTitle against search criteria."""
    score: int = 0
    range_contains_target: bool = False
    series_match: bool = False
    sub_series_type: str | None = None  # 'variant', 'arc', 'different_edition', None
    variant_accepted: bool = False
    detected_variant: str | None = None
    used_the_swap: bool = False  # matched using "The " prefix swap
    remaining_is_different_series: bool = False
    year_in_series_name: bool = False  # year-labeled edition (e.g., "2025 Annual")


@dataclass
class SearchCriteria:
    """Search parameters for matching a comic title."""
    series_name: str = ""
    issue_number: str = ""
    year: int | None = None
    series_volume: int | None = None
    volume_year: int | None = None
    publisher_name: str | None = None
    accept_variants: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# SCORING HELPERS — Pure functions for structured scoring
# ─────────────────────────────────────────────────────────────────────────────

def search_criteria(
    series_name: str,
    issue_number: str,
    year: int | None,
    series_volume: int | None = None,
    volume_year: int | None = None,
    publisher_name: str | None = None,
    accept_variants: list | None = None,
) -> SearchCriteria:
    """Build a SearchCriteria from individual parameters."""
    return SearchCriteria(
        series_name=series_name,
        issue_number=str(issue_number),
        year=year,
        series_volume=series_volume,
        volume_year=volume_year,
        publisher_name=publisher_name,
        accept_variants=list(accept_variants) if accept_variants else [],
    )


def _range_contains_target(issue_range_str: str, target_start: int, target_end: int) -> bool:
    """
    Check if a stored issue_range string (e.g. '(2, 5)') overlaps with [target_start, target_end].
    Used for in-memory filtering of range packs after the SQL query returns.
    """
    import ast
    try:
        r_start, r_end = ast.literal_eval(issue_range_str)
        return r_start <= target_start <= r_end or r_start <= target_end <= r_end
    except (ValueError, SyntaxError, TypeError):
        return False


def _detect_range_ends_on_target(title_lower: str, issue_num: str) -> bool:
    """
    Detect if title has a range that ENDS exactly on the target issue.
    When true, the result is a bulk pack ending on that issue → immediate -100.
    Does NOT match ranges that merely contain the target (use _detect_range_contains_target).
    """
    try:
        target_n = float(issue_num) if issue_num.replace('.', '', 1).isdigit() else -1
    except ValueError:
        target_n = -1
    if target_n == -1:
        return False

    # Pattern: " + TPBs (year-year)" at end
    tpbs_match = re.search(r'\s*\+\s*tpbs?\s*\(\d{4}[-\u2013\u2014]\d{4}\)\s*$', title_lower)
    if tpbs_match:
        before_tpbs = title_lower[:tpbs_match.start()]
        range_match = re.search(r'(\d+)\s*[-\u2013\u2014]\s*(\d+)(?:\s*\+\s*tpbs|$)', before_tpbs)
        if range_match:
            r_start, r_end = int(range_match.group(1)), int(range_match.group(2))
            if r_start <= target_n <= r_end and r_end == target_n:
                return True

    # Simple "#N-M" or "Issues N-M" without TPBs
    simple = re.search(r'#(\d+)\s*[-\u2013\u2014]\s*(\d+)', title_lower)
    if not simple:
        simple = re.search(r'\bissues?\s*(\d+)\s*[-\u2013\u2014]\s*(\d+)', title_lower, re.IGNORECASE)
    if simple:
        r_start, r_end = int(simple.group(1)), int(simple.group(2))
        if r_start <= target_n <= r_end and r_end == target_n:
            return True

    return False


def _detect_range_contains_target(title_lower: str, issue_num: str) -> bool:
    """
    Detect if title contains an issue range that includes the target issue.
    Returns True if range contains target (FALLBACK candidate).
    Returns False otherwise.
    """
    issue_str = issue_num
    issue_n = issue_str.lstrip('0') or '0'
    try:
        target_n = float(issue_n) if issue_n.replace('.', '', 1).isdigit() else -1
    except ValueError:
        target_n = -1

    # Pattern: " + TPBs (year-year)" at end
    tpbs_match = re.search(r'\s*\+\s*tpbs?\s*\(\d{4}[-\u2013\u2014]\d{4}\)\s*$', title_lower)
    if tpbs_match:
        before_tpbs = title_lower[:tpbs_match.start()]
        range_match = re.search(r'(\d+)\s*[-\u2013\u2014]\s*(\d+)(?:\s*\+\s*tpbs|$)', before_tpbs)
        if range_match:
            r_start, r_end = int(range_match.group(1)), int(range_match.group(2))
            if target_n != -1 and r_start <= target_n <= r_end:
                if r_end == target_n:
                    # Range ends on target — bulk pack ending on that issue
                    return True
                return True
        # Standalone number before "+ TPBs"
        standalone = re.search(r'\+\s*(\d+)(?:\s*\+\s*tpbs|$)', before_tpbs)
        if standalone and int(standalone.group(1)) == target_n:
            return False  # Not a range

    # Simple "#N-M" or "Issues N-M" without TPBs
    simple = re.search(r'#(\d+)\s*[-\u2013\u2014]\s*(\d+)', title_lower)
    if not simple:
        simple = re.search(r'\bissues?\s*(\d+)\s*[-\u2013\u2014]\s*(\d+)', title_lower, re.IGNORECASE)
    if simple:
        r_start, r_end = int(simple.group(1)), int(simple.group(2))
        if target_n != -1 and r_start <= target_n <= r_end:
            if r_end == target_n:
                return True  # bulk pack ending on target
            return True

    return False


def _score_series_match(
    title_lower: str,
    title_normalized: str,
    search: SearchCriteria,
) -> tuple[int, bool, str | None, str, bool, str | None]:
    """
    Score series name match and detect sub-series type.

    Returns:
        (score_delta, series_match, sub_series_type, remaining,
         used_the_swap, detected_variant)
    """
    series_lower = search.series_name.lower()
    VARIANT_KEYWORDS = get_variant_types()
    score_delta = 0
    series_match = False
    sub_series_type = None
    remaining = ""
    used_the_swap = False
    detected_variant = None

    # Build series name variants to try matching
    series_starts = [series_lower]
    if series_lower.startswith('the '):
        series_starts.append(series_lower[4:])
    else:
        series_starts.append('the ' + series_lower)

    series_normalized = series_lower.replace('&', '+').replace('/', '+').replace(' and ', ' + ')
    if series_normalized != series_lower and series_normalized not in series_starts:
        series_starts.append(series_normalized)

    for start in series_starts:
        for check_title in (title_lower, title_normalized):
            if check_title.startswith(start):
                remaining = check_title[len(start):].strip()
                if series_lower.startswith('the ') and start == series_lower[4:]:
                    used_the_swap = True

                if remaining.startswith(('-', '\u2013', '\u2014')):
                    dash_part = remaining.lstrip('-\u2013\u2014').strip().lower()
                    # Try to match a variant keyword
                    variant_found = False
                    for kw in VARIANT_KEYWORDS:
                        pattern = rf'(?<![a-zA-Z]){re.escape(kw)}(?![a-zA-Z])'
                        if re.search(pattern, dash_part, re.IGNORECASE):
                            sub_series_type = 'variant'
                            detected_variant = kw
                            variant_found = True
                            break
                    if not variant_found:
                        has_vol_before_dash = re.search(r'\bvol\.?\s*\d+\s*$', series_lower, re.IGNORECASE)
                        if not has_vol_before_dash:
                            sub_series_type = 'arc'
                        # else: brand/imprint dash — no sub_series_type
                else:
                    for kw in VARIANT_KEYWORDS:
                        pattern = rf'(?<![a-zA-Z]){re.escape(kw)}(?![a-zA-Z])'
                        if re.search(pattern, remaining, re.IGNORECASE):
                            sub_series_type = 'variant'
                            detected_variant = kw
                            break

                series_match = True
                break
        if series_match:
            break
        # Brand era fallback
        if series_has_same_brand(search.series_name, title_lower, search.publisher_name):
            brands = get_brand_keywords(search.publisher_name)
            for brand in brands:
                if brand in series_lower and brand in title_lower:
                    series_base = series_lower.replace(brand, '').strip()
                    if series_base and title_lower.startswith(series_base):
                        series_match = True
                        remaining = title_lower[len(series_base):].strip()
                        break
            if series_match:
                break

    if series_match:
        score_delta = 30

    return score_delta, series_match, sub_series_type, remaining, used_the_swap, detected_variant


def get_publication_types():
    """
    Get publication types from config settings.
    Publication types (e.g., 'annual', 'quarterly') create DIFFERENT series,
    not format variants. These are used to distinguish between:
    - "Batman Annual" (different series from "Batman")
    - "Batman Vol. 3" (same series, different volume)

    Returns:
        list of publication type keywords, or ['annual', 'quarterly'] as fallback
    """
    try:
        from core.config import config
        pub_types_str = config.get("SETTINGS", "PUBLICATION_TYPES", fallback="annual,quarterly")
        return [v.strip().lower() for v in pub_types_str.split(",") if v.strip()]
    except Exception:
        return ['annual', 'quarterly']


def get_variant_types():
    """
    Get variant types from config settings.
    Variant types include both publication types AND format variants.
    Format variants (tpB, omnibus, oneshot, etc.) describe the format of
    a collected edition but are still the SAME content.

    Returns:
        list of variant type keywords, or defaults from config
    """
    try:
        from core.config import config
        var_types_str = config.get(
            "SETTINGS",
            "VARIANT_TYPES",
            fallback="annual,quarterly,tpB,oneshot,one-shot,o.s.,os,trade paperback,trade-paperback,omni,omnibus,omb,hardcover,deluxe,prestige,gallery,absolute"
        )
        return [v.strip().lower() for v in var_types_str.split(",") if v.strip()]
    except Exception:
        return [
            'annual', 'quarterly', 'tpb', 'oneshot', 'one-shot', 'o.s.', 'os',
            'trade paperback', 'trade-paperback', 'omni', 'omnibus', 'omb',
            'hardcover', 'deluxe', 'prestige', 'gallery', 'absolute'
        ]


def get_format_variants():
    """
    Get format variants = VARIANT_TYPES - PUBLICATION_TYPES.
    Format variants describe the FORMAT (tpB, omnibus, oneshot, hardcover, etc.)
    but are the SAME content, just collected in a different format.

    Publication types (annual, quarterly) create DIFFERENT series and are NOT
    included here.

    Returns:
        list of format variant keywords
    """
    pub_types = set(get_publication_types())
    var_types = get_variant_types()
    return [v for v in var_types if v not in pub_types]


def get_crossover_keywords():
    """
    Get crossover keywords from config settings.
    Crossover keywords (meets, vs, x-over, etc.) separate two series names
    in a crossover/mashup title. Titles with crossover keywords after a
    year-like number are NOT variants of the base series.

    Returns:
        list of crossover keywords, or defaults
    """
    try:
        from core.config import config
        kw_str = config.get(
            "SETTINGS",
            "CROSSOVER_KEYWORDS",
            fallback="meets,vs,versus,x-over,crossover"
        )
        return [v.strip().lower() for v in kw_str.split(",") if v.strip()]
    except Exception:
        return ['meets', 'vs', 'versus', 'x-over', 'crossover']


def get_brand_keywords(publisher_name=None):
    """
    Get brand keywords for scoring.

    Brand keywords (e.g., 'Rebirth', 'New 52', 'Marvel NOW') are era/line identifiers
    that appear in series names. When comparing "Batman Rebirth" vs "Batman Vol. 3 - Rebirth",
    both contain "Rebirth" so they should match despite different volume numbers.

    Args:
        publisher_name: Optional publisher name to get publisher-specific brands

    Returns:
        list of brand keywords (lowercase)
    """
    try:
        from core.database import get_publisher_brand_keywords_with_defaults
        if publisher_name:
            keywords = get_publisher_brand_keywords_with_defaults(publisher_name)
        else:
            # No publisher specified - no brand keywords to match against
            return []
        if keywords:
            return [kw.lower() for kw in keywords]
        return []
    except Exception:
        return []


def extract_brand_from_title(title: str) -> list:
    """
    Extract brand keywords found in a title.

    Args:
        title: GetComics result title

    Returns:
        list of brand keywords found (lowercase)
    """
    title_lower = title.lower()
    brands = get_brand_keywords()
    found = []
    for brand in brands:
        # Use word boundaries to avoid partial matches
        pattern = rf'(?<![a-zA-Z]){re.escape(brand)}(?![a-zA-Z])'
        if re.search(pattern, title_lower):
            found.append(brand)
    return found


def series_has_same_brand(search_series: str, result_title: str, publisher_name: str = None) -> bool:
    """
    Check if search series and result title share the same brand era keyword.

    Args:
        search_series: Series name from CLU (e.g., "Batman Rebirth")
        result_title: GetComics result title (e.g., "Batman Vol. 3 - Rebirth #1")
        publisher_name: Publisher name for brand keyword lookup (e.g., "DC", "Marvel")

    Returns:
        True if both contain the same brand keyword, False otherwise
    """
    # Extract brand from search series
    search_lower = search_series.lower()
    result_lower = result_title.lower()

    brands = get_brand_keywords(publisher_name)

    # If no brand keywords configured, log suggestion and return False
    if not brands:
        logger.info(
            f"No brand keywords configured for series matching. "
            f"Consider adding brand keywords (e.g., 'rebirth', 'new 52') to publisher settings."
        )
        return False

    search_brands = []
    result_brands = []

    for brand in brands:
        pattern = rf'(?<![a-zA-Z]){re.escape(brand)}(?![a-zA-Z])'
        if re.search(pattern, search_lower):
            search_brands.append(brand)
        if re.search(pattern, result_lower):
            result_brands.append(brand)

    # If both have at least one common brand, they're from the same era
    if search_brands and result_brands:
        return bool(set(search_brands) & set(result_brands))

    return False


# Create a cloudscraper instance for bypassing Cloudflare protection
# This is reused across all requests for efficiency
scraper = cloudscraper.create_scraper(
    browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    }
)


def search_getcomics(query: str, max_pages: int = 3) -> list:
    """
    Search getcomics.org and return list of results.
    Uses cloudscraper to bypass Cloudflare protection.

    Args:
        query: Search query string
        max_pages: Maximum number of pages to search (default 3)

    Returns:
        List of dicts with keys: title, link, image
    """
    results = []
    base_url = "https://getcomics.org"

    for page in range(1, max_pages + 1):
        try:
            url = f"{base_url}/page/{page}/" if page > 1 else base_url
            params = {"s": query}

            logger.info(f"Searching getcomics.org page {page}: {query}")
            resp = scraper.get(url, params=params, timeout=30)
            resp.raise_for_status()

            soup = BeautifulSoup(resp.text, 'html.parser')

            # Find all article posts
            articles = soup.find_all("article", class_="post")
            if not articles:
                logger.info(f"No more results on page {page}")
                break

            for article in articles:
                title_el = article.find("h1", class_="post-title")
                if not title_el:
                    continue

                link_el = title_el.find("a")
                if not link_el:
                    continue

                # Get thumbnail image
                img_el = article.find("img")
                image = ""
                if img_el:
                    # Try data-src first (lazy loading), then src
                    image = img_el.get("data-lazy-src") or img_el.get("data-src") or img_el.get("src", "")

                results.append({
                    "title": title_el.get_text(strip=True),
                    "link": link_el.get("href", ""),
                    "image": image
                })

            logger.info(f"Found {len(articles)} results on page {page}")

        except Exception as e:
            logger.error(f"Error fetching/parsing page {page}: {e}")
            break

    logger.info(f"Total results found: {len(results)}")
    return results


def get_download_links(page_url: str) -> dict:
    """
    Fetch a getcomics page and extract download links.
    Uses cloudscraper to bypass Cloudflare protection.

    Args:
        page_url: URL of the getcomics page

    Returns:
        Dict with keys: pixeldrain, download_now, mega (values are URLs or None)
    """
    try:
        logger.info(f"Fetching download links from: {page_url}")
        resp = scraper.get(page_url, timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, 'html.parser')

        links = {"pixeldrain": None, "download_now": None, "mega": None}

        # Search for download links by title attribute
        for a in soup.find_all("a"):
            title = (a.get("title") or "").upper()
            href = a.get("href", "")

            if not href:
                continue

            if "PIXELDRAIN" in title and not links["pixeldrain"]:
                links["pixeldrain"] = href
                logger.info(f"Found PIXELDRAIN link: {href}")
            elif "DOWNLOAD NOW" in title and not links["download_now"]:
                links["download_now"] = href
                logger.info(f"Found DOWNLOAD NOW link: {href}")
            elif "MEGA" in title and not links["mega"]:
                links["mega"] = href
                logger.info(f"Found MEGA link: {href}")

        # If no links found by title, try button text content
        if not links["pixeldrain"] and not links["download_now"] and not links["mega"]:
            for a in soup.find_all("a", class_="aio-red"):
                text = a.get_text(strip=True).upper()
                href = a.get("href", "")

                if not href:
                    continue

                if "PIXELDRAIN" in text and not links["pixeldrain"]:
                    links["pixeldrain"] = href
                    logger.info(f"Found PIXELDRAIN link (by text): {href}")
                elif "DOWNLOAD" in text and not links["download_now"]:
                    links["download_now"] = href
                    logger.info(f"Found DOWNLOAD link (by text): {href}")
                elif "MEGA" in text and not links["mega"]:
                    links["mega"] = href
                    logger.info(f"Found MEGA link (by text): {href}")

        return links

    except Exception as e:
        logger.error(f"Error fetching/parsing page: {e}")
        return {"pixeldrain": None, "download_now": None, "mega": None}



ACCEPT_THRESHOLD = 40   # score >= this → ACCEPT
FALLBACK_MIN     = 0    # range fallback requires score >= this


def normalize_series_name(name: str) -> tuple[str, dict]:
    """
    Normalize a series name and extract metadata.

    Handles patterns like:
    - "Batman Vol. 3" -> ("Batman", {volume: 3})
    - "Batman V3" -> ("Batman", {volume: 3})
    - "Batman vol 3" -> ("Batman", {volume: 3})
    - "Justice League Dark 2021 Annual" -> ("Justice League Dark 2021 Annual", {}) - year is PART of name
    - "Flash Gordon Annual 2014" -> ("Flash Gordon Annual", {publication_year: 2014})
    - "Batman / Superman" -> ("Batman / Superman", {is_crossover: True})

    Returns:
        (normalized_name, metadata) where metadata contains:
        - volume: extracted volume number (or None)
        - publication_year: year that appears AFTER variant keywords (or None)
        - is_annual: True if "annual" in name
        - is_quarterly: True if "quarterly" in name
        - is_crossover: True if name contains /, +, or &
    """
    import re

    if not name:
        return "", {}

    original = name
    name = name.strip()

    metadata = {
        'volume': None,
        'publication_year': None,
        'is_annual': False,
        'is_quarterly': False,
        'is_crossover': False,
    }

    # Check for crossovers
    if '/' in name or '+' in name or '&' in name:
        metadata['is_crossover'] = True

    # Normalize multiple spaces to single space
    name = re.sub(r'\s+', ' ', name)

    # Extract volume number
    # Patterns: "Vol. 3", "Vol 3", "V3", "V.3", "Volume 3", "volume 3"
    volume_match = re.search(r'\b(?:vol\.?|v(?:ol(?:ume)?)?)\s*\.?\s*(\d+)', name, re.IGNORECASE)
    if volume_match:
        metadata['volume'] = int(volume_match.group(1))
        # Remove volume designation from name
        name = re.sub(r'\b(?:vol\.?|v(?:ol(?:ume)?)?)\s*\.?\s*\d+', '', name, flags=re.IGNORECASE)
        name = re.sub(r'\s+', ' ', name).strip()

    # Check for publication types and extract publication year
    # Publication year appears AFTER the variant keyword
    # e.g., "Flash Gordon Annual 2014" - 2014 is publication year
    # But "Justice League Dark 2021 Annual" - 2021 is part of series name
    for kw in get_publication_types():
        if re.search(rf'\b{kw}\b', name, re.IGNORECASE):
            metadata[f'is_{kw}'] = True
            # Look for year AFTER the keyword
            year_match = re.search(rf'\b{kw}\b\s+(\d{{4}})', name, re.IGNORECASE)
            if year_match:
                metadata['publication_year'] = int(year_match.group(1))

    # Clean up name
    name = name.strip()
    # Remove trailing punctuation
    name = name.rstrip('.,')

    return name, metadata


def normalize_series_for_compare(name: str) -> str:
    """
    Normalize a series name for comparison.

    This normalizes various separators so that names like:
    - "Batman - Year One" and "Batman: Year One" match
    - "Batman & Robin" and "Batman and Robin" match
    - "Batman / Superman" and "Batman + Superman" match

    Normalization:
    - Crossover separators: &, /, and -> +
    - Title separators: :, -, –, — -> space
    - Collapse multiple spaces

    Args:
        name: Series name to normalize

    Returns:
        Normalized series name for comparison
    """
    if not name:
        return ""

    name = name.lower().strip()
    # Normalize crossover separators
    name = name.replace('&', '+').replace('/', '+').replace(' and ', ' + ')
    # Normalize title separators
    name = re.sub(r'[-–—:]', ' ', name)
    # Collapse multiple spaces
    name = re.sub(r'\s+', ' ', name).strip()

    return name


def parse_result_title(title: str) -> ComicTitle:
    """
    Parse a GetComics result title into a ComicTitle dataclass.

    This function extracts ALL values from the title in a single pass,
    then constructs the series name by removing all found patterns.

    Returns:
        ComicTitle with all parsed fields populated.
        Empty ComicTitle (all defaults) if title is empty.
    """
    if not title:
        return ComicTitle()

    original = title

    # Normalize common encoding artifacts from GetComics pages.
    # BeautifulSoup sometimes mis-decodes Windows-1252/ISO-8859-1 as UTF-8,
    # producing replacement characters (U+FFFD) or wrong bytes. We also handle
    # raw Windows-1252 bytes that appear when UTF-8 decoding fails.
    title = title.replace('\x96', '-').replace('\x97', '-')  # Windows-1252 dashes
    title = title.replace('\x91', "'").replace('\x92', "'")  # Windows-1252 quotes
    title = title.replace('\x93', '"').replace('\x94', '"')  # Windows-1252 quotes
    title = title.replace('\ufffd', '-')  # U+FFFD replacement character often means a missing dash
    # Heuristic: "digit U+FFFD digit" in issue ranges like "Top 10 #1 � 12" — replace
    # U+FFFD (0xFFFD) that sits between digits with a dash so ranges parse correctly.
    title = re.sub(r'(\d)\ufffd(\d)', r'\1-\2', title)

    # Fields collected during parsing
    parsed_issue = None
    parsed_issue_range = None
    parsed_year = None
    parsed_volume = None
    parsed_publication_year = None
    parsed_is_annual = False
    parsed_is_quarterly = False
    parsed_is_arc = False
    parsed_arc_name = None
    parsed_format_variants: list[str] = []
    parsed_is_multi_series = False
    parsed_is_range_pack = False
    parsed_has_tpb_in_pack = False
    parsed_is_bulk_pack = False

    # Track all matched patterns so we can remove them from the title to get the series name
    matched_patterns = []

    # Extract year from parentheses at end: "(2020)"
    year_match = re.search(r'\((\d{4})\)\s*$', title)
    if year_match:
        parsed_year = int(year_match.group(1))
        matched_patterns.append((year_match.start(), year_match.end()))

    # Extract issue number and range: "#1", "#1-50", "#1 – 19", "Issue 5", "Issues 1-12"
    issue_match = re.search(r'#(\d+(?:\s*[-\u2013\u2014]\s*\d+)?)', title, re.IGNORECASE)
    if not issue_match:
        issue_match = re.search(r'\bissues?\s*(\d+(?:\s*[-\u2013\u2014]\s*\d+)?)\b', title, re.IGNORECASE)
    if issue_match:
        issue_str = issue_match.group(1)
        dash_match = re.search(r'\s*[-\u2013\u2014]\s*', issue_str)
        if dash_match:
            parts = re.split(r'\s*[-\u2013\u2014]\s*', issue_str)
            parsed_issue_range = (int(parts[0]), int(parts[1]))
            parsed_issue = issue_str
        else:
            parsed_issue = issue_str
        matched_patterns.append((issue_match.start(), issue_match.end()))

    # Extract volume
    volume_match = re.search(r'\b(?:vol\.?|v(?:ol(?:ume)?)?)\s*\.?\s*(\d+)', title, re.IGNORECASE)
    if volume_match:
        parsed_volume = int(volume_match.group(1))
        matched_patterns.append((volume_match.start(), volume_match.end()))

    # Format variants
    format_variants = get_format_variants()
    for variant in format_variants:
        variant_escaped = re.escape(variant)
        pattern = rf'\+?\s*{variant_escaped}(?:s)?\b'
        variant_match = re.search(pattern, title, re.IGNORECASE)
        if variant_match:
            parsed_format_variants.append(variant)
            matched_patterns.append((variant_match.start(), variant_match.end()))

    # Arc notation
    arc_match = re.search(r'[-–—]\s*(.+?)\s*(?:#|$)', title)
    if arc_match:
        potential_arc = arc_match.group(1).strip()
        arc_start = arc_match.start()
        pub_type_end_pattern = r'^(\d{4}\s+)?(' + '|'.join(get_publication_types()) + r')$'
        prefix_match = re.search(r'([^#\d]+)\s*[-–—]\s*.+$', title)
        if prefix_match:
            prefix = prefix_match.group(1).strip()
            has_volume_before_dash = re.search(r'\bvol\.?\s*\d+\s*$', prefix, re.IGNORECASE)
            # If the title at the arc position starts with "prefix-M", the hyphen is part of
            # a hyphenated name (Spider-Man, X-Men) — the dash is NOT an arc separator.
            # prefix_match.group(1) is the non-digit/hash prefix, but the regex greedily
            # stops before the arc dash. Check if the title resumes with "-M" at that point.
            arc_pos_in_full = prefix_match.start(0) + len(prefix_match.group(1))
            full_match_text = prefix_match.group(0)
            title_at_arc = title[arc_pos_in_full:]
            # title[arc_pos_in_full:] starts with the arc dash, e.g. "-Man #1"
            # If what follows is "-Word" (no space after dash), the prefix is hyphenated.
            prefix_is_compound_hyphen = (
                title_at_arc.startswith('-') and
                len(title_at_arc) > 1 and
                title_at_arc[1] not in ' \t\n-' and
                arc_pos_in_full > 0 and
                title[arc_pos_in_full - 1] not in ' \t-'
            )
            if (len(prefix) > 2 and not re.match(pub_type_end_pattern, prefix, re.IGNORECASE)
                    and not has_volume_before_dash and not prefix_is_compound_hyphen):
                parsed_is_arc = True
                parsed_arc_name = potential_arc
                matched_patterns.append((arc_start, len(title)))

    # Publication types (annual, quarterly)
    pub_type_pattern = r'\b(' + '|'.join(get_publication_types()) + r')\b'
    pub_type_match = re.search(pub_type_pattern, title, re.IGNORECASE)
    if pub_type_match:
        keyword = pub_type_match.group(1).lower()
        if keyword == 'annual':
            parsed_is_annual = True
        elif keyword == 'quarterly':
            parsed_is_quarterly = True
        after_keyword = title[pub_type_match.end():]
        year_after_match = re.search(r'\b(\d{4})\b(?!\s*\))', after_keyword)
        if year_after_match:
            parsed_publication_year = int(year_after_match.group(1))

    # Range pack detection
    if re.search(r'#\d+\s*[-–—]\s*\d+', title, re.IGNORECASE):
        parsed_is_range_pack = True
        range_match = re.search(r'#(\d+)\s*[-–—]\s*(\d+)', title, re.IGNORECASE)
        if range_match:
            range_start = int(range_match.group(1))
            range_end = int(range_match.group(2))
            if range_end - range_start >= 10:
                parsed_is_bulk_pack = True
        if parsed_is_range_pack:
            fmt_pattern = r'\+\s*(' + '|'.join(re.escape(v) for v in format_variants) + r')s?\b'
            if re.search(fmt_pattern, title, re.IGNORECASE):
                parsed_has_tpb_in_pack = True

    # Multi-series detection
    title_before_parens = original.split('(')[0]
    title_for_analysis = title_before_parens.lower()
    normalized_for_series = title_for_analysis.replace('&', '+').replace('/', '+').replace(' + ', '+')
    normalized_for_series = normalized_for_series.replace('\u2013', '+').replace('\u2014', '+')
    series_separators = normalized_for_series.count('+')
    if series_separators >= 1:
        if not re.search(r'#\d+\s*\+\s*\d+', title_for_analysis):
            fmt_pattern = r'\+\s*(' + '|'.join(re.escape(v) for v in format_variants) + r')s?\b'
            if not re.search(fmt_pattern, title_for_analysis, re.IGNORECASE):
                parsed_is_multi_series = True

    # Construct series name by removing all matched patterns
    if matched_patterns:
        matched_patterns.sort(key=lambda x: x[0], reverse=True)
        for start, end in matched_patterns:
            title = title[:start].strip() + ' ' + title[end:].strip()
        title = ' '.join(title.split())

    # Clean up
    title = re.sub(r'[-–—]', ' ', title)
    title = re.sub(r'\s+', ' ', title)
    title = title.strip(' .,')

    return ComicTitle(
        name=title,
        issue=parsed_issue,
        issue_range=parsed_issue_range,
        year=parsed_year,
        publication_year=parsed_publication_year,
        volume=parsed_volume,
        is_annual=parsed_is_annual,
        is_quarterly=parsed_is_quarterly,
        is_arc=parsed_is_arc,
        arc_name=parsed_arc_name,
        format_variants=parsed_format_variants,
        is_multi_series=parsed_is_multi_series,
        is_range_pack=parsed_is_range_pack,
        has_tpb_in_pack=parsed_has_tpb_in_pack,
        is_bulk_pack=parsed_is_bulk_pack,
    )


def match_structured(search: dict, result: dict) -> tuple[int, str]:
    """
    Structured matching between search criteria and parsed result.

    This is an alternative to score_getcomics_result() that uses structured
    data comparison instead of string-based scoring.

    Args:
        search: dict with keys:
            - name: series name (normalized)
            - volume: volume number (or None)
            - issue_number: issue number to match
            - year: publication year (or None)
            - brand: brand keyword if detected (e.g., "Rebirth", "New 52") or None
            - is_annual: True if series is an Annual series
            - is_crossover: True if series is a crossover

        result: dict from parse_result_title() with keys:
            - name: normalized series name from title
            - volume: volume number (or None)
            - issue: issue number (or None)
            - issue_range: tuple (start, end) if range (or None)
            - year: publication year (or None)
            - publication_year: year after variant keyword (or None)
            - is_annual: True if annual detected
            - is_arc: True if dash arc notation detected
            - arc_name: arc name if is_arc
            - format_variants: list of detected format variants (uses format_variants config)

    Returns:
        (score, match_type) where match_type is:
        - "accept": Strong match, should accept
        - "fallback": Range pack or secondary match
        - "reject": No match
    """
    score = 0
    match_type = "reject"

    # Get brand keywords once for use throughout
    brands = get_brand_keywords()

    # ── NAME MATCHING ─────────────────────────────────────────────────────────
    search_name = search.get('name', '').lower().strip()
    result_name = result.get('name', '').lower().strip()
    result_arc_name = (result.get('arc_name') or '').lower().strip()

    # Normalize both names for comparison
    search_name_norm = normalize_series_for_compare(search_name)
    result_name_norm = normalize_series_for_compare(result_name)

    # If result has an arc_name, also consider result name + arc as combined
    if result_arc_name:
        combined_name = normalize_series_for_compare(result_name + ' ' + result_arc_name)
    else:
        combined_name = None

    name_exact_match = search_name == result_name
    name_normalized_match = search_name_norm == result_name_norm
    name_combined_match = combined_name and search_name_norm == combined_name

    if not name_exact_match and not name_normalized_match and not name_combined_match:
        # Check if this is a brand era match
        # e.g., "Batman Vol. 3 - Rebirth" matches "Batman Rebirth" when both have "Rebirth"
        search_brand = search.get('brand', '')
        result_brand_in_name = None

        # Extract brand from result name if present
        for brand in brands:
            if brand.lower() in result_name:
                result_brand_in_name = brand.lower()
                break

        if search_brand:
            if result_brand_in_name == search_brand:
                # Both have same brand - compare base names (using normalized)
                search_base = normalize_series_for_compare(search_name.replace(search_brand, '').strip())
                result_base = normalize_series_for_compare(result_name.replace(result_brand_in_name, '').strip())
                if search_base == result_base:
                    name_exact_match = True
            elif result_brand_in_name is None and not name_exact_match:
                # Search has brand but result doesn't - check if base names match (using normalized)
                # e.g., search "Batman Rebirth" vs result "Batman Vol. 3"
                search_base = normalize_series_for_compare(search_name.replace(search_brand, '').strip())
                result_base_norm = normalize_series_for_compare(result_name.strip())
                if search_base == result_base_norm:
                    # Base names match and search has brand - this is a match
                    name_exact_match = True

        if not name_exact_match:
            return 0, "reject"

    # Name matched - add score
    score += 30

    # ── VOLUME MATCHING ───────────────────────────────────────────────────────
    search_volume = search.get('volume')
    result_volume = result.get('volume')

    if search_volume is not None and result_volume is not None:
        if search_volume == result_volume:
            score += 10
        else:
            # Check if same brand era allows different volumes
            search_brand = search.get('brand', '')
            result_brand_in_name = None
            for brand in brands:
                if brand.lower() in result_name:
                    result_brand_in_name = brand.lower()
                    break

            if search_brand and result_brand_in_name == search_brand:
                # Same brand era - volumes can differ, don't penalize
                pass
            else:
                # Different volumes = different series
                return 0, "reject"

    # ── ISSUE MATCHING ────────────────────────────────────────────────────────
    search_issue = search.get('issue_number', '')
    result_issue = result.get('issue')
    result_range = result.get('issue_range')

    if result_range:
        # Range pack - check if target issue is in range
        if result_range[0] <= int(search_issue) <= result_range[1]:
            score += 10  # Range contains target
            match_type = "fallback"
        else:
            # Range doesn't contain target
            return 0, "reject"
    elif result_issue:
        # Standalone issue - must match
        if search_issue == result_issue:
            score += 30
            match_type = "accept"
        else:
            # Wrong issue number
            return 0, "reject"
    else:
        # No issue in result - can't confirm match
        score -= 10

    # ── YEAR MATCHING ────────────────────────────────────────────────────────
    search_year = search.get('year')
    result_year = result.get('year') or result.get('publication_year')

    if search_year and result_year:
        if search_year == result_year:
            score += 20
        else:
            # Wrong year - for non-range packs, this is a harder rejection
            # because the exact issue should have the right year
            if match_type != "fallback":
                score -= 30  # Stronger penalty for exact matches
            else:
                score -= 20  # Range packs can span years


    # ── SERIES TYPE COMPATIBILITY ─────────────────────────────────────────────
    # Annual series must match annual
    if search.get('is_annual') and not result.get('is_annual'):
        # Searching for Annual but result isn't
        return 0, "reject"
    elif result.get('is_annual') and not search.get('is_annual'):
        # Result is Annual but searching for regular
        score -= 30

    # Arc sub-series - penalized but check if base names match first
    # Arcs like "Batman - Court of Owls" are DIFFERENT from plain "Batman" issues
    # even though they share the base series name
    if result.get('is_arc'):
        # If search doesn't have arc info, check if base names match
        # e.g., search "Batman Year One" vs result "Batman - Year One" with arc=True
        # The base names should be considered matching
        search_name_for_compare = search.get('name', '').lower().replace(':', ' ').replace('-', ' ').replace('  ', ' ')
        result_name_for_compare = result.get('name', '').lower().replace(':', ' ').replace('-', ' ').replace('  ', ' ')
        if search_name_for_compare == result_name_for_compare:
            # Base names match but this is an arc sub-series - force fallback
            # because arcs are story lines, not main series issues
            if match_type == "accept":
                match_type = "fallback"
            else:
                score -= 30
            # Arcs can be fallback if score is positive
            if match_type == "fallback" or score >= FALLBACK_MIN:
                pass  # Keep as fallback
            else:
                return score, "reject"
        else:
            score -= 30
            # Arcs can be fallback if score is positive
            if match_type == "fallback" or score >= FALLBACK_MIN:
                pass  # Keep as fallback
            else:
                return score, "reject"

    # Format variants (TPB, omnibus, oneshot)
    # These are format differences, not different series
    # But they should be fallback, not accept - a TPB containing issue #1 is
    # a secondary match compared to direct single-issue #1
    result_format_variants = result.get('format_variants', [])
    if result_format_variants:
        if not search.get('is_annual'):  # Not an annual series
            # Force fallback for format variants (even with exact issue match)
            if match_type == "accept":
                match_type = "fallback"
            else:
                score -= 20

    # ── FINAL DECISION ────────────────────────────────────────────────────────
    # For range packs, ALWAYS return "fallback" (not "accept") even if score is high
    # Range packs are by definition bulk/fallback matches
    if match_type == "fallback":
        return max(score, FALLBACK_MIN), "fallback"

    if score >= ACCEPT_THRESHOLD:
        return score, "accept"
    else:
        return max(0, score), "reject"


def score_getcomics_result(
    result_title: str,
    series_name: str,
    issue_number: str,
    year: int,
    accept_variants: list = None,
    series_volume: int = None,
    volume_year: int = None,
    publisher_name: str = None,
) -> tuple:
    """
    Score a GetComics search result against a wanted issue.

    Args:
        result_title: Title from GetComics search result
        series_name: Series name to match
        issue_number: Issue number to match
        year: Year to match (used for year-in-title matching)
        accept_variants: Optional list of variant types to accept without penalty.
                        E.g., ['annual'] - if Annual is detected but user searched for it,
                        don't penalize as sub-series. Maps to global VARIANT_TYPES config.
        series_volume: Volume number of the series (e.g., 3 for "Vol. 3")
        volume_year: Volume year of the series (e.g., 2024 for "Flash Gordon 2024")
        publisher_name: Publisher name for brand keyword matching (e.g., "DC", "Marvel")
    Returns:
        (score, range_contains_target, series_match)
        - score:                 Integer score; higher = better match
        - range_contains_target: True if title is a range pack containing the issue
        - series_match:          True if series name matched the title

    Scoring (max 95 + bonuses):
        +30  Series name match (starts-with, handles "The" prefix swaps)
        +15  Title tightness (zero extra words beyond series/issue/year)
        +30  Issue number match via #N or "Issue N" pattern
        +20  Issue number match via standalone bare number (lower confidence)
        +20  Year match (softened to +/-1 if volume_year provided)
        +10  Volume match (when both search and result have explicit volumes)

    Penalties:
        -10  Title tightness (1+ extra words)
        -30  Sub-series detected (dash after series name OR variant keyword)
        -30  Different series (remaining text indicates different series)
        -30  The prefix swap used but remaining does not match (e.g., The Flash Gordon vs Flash Gordon)
        -20  Wrong year explicitly present in title (softened if volume_year provided)
        -30  Collected edition keyword (omnibus, TPB, hardcover, etc.)
        -40  Confirmed issue mismatch (#N present but points to wrong number)
        -40  Volume mismatch (both search and result have explicit volumes but they differ)
        -20  Format pack mismatch (searching for regular issue, result is TPB/omnibus/oneshot pack)
        -10  Format pack partial (searching for format, result pack contains format but not standalone)

    Sub-series handling:
        - Variants (Annual, TPB, Quarterly, etc.): Penalized unless variant keyword in accept_variants
        - Arcs (Batman - Court of Owls): ALWAYS penalized - arc issue numbering differs from main series
        - Different Series (Batman Inc, Flash Gordon): Penalized - not the same series

    "The" prefix handling:
        The swap logic allows "The Flash" to match "Flash" for series flexibility.
        However, if the search uses "The " but result doesn't (or vice versa),
        the match is penalized as a different series.

    Range fallback logic:
        When a range like "#1-12" contains the target issue,
        range_contains_target=True is returned and the score is capped below
        ACCEPT_THRESHOLD. Use accept_result() to decide whether to use it.
        FALLBACK requires series_match=True — arc sub-series range packs ARE allowed
        (arcs are often bundled in packs).
    """
    search = search_criteria(
        series_name=series_name,
        issue_number=issue_number,
        year=year,
        series_volume=series_volume,
        volume_year=volume_year,
        publisher_name=publisher_name,
        accept_variants=accept_variants,
    )
    comic_score = score_comic(result_title, search)
    return comic_score.score, comic_score.range_contains_target, comic_score.series_match


def score_comic(result_title: str, search: SearchCriteria) -> ComicScore:
    """
    Score a comic title against search criteria — pure functional core.

    This is the main scoring composition. It delegates to small, focused helpers
    for each scoring phase while maintaining sequential state.

    Args:
        result_title: Raw GetComics result title string
        search: SearchCriteria dataclass with all search parameters

    Returns:
        ComicScore with score, series_match, sub_series_type, and all
        intermediate state used for downstream scoring decisions.
    """
    score = 0
    title_lower = result_title.lower()
    title_normalized = (title_lower
        .replace('&', '+').replace('/', '+').replace(' and ', ' + ')
        .replace('\u2013', '+').replace('\u2014', '+'))

    issue_str = str(search.issue_number)
    issue_num = issue_str.lstrip('0') or '0'
    is_dot_issue = '.' in issue_str
    series_lower = search.series_name.lower()

    # Parse title into structured data
    parsed = parse_result_title(result_title)
    result_volume = parsed.volume
    result_format_variants = parsed.format_variants
    result_has_format = len(result_format_variants) > 0

    # Detect "searching for format" — series name contains format variant keyword
    searching_for_format = _detect_searching_for_format(series_lower, result_format_variants)

    # Range detection — can cause early return
    range_contains_target = _detect_range_contains_target(title_lower, issue_num)
    range_ends_on_target = _detect_range_ends_on_target(title_lower, issue_num)
    if range_ends_on_target:
        return ComicScore(score=-100, range_contains_target=True)
    if range_contains_target and result_has_format:
        return ComicScore(score=-100, range_contains_target=True)

    # Series name matching
    delta, series_match, sub_series_type, remaining, used_the_swap, detected_variant = \
        _score_series_match(title_lower, title_normalized, search)

    score += delta

    if not series_match:
        return ComicScore(score=score, series_match=False)

    # Volume matching
    if search.series_volume is not None and result_volume is not None:
        if search.series_volume == result_volume:
            score += 10
        elif not series_has_same_brand(search.series_name, result_title, search.publisher_name):
            score -= 40

    # Variant acceptance
    variant_accepted = False
    if sub_series_type in ('variant', 'arc'):
        series_name_norm = series_lower.replace('-', '').replace('\u2013', '').replace('\u2014', '').lower()
        det_norm = (detected_variant or '').replace('-', '').lower()
        if det_norm and det_norm in series_name_norm:
            variant_accepted = True
        elif detected_variant:
            pub_types = set(get_publication_types())
            if det_norm not in pub_types:
                for kw in search.accept_variants:
                    kw_n = kw.replace('-', '').lower()
                    if (kw_n == det_norm or det_norm.startswith(kw_n) or kw_n in det_norm):
                        variant_accepted = True
                        break

    # Sub-series penalty (arc is always penalized)
    should_penalize = (
        sub_series_type is not None and not variant_accepted and not range_contains_target
    ) or sub_series_type == 'arc'
    if should_penalize:
        score -= 30

    # Format mismatch penalty
    if result_has_format and not searching_for_format:
        score -= 50 if not parsed.issue_range else 10
    elif searching_for_format and not result_has_format and sub_series_type is None:
        score -= 10

    # Remaining analysis: check if text after series name indicates a different series
    remaining_is_different_series, rem_delta = _score_remaining(
        remaining, sub_series_type, used_the_swap
    )
    score += rem_delta

    allow_issue_match = series_match and (
        (sub_series_type is None and not remaining_is_different_series) or
        (variant_accepted and sub_series_type != 'arc')
    ) and not (result_has_format and not searching_for_format)

    # ── Scoring phases ─────────────────────────────────────────────────────────

    # Phase 1: Issue matching (+30/#N, +20/standalone, -40/mismatch)
    score, issue_matched = _score_issue(
        score, title_lower, result_title, issue_num, is_dot_issue,
        allow_issue_match, series_match, range_contains_target
    )

    # Phase 2: Year matching (+20/-20, +10/-10 with volume_year)
    score, year_in_series_name = _score_year(
        score, result_title, remaining, search, series_lower, issue_num
    )

    # Phase 3: Title tightness (+15/-10)
    score += _score_title_tightness(title_lower, series_lower, issue_num, is_dot_issue, search)

    # Phase 4: Collected edition penalty (-30)
    score += _score_collected_edition(title_lower, series_lower, sub_series_type, variant_accepted)

    # Range fallback cap
    if range_contains_target and score >= FALLBACK_MIN:
        score = min(score, ACCEPT_THRESHOLD - 1)

    return ComicScore(
        score=score,
        range_contains_target=range_contains_target,
        series_match=series_match,
        sub_series_type=sub_series_type,
        variant_accepted=variant_accepted,
        detected_variant=detected_variant,
        used_the_swap=used_the_swap,
        remaining_is_different_series=remaining_is_different_series,
        year_in_series_name=year_in_series_name,
    )


def _score_issue(
    score: int,
    title_lower: str,
    result_title: str,
    issue_num: str,
    is_dot_issue: bool,
    allow_issue_match: bool,
    series_match: bool,
    range_contains_target: bool,
) -> tuple[int, bool]:
    """
    Phase 1 — Issue number matching.

    Returns (new_score, issue_matched).
    """
    issue_matched = False
    if is_dot_issue:
        if allow_issue_match:
            for pattern in [
                rf'#0*{re.escape(issue_num)}\b',
                rf'issue\s*0*{re.escape(issue_num)}\b',
                rf'\b0*{re.escape(issue_num)}\b',
            ]:
                if re.search(pattern, title_lower, re.IGNORECASE):
                    score += 30
                    issue_matched = True
                    break
    else:
        if allow_issue_match:
            for pattern in [rf'#0*{re.escape(issue_num)}\b', rf'issue\s*0*{re.escape(issue_num)}\b']:
                if re.search(pattern, title_lower, re.IGNORECASE):
                    score += 30
                    issue_matched = True
                    break
            if not issue_matched:
                standalone = re.search(rf'\b0*{re.escape(issue_num)}\b', title_lower)
                if standalone:
                    prefix = result_title[max(0, standalone.start() - 10):standalone.start()].lower()
                    if not re.search(r'[-\u2013\u2014]\s*$', prefix) and \
                       not re.search(r'\bvol(?:ume)?\.?\s*$', prefix):
                        score += 20
                        issue_matched = True

    # Confirmed issue mismatch
    if not issue_matched and series_match and not range_contains_target:
        explicit = re.search(rf'(?:#|issue\s)0*(\d+(?:\.\d+)?)\b', title_lower, re.IGNORECASE)
        if explicit:
            found_num = explicit.group(1).lstrip('0') or '0'
            if found_num != issue_num:
                score -= 40

    return score, issue_matched


def _score_year(
    score: int,
    result_title: str,
    remaining: str,
    search: SearchCriteria,
    series_lower: str,
    issue_num: str,
) -> tuple[int, bool]:
    """
    Phase 2 — Year matching.

    Returns (new_score, year_in_series_name).
    year_in_series_name is True when a year at the start of remaining text
    is part of the series name (e.g. "Flash Gordon Annual 2014" — the 2014
    belongs to the series name, not a publication year to match).
    """
    year_in_series_name = False
    if search.volume_year is not None:
        result_years = re.findall(r'\b(\d{4})\b', result_title)
        if result_years:
            ryr = int(result_years[0])
            if ryr == search.volume_year or abs(ryr - search.volume_year) == 1:
                score += 10
            else:
                score -= 10
    else:
        if remaining and search.year is None:
            yr_match = re.match(r'^(\d{4})\s+', remaining.strip())
            if yr_match:
                rem_check = (remaining.replace('-', '').replace('\u2013', '')
                             .replace('\u2014', '').lower())
                after = rem_check[yr_match.end():]
                for kw in get_variant_types():
                    if after.startswith(kw):
                        year_in_series_name = True
                        break

        if search.year and str(search.year) in result_title:
            score += 20 if not year_in_series_name else -20
        elif search.year:
            other_years = re.findall(r'\b(\d{4})\b', result_title)
            if other_years:
                if any(int(y) != search.year for y in other_years):
                    score -= 20
            else:
                score -= 10

    return score, year_in_series_name


def _score_title_tightness(
    title_lower: str,
    series_lower: str,
    issue_num: str,
    is_dot_issue: bool,
    search: SearchCriteria,
) -> int:
    """
    Phase 3 — Title tightness bonus/penalty.

    Returns the score delta: +15 for a tight match, -10 for loose.
    """
    noise = {'the', 'a', 'an', 'of', 'and', 'in', 'by', 'for', 'to', 'from', 'with', 'on', 'at', 'or', 'is'}
    expected = set(re.findall(r'[a-z0-9]+', series_lower))
    expected.add(issue_num)
    if is_dot_issue:
        expected.add(issue_num.split('.')[0])
    if search.year:
        expected.add(str(search.year))
    expected.update(['vol', 'volume', 'issue', 'comic', 'comics'])
    title_words = [w for w in re.findall(r'[a-z0-9]+', title_lower)
                   if w not in noise and len(w) > 1]
    extra = len(title_words) - sum(
        1 for w in title_words
        if w in expected or (w.isdigit() and (w.lstrip('0') or '0') == issue_num))
    return 15 if extra == 0 else -10


def _score_collected_edition(
    title_lower: str,
    series_lower: str,
    sub_series_type: str | None,
    variant_accepted: bool,
) -> int:
    """
    Phase 4 — Collected edition penalty.

    Returns the score delta: -30 if the result title looks like a collected
    edition (TPB, omnibus, compendium, etc.) but the search is for a single issue.
    """
    if sub_series_type is not None or variant_accepted:
        return 0
    title_rem = title_lower.replace(series_lower, '', 1)
    pub_pattern = r'\b(' + '|'.join(re.escape(p) for p in get_publication_types()) + r')s?\b'
    for kw in get_format_variants() + [pub_pattern, r'\bcompendium\b',
           r'\bcomplete\s+collection\b', r'\blibrary\s+edition\b', r'\bbook\s+\d+\b']:
        if re.search(kw, title_rem):
            return -30
    return 0


def _score_remaining(
    remaining: str,
    sub_series_type: str | None,
    used_the_swap: bool,
) -> tuple[bool, int]:
    """
    Analyze text remaining after the series name to determine if it indicates
    a different series and calculate any score penalty.

    Args:
        remaining: Text after the series name in the result title (may be empty)
        sub_series_type: 'variant', 'arc', 'different_edition' or None from series matching
        used_the_swap: True if "The " prefix swap was used to match the series

    Returns:
        (remaining_is_different_series, score_delta):
        - remaining_is_different_series: True if remaining text indicates a different series
        - score_delta: -30 if different series, 0 otherwise
    """
    if not remaining or sub_series_type is not None:
        return False, 0

    remaining_cleaned = (remaining.strip()
        .replace('-', '').replace('\u2013', '').replace('\u2014', '')
        .replace(' ', '').replace('#', '').replace('(', '').replace(')', ''))
    is_purely_range = bool(remaining_cleaned) and all(
        c.isdigit() or c == '.' for c in remaining_cleaned)
    starts_with_issue = bool(re.match(r'^#?\d', remaining.strip()))
    starts_with_issue_word = bool(re.match(r'^issue\s*\d', remaining.strip(), re.IGNORECASE))
    starts_with_volume = bool(re.match(r'^vol(ume)?\.?\s*\d', remaining.strip(), re.IGNORECASE))

    if used_the_swap:
        return True, -30
    if is_purely_range or starts_with_volume:
        return False, 0

    # Crossover detection: "Batman '66 Meets Steed and Mrs Peel" or "Batman 1984 Meets..."
    # A year-like number (66, '66, 1984, '1984) followed by a crossover keyword
    # indicates a crossover/mashup series — NOT the base series being searched.
    crossover_kws = get_crossover_keywords()
    kw_parts = []
    for kw in crossover_kws:
        if kw == 'vs':
            kw_parts.append(r'vs\.?')
        elif kw == 'x-over':
            kw_parts.append(r'x-?over')
        else:
            kw_parts.append(re.escape(kw))
    crossover_pat = r'[#\s]*[\'"]?(\d{2,4})[\'"]?\s+(' + '|'.join(kw_parts) + r')'
    if re.match(crossover_pat, remaining.strip(), re.IGNORECASE):
        return True, -30

    if not starts_with_issue and not starts_with_issue_word:
        if not remaining.startswith(('-', '\u2013', '\u2014', ':')):
            rem_check = (remaining.replace('-', '').replace('\u2013', '')
                         .replace('\u2014', '').lower())
            for kw in get_variant_types():
                if re.search(rf'\b{re.escape(kw)}\b', rem_check, re.IGNORECASE):
                    return False, 0
            return True, -30

    return False, 0


def _detect_searching_for_format(
    series_lower: str,
    format_variants: list[str],
) -> bool:
    """
    Detect if the search series name contains a format variant keyword,
    indicating the user is searching for a collected edition rather than
    single issues.

    Args:
        series_lower: series name in lowercase (with hyphens and special chars)
        format_variants: list of format variant keywords (from get_format_variants)

    Returns:
        True if series name contains a format variant keyword
    """
    series_norm = series_lower.replace('-', '').replace('\u2013', '').replace('\u2014', '').lower()
    for fv in format_variants:
        fv_n = fv.replace('-', '').replace('.', '').lower()
        if re.search(rf'\b{re.escape(fv)}\b', series_lower, re.IGNORECASE):
            return True
        if re.search(rf'\b{re.escape(fv_n)}\b', series_norm, re.IGNORECASE):
            return True
    return False



def accept_result(
    score: int,
    range_contains_target: bool,
    series_match: bool,
    single_issue_found: bool = False,
) -> str:
    """
    Two-tier acceptance decision for a scored GetComics result.

    Tier 1 — ACCEPT:   score >= ACCEPT_THRESHOLD (direct single-issue match)
    Tier 2 — FALLBACK: range pack containing the issue, series confirmed,
                       score >= FALLBACK_MIN, no better single-issue found yet
    Otherwise — REJECT

    Args:
        score:                 From score_getcomics_result()
        range_contains_target: From score_getcomics_result()
        series_match:          From score_getcomics_result()
        single_issue_found:    Set True once a Tier-1 result is found to
                               suppress range fallbacks in the same search pass.

    Returns:
        "ACCEPT", "FALLBACK", or "REJECT"
    """
    if score >= ACCEPT_THRESHOLD:
        return "ACCEPT"
    if (range_contains_target
            and score >= FALLBACK_MIN
            and series_match
            and not single_issue_found):
        return "FALLBACK"
    return "REJECT"


def simulate_search(
    series_name: str,
    issue_number: str,
    year: int = None,
    series_volume: int = None,
    volume_year: int = None,
    accept_variants: list = None,
    max_pages: int = 1,
) -> None:
    """
    Simulate a GetComics search and show detailed scoring for each result.
    Useful for debugging and understanding scoring decisions.

    Args:
        series_name: Series name to search for
        issue_number: Issue number to search for
        year: Year to match (optional)
        series_volume: Volume number (optional)
        volume_year: Volume start year for soft year matching (optional)
        accept_variants: List of variant types to accept (optional)
        max_pages: Maximum pages to search (default 1)
    """
    import pprint

    print(f"\n{'='*70}")
    print(f"SIMULATE SEARCH")
    print(f"{'='*70}")
    print(f"Series: {series_name}")
    print(f"Issue: {issue_number}")
    print(f"Year: {year}")
    print(f"Series Volume: {series_volume}")
    print(f"Volume Year: {volume_year}")
    print(f"Accept Variants: {accept_variants}")
    print(f"{'='*70}\n")

    # Build search query
    query_parts = [series_name, issue_number]
    if year:
        query_parts.append(str(year))
    query = " ".join(query_parts)

    print(f"Query: '{query}'")
    print(f"{'-'*70}\n")

    try:
        results = search_getcomics(query, max_pages=max_pages)
    except Exception as e:
        print(f"Search failed: {e}")
        return

    if not results:
        print("No results found.")
        return

    print(f"Found {len(results)} results\n")

    for i, result in enumerate(results, 1):
        title = result['title']
        print(f"[{i}] {title}")
        print(f"    Link: {result['link']}")

        score, range_contains, series_match = score_getcomics_result(
            title,
            series_name,
            issue_number,
            year,
            accept_variants=accept_variants,
            series_volume=series_volume,
            volume_year=volume_year,
        )

        decision = accept_result(score, range_contains, series_match)
        print(f"    Score: {score}")
        print(f"    Range contains target: {range_contains}")
        print(f"    Series match: {series_match}")
        print(f"    Decision: {decision}")
        print()



#########################
#   Weekly Packs        #
#########################

def get_weekly_pack_url_for_date(pack_date: str) -> str:
    """
    Generate the GetComics weekly pack URL for a specific date.

    Args:
        pack_date: Date in YYYY.MM.DD or YYYY-MM-DD format

    Returns:
        URL string like https://getcomics.org/other-comics/2026-01-14-weekly-pack/
    """
    # Normalize date to YYYY-MM-DD format
    normalized = pack_date.replace('.', '-')
    return f"https://getcomics.org/other-comics/{normalized}-weekly-pack/"


def get_weekly_pack_dates_in_range(start_date: str, end_date: str) -> list:
    """
    Generate list of weekly pack dates between start_date and end_date.
    Weekly packs are released on Wednesdays (or Tuesdays sometimes).

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format

    Returns:
        List of date strings in YYYY.MM.DD format (newest first)
    """
    from datetime import datetime, timedelta

    start = datetime.strptime(start_date, '%Y-%m-%d')
    end = datetime.strptime(end_date, '%Y-%m-%d')

    # Find all Wednesdays in the range (weekly packs typically release Wed)
    # Also include Tuesdays as some packs release then
    dates = []
    current = end

    while current >= start:
        # Check if this is a Tuesday (1) or Wednesday (2)
        if current.weekday() in [1, 2]:  # Tuesday or Wednesday
            dates.append(current.strftime('%Y.%m.%d'))
        current -= timedelta(days=1)

    return dates


def find_latest_weekly_pack_url():
    """
    Find the latest weekly pack URL from getcomics.org homepage.
    Uses cloudscraper to bypass Cloudflare protection.

    Searches the .cover-blog-posts section for links matching:
    <h2 class="post-title"><a href="...weekly-pack/">YYYY.MM.DD Weekly Pack</a></h2>

    Returns:
        Tuple of (pack_url, pack_date) or (None, None) if not found
        pack_date is in format "YYYY.MM.DD"
    """
    import re

    base_url = "https://getcomics.org"

    try:
        logger.info("Fetching getcomics.org homepage to find weekly pack")
        resp = scraper.get(base_url, timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, 'html.parser')

        # Find the cover-blog-posts section
        cover_section = soup.find(class_="cover-blog-posts")
        if not cover_section:
            logger.warning("Could not find .cover-blog-posts section on homepage")
            # Fall back to searching entire page
            cover_section = soup

        # Look for weekly pack links
        # Pattern: YYYY.MM.DD Weekly Pack or YYYY-MM-DD Weekly Pack
        weekly_pack_pattern = re.compile(r'(\d{4})[.\-](\d{2})[.\-](\d{2})\s*Weekly\s*Pack', re.IGNORECASE)

        for h2 in cover_section.find_all(['h2', 'h3'], class_='post-title'):
            link = h2.find('a')
            if not link:
                continue

            title = link.get_text(strip=True)
            href = link.get('href', '')

            match = weekly_pack_pattern.search(title)
            if match:
                # Found a weekly pack
                year, month, day = match.groups()
                pack_date = f"{year}.{month}.{day}"
                logger.info(f"Found weekly pack: {title} -> {href} (date: {pack_date})")
                return (href, pack_date)

        # Also check the URL pattern if title didn't match
        for link in cover_section.find_all('a', href=True):
            href = link.get('href', '')
            if 'weekly-pack' in href.lower():
                # Extract date from URL like: /other-comics/2026-01-14-weekly-pack/
                url_match = re.search(r'(\d{4})-(\d{2})-(\d{2})-weekly-pack', href, re.IGNORECASE)
                if url_match:
                    year, month, day = url_match.groups()
                    pack_date = f"{year}.{month}.{day}"
                    logger.info(f"Found weekly pack via URL: {href} (date: {pack_date})")
                    return (href, pack_date)

        logger.warning("No weekly pack found on homepage")
        return (None, None)

    except Exception as e:
        logger.error(f"Error fetching/parsing homepage for weekly pack: {e}")
        return (None, None)


def check_weekly_pack_availability(pack_url: str) -> bool:
    """
    Check if weekly pack download links are available yet.
    Uses cloudscraper to bypass Cloudflare protection.

    Returns:
        True if download links are present, False if still pending
    """
    try:
        logger.info(f"Checking weekly pack availability: {pack_url}")
        resp = scraper.get(pack_url, timeout=30)
        resp.raise_for_status()

        page_text = resp.text.lower()

        # Check for the "not ready" message
        not_ready_phrases = [
            "will be updated once all the files is complete",
            "will be updated once all the files are complete",
            "download link will be updated",
            "links will be updated"
        ]

        for phrase in not_ready_phrases:
            if phrase in page_text:
                logger.info(f"Weekly pack links not ready yet (found: '{phrase}')")
                return False

        # Check if PIXELDRAIN links exist
        soup = BeautifulSoup(resp.text, 'html.parser')
        pixeldrain_links = soup.find_all('a', href=lambda h: h and ('pixeldrain' in h.lower() or 'getcomics.org/dlds/' in h.lower()))

        if pixeldrain_links:
            logger.info(f"Weekly pack links are available ({len(pixeldrain_links)} PIXELDRAIN links found)")
            return True

        logger.info("No PIXELDRAIN links found on weekly pack page")
        return False

    except Exception as e:
        logger.error(f"Error checking pack availability: {e}")
        return False


def parse_weekly_pack_page(pack_url: str, format_preference: str, publishers: list) -> dict:
    """
    Parse a weekly pack page and extract PIXELDRAIN download links.
    Uses cloudscraper to bypass Cloudflare protection.

    Args:
        pack_url: URL of the weekly pack page
        format_preference: 'JPG' or 'WEBP'
        publishers: List of publishers to download ['DC', 'Marvel', 'Image', 'INDIE']

    Returns:
        Dict mapping publisher to pixeldrain URL: {publisher: url}
        Returns empty dict if links not yet available
    """
    import re

    result = {}

    try:
        logger.info(f"Parsing weekly pack page: {pack_url}")
        logger.info(f"Looking for format: {format_preference}, publishers: {publishers}")

        resp = scraper.get(pack_url, timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, 'html.parser')

        # Find the section for the requested format (JPG or WEBP)
        # Structure: <h3><span style="color: #3366ff;">JPG</span></h3> followed by <ul>
        target_section = None

        for h3 in soup.find_all('h3'):
            h3_text = h3.get_text(strip=True).upper()
            if format_preference.upper() in h3_text:
                # Found the right format section
                # Get the following <ul> element
                target_section = h3.find_next_sibling('ul')
                if target_section:
                    logger.info(f"Found {format_preference} section")
                    break

        if not target_section:
            logger.warning(f"Could not find {format_preference} section on page")
            return {}

        # Parse each <li> item for publisher packs
        # Structure: <li>2026.01.14 DC Week (489 MB) :<br>...<a href="...">PIXELDRAIN</a>...</li>
        for li in target_section.find_all('li'):
            li_text = li.get_text(strip=True)

            # Check which publisher this line is for
            for publisher in publishers:
                # Match patterns like "DC Week", "Marvel Week", "Image Week", "INDIE Week"
                publisher_patterns = [
                    rf'\b{re.escape(publisher)}\s*Week\b',
                    rf'\b{re.escape(publisher)}\b.*Week'
                ]

                matched = False
                for pattern in publisher_patterns:
                    if re.search(pattern, li_text, re.IGNORECASE):
                        matched = True
                        break

                if matched:
                    # Found the right publisher, now find the PIXELDRAIN link
                    pixeldrain_link = None

                    for a in li.find_all('a', href=True):
                        href = a.get('href', '')
                        link_text = a.get_text(strip=True).upper()

                        # Check if this is a PIXELDRAIN link
                        # Can be direct pixeldrain.com URL or getcomics.org/dlds/ redirect
                        if 'PIXELDRAIN' in link_text or 'pixeldrain.com' in href.lower():
                            pixeldrain_link = href
                            break
                        # Check for getcomics redirect link with PIXELDRAIN in text
                        elif 'getcomics.org/dlds/' in href.lower() and 'PIXELDRAIN' in link_text:
                            pixeldrain_link = href
                            break

                    if pixeldrain_link:
                        result[publisher] = pixeldrain_link
                        logger.info(f"Found {publisher} {format_preference} link: {pixeldrain_link[:80]}...")
                    else:
                        logger.warning(f"Could not find PIXELDRAIN link for {publisher}")

                    break  # Move to next li item

        logger.info(f"Parsed {len(result)} publisher links from weekly pack")
        return result

    except Exception as e:
        logger.error(f"Error fetching/parsing pack page: {e}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# SITEMAP INDEX — URL lookup and building
# ─────────────────────────────────────────────────────────────────────────────

def lookup_series_urls(series_name: str) -> list[dict]:
    """
    Look up indexed GetComics URLs for a series from the local sitemap DB.

    Matches on both exact series_norm and url_slug pattern. The url_slug pattern
    finds related series that share the same name prefix in the URL slug
    (e.g., "flash-gordon-kings-cross" matches "flash gordon").

    If series_name is a registered alias, resolves to the canonical series first.

    Args:
        series_name: Series name to look up (e.g., "Flash Gordon", "Batman")

    Returns:
        List of dicts with keys: series_norm, url_slug, full_url, category
    """
    # Resolve alias to canonical before looking up
    resolved = resolve_series_alias(series_name)
    series_norm, _ = normalize_series_name(resolved)

    # Build url_slug pattern: "Flash Gordon" -> "flash-gordon-%"
    slug_pattern = series_norm.replace(' ', '-').lower() + '%'

    # Lazy import to avoid circular dependency at module load time
    from core.database import get_db_connection
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    c = conn.execute(
        "SELECT series_norm, url_slug, full_url, category FROM getcomics_urls "
        "WHERE series_norm_norm = ? COLLATE NOCASE "
        "   OR search_aliases LIKE ? COLLATE NOCASE "
        "ORDER BY series_norm, url_slug",
        (series_norm.replace('-', ' ').replace('\u2013', ' ').replace('\u2014', ' ').strip().lower(), f"%{series_norm.replace('-', ' ').replace('\u2013', ' ').replace('\u2014', ' ').strip().lower()}%")
    )
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def build_sitemap_index(max_sitemaps: int | None = None, force_refresh: bool = False) -> int:
    """
    Build (or refresh) the GetComics sitemap URL index.

    Uses conditional fetching: stores the Last-Modified header for each sitemap
    page and sends If-Modified-Since on subsequent runs. Skips a sitemap
    entirely if it hasn't changed (304 Not Modified).

    Per-URL incremental updates: each sitemap URL entry carries a <lastmod>
    timestamp. If a URL's lastmod hasn't changed since the last index, we
    skip re-processing it.

    Args:
        max_sitemaps: Maximum number of post-sitemaps to process. None = all.
                      The first sitemap (post-sitemap.xml) is always processed.
        force_refresh: If True, ignore cached Last-Modified and re-fetch all
                       sitemaps (but still skips unchanged individual URLs).

    Returns:
        Total number of URLs added or updated
    """
    import xml.etree.ElementTree as ET
    from urllib.parse import urlparse
    from core.database import get_db_connection

    SITEMAP_INDEX = "https://getcomics.org/sitemap.xml"
    sitemap_urls = []

    # ── Discover sitemap URLs from the sitemap index ───────────────────────
    try:
        resp = scraper.get(SITEMAP_INDEX, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)
        ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}

        for sitemap in root.findall('sm:sitemap/sm:loc', ns):
            url = sitemap.text
            if url and 'post-sitemap' in url:
                sitemap_urls.append(url)
    except Exception as e:
        logger.warning(f"Could not fetch sitemap index: {e}")

    if not sitemap_urls:
        # Fallback: try the first sitemap directly
        sitemap_urls = ["https://getcomics.org/post-sitemap.xml"]

    if max_sitemaps:
        sitemap_urls = sitemap_urls[:max_sitemaps]

    logger.info(f"Checking {len(sitemap_urls)} sitemap(s)")

    # ── Load stored page metadata for conditional fetching ─────────────────
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    stored_pages = {}
    for row in conn.execute("SELECT sitemap_url, last_modified, etag FROM getcomics_sitemap_pages"):
        stored_pages[row["sitemap_url"]] = {
            "last_modified": row["last_modified"],
            "etag": row["etag"],
        }
    conn.close()

    # ── Pre-load existing URL lastmod values for per-URL incremental skip ──
    existing_lastmod = {}
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    for row in conn.execute("SELECT full_url, lastmod FROM getcomics_urls"):
        existing_lastmod[row["full_url"]] = row["lastmod"]
    conn.close()

    total_indexed = 0
    total_skipped = 0

    for sm_url in sitemap_urls:
        headers = {}
        if not force_refresh and sm_url in stored_pages:
            prev = stored_pages[sm_url]
            if prev["etag"]:
                headers["If-None-Match"] = prev["etag"]
            elif prev["last_modified"]:
                headers["If-Modified-Since"] = prev["last_modified"]

        try:
            resp = scraper.get(sm_url, timeout=60, headers=headers)

            # 304 Not Modified — sitemap hasn't changed, skip all its URLs
            if resp.status_code == 304:
                logger.info(f"  [304] {sm_url} — unchanged, skipping")
                if sm_url in stored_pages:
                    conn = get_db_connection()
                    conn.execute(
                        "UPDATE getcomics_sitemap_pages SET last_checked = CURRENT_TIMESTAMP "
                        "WHERE sitemap_url = ?",
                        (sm_url,)
                    )
                    conn.commit()
                    conn.close()
                continue

            resp.raise_for_status()

            # Extract lastmod/etag from response for next time
            last_modified = resp.headers.get("Last-Modified")
            etag = resp.headers.get("ETag")

            root = ET.fromstring(resp.text)
            ns = {'sm': 'http://www.sitemaps.org/schemas/sitemap/0.9'}

            url_entries = []
            entries_count = 0

            for url_el in root.findall('sm:url/sm:loc', ns):
                page_url = url_el.text
                if not page_url or page_url == 'https://getcomics.org/':
                    continue

                # Get per-URL lastmod from the sitemap XML entry
                lastmod_el = url_el.find("sm:lastmod", ns)
                url_lastmod = lastmod_el.text if lastmod_el is not None else None

                # Skip URL if we've already seen it with the same or newer lastmod
                if existing_lastmod.get(page_url) == url_lastmod and url_lastmod is not None:
                    total_skipped += 1
                    continue

                parsed = urlparse(page_url)
                path_parts = [p for p in parsed.path.strip('/').split('/') if p]
                if len(path_parts) < 2:
                    continue

                category = path_parts[0]
                url_slug = path_parts[-1]

                # Extract series name from URL slug
                # e.g. "flash-gordon-1-2-1995" -> "flash gordon"
                parts = url_slug.split('-')
                series_parts = []
                for part in parts:
                    if re.match(r'^\d+$', part):
                        break
                    series_parts.append(part)
                series_from_slug = ' '.join(series_parts) if series_parts else url_slug

                series_norm, _ = normalize_series_name(series_from_slug)

                url_entries.append({
                    'series_norm': series_norm,
                    'url_slug': url_slug,
                    'full_url': page_url,
                    'category': category,
                    'lastmod': url_lastmod,
                })
                existing_lastmod[page_url] = url_lastmod  # prevent duplicate processing
                entries_count += 1

            if url_entries:
                conn = get_db_connection()
                c = conn.cursor()
                c.executemany(
                    "INSERT OR REPLACE INTO getcomics_urls "
                    "(series_norm, url_slug, full_url, category, lastmod, indexed_at) "
                    "VALUES (:series_norm, :url_slug, :full_url, :category, :lastmod, CURRENT_TIMESTAMP)",
                    url_entries
                )
                conn.commit()
                conn.close()
                total_indexed += len(url_entries)

            # Update sitemap page tracking metadata
            conn = get_db_connection()
            conn.execute(
                "INSERT OR REPLACE INTO getcomics_sitemap_pages "
                "(sitemap_url, last_modified, etag, last_checked) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (sm_url, last_modified, etag)
            )
            conn.commit()
            conn.close()

            changed = "refreshed" if url_entries else "no changes"
            logger.info(f"  [200] {sm_url}: {entries_count} new/changed URLs ({changed})")

        except Exception as e:
            logger.error(f"Error processing sitemap {sm_url}: {e}")
            continue

    logger.info(
        f"Sitemap index complete: {total_indexed} URLs added/updated, "
        f"{total_skipped} skipped (unchanged)"
    )
    return total_indexed


def scrape_and_score_candidate(
    url: str,
    series_name: str,
    issue_number: str,
    year: int | None,
    series_volume: int | None = None,
    volume_year: int | None = None,
    publisher_name: str | None = None,
    accept_variants: list | None = None,
) -> tuple[dict, int] | None:
    """
    Scrape a GetComics candidate URL and score it against search criteria.

    Handles multi-entry listing pages: extracts ALL div.post-content entries,
    scores each heading individually, and returns the best match with its
    specific download URL.

    Used by the sitemap-first lookup: tries each indexed URL for a series
    and returns the first ACCEPT-scoring result.

    Args:
        url: Full GetComics page URL from sitemap index
        series_name: Series to match
        issue_number: Issue number to match
        year: Year to match
        series_volume: Volume number of the series
        volume_year: Volume start year
        publisher_name: Publisher name for brand matching
        accept_variants: List of accepted variant keywords

    Returns:
        (result_dict, score) if download links found and score is positive, else None.
        result_dict has keys: title, url, link, links (pixeldrain, download_now, mega)
    """
    try:
        resp = scraper.get(url, timeout=30)
        if resp.status_code != 200:
            return None

        soup = BeautifulSoup(resp.text, 'html.parser')

        def _extract_title(text: str) -> str:
            """Normalize title text for scoring."""
            for sep in [" - ", " \u2013 ", " \u2014 ", " \x97 ", " \u0097 "]:
                if sep in text:
                    text = text.split(sep)[0].strip()
                    break
            else:
                if "GetComics" in text:
                    text = text.split("GetComics")[0].strip().rstrip("-").rstrip("\u2013").rstrip("\u2014").rstrip()
            return text.replace('\u2013', '-').replace('\u2014', '-').replace('\x97', '-').replace('\u0097', '-')

        def _extract_links(container) -> dict:
            """Extract download links from a soup container."""
            links = {"pixeldrain": None, "download_now": None, "mega": None}
            for a in container.find_all("a", class_=lambda c: c and ('aio-red' in c or 'aio-blue' in c)):
                text = a.get_text(strip=True).upper()
                href = a.get("href", "") or ""
                if not href:
                    continue
                if "PIXELDRAIN" in text and not links["pixeldrain"]:
                    links["pixeldrain"] = href
                elif "DOWNLOAD" in text and not links["download_now"]:
                    links["download_now"] = href
                elif "MEGA" in text and not links["mega"]:
                    links["mega"] = href
            # Fallback: title attribute
            if not any(links.values()):
                for a in container.find_all("a"):
                    link_title = (a.get("title") or "").upper()
                    href = a.get("href", "") or ""
                    if not href:
                        continue
                    if "PIXELDRAIN" in link_title and not links["pixeldrain"]:
                        links["pixeldrain"] = href
                    elif "DOWNLOAD NOW" in link_title and not links["download_now"]:
                        links["download_now"] = href
                    elif "MEGA" in link_title and not links["mega"]:
                        links["mega"] = href
            return links

        best_score = -999
        best_result = None

        # Listing page: extract each div.post-content entry and score individually
        post_entries = soup.select("div.post-content")
        if post_entries:
            for el in post_entries:
                h5 = el.select_one("h5 a") or el.select_one("h4 a") or el.select_one("h3 a")
                if not h5:
                    continue
                heading = _extract_title(h5.get_text(strip=True))
                if not heading:
                    continue
                entry_links = _extract_links(el)
                if not any(entry_links.values()):
                    continue

                score, _, _ = score_getcomics_result(
                    heading, series_name, issue_number, year,
                    accept_variants=accept_variants,
                    series_volume=series_volume,
                    volume_year=volume_year,
                    publisher_name=publisher_name,
                )

                if score > best_score:
                    best_score = score
                    best_result = {
                        "title": heading,
                        "url": url,
                        "link": url,
                        "links": entry_links,
                    }

        # Also check page-level title + links (for individual comic pages)
        title_tag = soup.find("title")
        if title_tag:
            page_title = _extract_title(title_tag.get_text(strip=True))
            if page_title:
                page_links = _extract_links(soup)
                if any(page_links.values()):
                    score, _, _ = score_getcomics_result(
                        page_title, series_name, issue_number, year,
                        accept_variants=accept_variants,
                        series_volume=series_volume,
                        volume_year=volume_year,
                        publisher_name=publisher_name,
                    )
                    if score > best_score:
                        best_score = score
                        best_result = {
                            "title": page_title,
                            "url": url,
                            "link": url,
                            "links": page_links,
                        }

        if best_result is None or best_score < ACCEPT_THRESHOLD:
            return None

        return (best_result, best_score)

    except Exception as e:
        logger.debug(f"Error scraping candidate {url}: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPE INDEX — Structured content index from GetComics sitemap URLs
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# UNIFIED GETCOMICS URLS TABLE
# Replaces getcomics_sitemap_urls + getcomics_scrape_index with a single table.
# Stores both discovered URLs (sitemap stage) and scraped metadata (download_url,
# title, issue_num, year, etc.) in one place. The sitemap lastmod drives when to
# re-scrape; download_url being populated indicates a fresh, usable entry.
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_urls_table():
    """
    Create (or migrate) the unified getcomics_urls table.

    Migration from old schema:
      - getcomics_sitemap_urls: id, series_norm, url_slug, full_url, category, lastmod, indexed_at
      - getcomics_scrape_index: url, series_norm, url_slug, title, issue_num, issue_range,
                                year, volume, is_annual, is_bulk_pack, is_multi_series,
                                format_variants, download_url, lastmod, indexed_at, search_aliases
    """
    from core.database import get_db_connection

    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS getcomics_urls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            full_url TEXT NOT NULL,
            url_slug TEXT,
            series_norm TEXT,
            series_norm_norm TEXT,
            search_aliases TEXT,
            title TEXT,
            issue_num TEXT,
            issue_range TEXT,
            year INTEGER,
            volume INTEGER,
            is_annual INTEGER DEFAULT 0,
            is_bulk_pack INTEGER DEFAULT 0,
            is_multi_series INTEGER DEFAULT 0,
            format_variants TEXT,
            download_url TEXT,
            category TEXT,
            lastmod TEXT,
            indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            scrape_status TEXT DEFAULT 'pending' CHECK (scrape_status IN ('pending','success','failed','empty')),
            scrape_attempts INTEGER DEFAULT 0,
            last_scrape_attempt TIMESTAMP,
            url_last_modified TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_series_norm ON getcomics_urls(series_norm)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_series_norm_norm ON getcomics_urls(series_norm_norm)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_year ON getcomics_urls(year)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_volume ON getcomics_urls(volume)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_issue ON getcomics_urls(issue_num)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_aliases ON getcomics_urls(search_aliases)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_scrape_status ON getcomics_urls(scrape_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_missing_since ON getcomics_urls(last_scrape_attempt)")

    # Migration: add scrape_status columns to existing getcomics_urls table
    existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(getcomics_urls)").fetchall()}

    # Migration: add 'id' column for auto-increment PK if it doesn't exist.
    # Old entries have url as PRIMARY KEY without '#' — migrate them to url || '#canonical'
    # to avoid conflicts with the new multi-entry-per-page scraping.
    if 'id' not in existing_cols:
        conn.execute("ALTER TABLE getcomics_urls ADD COLUMN id INTEGER")
        # Use a temp table to avoid SQLite self-reference UPDATE issues
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _url_id_map AS SELECT url, ROWID as mapped_id FROM getcomics_urls")
        conn.execute("""
            UPDATE getcomics_urls SET id = (
                SELECT mapped_id FROM _url_id_map WHERE _url_id_map.url = getcomics_urls.url
            )
        """)
        conn.execute("DROP TABLE IF EXISTS _url_id_map")
        # Mark old-format entries (no '#' in url) with #canonical suffix
        conn.execute("UPDATE getcomics_urls SET url = url || '#canonical' WHERE url NOT LIKE '%#%'")
        logger.info("Migrated getcomics_urls: added id column, suffixed old URLs with #canonical")

    if 'scrape_status' not in existing_cols:
        conn.execute("ALTER TABLE getcomics_urls ADD COLUMN scrape_status TEXT DEFAULT 'pending'")
        conn.execute("ALTER TABLE getcomics_urls ADD COLUMN scrape_attempts INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE getcomics_urls ADD COLUMN last_scrape_attempt TIMESTAMP")
        conn.execute("ALTER TABLE getcomics_urls ADD COLUMN url_last_modified TEXT")
        # Backfill: existing rows with download_url are 'success', without are 'empty'
        conn.execute("UPDATE getcomics_urls SET scrape_status = 'success' WHERE download_url IS NOT NULL AND download_url != ''")
        conn.execute("UPDATE getcomics_urls SET scrape_status = 'empty' WHERE scrape_status = 'pending'")
        logger.info("Migrated scrape_status columns for existing getcomics_urls entries")

    # Migration: remove spurious UNIQUE constraint on full_url.
    # The old CREATE TABLE had "full_url TEXT NOT NULL UNIQUE" which was
    # incorrect — multiple page entries can share the same full_url (each
    # entry has a unique entry_url with fragment). We must rebuild the
    # table without the UNIQUE constraint on full_url.
    try:
        # Check if the UNIQUE index on full_url still exists
        autoindexes = [r[1] for r in conn.execute("PRAGMA index_list(getcomics_urls)").fetchall()
                       if r[1].startswith('sqlite_autoindex')]
        has_full_url_unique = any(
            idx for idx in autoindexes
            if conn.execute(f"PRAGMA index_info({idx})").fetchall()
               and conn.execute(f"PRAGMA index_info({idx})").fetchall()[0][2] == 'full_url'
        )
        if has_full_url_unique:
            logger.info("Removing UNIQUE constraint on full_url (rebuilding table)...")
            # Create new table without UNIQUE on full_url
            conn.execute("""
                CREATE TABLE IF NOT EXISTS _getcomics_urls_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL,
                    full_url TEXT NOT NULL,
                    url_slug TEXT,
                    series_norm TEXT,
                    series_norm_norm TEXT,
                    search_aliases TEXT,
                    title TEXT,
                    issue_num TEXT,
                    issue_range TEXT,
                    year INTEGER,
                    volume INTEGER,
                    is_annual INTEGER DEFAULT 0,
                    is_bulk_pack INTEGER DEFAULT 0,
                    is_multi_series INTEGER DEFAULT 0,
                    format_variants TEXT,
                    download_url TEXT,
                    category TEXT,
                    lastmod TEXT,
                    indexed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    scrape_status TEXT DEFAULT 'pending' CHECK (scrape_status IN ('pending','success','failed','empty')),
                    scrape_attempts INTEGER DEFAULT 0,
                    last_scrape_attempt TIMESTAMP,
                    url_last_modified TEXT
                )
            """)
            conn.execute(f"""
                INSERT INTO _getcomics_urls_new
                SELECT id, url, full_url, url_slug, series_norm, series_norm_norm, search_aliases,
                       title, issue_num, issue_range, year, volume, is_annual, is_bulk_pack,
                       is_multi_series, format_variants, download_url, category, lastmod, indexed_at,
                       scrape_status, scrape_attempts, last_scrape_attempt, url_last_modified
                FROM getcomics_urls
            """)
            conn.execute("DROP TABLE getcomics_urls")
            conn.execute("ALTER TABLE _getcomics_urls_new RENAME TO getcomics_urls")
            # Recreate indexes (without UNIQUE on full_url)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_series_norm ON getcomics_urls(series_norm)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_series_norm_norm ON getcomics_urls(series_norm_norm)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_year ON getcomics_urls(year)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_volume ON getcomics_urls(volume)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_issue ON getcomics_urls(issue_num)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_aliases ON getcomics_urls(search_aliases)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_scrape_status ON getcomics_urls(scrape_status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_urls_missing_since ON getcomics_urls(last_scrape_attempt)")
            logger.info("Removed UNIQUE constraint on full_url, rebuilt table")
    except Exception as e:
        logger.debug(f"UNIQUE constraint removal (may already be done): {e}")

    # Migration: if old tables exist and new table is empty, populate from them
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM getcomics_urls")
    existing_count = c.fetchone()[0]

    if existing_count == 0:
        migrated = _migrate_from_old_tables(conn)
        logger.info(f"Migrated {migrated} entries from old tables to getcomics_urls")

    conn.commit()
    conn.close()


def _migrate_from_old_tables(conn) -> int:
    """
    Populate getcomics_urls from the old getcomics_sitemap_urls and
    getcomics_scrape_index tables. Scrape index rows take precedence
    (they have full metadata); sitemap rows fill in gaps.

    After migration, the old tables are dropped.
    """
    import sqlite3

    migrated = 0

    # ── Step 1: Migrate all scrape_index entries (full metadata) ───────────
    try:
        c = conn.cursor()
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='getcomics_scrape_index'")
        if c.fetchone():
            # Collect all scrape_index entries keyed by URL
            scrape_rows = {}
            for row in conn.execute("SELECT * FROM getcomics_scrape_index"):
                scrape_rows[row['url']] = dict(row)

            for url, row in scrape_rows.items():
                norm_series = row.get('series_norm', '') or ''
                conn.execute("""
                    INSERT OR REPLACE INTO getcomics_urls
                    (url, full_url, url_slug, series_norm, series_norm_norm, search_aliases,
                     title, issue_num, issue_range, year, volume,
                     is_annual, is_bulk_pack, is_multi_series, format_variants,
                     download_url, lastmod, indexed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    row.get('url'),
                    row.get('url'),  # full_url = url for scrape entries
                    row.get('url_slug'),
                    norm_series,
                    norm_series.replace('-', ' ').replace('\u2013', ' ').replace('\u2014', ' ').strip().lower() if norm_series else None,
                    row.get('search_aliases'),
                    row.get('title'),
                    row.get('issue_num'),
                    row.get('issue_range'),
                    row.get('year'),
                    row.get('volume'),
                    row.get('is_annual', 0),
                    row.get('is_bulk_pack', 0),
                    row.get('is_multi_series', 0),
                    row.get('format_variants'),
                    row.get('download_url'),  # <-- was missing from INSERT
                    row.get('lastmod'),
                    row.get('indexed_at'),
                ))
                migrated += 1
    except Exception as e:
        logger.debug(f"Migration step 1 (scrape_index) error: {e}")

    # ── Step 2: Add sitemap-only entries (url exists in sitemap but not in scrape) ──
    try:
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='getcomics_sitemap_urls'")
        if c.fetchone():
            for row in conn.execute("SELECT * FROM getcomics_sitemap_urls"):
                url = row['full_url']
                # Skip if already migrated from scrape_index
                existing = conn.execute("SELECT url FROM getcomics_urls WHERE url = ?", (url,)).fetchone()
                if existing:
                    continue
                norm_series = row.get('series_norm', '') or ''
                conn.execute("""
                    INSERT OR REPLACE INTO getcomics_urls
                    (url, full_url, url_slug, series_norm, series_norm_norm, category, lastmod, indexed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    url,
                    url,
                    row.get('url_slug'),
                    norm_series,
                    norm_series.replace('-', ' ').replace('\u2013', ' ').replace('\u2014', ' ').strip().lower() if norm_series else None,
                    row.get('category'),
                    row.get('lastmod'),
                    row.get('indexed_at'),
                ))
                migrated += 1
    except Exception as e:
        logger.debug(f"Migration step 2 (sitemap) error: {e}")

    # ── Step 3: Drop old tables ──────────────────────────────────────────────
    try:
        c.execute("DROP TABLE IF EXISTS getcomics_scrape_index")
        c.execute("DROP TABLE IF EXISTS getcomics_sitemap_urls")
        c.execute("DROP TABLE IF EXISTS getcomics_sitemap_pages")
    except Exception as e:
        logger.debug(f"Migration step 3 (drop old tables) error: {e}")

    return migrated


# ─────────────────────────────────────────────────────────────────────────────
# EXPLICIT SERIES ALIAS TABLE
# Stores user-defined aliases that map alternative series names to their
# canonical GetComics series. Used to avoid redundant scraping when the same
# series appears under multiple names (e.g., "Spider-Man" → "Amazing Spider-Man").
# ─────────────────────────────────────────────────────────────────────────────

def _normalize_alias(name: str) -> str:
    """Normalize a series name for alias storage and lookup."""
    return name.replace('-', ' ').replace('\u2013', ' ').replace('\u2014', ' ').strip().lower()


def _ensure_alias_table():
    """Create the getcomics_series_aliases table if it doesn't exist."""
    from core.database import get_db_connection
    conn = get_db_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS getcomics_series_aliases (
            alias TEXT PRIMARY KEY,
            alias_norm TEXT NOT NULL,
            canonical TEXT NOT NULL,
            canonical_norm TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alias_norm ON getcomics_series_aliases(alias_norm)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_canonical_norm ON getcomics_series_aliases(canonical_norm)")
    conn.commit()
    conn.close()


def is_alias(name: str) -> bool:
    """
    Check if a series name is registered as an alias.

    Args:
        name: Series name to check

    Returns:
        True if name is a registered alias, False otherwise
    """
    _ensure_alias_table()
    from core.database import get_db_connection
    norm = _normalize_alias(name)
    conn = get_db_connection()
    c = conn.execute(
        "SELECT 1 FROM getcomics_series_aliases WHERE alias_norm = ? LIMIT 1",
        (norm,)
    )
    result = c.fetchone()
    conn.close()
    return result is not None


def get_canonical_series(alias: str) -> str | None:
    """
    Get the canonical series name for an alias.

    Args:
        alias: The alias to look up

    Returns:
        The canonical series name, or None if not found
    """
    _ensure_alias_table()
    norm = _normalize_alias(alias)
    from core.database import get_db_connection
    conn = get_db_connection()
    c = conn.execute(
        "SELECT canonical FROM getcomics_series_aliases WHERE alias_norm = ? LIMIT 1",
        (norm,)
    )
    row = c.fetchone()
    conn.close()
    return row[0] if row else None


def resolve_series_alias(name: str) -> str:
    """
    Given any series name (alias or canonical), return the canonical series name.

    If name is a registered alias, returns the canonical series.
    Otherwise returns the original name unchanged.

    Args:
        name: Series name to resolve

    Returns:
        Canonical series name, or original name if not an alias
    """
    canonical = get_canonical_series(name)
    return canonical if canonical else name


def add_series_alias(alias: str, canonical: str) -> bool:
    """
    Register an alias → canonical series mapping.

    Args:
        alias: The alias name (e.g., "Spider-Man")
        canonical: The canonical series name (e.g., "Amazing Spider-Man")

    Returns:
        True if added successfully, False if alias already exists
    """
    if not alias or not canonical:
        return False
    if alias.strip().lower() == canonical.strip().lower():
        return False  # Can't alias to itself
    _ensure_alias_table()
    from core.database import get_db_connection
    conn = get_db_connection()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO getcomics_series_aliases
            (alias, alias_norm, canonical, canonical_norm)
            VALUES (?, ?, ?, ?)
        """, (
            alias.strip(),
            _normalize_alias(alias),
            canonical.strip(),
            _normalize_alias(canonical),
        ))
        conn.commit()
        return True
    except Exception:
        return False
    finally:
        conn.close()


def delete_series_alias(alias: str) -> bool:
    """
    Remove an alias mapping.

    Args:
        alias: The alias to remove

    Returns:
        True if deleted, False if not found
    """
    _ensure_alias_table()
    from core.database import get_db_connection
    conn = get_db_connection()
    c = conn.execute(
        "DELETE FROM getcomics_series_aliases WHERE alias_norm = ?",
        (_normalize_alias(alias),)
    )
    deleted = c.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def get_all_aliases() -> list[dict]:
    """
    List all registered series aliases.

    Returns:
        List of dicts with keys: alias, alias_norm, canonical, canonical_norm, created_at
    """
    _ensure_alias_table()
    from core.database import get_db_connection
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    c = conn.execute(
        "SELECT alias, alias_norm, canonical, canonical_norm, created_at "
        "FROM getcomics_series_aliases ORDER BY alias"
    )
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def _scrape_url_to_index(url: str, url_slug: str = "", series_norm: str = "", lastmod: str = "", search_aliases: str = "") -> list[dict] | None:
    """
    Scrape a GetComics URL and store parsed results in the scrape index.
    Returns the stored rows as dicts, or None if page not modified (304).

    Handles multi-entry listing pages by generating a unique URL per entry:
      - Individual comic page: url is used as-is (single entry)
      - Listing page: each div.post-content entry gets url || '#' || slugified(h5_text)

    This means re-scraping the same page with the same titles will UPDATE existing
    entries rather than create duplicates. Old entries without '#' in URL are
    treated as canonical entries (migration adds #canonical suffix).

    Does NOT use conditional fetching — always scrapes and stores.
    Use update_scrape_index() for conditional re-scraping.
    """
    import re
    from core.database import get_db_connection

    def _slugify(text: str) -> str:
        """Create a URL-safe slug from title text for use as entry identifier."""
        # Get issue number if present (e.g., "#1" -> "1", "1-5" -> "1-5")
        issue_match = re.search(r'#?(\d+(?:\s*[-–—]\s*\d+)?)', text)
        issue_slug = issue_match.group(1).replace(' ', '').replace('\u2013', '-').replace('\u2014', '-') if issue_match else ""
        # Slugify the whole title for uniqueness
        slug = re.sub(r'[^a-z0-9]+', '-', text.lower())
        slug = slug.strip('-')
        # Return issue-slug or just slug
        if issue_slug:
            return f"{issue_slug}-{slug[:40]}"
        return slug[:50]

    scraper = cloudscraper.create_scraper()
    try:
        resp = scraper.get(url, timeout=15)
        if resp.status_code == 304:
            return None
        if resp.status_code != 200:
            return []

        soup = BeautifulSoup(resp.text, 'html.parser')
        results = []
        http_last_modified = resp.headers.get("Last-Modified")
        # Track entry URLs for this page to build result dicts
        entry_urls = []

        def _parse_and_store(title_text: str, download_url: str | None, entry_url: str):
            """Parse a title and store in scrape index."""
            if not title_text or len(title_text) < 3:
                return
            # Normalize ALL dash variants to ASCII dash immediately, before any
            # split or parse. BeautifulSoup's get_text() can return Unicode
            # en-dashes (U+2013) directly from HTML — we normalize those here.
            title_text = title_text.replace('\u2013', '-').replace('\u2014', '-')  # Unicode dashes
            title_text = title_text.replace('\x96', '-').replace('\x97', '-')      # Windows-1252
            title_text = title_text.replace('\ufffd', '-')                          # Replacement char
            title_text = re.sub(r'(\d)\ufffd(\d)', r'\1-\2', title_text)          # digit�digit → digit-digit
            # Normalize title suffix — but only split on " - " when it looks like
            # a suffix separator (followed by something like "GetComics" or a year),
            # NOT when " - " is part of an issue range like "Top 10 #1 - 12".
            # We detect this by checking: if the character before " - " is a digit,
            # it's likely an issue range separator, not a title suffix.
            suffix_split = False
            for sep in [" - ", " \u2013 ", " \u2014 ", " \x97 "]:
                idx = title_text.find(sep)
                if idx > 0 and idx < len(title_text) - len(sep):
                    char_before = title_text[idx - 1]
                    if char_before.isdigit():
                        continue  # Skip this separator — it's an issue range dash
                if sep in title_text:
                    title_text = title_text.split(sep)[0].strip()
                    suffix_split = True
                    break
            if not suffix_split:
                if "GetComics" in title_text:
                    title_text = title_text.split("GetComics")[0].strip().rstrip("-").rstrip()

            parsed = parse_result_title(title_text)
            # Use the passed-in series_norm as canonical (preserves CLU's naming),
            # but when the page title normalizes to something different, record that
            # as a search_alias so searches for either name find this scrape.
            # E.g. CLU searches "The Punisher" but page title normalized to "Punisher" —
            # both stored_series="The Punisher" and aliases="Punisher" get stored.
            entry_aliases = search_aliases
            if parsed.name:
                page_norm = normalize_series_name(parsed.name)[0]
                stored_series = series_norm if series_norm else page_norm
                if page_norm and page_norm != stored_series:
                    existing = entry_aliases or ""
                    if page_norm not in existing:
                        entry_aliases = f"{existing},{page_norm}" if existing else page_norm
            elif series_norm:
                stored_series = series_norm
            else:
                stored_series = ""

            conn = get_db_connection()
            # Get current scrape_attempts before INSERT OR REPLACE wipes it
            current_attempts = conn.execute(
                "SELECT COALESCE(scrape_attempts, 0) FROM getcomics_urls WHERE url = ?", (entry_url,)
            ).fetchone()
            current_attempts = current_attempts[0] if current_attempts else 0
            if download_url:
                scrape_status = 'success'
            else:
                scrape_status = 'empty'
            conn.execute("""
                INSERT OR REPLACE INTO getcomics_urls
                (url, full_url, url_slug, series_norm, series_norm_norm, search_aliases,
                 title, issue_num, issue_range, year, volume,
                 is_annual, is_bulk_pack, is_multi_series, format_variants,
                 download_url, lastmod, indexed_at, scrape_status, scrape_attempts,
                 last_scrape_attempt, url_last_modified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP,
                        ?, ?, CURRENT_TIMESTAMP, ?)
            """, (
                entry_url,     # unique per-entry URL
                url,           # base page URL for lookups
                url_slug,
                stored_series,
                stored_series.replace('-', ' ').replace('\u2013', ' ').replace('\u2014', ' ').strip().lower() if stored_series else None,
                entry_aliases,
                title_text,
                parsed.issue,
                str(parsed.issue_range) if parsed.issue_range else None,
                parsed.year,
                parsed.volume,
                int(parsed.is_annual),
                int(parsed.is_bulk_pack),
                int(parsed.is_multi_series),
                ','.join(parsed.format_variants) if parsed.format_variants else None,
                download_url,
                lastmod,
                scrape_status,
                current_attempts + 1,
                http_last_modified,
            ))
            conn.commit()
            conn.close()
            entry_urls.append(entry_url)
            results.append({
                'url': entry_url, 'full_url': url, 'series_norm': stored_series, 'url_slug': url_slug,
                'title': title_text, 'issue_num': parsed.issue,
                'issue_range': parsed.issue_range, 'year': parsed.year,
                'volume': parsed.volume, 'is_annual': parsed.is_annual,
                'is_bulk_pack': parsed.is_bulk_pack, 'download_url': download_url,
            })

        # Individual comic page: title from <title> tag, download from button
        title_tag = soup.find("title")
        if title_tag:
            title_text = title_tag.get_text(strip=True)
            download_url = None
            for btn in soup.select('a[class*="aio-red"], a[class*="aio-blue"]'):
                href = btn.get('href', '')
                if href.startswith('http'):
                    download_url = href
                    break
            _parse_and_store(title_text, download_url, url)  # single entry uses base url

        # Listing page (variant 1): titles from post-content divs
        # Each entry gets a unique URL: base_url + '#' + slugified(h5_text)
        for el in soup.select("div.post-content"):
            h5 = el.select_one("h5 a") or el.select_one("h4 a") or el.select_one("h3 a")
            if not h5:
                continue
            title_text = h5.get_text(strip=True)
            entry_slug = _slugify(title_text)
            entry_url = f"{url}#{entry_slug}"
            download_url = None
            for btn in el.select('a[class*="aio-red"], a[class*="aio-blue"]'):
                href = btn.get('href', '')
                if href.startswith('http'):
                    download_url = href
                    break
            _parse_and_store(title_text, download_url, entry_url)

        # Listing page (variant 2): Top-10-style collection page.
        # Structure: titles are in <strong> tags (direct children of <p> elements in
        # <article>), download buttons are in the same <article>. We match by order:
        # titles[i] -> buttons[i]. The title <strong> is identified by filtering out
        # metadata labels (Language, Year, Size, Image Format, etc.).
        articles = soup.find_all('article', class_='post-body')
        if articles:
            article = articles[0]
            all_buttons = article.find_all('a', class_=lambda c: c and 'aio-red' in c)
            if len(all_buttons) >= 2:
                # Title <strong> tags are direct children of <p>, filtered by excluding
                # metadata labels
                _METADATA_LABELS = ('Language', 'Image Format', 'Year', 'Size',
                                    'Download', 'Mirror', 'Notes', 'Screenshots', 'If you')
                titles_found: list = []
                for p in article.find_all('p'):
                    for s in p.find_all('strong', recursive=False):
                        text = s.get_text(strip=True)
                        if text and not any(text.startswith(kw) for kw in _METADATA_LABELS):
                            if len(text) > 3:
                                titles_found.append(text)
                                break
                # Match by order
                for title_text, btn in zip(titles_found, all_buttons):
                    download_url = btn.get('href', '') if btn else None
                    entry_slug = _slugify(title_text)
                    entry_url = f"{url}#{entry_slug}"
                    _parse_and_store(title_text, download_url, entry_url)

        return results
    except Exception as e:
        logger.debug(f"Error scraping {url}: {e}")
        return []


def build_scrape_index(
    max_urls: int | None = None,
    force_refresh: bool = False,
    max_workers: int = 10,
    rate_limit: float = 0.5,
    progress_callback=None,
) -> int:
    """
    Build the GetComics scrape index using concurrent threaded scraping.

    Populates the getcomics_scrape_index table with parsed title data from
    sitemap URLs. Uses a ThreadPoolExecutor for concurrent I/O — 10 workers
    at 0.5s rate limit means ~20 URLs/sec, so 70K URLs takes ~1 hour.

    Args:
        max_urls: Maximum number of URLs to scrape (None = all)
        force_refresh: Re-scrape even already-indexed URLs
        max_workers: Number of concurrent scraping threads (default 10)
        rate_limit: Seconds to wait between requests per thread (default 0.5)
        progress_callback: Optional callable(processed, total) for progress updates

    Returns:
        Number of URLs successfully scraped and indexed
    """
    import time
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from core.database import get_db_connection

    _ensure_urls_table()

    conn = get_db_connection()
    if force_refresh:
        query = "SELECT full_url, url_slug, series_norm, lastmod FROM getcomics_urls"
        if max_urls:
            query += f" LIMIT {max_urls}"
        urls_to_scrape = conn.execute(query).fetchall()
    else:
        # Retry entries that need scraping: pending, empty, or failed.
        # Cap retries at 5 attempts to avoid hammering GetComics.
        query = """SELECT full_url, url_slug, series_norm, lastmod
                   FROM getcomics_urls
                   WHERE scrape_status IN ('pending', 'empty', 'failed')
                     AND (scrape_attempts IS NULL OR scrape_attempts < 5)"""
        if max_urls:
            query += f" LIMIT {max_urls}"
        urls_to_scrape = conn.execute(query).fetchall()
    conn.close()

    total = len(urls_to_scrape)
    scraped = 0
    scraped_lock = threading.Lock()

    def _scrape_one(row):
        nonlocal scraped
        _, full_url, url_slug, series_norm, lastmod = row
        time.sleep(rate_limit)  # Per-worker rate limit
        results = _scrape_url_to_index(full_url, url_slug, series_norm, lastmod or "")
        with scraped_lock:
            if results is not None:
                scraped += 1
            if progress_callback:
                progress_callback(scraped, total)
        return results

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_scrape_one, row): row for row in urls_to_scrape}
        for future in as_completed(futures):
            # Results collected via callback; any exception is logged and ignored
            try:
                future.result()
            except Exception as e:
                logger.debug(f"Scraping error: {e}")

    return scraped


def update_scrape_index(
    series_name: str,
    force_refresh: bool = False,
    max_workers: int = 10,
    rate_limit: float = 0.5,
    progress_callback=None,
    freshness_days: int = 3,
    refresh_for_issue: str | None = None,
) -> int:
    """
    Update the scrape index for a specific series using concurrent scraping.

    Uses sitemap lastmod for conditional fetching — only re-scrapes URLs whose
    sitemap entry has changed since last index, OR whose index entry is older
    than freshness_days (to catch new releases not in sitemap lastmod).

    Additionally, when an upcoming issue is known (refresh_for_issue), the
    metadata provider tells us which issue number is about to release. If that
    issue is not yet indexed or is stale, we mark it for scraping so the index
    is ready when the scheduled download runs.

    Args:
        series_name: Series to update (e.g., "Spider-Man")
        force_refresh: Re-scrape all URLs for series regardless of lastmod
        max_workers: Number of concurrent threads (default 10)
        rate_limit: Seconds between requests per thread (default 0.5)
        progress_callback: Optional callable(processed, total) for progress updates
        freshness_days: Re-scrape entries older than this many days (default 3).
                        Set to 0 to disable time-based refresh.
        refresh_for_issue: Issue number coming soon (e.g., "50"). If the scrape
                          index doesn't have an entry for this issue, the
                          relevant URL will be scraped proactively.

    Returns:
        Number of URLs successfully scraped and indexed
    """
    import time
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from core.database import get_db_connection

    _ensure_urls_table()

    urls = lookup_series_urls(series_name)
    if not urls:
        return 0

    # Pre-load existing indexed_at timestamps, lastmod, and scrape_status
    conn = get_db_connection()
    existing = {}
    for row in conn.execute("SELECT url, lastmod, indexed_at, scrape_status FROM getcomics_urls").fetchall():
        existing[row[0]] = {"lastmod": row[1], "indexed_at": row[2], "scrape_status": row[3]}
    conn.close()

    now = time.time()

    # Build a set of issue numbers we already have indexed for this series
    # to determine if refresh_for_issue is missing
    indexed_issues: set[str] = set()
    for idx_url in existing:
        # URL format: .../series-name-ISSUE-2024/ → extract issue number
        # e.g., spider-man-50-2024 → issue 50
        url_lower = idx_url.lower()
        # Try to find an issue number in the URL
        import re
        url_issue = re.search(r'/([a-z-]+)-(\d+)(?:-\d{4})?/', url_lower)
        if url_issue:
            indexed_issues.add(url_issue.group(2))

    # Filter to URLs that need scraping
    urls_to_scrape = []
    for entry in urls:
        url = entry['full_url']
        sm_lastmod = entry.get('lastmod') or ""
        idx_info = existing.get(url)

        if force_refresh:
            urls_to_scrape.append(entry)
            continue

        if idx_info is None:
            # Not yet indexed — scrape it
            urls_to_scrape.append(entry)
            continue

        # Check scrape status — 'empty' and 'failed' entries need re-scrape regardless
        # of lastmod (rate-limiting or partial scrape means we don't trust lastmod)
        scrape_status = idx_info.get("scrape_status", "pending")
        if scrape_status in ('empty', 'failed'):
            urls_to_scrape.append(entry)
            continue

        # Already indexed — check staleness (sitemap lastmod only)
        # Note: Time-based staleness removed — proactive scraping via
        # refresh_for_issue and live-result indexing handle freshness.
        is_stale = False

        if sm_lastmod and idx_info["lastmod"] != sm_lastmod:
            # Sitemap lastmod changed — fresh content on GetComics
            is_stale = True

        # If we know an upcoming issue and this URL is for that issue but
        # not yet indexed — scrape it proactively
        needs_proactive_scrape = False
        if refresh_for_issue and not is_stale:
            # Check if this URL matches the upcoming issue number
            url_lower = url.lower()
            if f"-{refresh_for_issue}-" in url_lower or url_lower.endswith(f"-{refresh_for_issue}/"):
                # This URL is for the upcoming issue — make sure it's fresh
                if idx_info and idx_at:
                    try:
                        from datetime import datetime, timedelta
                        cutoff = datetime.now() - timedelta(days=1)
                        idx_dt = datetime.fromisoformat(idx_at) if idx_at else None
                        if idx_dt is None or idx_dt < cutoff:
                            needs_proactive_scrape = True
                    except Exception:
                        pass
                elif idx_info is None:
                    needs_proactive_scrape = True

        if is_stale or needs_proactive_scrape:
            urls_to_scrape.append(entry)

    total = len(urls_to_scrape)
    scraped = 0
    scraped_lock = threading.Lock()

    def _scrape_one(entry):
        nonlocal scraped
        url = entry['full_url']
        url_slug = entry['url_slug']
        series_norm = entry['series_norm']
        lastmod = entry.get('lastmod') or ""
        time.sleep(rate_limit)
        results = _scrape_url_to_index(url, url_slug, series_norm, lastmod)
        with scraped_lock:
            if results is not None:
                scraped += 1
            if progress_callback:
                progress_callback(scraped, total)
        return results

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_scrape_one, entry): entry for entry in urls_to_scrape}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                logger.debug(f"Scraping error: {e}")

    logger.info(f"update_scrape_index('{series_name}'): scraped {scraped}/{total} URLs")
    return scraped


def index_live_results(urls: list[str], series_norm: str = "", rate_limit: float = 1.0) -> int:
    """
    Index live search result URLs into the scrape index.

    Checks which URLs are new or have changed since last indexed,
    then scrapes and stores them. Only indexes URLs not already in
    the index with the same content — respects conditional fetching
    via lastmod comparison.

    This is called after a live GetComics search finds results,
    so we capture those URLs for future scrape-index lookups.

    Args:
        urls: List of GetComics page URLs to index
        series_norm: Series name for context (extracted from URL slug if not provided)
        rate_limit: Seconds between requests (default 1.0 — good GetComics citizen)

    Returns:
        Number of URLs newly indexed or updated
    """
    import time
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    from core.database import get_db_connection

    if not urls:
        return 0

    _ensure_urls_table()

    # Check which URLs need indexing (new or changed since last index)
    conn = get_db_connection()
    existing = {}
    for row in conn.execute(
        "SELECT url, lastmod, indexed_at FROM getcomics_urls WHERE url IN ("
        + ",".join("?" * len(urls)) + ")",
        urls
    ).fetchall():
        existing[row[0]] = {"lastmod": row[1], "indexed_at": row[2]}
    conn.close()

    # For new URLs we have no lastmod — scrape them
    # For existing URLs, we need to check if GetComics page has changed
    # We don't store lastmod for live results, so we scrape and compare ETag/Last-Modified
    to_scrape = []
    for url in urls:
        if url not in existing:
            to_scrape.append((url, "new"))
        # For already-indexed URLs, we rely on update_scrape_index to handle refreshes
        # based on sitemap lastmod. Live-result URLs won't have lastmod from sitemap,
        # so we skip re-scraping existing ones here (they'll be picked up in periodic refresh).

    if not to_scrape:
        logger.debug(f"index_live_results: all {len(urls)} URLs already indexed, skipping")
        return 0

    total = len(to_scrape)
    indexed = 0
    lock = threading.Lock()

    def _scrape_one(item):
        nonlocal indexed
        url, reason = item
        time.sleep(rate_limit)

        # Extract series_norm from URL slug if not provided
        # e.g., "https://getcomics.org/marvel/spider-man-1-2019/" -> "spider-man"
        slug_series = series_norm
        if not slug_series:
            parts = url.rstrip("/").split("/")
            slug = parts[-1] if parts else ""
            slug_series = slug.replace("-", " ").replace("_", " ").strip()

        result = _scrape_url_to_index(url, url_slug="", series_norm=slug_series, lastmod="")
        with lock:
            if result is not None:
                indexed += 1

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {executor.submit(_scrape_one, item): item for item in to_scrape}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                logger.debug(f"index_live_results error: {e}")

    logger.info(f"index_live_results: indexed {indexed}/{total} new URLs for series '{series_norm}'")
    return indexed


def prepopulate_series_index(series_name: str, max_workers: int = 5, rate_limit: float = 0.5):
    """
    Pre-populate the scrape index for a newly added series.

    This runs in a background thread and updates the scrape index for the
    given series so it's ready for future searches. Called when a series
    is subscribed/mapped to a pull list.

    Args:
        series_name: Series to prepopulate (e.g., "Batman", "Spider-Man")
        max_workers: Concurrent scraping threads (default 5)
        rate_limit: Seconds between requests per thread (default 0.5)
    """
    import threading

    def _background_scrape():
        logger.info(f"Pre-populating scrape index for series: {series_name}")
        try:
            count = update_scrape_index(
                series_name,
                force_refresh=False,
                max_workers=max_workers,
                rate_limit=rate_limit,
            )
            logger.info(f"Pre-populated {count} URLs for series '{series_name}'")
        except Exception as e:
            logger.error(f"Error pre-populating scrape index for {series_name}: {e}")

    thread = threading.Thread(target=_background_scrape, daemon=True)
    thread.start()


def update_series_aliases(series_name: str, aliases: str) -> int:
    """
    Update search_aliases for all scrape index entries matching a series.

    Called when a user edits aliases on the series page. The aliases string
    is comma-separated, stored normalized (lowercase, hyphens→spaces).

    Args:
        series_name: The series whose entries to update
        aliases: Comma-separated alias names to store

    Returns:
        Number of entries updated
    """
    from core.database import get_db_connection
    _ensure_urls_table()

    # Normalize aliases: lowercase, hyphens to spaces
    alias_list = []
    for a in aliases.split(','):
        a = a.strip().lower().replace('-', ' ').replace('\u2013', ' ').replace('\u2014', ' ')
        if a:
            alias_list.append(a)

    normalized_aliases = ','.join(alias_list)

    conn = get_db_connection()
    norm_series, _ = normalize_series_name(series_name)
    # Match series_norm after normalization
    norm_series_lower = norm_series.lower().replace('-', ' ').replace('\u2013', ' ').replace('\u2014', ' ')

    updated = conn.execute("""
        UPDATE getcomics_urls
        SET search_aliases = ?
        WHERE LOWER(REPLACE(REPLACE(series_norm, '-', ' '), '\u2013', ' ')) = ?
    """, (normalized_aliases, norm_series_lower)).rowcount

    conn.commit()
    conn.close()
    return updated


def get_sitemap_subseries_aliases(series_name: str) -> list[str]:
    """
    Get candidate aliases for a series from the GetComics sitemap.

    Returns all series_norm values in the sitemap that share the same URL
    prefix as the given series. These are GetComics' own categorization
    and make good candidate aliases for the series page.

    Args:
        series_name: Series to look up (e.g., "Spider-Man", "Amazing Spider-Man")

    Returns:
        List of unique series_norm values (normalized) that share the URL prefix
    """
    from core.database import get_db_connection

    norm_series, _ = normalize_series_name(series_name)
    slug_pattern = norm_series.replace(' ', '-') + '%'

    conn = get_db_connection()
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT DISTINCT series_norm
        FROM getcomics_urls
        WHERE url_slug LIKE ? COLLATE NOCASE
        ORDER BY series_norm
    """, (slug_pattern,)).fetchall()

    conn.close()
    return [dict(r)['series_norm'] for r in rows]


@dataclass
class ScrapeSearchCriteria:
    """Structured search criteria for the scrape index."""
    series_norm: str = ""
    year: int | None = None
    volume: int | None = None
    issue_num: str = ""
    issue_range: tuple[int, int] | None = None
    is_annual: bool = False
    is_bulk_pack: bool | None = None  # None = no filter, True = only bulk, False = exclude bulk
    is_multi_series: bool | None = None  # None = no filter, True = only multi, False = exclude multi
    accept_variants: list[str] = field(default_factory=list)


def search_scrape_index(criteria: ScrapeSearchCriteria, limit: int = 50) -> list[dict]:
    """
    Search the scrape index using structured criteria.

    Args:
        criteria: ScrapeSearchCriteria with series_norm, year, volume, issue_num, etc.
        limit: Maximum results to return

    Returns:
        List of matching index rows as dicts (includes download_url)
    """
    from core.database import get_db_connection
    _ensure_urls_table()

    # Resolve alias to canonical before searching
    resolved_series = resolve_series_alias(criteria.series_norm)

    conn = get_db_connection()

    # Normalize series_norm to match sitemap convention: hyphens → spaces
    # The sitemap stores 'spider man', 'batman', etc. (spaces, lowercase)
    # The search query might use 'Spider-Man' (hyphens) or 'Spider Man' (spaces)
    norm_series = resolved_series.replace('-', ' ').replace('\u2013', ' ').replace('\u2014', ' ').strip().lower()

    # Use series_norm_norm (pre-computed, indexed) when available.
    # Falls back to LOWER/REPLACE for existing installs that haven't migrated.
    norm_col = "series_norm_norm"
    if not conn.execute("PRAGMA table_info(getcomics_urls)").fetchall():
        norm_col = None
    else:
        existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(getcomics_urls)").fetchall()}
        if "series_norm_norm" not in existing_cols:
            norm_col = None

    if norm_col:
        query = f"""SELECT * FROM getcomics_urls WHERE {norm_col} = ? COLLATE NOCASE"""
    else:
        query = """SELECT * FROM getcomics_urls
                   WHERE LOWER(REPLACE(REPLACE(series_norm, '-', ' '), '\u2013', ' ')) = ? COLLATE NOCASE"""
    alias_query = """SELECT * FROM getcomics_urls
                    WHERE LOWER(REPLACE(REPLACE(search_aliases, '-', ' '), '\u2013', ' ')) LIKE ? COLLATE NOCASE"""
    params = [norm_series]
    alias_params = [f"%{norm_series}%"]

    if criteria.year is not None:
        query += " AND (year = ? OR year IS NULL)"
        params.append(criteria.year)
        alias_query += " AND (year = ? OR year IS NULL)"
        alias_params.append(criteria.year)

    if criteria.volume is not None:
        query += " AND volume = ?"
        params.append(criteria.volume)
        alias_query += " AND volume = ?"
        alias_params.append(criteria.volume)

    if criteria.issue_num:
        query += " AND (issue_num = ? OR issue_num LIKE ?"
        params.extend([criteria.issue_num, f"{criteria.issue_num}-%"])
        if criteria.issue_range:
            # Also include range packs so the in-memory filter can check containment
            query += " OR issue_range IS NOT NULL"
        query += ")"
        alias_query += " AND (issue_num = ? OR issue_num LIKE ?"
        alias_params.extend([criteria.issue_num, f"{criteria.issue_num}-%"])
        if criteria.issue_range:
            alias_query += " OR issue_range IS NOT NULL"
        alias_query += ")"
    elif criteria.issue_range:
        # No exact issue_num requested — fetch everything for this series so we can
        # check range packs in-memory (issue_range stored as string "(start,end)")
        pass
    elif not criteria.issue_num and not criteria.issue_range:
        # No issue filter at all — multi-series and other NULL-issue entries are valid
        pass

    if criteria.is_annual:
        query += " AND is_annual = 1"
        alias_query += " AND is_annual = 1"

    if criteria.is_bulk_pack is not None:
        query += " AND is_bulk_pack = ?"
        params.append(int(criteria.is_bulk_pack))
        alias_query += " AND is_bulk_pack = ?"
        alias_params.append(int(criteria.is_bulk_pack))

    # Only search entries that have been fully scraped (download_url is usable)
    query += " AND scrape_status = 'success'"
    alias_query += " AND scrape_status = 'success'"

    if criteria.is_multi_series is not None:
        # multi-series entries have NULL issue_num — only filter if explicitly requested
        query += " AND is_multi_series = ?"
        params.append(int(criteria.is_multi_series))
        alias_query += " AND is_multi_series = ?"
        alias_params.append(int(criteria.is_multi_series))

    query += " ORDER BY year DESC, volume DESC, issue_num LIMIT ?"
    params.append(limit)
    alias_query += " ORDER BY year DESC, volume DESC, issue_num LIMIT ?"
    alias_params.append(limit)

    rows = conn.execute(query, params).fetchall()

    # If no results, fall back to searching by search_aliases — useful when GetComics
    # uses a generic name (e.g. "spider man") but CLU's canonical name is "Amazing Spider-Man"
    if not rows:
        rows = conn.execute(alias_query, alias_params).fetchall()
    conn.close()

    # In-memory range pack filter: keep rows whose issue_range contains the target issue
    if criteria.issue_range:
        target_start, target_end = criteria.issue_range
        rows = [
            r for r in rows
            if r['issue_range'] is None or _range_contains_target(r['issue_range'], target_start, target_end)
        ]

    results = []
    for row in rows:
        results.append({
            'url': row['url'],
            'series_norm': row['series_norm'],
            'url_slug': row['url_slug'],
            'title': row['title'],
            'issue_num': row['issue_num'],
            'issue_range': row['issue_range'],
            'year': row['year'],
            'volume': row['volume'],
            'is_annual': bool(row['is_annual']),
            'is_bulk_pack': bool(row['is_bulk_pack']),
            'is_multi_series': bool(row['is_multi_series']),
            'format_variants': row['format_variants'].split(',') if row['format_variants'] else [],
            'download_url': row['download_url'],
            'search_aliases': row['search_aliases'],
        })
    return results


def try_scrape_index(
    search_name: str,
    issue_num,
    issue_year=None,
    series_volume=None,
    series_year=None,
    search_variants=None,
) -> tuple[dict | None, int]:
    """
    Search the scrape index for a matching comic and return the first ACCEPT result.

    This is the core of the scrape-index-first search strategy: rather than hitting
    GetComics live, we search the locally-stored scrape index which has already been
    populated by background scraping.

    Args:
        search_name: Series name to search for
        issue_num: Issue number to match
        issue_year: Year of the issue (optional)
        series_volume: Volume number (optional)
        series_year: Volume year for soft year matching (optional)
        search_variants: List of variant keywords to accept (optional)

    Returns:
        (result_dict, score) if ACCEPT found, else (None, 0)
        result_dict has keys: title, link, image, download_url
    """
    """
    Search the scrape index for a matching comic and return the first ACCEPT result.

    This is the core of the scrape-index-first search strategy: rather than hitting
    GetComics live, we search the locally-stored scrape index which has already been
    populated by background scraping.

    The search uses a tiered approach:
      1. Direct issue-number match — uses stored issue_num/issue_range fields (no scoring needed)
      2. Fuzzy title scoring — falls back to score_comic when direct match is ambiguous

    Args:
        search_name: Series name to search for
        issue_num: Issue number to match
        issue_year: Year of the issue (optional)
        series_volume: Volume number (optional)
        series_year: Volume year for soft year matching (optional)
        search_variants: List of variant keywords to accept (optional)

    Returns:
        (result_dict, score) if ACCEPT found, else (None, 0)
        result_dict has keys: title, link, image, download_url
    """
    resolved = resolve_series_alias(search_name)
    norm_series = resolved.replace('-', ' ').replace('\u2013', ' ').replace('\u2014', ' ').strip().lower()

    target_issue = int(str(issue_num).lstrip('0') or '0') if issue_num else None

    # ── Tier 1: Direct issue-number match via SQL ──────────────────────────────
    # Uses the stored issue_num / issue_range columns — no title scoring needed.
    # Only returns entries with scrape_status='success' (fully scraped with download_url).
    # Entries with 'empty' or 'failed' status are retried by the background indexer but
    # not served to users (don't want to return a missing download to a user).
    from core.database import get_db_connection
    _ensure_urls_table()
    conn = get_db_connection()

    def _build_query(year_filter, volume_filter):
        """Build parameterized query for exact + alias matching."""
        col = "series_norm_norm"
        existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(getcomics_urls)").fetchall()}
        use_norm = "series_norm_norm" in existing_cols

        if use_norm:
            base_q = f"SELECT * FROM getcomics_urls WHERE {col} = ? COLLATE NOCASE"
        else:
            base_q = """SELECT * FROM getcomics_urls
                        WHERE LOWER(REPLACE(REPLACE(series_norm, '-', ' '), '\u2013', ' ')) = ? COLLATE NOCASE"""
        alias_q = """SELECT * FROM getcomics_urls
                     WHERE LOWER(REPLACE(REPLACE(search_aliases, '-', ' '), '\u2013', ' ')) LIKE ? COLLATE NOCASE"""
        p = [norm_series]
        ap = [f"%{norm_series}%"]

        if year_filter is not None:
            base_q += " AND (year = ? OR year IS NULL)"
            alias_q += " AND (year = ? OR year IS NULL)"
            p.append(year_filter)
            ap.append(year_filter)
        if volume_filter is not None:
            base_q += " AND volume = ?"
            alias_q += " AND volume = ?"
            p.append(volume_filter)
            ap.append(volume_filter)

        # Direct issue match: exact issue_num OR range containing target
        base_q += " AND (issue_num = ? OR issue_num LIKE ? OR issue_range IS NOT NULL)"
        alias_q += " AND (issue_num = ? OR issue_num LIKE ? OR issue_range IS NOT NULL)"
        p.extend([str(issue_num), f"{issue_num}-%"])
        ap.extend([str(issue_num), f"{issue_num}-%"])

        base_q += " AND is_bulk_pack = 0 AND scrape_status = 'success'"
        alias_q += " AND is_bulk_pack = 0 AND scrape_status = 'success'"

        base_q += " ORDER BY year DESC, volume DESC LIMIT 20"
        alias_q += " ORDER BY year DESC, volume DESC LIMIT 20"
        return base_q, alias_q, p, ap

    # Try exact → alias fallback
    base_q, alias_q, params, alias_params = _build_query(issue_year, series_volume)
    rows = conn.execute(base_q, params).fetchall()
    if not rows:
        rows = conn.execute(alias_q, alias_params).fetchall()

    # Filter range packs in Python (issue_range stored as string "(start, end)")
    def _range_overlaps(rng, target):
        if rng is None:
            return False
        try:
            import ast
            rng_clean = rng.strip("()")
            parts = [int(x.strip()) for x in rng_clean.split(",")]
            start, end = parts[0], parts[1]
            return start <= target <= end
        except Exception:
            return False

    if target_issue is not None:
        rows = [r for r in rows if r['issue_num'] == str(issue_num) or
                (r['issue_num'] or '').startswith(str(issue_num) + '-') or
                (r['issue_range'] and _range_overlaps(r['issue_range'], target_issue))]

    if rows:
        conn.close()
        first = rows[0]
        return {
            'title': first['title'],
            'link': first['url'],
            'image': '',
            'download_url': first['download_url'] or None,
        }, 50  # high confidence for direct DB match

    conn.close()

    # ── Tier 2: Fuzzy scoring fallback ─────────────────────────────────────────
    criteria = ScrapeSearchCriteria(
        series_norm=search_name,
        year=issue_year,
        volume=series_volume,
        issue_num=str(issue_num),
        issue_range=(target_issue, target_issue) if issue_num else None,
    )
    results = search_scrape_index(criteria, limit=50)
    if not results:
        return None, 0

    sc = search_criteria(
        series_name=search_name,
        issue_number=str(issue_num),
        year=issue_year,
        series_volume=series_volume,
        volume_year=series_year,
        accept_variants=search_variants,
    )
    scored = []
    for r in results:
        cs = score_comic(r['title'], sc)
        decision = accept_result(cs.score, cs.range_contains_target, cs.series_match)
        scored.append((cs.score, decision, cs, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    for score, decision, cs, r in scored:
        if decision == "ACCEPT":
            return {
                'title': r['title'],
                'link': r['url'],
                'image': '',
                'download_url': r.get('download_url') or None,
            }, score
    return None, 0


def search_getcomics_for_issue(
    series_name,
    issue_num,
    issue_year=None,
    series_volume=None,
    series_year=None,  # Volume year (e.g., 2024 for "Flash Gordon 2024")
    search_variants=None,
    rate_limit=2,
):
    """
    Search GetComics for a specific issue, combining base and variant searches.

    Strategy:
    1. Try sitemap index to pre-filter: skip if series not in GetComics at all
    2. Try direct URL candidates from sitemap index (bypasses search for indexed series)
    3. Fall back to search queries if sitemap doesn't yield ACCEPT

    Args:
        series_name: Name of the series (e.g., "Captain America")
        issue_num: Issue number (e.g., "1", "1.5", "10A")
        issue_year: Year of the issue release (e.g., 2005) - optional
        series_volume: Volume number of the series (e.g., 5 for Vol 5) - optional
        series_year: Volume year of the series (e.g., 2024 for "Flash Gordon 2024") - optional
        search_variants: List of variant keywords to include in search - optional
        rate_limit: Seconds to wait between searches (default 2)

    Returns:
        list: Combined search results from GetComics (deduplicated), or empty list if none found
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    if not series_name or not issue_num:
        return []

    search_variants = search_variants or []

    # Build searchable context for logging
    ctx_parts = [f"{series_name} #{issue_num}"]
    if series_volume:
        ctx_parts.insert(1, f"Vol {series_volume}")
    if issue_year:
        ctx_parts.append(str(issue_year))
    search_context = "[" + ", ".join(ctx_parts) + "]"

    # Rate-limit semaphore: allows up to 2 concurrent requests, each waits 1s between calls
    rate_limiter = threading.Semaphore(2)
    def _rate_limited_scrape(url, *args, **kwargs):
        with rate_limiter:
            time.sleep(1)
            return scrape_and_score_candidate(url, *args, **kwargs)

    # ── STEP 1: Try sitemap index ─────────────────────────────────────────────
    series_urls = lookup_series_urls(series_name)

    if not series_urls:
        app_logger.info(f"🔍 Series '{series_name}' not in sitemap index, using search "
                       f"{search_context}")
    else:
        app_logger.info(f"🔍 Sitemap index has {len(series_urls)} URLs for '{series_name}', "
                       f"trying direct candidates first {search_context}")

        # Try sitemap candidates CONCURRENTLY — up to 5 workers scraping in parallel
        candidates_accepted = []
        accept_lock = threading.Lock()

        def _try_candidate(entry):
            app_logger.info(f"   Trying sitemap candidate: {entry['full_url']}")
            result_tuple = _rate_limited_scrape(
                entry['full_url'], series_name, issue_num, issue_year,
                series_volume=series_volume, volume_year=series_year,
                publisher_name=None, accept_variants=search_variants,
            )
            if result_tuple:
                result, score = result_tuple
                app_logger.info(f"   Sitemap candidate ACCEPT (score={score}): "
                               f"{result['title'][:60]}")
                with accept_lock:
                    candidates_accepted.append((result, score))

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(_try_candidate, entry) for entry in series_urls[:10]]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    pass

        if candidates_accepted:
            candidates_accepted.sort(key=lambda x: -x[1])
            best = candidates_accepted[0][0]
            app_logger.info(f"✅ Sitemap direct match (score={candidates_accepted[0][1]}): "
                           f"{best['title'][:60]} {search_context}")
            return [best]

        app_logger.info(f"   No sitemap match, falling back to search "
                       f"(tried {len(series_urls)} candidates) {search_context}")

    # ── STEP 1b: Try scrape index — structured search before live search ───────
    app_logger.info(f"   Checking scrape index for '{series_name}' {search_context}")
    result, score = try_scrape_index(
        series_name, issue_num, issue_year, series_volume, series_year, search_variants
    )
    if result:
        app_logger.info(f"✅ Scrape index ACCEPT via canonical name (score={score}): "
                       f"{result['title'][:60]} {search_context}")
        return [result]

    criteria_check = ScrapeSearchCriteria(series_norm=series_name, issue_num=str(issue_num))
    hit_count = len(search_scrape_index(criteria_check, limit=1))
    if hit_count > 0:
        app_logger.info(f"   Scrape index had {hit_count} candidates but no ACCEPT "
                       f"— falling through to live search {search_context}")
    else:
        base_name = series_name
        for prefix in ["Amazing ", "Incredible ", "Super ", "The "]:
            if base_name.startswith(prefix):
                base_name = base_name[len(prefix):].strip()
                break
        if base_name != series_name:
            app_logger.info(f"   No scrape index hits for canonical '{series_name}', "
                           f"trying base name '{base_name}' {search_context}")
            result, score = try_scrape_index(
                base_name, issue_num, issue_year, series_volume, series_year, search_variants
            )
            if result:
                app_logger.info(f"✅ Scrape index ACCEPT via base name (score={score}): "
                               f"{result['title'][:60]} {search_context}")
                return [result]
            app_logger.info(f"   No scrape index hits for base name either "
                           f"— falling through to live search {search_context}")
        else:
            app_logger.info(f"   No scrape index hits for '{series_name}' "
                           f"— falling through to live search {search_context}")

    # ── STEP 2: Build search queries ───────────────────────────────────────────
    queries_to_try = []
    if series_year:
        queries_to_try.append(" ".join([series_name, str(series_year), issue_num]))
    if series_volume and series_year:
        queries_to_try.append(" ".join([series_name, "vol", str(series_volume), str(series_year), issue_num]))
    if series_volume and not series_year:
        queries_to_try.append(" ".join([series_name, "vol", str(series_volume), issue_num]))
    if issue_year and issue_year != series_year:
        queries_to_try.append(" ".join([series_name, str(issue_year), issue_num]))
    queries_to_try.append(" ".join([series_name, issue_num]))  # bare query always last

    # Execute all queries CONCURRENTLY (3 workers)
    all_results = []
    results_lock = threading.Lock()
    search_semaphore = threading.Semaphore(3)

    def _run_query(query):
        with search_semaphore:
            app_logger.info(f"🔍 Searching GetComics: {query} {search_context}")
            time.sleep(rate_limit)
            query_results = search_getcomics(query, max_pages=1)
            if query_results:
                with results_lock:
                    seen_links = {r["link"] for r in all_results}
                    for r in query_results:
                        if r["link"] not in seen_links:
                            all_results.append(r)
                app_logger.info(f"   Found {len(query_results)} results")

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(_run_query, q) for q in queries_to_try]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                pass

    results = list(all_results)  # snapshot under lock

    # Variant searches — also concurrent with main searches
    if search_variants:
        variant_futures = []
        def _run_variant(query):
            with search_semaphore:
                variant_query_parts = query.split()
                variant_query_parts[-1:-1] = search_variants
                variant_query = " ".join(variant_query_parts)
                app_logger.info(f"🔍 Variant search: {variant_query}")
                time.sleep(rate_limit)
                variant_results = search_getcomics(variant_query, max_pages=1)
                if variant_results:
                    with results_lock:
                        seen_links = {r["link"] for r in results}
                        for r in variant_results:
                            if r["link"] not in seen_links:
                                results.append(r)

        with ThreadPoolExecutor(max_workers=3) as executor:
            variant_futures = [executor.submit(_run_variant, q) for q in queries_to_try]
            for future in as_completed(variant_futures):
                try:
                    future.result()
                except Exception:
                    pass

        app_logger.info(f"✅ Total: {len(results)} unique results after variant searches")

    # ── STEP 3: Background index — fire-and-forget daemon thread ───────────────
    # Index results in background so future searches hit the scrape index immediately.
    # This runs asynchronously while the caller processes results.
    if results:
        def _background_index():
            try:
                result_urls = [r.get("link") or r.get("url") for r in results if r.get("link") or r.get("url")]
                if result_urls:
                    index_live_results(result_urls, series_norm=series_name, rate_limit=0.5)
            except Exception as e:
                app_logger.debug(f"Background index of live results failed: {e}")

        index_thread = threading.Thread(target=_background_index, daemon=True)
        index_thread.start()

    return results
