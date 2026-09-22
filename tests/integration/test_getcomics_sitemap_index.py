"""The sitemap index: it must actually index something, and be looked up once.

Two defects found while tracing a support package whose GetComics sweep took
2h37m and downloaded the same file for issue after issue:

* ``build_sitemap_index`` built its rows without the ``url`` column, which is
  NOT NULL. Every ``executemany`` raised, the per-sitemap ``except`` swallowed
  it, and the weekly job reported "0 URLs -- check network access" -- blaming
  the network for a schema bug.

* ``lookup_series_urls`` returns one row per scrape-index *entry*, and a
  listing page holds many entries sharing one ``full_url``. The caller scrapes
  ``full_url``, so it fetched the same page over and over: in the reported log
  all 20 candidate slots went to three distinct pages (8x, 7x, 5x), for every
  issue searched.
"""
import sqlite3

import pytest


def _rows(conn):
    return conn.execute(
        "SELECT url, full_url, series_norm, series_norm_norm, url_slug, "
        "       category, lastmod FROM getcomics_urls ORDER BY url"
    ).fetchall()


SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://getcomics.org/dc/batman-1-2024/</loc>
       <lastmod>2026-09-01T00:00:00+00:00</lastmod></url>
  <url><loc>https://getcomics.org/dc/batman-2-2024/</loc>
       <lastmod>2026-09-02T00:00:00+00:00</lastmod></url>
</urlset>
"""

SITEMAP_INDEX_XML = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://getcomics.org/post-sitemap.xml</loc></sitemap>
</sitemapindex>
"""


class _Resp:
    def __init__(self, text, status=200):
        self.text = text
        self.status_code = status
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


@pytest.fixture(autouse=True)
def urls_table(db_connection):
    """getcomics_urls is created lazily by the module, not by init_db()."""
    from models.getcomics import _ensure_urls_table

    _ensure_urls_table()
    return db_connection


@pytest.fixture
def stub_scraper(monkeypatch):
    """Serve the index and one post-sitemap; everything else 404s."""
    import models.getcomics as gc

    def fake_get(url, *a, **k):
        if url.endswith("/sitemap.xml"):
            return _Resp(SITEMAP_INDEX_XML)
        if "post-sitemap" in url:
            return _Resp(SITEMAP_XML)
        return _Resp("", 404)

    monkeypatch.setattr(gc.scraper, "get", fake_get)


class TestBuildSitemapIndexActuallyWrites:

    def test_a_rebuild_indexes_its_urls(self, db_connection, stub_scraper):
        from models.getcomics import build_sitemap_index

        assert build_sitemap_index() == 2, \
            "the rebuild reported 0 URLs -- the insert is raising again"
        assert len(_rows(db_connection)) == 2

    def test_every_row_carries_the_columns_the_lookup_matches_on(
            self, db_connection, stub_scraper):
        """A row without series_norm_norm is indexed and then never found."""
        from models.getcomics import build_sitemap_index

        build_sitemap_index()
        for row in _rows(db_connection):
            assert row[0], "url is the row key and is NOT NULL"
            assert row[3], "series_norm_norm is what lookup_series_urls matches"

    def test_the_url_key_is_the_page(self, db_connection, stub_scraper):
        """A scraped *entry* is keyed "<page>#<slug>", so the two never collide."""
        from models.getcomics import build_sitemap_index

        build_sitemap_index()
        for row in _rows(db_connection):
            assert "#" not in row[0]
            assert row[0] == row[1]

    def test_rebuilding_replaces_rather_than_appends(
            self, db_connection, stub_scraper):
        from models.getcomics import build_sitemap_index

        build_sitemap_index()
        build_sitemap_index(force_refresh=True)
        assert len(_rows(db_connection)) == 2, \
            "a second rebuild must not duplicate every row"

    def test_a_rebuild_does_not_erase_what_a_scrape_learned(
            self, db_connection, stub_scraper):
        """INSERT OR REPLACE deleted the row, taking title/issue/download_url
        with it. A single-comic page is keyed by its page URL, so the sitemap
        refresh lands on exactly the row the scraper filled in."""
        from models.getcomics import build_sitemap_index

        build_sitemap_index()
        db_connection.execute(
            "UPDATE getcomics_urls SET title = ?, issue_num = ?, download_url = ? "
            "WHERE url = ?",
            ("Batman #1", "1", "https://example.invalid/x",
             "https://getcomics.org/dc/batman-1-2024/"),
        )
        db_connection.commit()

        build_sitemap_index(force_refresh=True)

        row = db_connection.execute(
            "SELECT title, issue_num, download_url FROM getcomics_urls WHERE url = ?",
            ("https://getcomics.org/dc/batman-1-2024/",),
        ).fetchone()
        assert tuple(row) == ("Batman #1", "1", "https://example.invalid/x")


class TestLookupReturnsEachPageOnce:

    def _seed_listing_page(self, conn, page, series, n_entries):
        from models.getcomics import _norm_series_key

        for i in range(n_entries):
            conn.execute(
                "INSERT INTO getcomics_urls "
                "(url, full_url, series_norm, series_norm_norm, url_slug) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"{page}#entry-{i}", page, series, _norm_series_key(series),
                 page.rstrip("/").rsplit("/", 1)[-1]),
            )
        conn.commit()

    def test_a_listing_page_is_offered_once(self, db_connection):
        from models.getcomics import lookup_series_urls

        self._seed_listing_page(
            db_connection, "https://getcomics.org/dc/weekly-pack-2026/", "batman", 8
        )
        urls = [r["full_url"] for r in lookup_series_urls("Batman")]
        assert urls == ["https://getcomics.org/dc/weekly-pack-2026/"], \
            "the same page must not consume eight candidate slots"

    def test_distinct_pages_are_all_kept(self, db_connection):
        from models.getcomics import lookup_series_urls

        for page in ("https://getcomics.org/dc/a-2026/",
                     "https://getcomics.org/dc/b-2026/",
                     "https://getcomics.org/dc/c-2026/"):
            self._seed_listing_page(db_connection, page, "batman", 4)

        urls = [r["full_url"] for r in lookup_series_urls("Batman")]
        assert len(urls) == 3
        assert len(set(urls)) == 3

    def test_the_first_row_for_a_page_is_the_one_returned(self, db_connection):
        """Order is stable, so a candidate list does not reshuffle per call."""
        from models.getcomics import lookup_series_urls

        self._seed_listing_page(
            db_connection, "https://getcomics.org/dc/weekly-pack-2026/", "batman", 5
        )
        first = lookup_series_urls("Batman")
        second = lookup_series_urls("Batman")
        assert first == second
