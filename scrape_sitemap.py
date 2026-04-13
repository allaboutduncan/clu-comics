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
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime


def _scrape_url_for_index(url: str, url_slug: str = "", series_norm: str = "",
                          lastmod: str = "") -> list[dict]:
    """
    Scrape a GetComics URL and store ALL entries in scrape index — no score filtering.

    Returns a list of stored result dicts, each with:
        title, parsed, primary_download_url, entry_url
    Returns empty list on failure.

    Multi-entry support: listing pages (div.post-content) produce multiple entries,
    each with a unique entry_url = url || '#' || slugified(h5_text).
    Single comic pages produce one entry with entry_url = url.
    """
    import cloudscraper
    import re
    from bs4 import BeautifulSoup
    from core.database import get_db_connection
    from models.getcomics import parse_result_title, normalize_series_name

    def _slugify(text: str) -> str:
        """Create URL-safe slug from title text for use as entry identifier."""
        issue_match = re.search(r'#?(\d+(?:\s*[-–—]\s*\d+)?)', text)
        issue_slug = issue_match.group(1).replace(' ', '').replace('\u2013', '-').replace('\u2014', '-') if issue_match else ""
        slug = re.sub(r'[^a-z0-9]+', '-', text.lower())
        slug = slug.strip('-')
        if issue_slug:
            return f"{issue_slug}-{slug[:40]}"
        return slug[:50]

    scraper = cloudscraper.create_scraper()
    try:
        resp = scraper.get(url, timeout=15)
        if resp.status_code != 200:
            return []
    except Exception:
        return []

    soup = BeautifulSoup(resp.text, 'html.parser')
    results = []

    def _parse_and_store(title_text: str, download_url: str, entry_url: str):
        """Parse a title and store in scrape index."""
        if not title_text or len(title_text) < 3:
            return
        # Normalize all dash variants before any split or parse
        title_text = title_text.replace('\u2013', '-').replace('\u2014', '-')  # Unicode dashes
        title_text = title_text.replace('\x96', '-').replace('\x97', '-')      # Windows-1252
        title_text = title_text.replace('\ufffd', '-')                          # Replacement char
        # Split on suffix separator — but only if it doesn't look like an issue range dash.
        # E.g. "Top 10 #1 - 12 - GetComics" should split to "Top 10 #1 - 12"
        # (digit before " - " means it's likely an issue range separator).
        suffix_split = False
        for sep in [" - ", " \u2013 ", " \u2014 ", " \x97 "]:
            idx = title_text.find(sep)
            if idx > 0 and idx < len(title_text) - len(sep):
                char_before = title_text[idx - 1]
                if char_before.isdigit():
                    continue  # Skip — it's an issue range separator
            if sep in title_text:
                title_text = title_text.split(sep)[0].strip()
                suffix_split = True
                break
        if not suffix_split:
            if "GetComics" in title_text:
                title_text = title_text.split("GetComics")[0].strip().rstrip("-").rstrip()
        if not title_text:
            return

        parsed = parse_result_title(title_text)
        stored_series = series_norm
        entry_aliases = ''
        if parsed.name:
            page_norm = normalize_series_name(parsed.name)[0]
            if page_norm and page_norm != stored_series:
                entry_aliases = page_norm
        elif series_norm:
            stored_series = series_norm

        now_ts = datetime.now().isoformat()
        conn = get_db_connection()
        scrape_status = 'success' if download_url else 'empty'
        # Get existing scrape_attempts to preserve on update
        existing = conn.execute(
            "SELECT COALESCE(scrape_attempts, 0) FROM getcomics_urls WHERE url = ?", (entry_url,)
        ).fetchone()
        current_attempts = existing[0] if existing else 0
        conn.execute("""
            INSERT OR REPLACE INTO getcomics_urls
            (url, full_url, series_norm, url_slug, series_norm_norm, title, issue_num,
             issue_range, year, volume, is_annual, is_bulk_pack, is_multi_series,
             format_variants, download_url, lastmod, indexed_at, search_aliases,
             scrape_status, scrape_attempts, last_scrape_attempt, url_last_modified)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            entry_url,
            url,
            stored_series,
            url_slug,
            stored_series.replace('-', ' ').replace('\u2013', ' ').replace('\u2014', ' ').strip().lower() if stored_series else None,
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
            now_ts,
            entry_aliases,
            scrape_status,
            current_attempts + 1,
            None,
            '',
        ))
        conn.commit()
        conn.close()

        results.append({
            'title': title_text,
            'parsed': parsed,
            'primary_download_url': download_url,
            'entry_url': entry_url,
        })

    # Individual comic page: title from <title> tag, download from button
    title_tag = soup.find('title')
    if title_tag:
        title = title_tag.get_text(strip=True)
        for sep in [" - ", " \u2013 ", " \u2014 ", " \x97 "]:
            if sep in title:
                title = title.split(sep)[0].strip()
                break
        else:
            if "GetComics" in title:
                title = title.split("GetComics")[0].strip().rstrip("-").rstrip()
        title = title.replace('\u2013', '-').replace('\u2014', '-').replace('\x97', '-')

        download_url = None
        for btn in soup.select('a[class*="aio-red"], a[class*="aio-blue"]'):
            href = btn.get('href', '')
            if href.startswith('http'):
                download_url = href
                break
        if title:
            _parse_and_store(title, download_url, url)  # single entry uses base url

    # Listing page (variant 1): titles from post-content divs
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
            for title_text, btn in zip(titles_found, all_buttons):
                download_url = btn.get('href', '') if btn else None
                entry_slug = _slugify(title_text)
                entry_url = f"{url}#{entry_slug}"
                _parse_and_store(title_text, download_url, entry_url)

    return results


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
        # Returns list of dicts: [{title, parsed, primary_download_url, entry_url}, ...]
        results = _scrape_url_for_index(
            full_url, url_slug=url_slug,
            series_norm=series_norm, lastmod=''
        )

        if not results:
            return

        with lock:
            for r in results:
                total_scraped += 1
                if r.get('primary_download_url'):
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