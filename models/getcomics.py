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
) -> tuple:
    """
    Score a GetComics search result against a wanted issue.

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
        -20  Sub-series detected (dash after series name)
        -20  Wrong year explicitly present in title
        -30  Collected edition keyword (omnibus, TPB, hardcover, etc.)
        -40  Confirmed issue mismatch (#N present but points to wrong number)

    Range fallback logic:
        When a range like "#1-12" contains the target issue,
        range_contains_target=True is returned and the score is capped below
        ACCEPT_THRESHOLD. Use accept_result() to decide whether to use it.
        FALLBACK requires series_match=True — sub-series arc packs are rejected.
    """
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

    series_match = False
    sub_series_detected = False
    for start in series_starts:
        if title_lower.startswith(start):
            remaining = title_lower[len(start):].strip()
            if remaining.startswith(('-', '\u2013', '\u2014')):
                if re.match(r'[-\u2013\u2014]\s*\w+', remaining):
                    sub_series_detected = True
                    continue
            series_match = True
            break

    if series_match:
        score += 30
        logger.debug(f"Series name match: +30")

    # Sub-series penalty — skip when range already flagged so arc packs
    # (e.g. "Batman – Court of Owls #1-11") can still surface as FALLBACK
    # if series_match happens to be True.
    if sub_series_detected and not range_contains_target:
        score -= 20
        logger.debug(f"Sub-series penalty: -20")

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
    issue_matched = False

    if is_dot_issue:
        dot_patterns = [
            rf'#0*{re.escape(issue_num)}\b',
            rf'issue\s*0*{re.escape(issue_num)}\b',
            rf'\b0*{re.escape(issue_num)}\b',
        ]
        for pattern in dot_patterns:
            if re.search(pattern, title_lower, re.IGNORECASE):
                score += 30
                issue_matched = True
                logger.debug(f"Dot-issue match: +30")
                break
    else:
        explicit_patterns = [
            rf'#0*{re.escape(issue_num)}\b',
            rf'issue\s*0*{re.escape(issue_num)}\b',
        ]
        for pattern in explicit_patterns:
            if re.search(pattern, title_lower, re.IGNORECASE):
                score += 30
                issue_matched = True
                logger.debug(f"Issue match ({pattern}): +30")
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
                    logger.debug(f"Issue match (standalone): +20")

    # Confirmed mismatch — explicit #N found but it's the wrong number
    if not issue_matched:
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
        r'\bannual\b',
    ]
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
