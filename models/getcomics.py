"""
GetComics.org search and download functionality.
Uses cloudscraper to bypass Cloudflare protection.
"""
import cloudscraper
from bs4 import BeautifulSoup
from dataclasses import dataclass, field
import logging
import re

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
            if len(prefix) > 2 and not re.match(pub_type_end_pattern, prefix, re.IGNORECASE) and not has_volume_before_dash:
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
    format_variants = get_format_variants()
    searching_for_format = False
    if format_variants:
        series_norm = series_lower.replace('-', '').replace('\u2013', '').replace('\u2014', '').lower()
        for fv in format_variants:
            fv_n = fv.replace('-', '').replace('.', '').lower()
            if re.search(rf'\b{re.escape(fv)}\b', series_lower, re.IGNORECASE):
                searching_for_format = True
                break
            if re.search(rf'\b{re.escape(fv_n)}\b', series_norm, re.IGNORECASE):
                searching_for_format = True
                break

    # Range detection — can cause early return
    range_contains_target = _detect_range_contains_target(title_lower, issue_num)
    # Check if range ENDS on target (bulk pack ending on that issue) — immediate -100
    range_ends_on_target = _detect_range_ends_on_target(title_lower, issue_num)
    if range_ends_on_target:
        return ComicScore(score=-100, range_contains_target=True)
    if range_contains_target and result_has_format:
        # Range ending on target with format variant = REJECT
        return ComicScore(score=-100, range_contains_target=True)
    if range_contains_target:
        # Will be capped later; continue scoring
        pass

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

    # Remaining analysis for issue/year matching
    remaining_is_different_series = False
    year_in_series_name = False
    if remaining and sub_series_type is None:
        remaining_cleaned = (remaining.strip()
            .replace('-', '').replace('\u2013', '').replace('\u2014', '')
            .replace(' ', '').replace('#', '').replace('(', '').replace(')', ''))
        is_purely_range = bool(remaining_cleaned) and all(
            c.isdigit() or c == '.' for c in remaining_cleaned)
        starts_with_issue = bool(re.match(r'^#?\d', remaining.strip()))
        starts_with_issue_word = bool(re.match(r'^issue\s*\d', remaining.strip(), re.IGNORECASE))
        starts_with_volume = bool(re.match(r'^vol(ume)?\.?\s*\d', remaining.strip(), re.IGNORECASE))
        if used_the_swap:
            remaining_is_different_series = True
        elif is_purely_range or starts_with_volume:
            remaining_is_different_series = False
        elif not starts_with_issue and not starts_with_issue_word:
            if not remaining.startswith(('-', '\u2013', '\u2014', ':')):
                has_kw = False
                rem_check = (remaining.replace('-', '').replace('\u2013', '')
                             .replace('\u2014', '').lower())
                for kw in get_variant_types():
                    if re.search(rf'\b{re.escape(kw)}\b', rem_check, re.IGNORECASE):
                        has_kw = True
                        break
                if not has_kw:
                    remaining_is_different_series = True

    if remaining_is_different_series:
        score -= 30

    allow_issue_match = series_match and (
        (sub_series_type is None and not remaining_is_different_series) or
        (variant_accepted and sub_series_type != 'arc')
    ) and not (result_has_format and not searching_for_format)

    # Issue matching
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

    # Year matching
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
            if any(int(y) != search.year for y in other_years):
                score -= 20

    # Title tightness
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
    score += 15 if extra == 0 else -10

    # Collected edition penalty
    if sub_series_type is None and not variant_accepted:
        title_rem = title_lower.replace(series_lower, '', 1)
        pub_pattern = r'\b(' + '|'.join(re.escape(p) for p in get_publication_types()) + r')s?\b'
        for kw in get_format_variants() + [pub_pattern, r'\bcompendium\b',
               r'\bcomplete\s+collection\b', r'\blibrary\s+edition\b', r'\bbook\s+\d+\b']:
            if re.search(kw, title_rem):
                score -= 30
                break

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
