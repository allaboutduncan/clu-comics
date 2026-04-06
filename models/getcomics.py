"""
GetComics.org search and download functionality.
Uses cloudscraper to bypass Cloudflare protection.
"""
import cloudscraper
from bs4 import BeautifulSoup
import logging
import re

logger = logging.getLogger(__name__)


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


def series_has_same_brand(search_series: str, result_title: str) -> bool:
    """
    Check if search series and result title share the same brand era keyword.

    Args:
        search_series: Series name from CLU (e.g., "Batman Rebirth")
        result_title: GetComics result title (e.g., "Batman Vol. 3 - Rebirth #1")

    Returns:
        True if both contain the same brand keyword, False otherwise
    """
    # Extract brand from search series
    search_lower = search_series.lower()
    result_lower = result_title.lower()

    brands = get_brand_keywords()
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


def parse_result_title(title: str) -> dict:
    """
    Parse a GetComics result title into structured data.

    This function extracts ALL values from the title in a single pass,
    then constructs the series name by removing all found patterns.

    Examples:
    - "Batman #1 (2020)" -> {name: "Batman", issue: "1", year: 2020}
    - "Batman Vol. 3 #1 (2020)" -> {name: "Batman", volume: 3, issue: "1", year: 2020}
    - "Batman Annual #1 (2020)" -> {name: "Batman Annual", issue: "1", year: 2020}
    - "Justice League Dark 2021 Annual Vol. 1" -> {name: "Justice League Dark 2021 Annual", volume: 1}
    - "Flash Gordon Annual 2014 Vol. 1" -> {name: "Flash Gordon Annual", volume: 1, publication_year: 2014}
    - "Batman - Court of Owls #1 (2020)" -> {name: "Batman", arc_name: "Court of Owls", issue: "1", year: 2020}
    - "Captain America Vol. 5 #1-50 + TPBs" -> {name: "Captain America", volume: 5, issue_range: (1, 50), format_variants: ['tpb']}
    - "Batman One-Shot (2025)" -> {name: "Batman One-Shot", year: 2025, format_variants: ['one-shot']}
    - "Batman: The Killing Joke (2025)" -> {name: "Batman: The Killing Joke", format_variants: ['one-shot']}

    Returns:
        dict with keys:
        - name: normalized series name
        - issue: issue number (or None)
        - issue_range: tuple (start, end) if range (or None)
        - year: publication year from parentheses (or None)
        - volume: volume number (or None)
        - publication_year: year extracted from after variant keywords (or None)
        - is_annual: True if annual in name
        - is_quarterly: True if quarterly in name
        - is_arc: True if has dash notation for arc
        - arc_name: extracted arc name if is_arc
        - format_variants: list of detected format variant keywords (from config)
        - is_multi_series: True if title contains multiple series separators
        - is_range_pack: True if title contains issue range
        - has_tpb_in_pack: True if range pack includes TPBs
        - is_bulk_pack: True if large range pack (10+ issues)
    """
    import re

    if not title:
        return {}

    original = title
    result = {
        'name': None,
        'issue': None,
        'issue_range': None,
        'year': None,
        'volume': None,
        'publication_year': None,
        'is_annual': False,
        'is_quarterly': False,
        'is_arc': False,
        'arc_name': None,
        # Format variants detected - uses format_variants config, stored as list in 'format_variants'
        # Multi-content detection
        'is_multi_series': False,  # Title contains multiple series (&, /, +, "and" separators)
        'is_range_pack': False,    # Title contains issue range (#1-50)
        'has_tpb_in_pack': False,  # Range pack includes TPBs (+ TPBs)
        'is_bulk_pack': False,     # Large range pack (likely bulk download)
    }

    # Track all matched patterns so we can remove them from the title to get the series name
    matched_patterns = []

    # Extract year from parentheses at end: "(2020)"
    year_match = re.search(r'\((\d{4})\)\s*$', title)
    if year_match:
        result['year'] = int(year_match.group(1))
        matched_patterns.append((year_match.start(), year_match.end()))

    # Extract issue number and range: "#1", "#1-50", "#1 – 19", "Issue 5", "Issues 1-12"
    # Note: GetComics uses en-dashes (–) and em-dashes (—) in ranges, with optional spaces around them
    issue_match = re.search(r'#(\d+(?:\s*[-\u2013\u2014]\s*\d+)?)', title, re.IGNORECASE)
    if not issue_match:
        issue_match = re.search(r'\bissues?\s*(\d+(?:\s*[-\u2013\u2014]\s*\d+)?)\b', title, re.IGNORECASE)
    if issue_match:
        issue_str = issue_match.group(1)
        # Handle range with any dash type and optional spaces: "1 - 19", "1–19", "1 – 19"
        dash_match = re.search(r'\s*[-\u2013\u2014]\s*', issue_str)
        if dash_match:
            parts = re.split(r'\s*[-\u2013\u2014]\s*', issue_str)
            result['issue_range'] = (int(parts[0]), int(parts[1]))
            result['issue'] = issue_str
        else:
            result['issue'] = issue_str
        matched_patterns.append((issue_match.start(), issue_match.end()))

    # Extract volume - this can appear anywhere
    volume_match = re.search(r'\b(?:vol\.?|v(?:ol(?:ume)?)?)\s*\.?\s*(\d+)', title, re.IGNORECASE)
    if volume_match:
        result['volume'] = int(volume_match.group(1))
        matched_patterns.append((volume_match.start(), volume_match.end()))

    # Check for format variants (TPB, omnibus, oneshot, etc.) using config settings
    # These can appear anywhere in the title, including after the issue number
    # e.g., "Batman #1-50 + TPBs" or "Batman Omnibus #1"
    # Store detected format variants as a list - dynamically set based on format_variants config
    format_variants_found = []
    format_variants = get_format_variants()
    for variant in format_variants:
        variant_escaped = re.escape(variant)
        # Pattern matches variant with optional 's' suffix, word boundary at end
        pattern = rf'\+?\s*{variant_escaped}(?:s)?\b'
        variant_match = re.search(pattern, title, re.IGNORECASE)
        if variant_match:
            format_variants_found.append(variant)
            matched_patterns.append((variant_match.start(), variant_match.end()))

    # Store detected format variants as a list in result
    # Uses format_variants config to know what to look for
    if format_variants_found:
        result['format_variants'] = format_variants_found

    # Check for arc notation (dash): "Batman - Court of Owls"
    # Arc can have format: "Series - Arc Name #issue" or "Series - Arc Name"
    # Arc name is captured up to the first # (issue) or end of string
    #
    # HOWEVER: "Batman Vol. 3 – Rebirth" should NOT be treated as arc
    # The dash here is a brand/imprint separator, not story arc notation.
    # Distinction: after series+volume, the dash precedes "Rebirth" (brand),
    # not after just "Batman" (series name).
    arc_match = re.search(r'[-–—]\s*(.+?)\s*(?:#|$)', title)
    if arc_match:
        potential_arc = arc_match.group(1).strip()
        arc_start = arc_match.start()
        # Check if what comes before the dash could be a series name
        # by checking if it ends with a publication type (annual, quarterly)
        pub_type_end_pattern = r'^(\d{4}\s+)?(' + '|'.join(get_publication_types()) + r')$'
        prefix_match = re.search(r'([^#\d]+)\s*[-–—]\s*.+$', title)
        if prefix_match:
            prefix = prefix_match.group(1).strip()
            # Check if prefix ends with volume notation - if so, this is likely a brand/imprint
            # not a story arc. e.g., "Batman Vol. 3 – Rebirth" has "Vol. 3" before the dash.
            has_volume_before_dash = re.search(r'\bvol\.?\s*\d+\s*$', prefix, re.IGNORECASE)
            if len(prefix) > 2 and not re.match(pub_type_end_pattern, prefix, re.IGNORECASE) and not has_volume_before_dash:
                result['is_arc'] = True
                result['arc_name'] = potential_arc
                matched_patterns.append((arc_start, len(title)))

    # Check for publication types (annual, quarterly)
    # Year can appear AFTER these keywords, e.g., "Flash Gordon Annual 2014"
    # But NOT if it's inside parentheses (those are "year" not "publication_year")
    pub_type_pattern = r'\b(' + '|'.join(get_publication_types()) + r')\b'
    pub_type_match = re.search(pub_type_pattern, title, re.IGNORECASE)
    if pub_type_match:
        keyword = pub_type_match.group(1).lower()
        result[f'is_{keyword}'] = True
        # Find position after the keyword and look for 4-digit year
        # Don't match if the year is inside parentheses
        after_keyword = title[pub_type_match.end():]
        year_after_match = re.search(r'\b(\d{4})\b(?!\s*\))', after_keyword)
        if year_after_match:
            result['publication_year'] = int(year_after_match.group(1))

    # Check if range pack includes format variants (TPBs, oneshots, omnibus, etc.)
    if result['is_range_pack']:
        format_pattern = r'\+\s*(' + '|'.join(re.escape(v) for v in format_variants) + r')s?\b'
        if re.search(format_pattern, title, re.IGNORECASE):
            result['has_tpb_in_pack'] = True

    # ── MULTI-SERIES / PACK DETECTION ───────────────────────────────────────
    # Check for multiple series in title (crossover separators but NOT inside parentheses)
    # Normalize: "&", "/", "+", " and " -> "+" for easier detection
    # But don't count if inside parentheses (those are years/descriptions)
    title_before_parens = original.split('(')[0]  # Only check outside parentheses
    title_for_analysis = title_before_parens.lower()

    # Normalize separators for detection
    normalized_for_series = title_for_analysis.replace('&', '+').replace('/', '+').replace(' + ', '+')
    # Count series separators: + indicates crossover/team-up
    series_separators = normalized_for_series.count('+')
    if series_separators >= 1:
        # Check if these are actual series joins vs. plus variants
        # "+" after "Issue" or "issues" is a range indicator, not a series separator
        # "+" followed by format variants (tpbs, omnibus, etc.) is a format indicator, not a series separator
        if not re.search(r'#\d+\s*\+\s*\d+', title_for_analysis):
            # Build regex pattern from format_variants to check if + is followed by format variant
            format_pattern = r'\+\s*(' + '|'.join(re.escape(v) for v in format_variants) + r')s?\b'
            if not re.search(format_pattern, title_for_analysis, re.IGNORECASE):
                result['is_multi_series'] = True

    # Check if this is a range pack (#1-50, #1 – 50, etc.)
    if re.search(r'#\d+\s*[-–—]\s*\d+', title, re.IGNORECASE):
        result['is_range_pack'] = True
        # Check if it's a bulk pack (large range like #1-50, #1-100)
        range_match = re.search(r'#(\d+)\s*[-–—]\s*(\d+)', title, re.IGNORECASE)
        if range_match:
            range_start = int(range_match.group(1))
            range_end = int(range_match.group(2))
            if range_end - range_start >= 10:  # 10+ issues = bulk pack
                result['is_bulk_pack'] = True

    # Construct the series name by removing all matched patterns
    # Sort matches by position in reverse order to remove from end to start
    if matched_patterns:
        matched_patterns.sort(key=lambda x: x[0], reverse=True)
        for start, end in matched_patterns:
            title = title[:start].strip() + ' ' + title[end:].strip()
        title = ' '.join(title.split())  # Normalize spaces

    # Clean up remaining title
    # Only replace dashes (which can be arc separators) and en/em dashes
    # DO NOT replace colons - they are part of series names (e.g., "Batman: Year One",
    # "Batman / Superman: World's Finest") and should be preserved
    title = re.sub(r'[-–—]', ' ', title)  # Replace dashes with spaces
    title = re.sub(r'\s+', ' ', title)  # Normalize spaces
    title = title.strip(' .,')

    result['name'] = title

    return result


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
        series_volume: Volume number of the series we're searching for (from series data).
                      Used to enforce exact volume matching — different volume = -40.
        volume_year: Year the volume started (from series data). Used for soft year matching —
                    if result year is within ±1 of volume_year, treat as match (+10/-10)
                    rather than exact year match (+20/-20). Comics often span multiple years.

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
        +20  Year match (softened to ±1 if volume_year provided)
        +10  Volume match (when both search and result have explicit volumes)

    Penalties:
        -10  Title tightness (1+ extra words)
        -30  Sub-series detected (dash after series name OR variant keyword)
        -30  Different series (remaining text indicates different series)
        -30  "The" prefix swap used but remaining doesn't match (e.g., "The Flash Gordon" vs "Flash Gordon")
        -20  Wrong year explicitly present in title (softened if volume_year provided)
        -30  Collected edition keyword (omnibus, TPB, hardcover, etc.)
        -40  Confirmed issue mismatch (#N present but points to wrong number)
        -40  Volume mismatch (both search and result have explicit volumes but they differ)
        -20  Format pack mismatch (searching for regular issue, result is TPB/omnibus/oneshot pack)
        -10  Format pack partial (searching for format, result pack contains format but isn't standalone)

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
    if accept_variants is None:
        accept_variants = []
    import re

    score = 0
    title_lower = result_title.lower()
    series_lower = series_name.lower()

    # Normalise issue number — strip leading zeros, preserve dot notation
    issue_str = str(issue_number)
    issue_num = issue_str.lstrip('0') or '0'
    is_dot_issue = '.' in issue_str

    # Parse the result title for structured data (format variants, volume, etc.)
    parsed_result = parse_result_title(result_title)
    result_volume = parsed_result.get('volume')
    result_format_variants = parsed_result.get('format_variants', [])
    result_has_format = len(result_format_variants) > 0

    # Detect if search is for a format variant (series name contains a format variant keyword)
    # Format variants = VARIANT_TYPES - PUBLICATION_TYPES
    format_variants = get_format_variants()
    pub_types = get_publication_types()
    searching_for_format = False
    if format_variants:
        series_lower_normalized = series_lower.replace('-', '').replace('\u2013', '').replace('\u2014', '').lower()
        for fv in format_variants:
            fv_normalized = fv.replace('-', '').replace('.', '').lower()
            # Check if format variant appears as a word boundary in series name
            if re.search(rf'\b{re.escape(fv)}\b', series_lower, re.IGNORECASE):
                searching_for_format = True
                break
            if re.search(rf'\b{re.escape(fv_normalized)}\b', series_lower_normalized, re.IGNORECASE):
                searching_for_format = True
                break

    # ── RANGE DETECTION ──────────────────────────────────────────────────────
    # Parse issue ranges from GetComics titles.
    # Format examples:
    #   "Batman #1-50"                    → range 1-50
    #   "Batman #1 – 50 + TPBs (2020)"    → range 1-50, format variant TPB
    #   "Batman Vol. 3 #1 + 1 – 126 + TPBs (2016-2022)" → range 1-126 (from "#1 + 1 – 126")
    #   "Batman Vol. 3 – Rebirth #1 + 1 – 126 + TPBs (2016-2022)" → same
    #
    # The pattern we need to detect is:
    #   #N + START – END + TPBs (YEAR-YEAR)
    #   or variations with #N removed (just "START – END + TPBs")
    #
    # Strategy: Work backwards from the end of the title, like file extension parsing.
    # 1. First find " + TPBs (YYYY-YYYY)" at the end
    # 2. Then find the dash-separated range "START – END" before it
    # 3. Then determine if there's a standalone "#N" before the range
    range_contains_target = False

    # Pattern for " + TPBs (year-year)" at the end of title
    tpbs_pattern = r'\s*\+\s*tpbs?\s*\(\d{4}[-\u2013\u2014]\d{4}\)\s*$'
    tpbs_match = re.search(tpbs_pattern, title_lower)
    if tpbs_match:
        # Found TPBs pattern at end. Now look for range before it.
        # Title before TPBs pattern: "... + 1 – 126" or "... + 126"
        before_tpbs = title_lower[:tpbs_match.start()]
        # Look for "number – number" pattern (en-dash/em-dash) near the end
        range_pattern = r'(\d+)\s*[-\u2013\u2014]\s*(\d+)(?:\s*\+\s*tpbs|$)'
        range_match = re.search(range_pattern, before_tpbs)
        if range_match:
            range_start = int(range_match.group(1))
            range_end = int(range_match.group(2))
            # Check if target issue is in range
            try:
                target_n = float(issue_num) if issue_num.replace('.', '', 1).isdigit() else -1
            except ValueError:
                target_n = -1
            if target_n != -1 and range_start <= target_n <= range_end:
                if range_end == target_n:
                    # Range ends on target - bulk pack ending on that number
                    return -100, None, None
                range_contains_target = True
        else:
            # No range found, but we have TPBs - look for standalone number before "+ TPBs"
            # e.g., " + 126 + TPBs" means issue 126 standalone with TPB variant
            standalone_pattern = r'\+\s*(\d+)(?:\s*\+\s*tpbs|$)'
            standalone_match = re.search(standalone_pattern, before_tpbs)
            if standalone_match:
                standalone_num = int(standalone_match.group(1))
                try:
                    target_n = float(issue_num) if issue_num.replace('.', '', 1).isdigit() else -1
                except ValueError:
                    target_n = -1
                if standalone_num == target_n:
                    # Issue 126 standalone with TPB variant - not a range
                    pass  # Not range_contains_target

    # Also check for simpler patterns: "#N-M", "#N – M", or "Issues N-M" without TPBs
    if not range_contains_target:
        simple_range_pattern = r'#(\d+)\s*[-\u2013\u2014]\s*(\d+)'
        simple_match = re.search(simple_range_pattern, title_lower)
        if not simple_match:
            # Try "Issues N-M" pattern (no "#" prefix)
            simple_match = re.search(r'\bissues?\s*(\d+)\s*[-\u2013\u2014]\s*(\d+)', title_lower, re.IGNORECASE)
        if simple_match:
            range_start = int(simple_match.group(1))
            range_end = int(simple_match.group(2))
            try:
                target_n = float(issue_num) if issue_num.replace('.', '', 1).isdigit() else -1
            except ValueError:
                target_n = -1
            if target_n != -1:
                if range_start <= target_n <= range_end:
                    if range_end == target_n:
                        return -100, None, None
                    range_contains_target = True

    # ── SERIES NAME MATCH (+30) ──────────────────────────────────────────────
    series_starts = [series_lower]
    if series_lower.startswith('the '):
        series_starts.append(series_lower[4:])
    else:
        series_starts.append('the ' + series_lower)

    # Normalize crossover separators in series name for matching
    # "Batman & Robin", "Batman / Superman", and "Batman and Robin" should all match
    # Normalize: "&", "/" → "+" (common crossover separator)
    series_normalized = series_lower.replace('&', '+').replace('/', '+').replace(' and ', ' + ')
    if series_normalized != series_lower and series_normalized not in series_starts:
        series_starts.append(series_normalized)

    # Also normalize title for matching - GetComics may use "&", "/" or "and"
    title_normalized = title_lower.replace('&', '+').replace('/', '+').replace(' and ', ' + ')

    # Get variant keywords from config — covers both publication types and format variants
    VARIANT_KEYWORDS = get_variant_types()

    series_match = False
    sub_series_type = None  # 'variant' (annual, tpB, etc.), 'arc' (story arc), or None
    remaining = ""  # Initialize for scope
    detected_variant = None  # Store which specific variant was detected
    used_the_swap = False  # Track if we matched using "The " prefix swap
    for start in series_starts:
        # Check both original title and normalized title for crossover separator matching
        for check_title in (title_lower, title_normalized):
            if check_title.startswith(start):
                remaining = check_title[len(start):].strip()
                # Track if we matched using the swapped "the " version
                # This helps detect different series like "The Flash Gordon" vs "Flash Gordon"
                # If search is "The Flash Gordon" but result matches "Flash Gordon" (without "The"),
                # that's a different series, not the same series with swapped prefix
                if series_lower.startswith('the ') and start == series_lower[4:]:
                    used_the_swap = True
                # Sub-series with dash: "Series - Quarterly", "Series – Arc Name"
                if remaining.startswith(('-', '\u2013', '\u2014')):
                    if re.match(r'[-\u2013\u2014]\s*\w+', remaining):
                        # Check if dash sub-series matches a known variant keyword
                        dash_part = remaining.lstrip('-\u2013\u2014').strip().lower()
                        variant_found = False
                        for keyword in VARIANT_KEYWORDS:
                            # Match whole word anywhere in dash_part to catch variants like "one-shot"
                            # Use negative lookbehind/lookahead to ensure keyword is NOT part of another word
                            # e.g., "os" should NOT match inside "one", but SHOULD match in "Year One OS"
                            # The keyword must be preceded/followed by non-letter characters (space, dash, etc.)
                            pattern = rf'(?<![a-zA-Z]){re.escape(keyword)}(?![a-zA-Z])'
                            if re.search(pattern, dash_part, re.IGNORECASE):
                                sub_series_type = 'variant'
                                detected_variant = keyword
                                variant_found = True
                                break
                        # If no variant keyword matched, check if this is a brand/imprint dash
                        # (e.g., "Batman Vol. 3 – Rebirth") not a story arc (e.g., "Batman - Court of Owls")
                        # Brand/imprint dashes have volume notation BEFORE the dash
                        if not variant_found:
                            has_volume_before_dash = re.search(r'\bvol\.?\s*\d+\s*$', series_lower, re.IGNORECASE)
                            if has_volume_before_dash:
                                # This is a brand/imprint separator, not a story arc - don't penalize
                                pass
                            else:
                                sub_series_type = 'arc'
                # Sub-series with variant keyword (even without dash):
                # "Batman Annual #1" - "Annual" is part of series name (different series)
                else:
                    for keyword in VARIANT_KEYWORDS:
                        # Use negative lookbehind/lookahead to ensure keyword is NOT part of another word
                        pattern = rf'(?<![a-zA-Z]){re.escape(keyword)}(?![a-zA-Z])'
                        if re.search(pattern, remaining, re.IGNORECASE):
                            sub_series_type = 'variant'
                            detected_variant = keyword
                            break
                series_match = True
                break
        if series_match:
            break
        else:
            # No direct series match - check if this is a brand era match
            # e.g., "Batman Rebirth" (CLU) vs "Batman Vol. 3 - Rebirth" (GetComics)
            # Both contain "Rebirth" so they're the same series era
            if series_has_same_brand(series_name, result_title):
                # Try to find series base name (before brand keyword) in title
                brands = get_brand_keywords()
                for brand in brands:
                    if brand in series_lower and brand in title_lower:
                        # Remove brand from series name to get base
                        series_base = series_lower.replace(brand, '').strip()
                        if series_base and title_lower.startswith(series_base):
                            # Match! Series base matches and both have same brand
                            series_match = True
                            remaining = title_lower[len(series_base):].strip()
                            logger.debug(f"Brand match: series='{series_name}', title starts with base '{series_base}', both have '{brand}'")
                            break
                if series_match:
                    break

    if series_match:
        score += 30
        logger.debug(f"Series name match: +30")

    # ── VOLUME MATCHING (+10 / -40) ─────────────────────────────────────────
    # If both search and result have explicit volumes, they must match exactly.
    # Different volumes of the same series are different numbering sequences.
    # e.g., "Batman Vol. 3" ≠ "Batman Vol. 6" — different volumes, different issues.
    #
    # HOWEVER: If both search series and result title share the same brand keyword
    # (e.g., "Batman Rebirth" and "Batman Vol. 3 - Rebirth" both have "Rebirth"),
    # then volume mismatch should be excused - they're the same era/line.
    if series_volume is not None and result_volume is not None:
        if series_volume == result_volume:
            score += 10
            logger.debug(f"Volume match (Vol. {result_volume}): +10")
        else:
            # Check if same brand era - if so, skip volume mismatch penalty
            same_brand = series_has_same_brand(series_name, result_title)
            if same_brand:
                logger.debug(f"Volume mismatch ignored (same brand era): search Vol. {series_volume}, result Vol. {result_volume}")
            else:
                score -= 40
                logger.debug(f"Volume mismatch (search Vol. {series_volume}, result Vol. {result_volume}): -40")

    # Sub-series penalty — skip when range already flagged so arc packs
    # (e.g. "Batman – Court of Owls #1-11") can still surface as FALLBACK
    # if series_match happens to be True.
    # Variant sub-series (Annual, TPB, Quarterly, etc.) are publication variants,
    # not story arcs. They are penalized unless explicitly accepted via VARIANT_TYPES.
    # Arc sub-series (story arcs with dash) are also penalized but for different reasons.

    # Check if any accept_variants keyword matches the detected variant
    # Accept if:
    #   1. the search series_name itself contains the variant keyword (e.g., searching for
    #      "Flash Gordon - Quarterly" should not penalize "Flash Gordon - Quarterly #5"), OR
    #   2. detected_variant is in accept_variants and is a PUBLICATION FORMAT variant (tpB, omnibus, etc.)
    #      NOT a series modifier like "Annual" which creates a different series
    #
    # IMPORTANT: "Annual" is NOT a publication format - "Batman Annual" is a DIFFERENT series
    # from "Batman". When searching for "Batman #1", we should NOT accept "Batman Annual #1"
    # as a direct match. The variant must be in the SEARCH series name to be accepted.
    variant_accepted = False
    if sub_series_type in ('variant', 'arc'):
        # Normalize series_name for checking if variant is in the search series name
        series_name_normalized = series_lower.replace('-', '').replace('\u2013', '').replace('\u2014', '').lower()
        detected_normalized = detected_variant.replace('-', '').lower() if detected_variant else None

        # First check: is the variant keyword in the SEARCH series name?
        # This handles cases like "Flash Gordon - Quarterly" matching "Flash Gordon - Quarterly #5"
        if detected_normalized and detected_normalized in series_name_normalized:
            variant_accepted = True

        # Second check: for FORMAT variants only (tpB, omnibus, oneshot, hardcover, etc.)
        # Publication types (Annual, Quarterly) create DIFFERENT series and are NOT
        # accepted via accept_variants alone - they must be in the search series name
        if not variant_accepted and detected_variant:
            # Only accept if it's a FORMAT variant (not a publication type)
            pub_types = set(get_publication_types())
            if detected_normalized not in pub_types:
                # It's a format variant, check if accepted
                for keyword in accept_variants:
                    keyword_normalized = keyword.replace('-', '').lower()
                    # Check exact match or prefix match (omni matches omnibus, tpB matches tpb, etc.)
                    if (keyword_normalized == detected_variant or
                        keyword_normalized == detected_normalized or
                        detected_normalized.startswith(keyword_normalized) or
                        keyword_normalized in detected_normalized):
                        variant_accepted = True
                        break

    # For variants, we don't penalize if variant_accepted is True (user explicitly searched for variants)
    # But for ARCS, we ALWAYS penalize because arc issue numbering is different from main series numbering
    should_penalize_subseries = (
        sub_series_type is not None and
        not variant_accepted and
        not range_contains_target
    )
    # Arcs are ALWAYS penalized because "Batman - Court of Owls #1" is NOT "Batman #1"
    # Even if user accepts the arc keyword, the arc issue numbering is different
    if sub_series_type == 'arc':
        should_penalize_subseries = True

    if should_penalize_subseries:
        score -= 30
        penalty_type = detected_variant if detected_variant else sub_series_type
        logger.debug(f"Sub-series penalty ({penalty_type}): -30")

    # DEBUG: trace variant acceptance and issue matching
    logger.debug(f"[SCORING] variant_accepted={variant_accepted}, sub_series_type={sub_series_type}, remaining='{remaining[:50]}...'")

    # ── FORMAT PACK MISMATCH (-20 / -10) ────────────────────────────────────
    # Detect when result has format variants (TPB, omnibus, oneshot) but search is for regular issues
    # OR when search is for a format but result pack doesn't have it
    if result_has_format and not searching_for_format and sub_series_type is None:
        # Searching for regular issue but result is a format pack (TPB/omnibus/oneshot)
        # This is NOT a sub-series penalty — it's a format mismatch
        if parsed_result.get('issue_range'):
            # Range pack with format — downgraded but still usable as fallback
            score -= 10
            logger.debug(f"Format pack mismatch (searching regular, result is format pack with range): -10")
        else:
            # Standalone format edition when searching for regular — reject
            score -= 20
            logger.debug(f"Format pack mismatch (searching regular, result is standalone format edition): -20")
    elif searching_for_format and not result_has_format and sub_series_type is None:
        # Searching for format but result doesn't have format variants
        score -= 10
        logger.debug(f"Format mismatch (searching for format, result has no format variants): -10")

    # ── TITLE TIGHTNESS (+15 / -10) ──────────────────────────────────────────
    noise_words = {
        'the', 'a', 'an', 'of', 'and', 'in', 'by', 'for',
        'to', 'from', 'with', 'on', 'at', 'or', 'is',
    }
    expected_words = set(re.findall(r'[a-z0-9]+', series_lower))
    expected_words.add(issue_num)
    if is_dot_issue:
        expected_words.add(issue_num.split('.')[0])
    if year:
        expected_words.add(str(year))
    expected_words.update(['vol', 'volume', 'issue', 'comic', 'comics'])

    title_word_list = re.findall(r'[a-z0-9]+', title_lower)
    title_word_list = [w for w in title_word_list if w not in noise_words and len(w) > 1]
    expected_count = sum(
        1 for w in title_word_list
        if w in expected_words or (w.isdigit() and (w.lstrip('0') or '0') == issue_num)
    )
    extra_count = len(title_word_list) - expected_count

    if extra_count == 0:
        score += 15
        logger.debug(f"Title tightness bonus: +15")
    else:
        score -= 10
        logger.debug(f"Title tightness penalty ({extra_count} extra words): -10")

    # ── ISSUE NUMBER MATCH (+30 / +20) ───────────────────────────────────────
    # Cross-series fix: issue matching only counts when series_match is True.
    # If series doesn't match, finding #N in a different series is meaningless.
    # Variant sub-series fix: when a variant (Annual, TPB, Quarterly, etc.) is detected,
    # the issue number is for that variant, not the main series, so don't count unless variant_accepted.
    # Different series fix: when remaining text exists but wasn't classified as variant or arc,
    # it's a DIFFERENT series (e.g., "Batman Inc #1" is not "Batman #1"), so don't count issue.
    issue_matched = False

    # Check if remaining text indicates a different series (not variant, not arc)
    remaining_is_different_series = False
    if remaining and sub_series_type is None:
        # Check if remaining is primarily a range pattern (digits, dashes, spaces, parens)
        # These are NOT different series - they're issue ranges for the same series
        remaining_cleaned = remaining.strip().replace('-', '').replace('\u2013', '').replace('\u2014', '').replace(' ', '').replace('#', '').replace('(', '').replace(')', '')
        is_purely_range = bool(remaining_cleaned) and all(c.isdigit() or c == '.' for c in remaining_cleaned)

        # First check: does remaining START with an issue number? If so, NOT different series
        # (remaining would be "#1 2025" or "1 2025" which is just the issue number)
        starts_with_issue = re.match(r'^#?\d', remaining.strip())

        # Also check if remaining starts with "Issue" (issue as a word) - e.g., "Batman Issue 7"
        # This is NOT a different series, just the issue number written as a word
        starts_with_issue_word = re.match(r'^issue\s*\d', remaining.strip(), re.IGNORECASE)

        # Check if remaining starts with volume notation like "Vol 5" or "Volume 5"
        # This is NOT a different series - it's just describing which volume of the series
        starts_with_volume = re.match(r'^vol(ume)?\.?\s*\d', remaining.strip(), re.IGNORECASE)

        # If we matched using the "The " swap but result doesn't have "The ", treat as different series
        # e.g., searching "The Flash Gordon" should NOT match "Flash Gordon"
        # This must be checked BEFORE is_purely_range because "#1" would be range but should still
        # be treated as different series when swap was used
        if used_the_swap:
            remaining_is_different_series = True
        # Ranges like "#1-5" that don't use swap are NOT different series
        elif is_purely_range:
            remaining_is_different_series = False
        # Volume notation like "Vol 5" is NOT a different series - it's the same series
        elif starts_with_volume:
            remaining_is_different_series = False
        elif not starts_with_issue and not starts_with_issue_word:
            # Remaining doesn't start with issue number or "issue" word - might be different series
            # Check if remaining starts with a dash (would be arc - handled above)
            if not remaining.startswith(('-', '\u2013', '\u2014')):
                # Doesn't start with dash either - check for variant keywords
                has_variant_keyword = False
                remaining_check = remaining.replace('-', '').replace('\u2013', '').replace('\u2014', '').lower()
                for kw in VARIANT_KEYWORDS:
                    if re.search(rf'\b{re.escape(kw)}\b', remaining_check, re.IGNORECASE):
                        has_variant_keyword = True
                        break
                if not has_variant_keyword:
                    remaining_is_different_series = True

    # Apply different-series penalty: when remaining text indicates a different series
    # (e.g., "Batman Inc #1" is not "Batman #1", "Batman Adventures #1" is not "Batman #1")
    if remaining_is_different_series:
        score -= 30
        logger.debug(f"Different series penalty: -30 (remaining: '{remaining[:30]}...')")

    # Determine if we should allow issue matching based on variant_accepted
    # Allow issue matching if:
    #   1. no sub-series AND remaining text is empty (clean match), OR
    #   2. variant was accepted (but NOT for arcs - arc issue numbers are arc-internal)
    # DON'T allow issue matching for arcs - "Batman - Court of Owls #1" is NOT the same as "Batman #1"
    # Arcs have their own issue numbering within the arc, separate from the main series
    # DON'T allow if remaining text indicates a different series
    allow_issue_match = series_match and (
        (sub_series_type is None and not remaining_is_different_series) or
        (variant_accepted and sub_series_type != 'arc')
    )

    if is_dot_issue:
        if allow_issue_match:
            dot_patterns = [
                rf'#0*{re.escape(issue_num)}\b',
                rf'issue\s*0*{re.escape(issue_num)}\b',
                rf'\b0*{re.escape(issue_num)}\b',
            ]
            for pattern in dot_patterns:
                if re.search(pattern, title_lower, re.IGNORECASE):
                    score += 30
                    issue_matched = True
                    variant_note = f", accepted variant ({detected_variant})" if variant_accepted else ", not sub-series"
                    logger.debug(f"Dot-issue match (series confirmed{variant_note}): +30")
                    break
    else:
        if allow_issue_match:
            explicit_patterns = [
                rf'#0*{re.escape(issue_num)}\b',
                rf'issue\s*0*{re.escape(issue_num)}\b',
            ]
            for pattern in explicit_patterns:
                if re.search(pattern, title_lower, re.IGNORECASE):
                    score += 30
                    issue_matched = True
                    variant_note = f", accepted variant ({detected_variant})" if variant_accepted else ""
                    logger.debug(f"Issue match ({pattern}, series confirmed{variant_note}): +30")
                    break

            if not issue_matched:
                standalone = re.search(rf'\b0*{re.escape(issue_num)}\b', title_lower)
                if standalone:
                    match_start = standalone.start()
                    prefix = result_title[max(0, match_start - 10):match_start].lower()
                    if (not re.search(r'[-\u2013\u2014]\s*$', prefix) and
                            not re.search(r'\bvol(?:ume)?\.?\s*$', prefix)):
                        score += 20
                        issue_matched = True
                        variant_note = f", accepted variant ({detected_variant})" if variant_accepted else ""
                        logger.debug(f"Issue match (standalone, series confirmed{variant_note}): +20")
        elif series_match and sub_series_type is not None and not variant_accepted:
            logger.debug(f"Skipping issue match - sub-series detected ({detected_variant or sub_series_type}), not in accept_variants")
        elif not series_match:
            logger.debug(f"Skipping issue match - series does not match")

    # Confirmed mismatch — explicit #N found but it's the wrong number
    # Only penalize when series matches but issue number is different
    # NOTE: In range pack context (e.g., "#1 + 1 - 126 + TPBs"), the #N is the START of the range,
    # not a standalone issue. Skip mismatch penalty when range_contains_target=True.
    if not issue_matched and series_match and not range_contains_target:
        explicit = re.search(
            rf'(?:#|issue\s)0*(\d+(?:\.\d+)?)\b', title_lower, re.IGNORECASE
        )
        if explicit:
            found_num = explicit.group(1).lstrip('0') or '0'
            if found_num != issue_num:
                score -= 40
                logger.debug(f"Confirmed issue mismatch (found #{found_num}): -40")

    # ── YEAR MATCH (+20 / -10 / -10) ────────────────────────────────────────
    # When volume_year is provided (from series data), use soft matching (±1 tolerance).
    # This is because comic volumes often span multiple years — a volume starting
    # in 2025 can have issues released through 2026 or later.
    # Without volume_year, use hard matching (exact year or wrong year).
    result_years = re.findall(r'\b(\d{4})\b', result_title)
    if volume_year is not None:
        # Soft year matching using volume_year ± 1
        if result_years:
            result_year = int(result_years[0])
            if result_year == volume_year or abs(result_year - volume_year) == 1:
                score += 10
                logger.debug(f"Year soft match (result={result_year}, volume_year={volume_year}): +10")
            else:
                score -= 10
                logger.debug(f"Year soft mismatch (result={result_year}, volume_year={volume_year}): -10")
    else:
        # Hard year matching (original behavior)
        if year and str(year) in result_title:
            score += 20
            logger.debug(f"Year match ({year}): +20")
        elif year:
            other_years = re.findall(r'\b(\d{4})\b', result_title)
            if any(int(y) != year for y in other_years):
                score -= 20
                logger.debug(f"Wrong year in title: -20")

    # ── COLLECTED EDITION PENALTY (-30) ──────────────────────────────────────
    title_remainder = title_lower.replace(series_lower, '', 1)
    # Build collected keywords from format variants in config
    # Format variants are the same as what we use for format detection
    format_variant_patterns = []
    for fv in get_format_variants():
        # Escape special regex chars but allow word boundaries
        fv_escaped = re.escape(fv)
        # Build pattern with optional 's' suffix
        format_variant_patterns.append(rf'\b{fv_escaped}s?\b')
    # Add other collected edition terms not in format variants
    collected_keywords = format_variant_patterns + [
        r'\bcompendium\b',
        r'\bcomplete\s+collection\b',
        r'\blibrary\s+edition\b',
        r'\bbook\s+\d+\b',
    ]
    # Skip "annual"/"quarterly" penalty if already detected as variant sub-series (Issue #193)
    # Annual and Quarterly are publication frequencies, not collected editions
    # So we only penalize them once via sub-series penalty
    # But TPB, Hardcover, Omnibus etc. can be both variants AND collected editions,
    # so they get double-penalized (which is correct - TPB with issue # is weird)
    # HOWEVER: if variant_accepted is True, the user explicitly wants this variant,
    # so don't penalize it as a collected edition
    if sub_series_type is None:
        pub_types_pattern = r'\b(' + '|'.join(re.escape(p) for p in get_publication_types()) + r')s?\b'
        collected_keywords.append(pub_types_pattern)

    if not variant_accepted:
        for kw in collected_keywords:
            if re.search(kw, title_remainder):
                score -= 30
                logger.debug(f"Collected edition penalty ({kw}): -30")
                break

    # Range fallbacks must never reach ACCEPT on their own.
    # Use accept_result() to explicitly opt in to the FALLBACK tier.
    # Cap at ACCEPT_THRESHOLD - 1 (39), but only if score would otherwise be positive.
    # Don't let negative scores become "hidden" fallbacks - they should still be rejected.
    if range_contains_target and score >= FALLBACK_MIN:
        score = min(score, ACCEPT_THRESHOLD - 1)

    logger.debug(
        f"Score for '{result_title}' vs '{series_name} #{issue_number} ({year})': "
        f"{score} (range={range_contains_target}, series={series_match})"
    )
    return score, range_contains_target, series_match


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
