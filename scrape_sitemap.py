#!/usr/bin/env python
"""
Scrape GetComics sitemap URLs and store results in the scrape index.

Usage:
    python scrape_sitemap.py "Amazing Spider-Man" "Captain America" ...

This pre-populates the scrape index so future wanted-issues simulations
can find results without hitting GetComics live.

For each series:
  1. Look up sitemap URLs via lookup_series_urls()
  2. Scrape each URL (sitemap-first, full HTML scraping)
  3. Store title, issue info, ALL download links in getcomics_scrape_index
"""
import sys
import os
sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)
sys.stderr = os.fdopen(sys.stderr.fileno(), 'w', buffering=1)

# Disable logging BEFORE importing core modules (which may log emojis on load)
import logging as _logging
_logging.disable(_logging.CRITICAL)

import argparse
import time
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed


def _scrape_url_for_index(url: str, url_slug: str = "", series_norm: str = "",
                          lastmod: str = "") -> tuple | None:
    """
    Scrape a GetComics URL and store in scrape index — no score filtering.
    Returns (title, parsed, primary_download_url) or None on failure.
    """
    import cloudscraper
    from bs4 import BeautifulSoup
    from core.database import get_db_connection
    from models.getcomics import parse_result_title, normalize_series_name

    scraper = cloudscraper.create_scraper()
    try:
        resp = scraper.get(url, timeout=15)
        if resp.status_code != 200:
            return None
    except Exception:
        return None

    soup = BeautifulSoup(resp.text, 'html.parser')

    # Extract title
    title_tag = soup.find('title')
    if not title_tag:
        return None
    title = title_tag.get_text(strip=True)
    for sep in [" - ", " \u2013 ", " \u2014 ", " \x97 "]:
        if sep in title:
            title = title.split(sep)[0].strip()
            break
    else:
        if "GetComics" in title:
            title = title.split("GetComics")[0].strip().rstrip("-").rstrip()
    title = title.replace('\u2013', '-').replace('\u2014', '-').replace('\x97', '-')
    if not title:
        return None

    # Extract download links
    links = {"pixeldrain": None, "download_now": None, "mega": None}
    for a in soup.find_all("a"):
        title_attr = (a.get("title") or "").upper()
        href = a.get("href", "") or ""
        if not href:
            continue
        if "PIXELDRAIN" in title_attr and not links["pixeldrain"]:
            links["pixeldrain"] = href
        elif "DOWNLOAD NOW" in title_attr and not links["download_now"]:
            links["download_now"] = href
        elif "MEGA" in title_attr and not links["mega"]:
            links["mega"] = href
    if not any(links.values()):
        for a in soup.find_all("a", class_="aio-red"):
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

    all_links = {k: v for k, v in links.items() if v}
    primary_url = (all_links.get('pixeldrain') or
                   all_links.get('download_now') or
                   all_links.get('mega') or '')

    # Parse title for structured data
    parsed = parse_result_title(title)

    # Determine stored_series and search_aliases
    stored_series = series_norm
    search_aliases = ''
    if parsed.name:
        page_norm = normalize_series_name(parsed.name)[0]
        if page_norm and page_norm != stored_series:
            search_aliases = page_norm

    # Store in database
    conn = get_db_connection()
    conn.execute("""
        INSERT OR REPLACE INTO getcomics_scrape_index
        (url, series_norm, url_slug, title, issue_num, issue_range, year,
         volume, is_annual, is_bulk_pack, is_multi_series, format_variants,
         download_url, lastmod, indexed_at, search_aliases, series_norm_norm)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
    """, (
        url,
        stored_series,
        url_slug,
        title,
        parsed.issue,
        str(parsed.issue_range) if parsed.issue_range else None,
        parsed.year,
        parsed.volume,
        int(parsed.is_annual),
        int(parsed.is_bulk_pack),
        int(parsed.is_multi_series),
        ','.join(parsed.format_variants) if parsed.format_variants else None,
        primary_url,
        lastmod,
        search_aliases,
        stored_series.replace('-', ' ').replace('\u2013', ' ').replace('\u2014', ' ').strip().lower() if stored_series else None,
    ))
    conn.commit()
    conn.close()

    return title, parsed, primary_url


def scrape_series(series_name: str, max_urls: int = 0) -> tuple[int, int]:
    """
    Scrape all indexed sitemap URLs for a series and store in scrape index.

    Returns (urls_scraped, links_found).
    """
    from core.database import get_db_connection
    from models.getcomics import (
        lookup_series_urls,
        scrape_and_score_candidate,
        normalize_series_name,
    )

    series_norm, _ = normalize_series_name(series_name)

    # Look up sitemap URLs for this series
    sitemap_urls = lookup_series_urls(series_name)
    if not sitemap_urls:
        print(f"  No sitemap URLs found for '{series_name}'", flush=True)
        return 0, 0

    print(f"  Found {len(sitemap_urls)} sitemap URLs for '{series_norm}'", flush=True)

    total_scraped = 0
    total_links = 0
    lock = threading.Lock()

    def _scrape_one(entry):
        nonlocal total_scraped, total_links
        full_url = entry['full_url']
        url_slug = entry.get('url_slug', '')

        # Rate limit: be a good GetComics citizen
        time.sleep(1.5)

        # Scrape the page directly (bypass scoring filter)
        result = _scrape_url_for_index(
            full_url, url_slug=url_slug,
            series_norm=series_norm, lastmod=''
        )

        if not result:
            return

        title, parsed, primary_url = result

        with lock:
            total_scraped += 1
            if primary_url:
                total_links += 1

    # Limit URLs to process
    urls_to_process = sitemap_urls[:max_urls] if max_urls > 0 else sitemap_urls
    print(f"  Scraping {len(urls_to_process)} URLs (max_urls={max_urls})...", flush=True)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_scrape_one, entry) for entry in urls_to_process]
        done_count = 0
        for future in as_completed(futures):
            try:
                future.result()
                done_count += 1
                if done_count % 10 == 0:
                    print(f"  ... {done_count}/{len(urls_to_process)} done", flush=True)
            except Exception as e:
                print(f"  Error: {e}", flush=True)

    return total_scraped, total_links


def main():
    parser = argparse.ArgumentParser(description='Scrape GetComics sitemap and store in index')
    parser.add_argument('series', nargs='+', help='Series names to scrape')
    parser.add_argument('--max', type=int, default=0,
                        help='Max URLs per series (0=all, default=all)')
    parser.add_argument('--rate', type=float, default=1.5,
                        help='Seconds between requests (default=1.5)')
    args = parser.parse_args()

    from core.database import get_db_connection
    from models.getcomics import _ensure_scrape_index_table
    _ensure_scrape_index_table()

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM getcomics_scrape_index')
    before = c.fetchone()[0]
    print(f"Starting. Scrape index has {before} rows.", flush=True)
    conn.close()

    total_scraped = 0
    total_links = 0

    for series_name in args.series:
        print(f"\nScraping: {series_name}", flush=True)
        t0 = time.time()
        scraped, links = scrape_series(series_name, max_urls=args.max)
        elapsed = time.time() - t0
        print(f"  Done: {scraped} pages scraped, {links} with download links "
              f"in {elapsed:.1f}s ({elapsed/60:.1f} min)", flush=True)
        total_scraped += scraped
        total_links += links

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM getcomics_scrape_index')
    after = c.fetchone()[0]
    conn.close()

    print(f"\n=== Total: {total_scraped} pages scraped, {total_links} with links ===", flush=True)
    print(f" Scrape index grew: {before} -> {after} (+{after - before})", flush=True)


if __name__ == '__main__':
    main()