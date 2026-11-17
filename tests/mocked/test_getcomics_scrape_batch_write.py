"""A scraped page is written in one transaction, not one per entry.

``_scrape_url_to_index`` used to open a connection, read ``scrape_attempts``,
``INSERT OR REPLACE`` and commit once per comic on the page, so a listing page
of thirty comics was thirty write transactions, and a write that raised left
its connection (holding the WAL write lock) to the garbage collector. The rows
are now staged while the page is parsed and written together through
``db_conn()`` after the fetch, so no connection is open across network I/O.
"""
from unittest.mock import MagicMock, patch

import pytest

PAGE = "https://getcomics.org/weekly-update/"

LISTING_HTML = """\
<html><head><title>Weekly Update – GetComics</title></head>
<body><article class="post-body"><section class="post-contents">
<div class="post-content"><h5><a href="https://getcomics.org/a">Star Wars - Jedi Knights 010 (2026)</a></h5>
<a class="aio-red" title="PIXELDRAIN" href="https://getcomics.org/dls/jedi">PIXELDRAIN</a></div>
<div class="post-content"><h5><a href="https://getcomics.org/b">Star Wars 023 (2026)</a></h5>
<a class="aio-red" title="PIXELDRAIN" href="https://getcomics.org/dls/sw23">PIXELDRAIN</a></div>
<div class="post-content"><h5><a href="https://getcomics.org/c">Star Wars 024 (2026)</a></h5>
<a class="aio-red" title="PIXELDRAIN" href="https://getcomics.org/dls/sw24">PIXELDRAIN</a></div>
</section></article></body></html>
"""


def _fake_scraper(events=None):
    resp = MagicMock(status_code=200, text=LISTING_HTML)
    resp.headers = {"Last-Modified": "Mon, 16 Nov 2026 00:00:00 GMT"}
    scraper = MagicMock()

    def get(*a, **k):
        if events is not None:
            events.append("fetch")
        return resp

    scraper.get.side_effect = get
    return scraper


class _CountingConnection:
    """Wraps a real connection, recording commits and closes."""

    def __init__(self, conn, events):
        self._conn = conn
        self._events = events

    def commit(self):
        self._events.append("commit")
        return self._conn.commit()

    def close(self):
        self._events.append("close")
        return self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.fixture
def counted(db_connection):
    from core import database
    from models.getcomics import _ensure_urls_table

    _ensure_urls_table()
    real = database.get_db_connection
    events = []

    def counting():
        events.append("open")
        return _CountingConnection(real(), events)

    with patch("core.database.get_db_connection", side_effect=counting):
        yield events


def _rows():
    from core.database import get_db_connection

    conn = get_db_connection()
    try:
        return conn.execute(
            "SELECT url, scrape_attempts, scrape_status, url_last_modified "
            "FROM getcomics_urls WHERE full_url = ? ORDER BY url", (PAGE,)
        ).fetchall()
    finally:
        conn.close()


def test_one_write_transaction_per_page(counted):
    from models.getcomics import _scrape_url_to_index

    with patch("models.getcomics.cloudscraper.create_scraper",
               return_value=_fake_scraper(counted)):
        results = _scrape_url_to_index(PAGE)

    # The page's <title> plus three entries.
    assert len(results) == 4
    assert counted.count("commit") == 1
    # The If-Modified-Since read is closed before the fetch, and every
    # connection opened (title parsing reads some settings) is closed.
    assert counted.count("open") == counted.count("close")
    assert counted[:3] == ["open", "close", "fetch"]
    assert counted.index("fetch") < counted.index("commit")
    rows = _rows()
    assert len(rows) == 4
    assert {r["scrape_attempts"] for r in rows} == {1}
    assert {r["url_last_modified"] for r in rows} == {"Mon, 16 Nov 2026 00:00:00 GMT"}


def test_rescrape_counts_attempts(db_connection):
    from models.getcomics import _ensure_urls_table, _scrape_url_to_index

    _ensure_urls_table()
    for _ in range(2):
        with patch("models.getcomics.cloudscraper.create_scraper",
                   return_value=_fake_scraper()):
            _scrape_url_to_index(PAGE)
    assert {r["scrape_attempts"] for r in _rows()} == {2}


def test_a_failed_write_stores_nothing_and_closes(counted):
    from models import getcomics

    real_store = getcomics._store_scraped_rows

    def failing(rows):
        # Fail mid-batch: the first rows are written, then the batch raises.
        real_execute_rows = rows[:2] + [("boom",)]
        return real_store(real_execute_rows)

    with patch("models.getcomics.cloudscraper.create_scraper",
               return_value=_fake_scraper()), \
            patch.object(getcomics, "_store_scraped_rows", side_effect=failing):
        assert getcomics._scrape_url_to_index(PAGE) == []

    assert counted.count("commit") == 0
    assert counted.count("open") == counted.count("close")
    assert _rows() == []
