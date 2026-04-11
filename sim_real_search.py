"""
Real GetComics search using the sitemap index.
1. Look up indexed URLs for a series using lookup_series_urls()
2. Scrape each URL and extract the comic title
3. Score every result against the search criteria
"""
import os
import sys
import logging

# Must disable logging BEFORE importing app modules that trigger emoji log messages
logging.disable(logging.CRITICAL)

sys.path.insert(0, 'C:/Users/trumb/clu-comics')

import cloudscraper
from bs4 import BeautifulSoup
from models.getcomics import (
    score_getcomics_result, accept_result, search_criteria,
    score_comic, ACCEPT_THRESHOLD, normalize_series_name,
    lookup_series_urls,
)

# ── CONFIGURATION ────────────────────────────────────────────────────────────
SERIES = "Batman"
ISSUE = "1"
YEAR = 2016
# ─────────────────────────────────────────────────────────────────────────────

SEARCH_TERM = f"{SERIES} {ISSUE} {YEAR}"


def scrape_page(page_url: str) -> list[str]:
    """Scrape a GetComics page and return comic titles found."""
    scraper = cloudscraper.create_scraper()
    try:
        resp = scraper.get(page_url, timeout=15)
        if resp.status_code != 200:
            return []
        soup = BeautifulSoup(resp.text, 'html.parser')

        # Individual comic page: extract from <title> tag
        title_tag = soup.find("title")
        if title_tag:
            title_text = title_tag.get_text(strip=True)
            # Strip " - GetComics" suffix (various dash types, including garbled)
            for sep in [" - ", " \u2013 ", " \u2014 ", " \x97 "]:
                if sep in title_text:
                    title_text = title_text.split(sep)[0].strip()
                    break
            else:
                if "GetComics" in title_text:
                    title_text = title_text.split("GetComics")[0].strip().rstrip("-").rstrip()
            title_text = title_text.replace('\u2013', '-').replace('\u2014', '-').replace('\x97', '-')
            if title_text and len(title_text) > 3:
                return [title_text]

        # Search/listing page: extract from post-content divs
        titles = []
        for el in soup.select("div.post-content"):
            h5 = el.select_one("h5 a") or el.select_one("h4 a") or el.select_one("h3 a")
            if h5:
                titles.append(h5.get_text(strip=True))

        return titles
    except Exception as e:
        print(f"    Error scraping {page_url}: {e}")
        return []


print(f"\n{'='*70}")
print(f"SITEMAP-BASED GETCOMICS SEARCH: '{SEARCH_TERM}'")
print(f"{'='*70}")

sitemap_urls = lookup_series_urls(SERIES)
print(f"\nSitemap index has {len(sitemap_urls)} URLs for '{SERIES}'")

if not sitemap_urls:
    print(f"\nNo sitemap entries found for '{SERIES}'.")
    print("Run build_sitemap_index() to populate the index.")
else:
    print(f"\nScraping {len(sitemap_urls)} indexed URLs...")
    all_titles = []
    for entry in sitemap_urls:
        titles = scrape_page(entry['full_url'])
        all_titles.extend(titles)
        status = f": {len(titles)} result(s)" if titles else ""
        print(f"  {entry['series_norm']}: {entry['url_slug']}{status}")

    unique_titles = list(dict.fromkeys(all_titles))
    print(f"\nTotal unique titles: {len(unique_titles)}")

    if unique_titles:
        scored = []
        for title in unique_titles:
            score, range_contains, series_match = score_getcomics_result(
                title, SERIES, ISSUE, YEAR
            )
            decision = accept_result(score, range_contains, series_match)
            scored.append((score, decision, series_match, range_contains, title))

        scored.sort(key=lambda x: x[0], reverse=True)

        print(f"\n{'='*70}")
        print(f"SCORING: {SERIES} #{ISSUE} ({YEAR})")
        print(f"{'='*70}")
        header = f"{'SCORE':>6}  {'DECISION':<10}  {'SERIES':<7}  {'RANGE':<6}  TITLE"
        print(header)
        print('-' * len(header))
        for score, decision, sm, rt, title in scored:
            flag = " *" if score >= ACCEPT_THRESHOLD else ""
            print(f"{score:>6}  {decision:<10}  {str(sm):<7}  {str(rt):<6}  {title}{flag}")

        print(f"\n{'='*70}")
        accepts = [(s, d, t) for s, d, sm, rt, t in scored if d == "ACCEPT"]
        if accepts:
            print(f"ACCEPTED ({len(accepts)}):")
            for score, decision, title in accepts[:10]:
                print(f"  * [{score}] {title}")
        else:
            print("ACCEPTED: (none)")

        fallbacks = [(s, t) for s, d, sm, rt, t in scored if d == "FALLBACK"]
        if fallbacks:
            print(f"\nFALLBACK ({len(fallbacks)}):")
            for score, title in fallbacks[:10]:
                print(f"  ~ [{score}] {title}")

        print(f"\n{'='*70}")
        print("DETAILED SCORING (top 3):")
        print(f"{'='*70}")
        for score, decision, sm, rt, title in scored[:3]:
            search = search_criteria(SERIES, ISSUE, YEAR, None, None, None, [])
            cs = score_comic(title, search)
            print(f"\nTITLE: {title}")
            print(f"  score={cs.score}  decision={accept_result(cs.score, cs.range_contains_target, cs.series_match)}")
            for field in ["series_match", "sub_series_type", "variant_accepted",
                          "detected_variant", "used_the_swap",
                          "remaining_is_different_series", "year_in_series_name",
                          "range_contains_target"]:
                val = getattr(cs, field)
                if val not in (None, False, 0, ""):
                    print(f"  {field}: {val}")
