"""
GetComics.org search and download functionality.
Uses cloudscraper to bypass Cloudflare protection.
"""
import cloudscraper
from bs4 import BeautifulSoup
import logging

logger = logging.getLogger(__name__)

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


def score_getcomics_result(
    result_title: str,
    series_name: str,
    issue_number: str,
    year: int,
    accept_variants: list = None,
) -> tuple:
    """
    Score a GetComics search result against a wanted issue.

    Args:
        result_title: Title from GetComics search result
        series_name: Series name to match
        issue_number: Issue number to match
        year: Year to match
        accept_variants: Optional list of variant types to accept without penalty.
                        E.g., ['annual'] - if Annual is detected but user searched for it,
                        don't penalize as sub-series. Maps to global SEARCH_VARIANTS config.

    Returns:
        (score, range_contains_target, series_match)
        - score:                 Integer score; higher = better match
        - range_contains_target: True if title is a range pack containing the issue
        - series_match:          True if series name matched the title

    Scoring (max 95):
        +30  Series name match (starts-with, handles "The" prefix swaps)
        +15  Title tightness (zero extra words beyond series/issue/year)
        +30  Issue number match via #N or "Issue N" pattern
        +20  Issue number match via standalone bare number (lower confidence)
        +20  Year match

    Penalties:
        -10  Title tightness (1+ extra words)
        -30  Sub-series detected (dash after series name OR variant keyword)
        -30  Different series (remaining text indicates different series)
        -30  "The" prefix swap used but remaining doesn't match (e.g., "The Flash Gordon" vs "Flash Gordon")
        -20  Wrong year explicitly present in title
        -30  Collected edition keyword (omnibus, TPB, hardcover, etc.)
        -40  Confirmed issue mismatch (#N present but points to wrong number)

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

    # ── RANGE DETECTION ──────────────────────────────────────────────────────
    # If the range contains our target, flag as fallback candidate.
    # Ranges that end on our issue are disqualified (-100) because the user
    # wants a single issue, not a bulk pack ending on that number.
    issue_range_patterns = [
        rf'#\d+\s*[-\u2013\u2014]\s*#?\d+',
        rf'issues?\s*\d+\s*[-\u2013\u2014]\s*\d+',
        rf'\(\d{{4}}\s*[-\u2013\u2014]\s*\d{{4}}\)',
    ]
    range_contains_target = False
    for range_pattern in issue_range_patterns:
        range_match = re.search(range_pattern, title_lower, re.IGNORECASE)
        if range_match:
            # Range ends on our issue number — disqualify
            end_pattern = rf'[-\u2013\u2014]\s*#?0*{re.escape(issue_num)}\b'
            if re.search(end_pattern, result_title, re.IGNORECASE):
                return -100, None, None
            # Range spans across our issue number
            numbers = re.findall(r'\d+', range_match.group())
            if len(numbers) == 2:
                start_n, end_n = int(numbers[0]), int(numbers[1])
                try:
                    target_n = float(issue_num) if issue_num.replace('.', '', 1).isdigit() else -1
                except ValueError:
                    target_n = -1
                if target_n != -1 and start_n <= target_n <= end_n:
                    range_contains_target = True
                    break

    # ── SERIES NAME MATCH (+30) ──────────────────────────────────────────────
    series_starts = [series_lower]
    if series_lower.startswith('the '):
        series_starts.append(series_lower[4:])
    else:
        series_starts.append('the ' + series_lower)

    # Known variant type keywords - these are publication variants, not arc/story sub-series
    # These can be accepted via SEARCH_VARIANTS config
    VARIANT_KEYWORDS = [
        'annual',
        'tpb', 'tpb',  # Trade Paperback
        'trade paperback', 'trade-paperback',
        'oneshot', 'one-shot',  # One-shot
        'o.s.', 'os',  # Original Series (same as oneshot)
        'quarterly',
        'omni', 'omnibus', 'omb',  # Omnibus
        'hardcover',  # Hardcover edition
        'deluxe', 'deluxe edition',
        'absolute',
        'prestige',
        'gallery',
    ]

    series_match = False
    sub_series_type = None  # 'variant' (annual, tpB, etc.), 'arc' (story arc), or None
    remaining = ""  # Initialize for scope
    detected_variant = None  # Store which specific variant was detected
    used_the_swap = False  # Track if we matched using "The " prefix swap
    for start in series_starts:
        if title_lower.startswith(start):
            remaining = title_lower[len(start):].strip()
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
                        # that don't appear at the start. \b ensures we match whole words only.
                        if re.search(rf'\b{re.escape(keyword)}\b', dash_part, re.IGNORECASE):
                            sub_series_type = 'variant'
                            detected_variant = keyword
                            variant_found = True
                            break
                    # If no variant keyword matched, treat as story arc (not a publication variant)
                    if not variant_found:
                        sub_series_type = 'arc'
            # Sub-series with variant keyword (even without dash):
            # "Absolute Batman 2025 Annual #1" or "Batman Annual #1"
            # "Annual" is a publication variant, not the main series
            else:
                for keyword in VARIANT_KEYWORDS:
                    if re.search(rf'\b{re.escape(keyword)}\b', remaining, re.IGNORECASE):
                        sub_series_type = 'variant'
                        detected_variant = keyword
                        break
            series_match = True
            break

    if series_match:
        score += 30
        logger.debug(f"Series name match: +30")

    # Sub-series penalty — skip when range already flagged so arc packs
    # (e.g. "Batman – Court of Owls #1-11") can still surface as FALLBACK
    # if series_match happens to be True.
    # Variant sub-series (Annual, TPB, Quarterly, etc.) are publication variants,
    # not story arcs. They are penalized unless explicitly accepted via SEARCH_VARIANTS.
    # Arc sub-series (story arcs with dash) are also penalized but for different reasons.

    # Check if any accept_variants keyword matches the detected variant
    # Accept if:
    #   1. detected_variant is in accept_variants, OR
    #   2. any accept_variants keyword matches the remaining, OR
    #   3. the search series_name itself contains the variant keyword (e.g., searching for
    #      "Flash Gordon - Quarterly" should not penalize "Flash Gordon - Quarterly #5")
    variant_accepted = False
    if sub_series_type in ('variant', 'arc'):
        # Normalize remaining text for matching (remove hyphens to handle "one-shot" = "oneshot")
        remaining_normalized = remaining.replace('-', '').replace('\u2013', '').replace('\u2014', '').lower()
        # Normalize series_name for checking if variant is in the search series name
        series_name_normalized = series_lower.replace('-', '').replace('\u2013', '').replace('\u2014', '').lower()

        for keyword in accept_variants:
            keyword_normalized = keyword.replace('-', '').lower()
            # Check exact match or normalized match (remove hyphens for comparison)
            # e.g., 'one-shot' normalized = 'oneshot' matches 'oneshot' normalized = 'oneshot'
            if (detected_variant and keyword == detected_variant) or \
               (detected_variant and keyword_normalized == detected_variant.replace('-', '').lower()) or \
               keyword_normalized in remaining_normalized or \
               (detected_variant and detected_variant in series_name_normalized):
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

        # If we matched using the "The " swap but result doesn't have "The ", treat as different series
        # e.g., searching "The Flash Gordon" should NOT match "Flash Gordon"
        # This must be checked BEFORE is_purely_range because "#1" would be range but should still
        # be treated as different series when swap was used
        if used_the_swap:
            remaining_is_different_series = True
        # Ranges like "#1-5" that don't use swap are NOT different series
        elif is_purely_range:
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
    if not issue_matched and series_match:
        explicit = re.search(
            rf'(?:#|issue\s)0*(\d+(?:\.\d+)?)\b', title_lower, re.IGNORECASE
        )
        if explicit:
            found_num = explicit.group(1).lstrip('0') or '0'
            if found_num != issue_num:
                score -= 40
                logger.debug(f"Confirmed issue mismatch (found #{found_num}): -40")

    # ── YEAR MATCH (+20 / -20) ───────────────────────────────────────────────
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
    collected_keywords = [
        r'\bomnibus\b',
        r'\btpb\b',
        r'\bhardcover\b',
        r'\bdeluxe\s+edition\b',
        r'\bcompendium\b',
        r'\bcomplete\s+collection\b',
        r'\blibrary\s+edition\b',
        r'\bbook\s+\d+\b',
    ]
    # Skip "annual" penalty if already detected as variant sub-series (Issue #193)
    # Annual and Quarterly are publication frequencies, not collected editions
    # So we only penalize them once via sub-series penalty
    # But TPB, Hardcover, Omnibus etc. can be both variants AND collected editions,
    # so they get double-penalized (which is correct - TPB with issue # is weird)
    if sub_series_type is None:
        collected_keywords.extend([r'\bannual\b', r'\bquarterly\b'])
    for kw in collected_keywords:
        if re.search(kw, title_remainder):
            score -= 30
            logger.debug(f"Collected edition penalty ({kw}): -30")
            break

    # Range fallbacks must never reach ACCEPT on their own.
    # Use accept_result() to explicitly opt in to the FALLBACK tier.
    if range_contains_target:
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
