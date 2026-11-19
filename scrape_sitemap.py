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
  3. Store title, issue info, ALL download links in getcomics_urls
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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed


def _scrape_url_for_index(url: str, url_slug: str = "", series_norm: str = "",
                          lastmod: str = "") -> list[dict]:
    """
    Scrape a GetComics URL and store ALL entries in scrape index -- no score filtering.

    A thin wrapper over the app's own indexer. This file used to carry a private
    copy of it, and the copy drifted: it filed every entry on a page under the
    series being scraped for, whatever the entry was (#777), and slugified entry
    keys its own way.

    Returns the stored rows (each with title, download_url, url), or an empty
    list on failure or when the page has not changed.
    """
    from models.getcomics import _scrape_url_to_index

    return _scrape_url_to_index(url, url_slug=url_slug, series_norm=series_norm,
                                lastmod=lastmod) or []


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
        # Returns the stored rows: [{title, download_url, url, ...}, ...]
        results = _scrape_url_for_index(
            full_url, url_slug=url_slug,
            series_norm=series_norm, lastmod=''
        )

        if not results:
            return

        with lock:
            for r in results:
                total_scraped += 1
                if r.get('download_url'):
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
    from models.getcomics import _ensure_urls_table
    _ensure_urls_table()

    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT COUNT(*) FROM getcomics_urls WHERE scrape_status = \'success\'')
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
    c.execute('SELECT COUNT(*) FROM getcomics_urls WHERE scrape_status = \'success\'')
    after = c.fetchone()[0]
    conn.close()

    print(f"\n=== Total: {total_scraped} pages scraped, {total_links} with links ===", flush=True)
    print(f" Scrape index grew: {before} -> {after} (+{after - before})", flush=True)


if __name__ == '__main__':
    main()