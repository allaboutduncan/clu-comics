"""The scrape index must not vouch for a series it never checked (#777).

A live search for "52" returns every DC "New 52" collection page, and those
pages were indexed under the series being *searched for*: 458 of 473 rows filed
under "52" were Batgirl, Green Lantern and Batman trades. ``try_scrape_index``
then matched on that tag plus an issue range and returned a fixed score of 50
without reading the title, so "Batgirl Vol. 1: The Darkest Reflection (#1-6)"
was an ACCEPT for 52 #2 through #6 -- and because an index hit is returned
instead of a live search, the real issue was never looked for.

Three things are pinned here: the index only returns what the shared scorer
accepts, an entry is filed under the series its own title names, and the rows
already mis-filed are repaired once.
"""
from unittest.mock import MagicMock, patch

import pytest

BATGIRL_TITLE = "Batgirl Vol. 1: The Darkest Reflection (#1-6) (223 MB)"
NEW52_PAGE = "https://getcomics.org/dc/batgirl-vol-4-new-52-tpb-collection-2011-2016/"


def _insert(conn, url, series, title, issue_num=None, issue_range=None,
            year=None, volume=None, full_url=None):
    conn.execute(
        "INSERT INTO getcomics_urls "
        "(url, full_url, series_norm, series_norm_norm, title, issue_num, issue_range, "
        " year, volume, is_annual, is_bulk_pack, is_multi_series, download_url, scrape_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, 'success')",
        (url, full_url or url, series, series.lower(), title, issue_num, issue_range,
         year, volume, "https://getcomics.org/dlds/" + str(abs(hash(url)))),
    )


@pytest.fixture
def urls_table(db_connection):
    from core.database import get_db_connection
    from models.getcomics import _ensure_urls_table

    _ensure_urls_table()
    conn = get_db_connection()
    yield conn
    conn.close()


def _tags(conn):
    return {
        title: tag
        for title, tag in conn.execute(
            "SELECT title, series_norm_norm FROM getcomics_urls WHERE title IS NOT NULL"
        ).fetchall()
    }


class TestIndexHitIsScored:
    def test_other_series_filed_under_the_tag_is_not_accepted(self, urls_table):
        """The issue's own row: tag 52, a Batgirl trade, range holding #2."""
        from models.getcomics import try_scrape_index

        _insert(urls_table, NEW52_PAGE + "#1-6-batgirl", "52", BATGIRL_TITLE,
                issue_num="1-6", issue_range="(1, 6)", volume=1, full_url=NEW52_PAGE)
        urls_table.commit()

        for issue in ("2", "3", "4", "5", "6"):
            assert try_scrape_index("52", issue, 2006, 1, 2006) == (None, 0)

    def test_the_real_issue_is_returned_with_its_real_score(self, urls_table):
        from models.getcomics import ACCEPT_THRESHOLD, try_scrape_index

        _insert(urls_table, "https://getcomics.org/dc/52-2-2006/", "52",
                "52 #2 (2006)", issue_num="2", year=2006)
        urls_table.commit()

        result, score = try_scrape_index("52", "2", 2006, None, 2006)

        assert result["title"] == "52 #2 (2006)"
        assert result["link"] == "https://getcomics.org/dc/52-2-2006/"
        assert result["download_url"]
        # The scorer's verdict, not the old fixed 50.
        assert score >= ACCEPT_THRESHOLD and score != 50

    def test_the_real_issue_beats_a_mis_filed_neighbour(self, urls_table):
        from models.getcomics import try_scrape_index

        _insert(urls_table, NEW52_PAGE + "#1-6-batgirl", "52", BATGIRL_TITLE,
                issue_num="1-6", issue_range="(1, 6)", full_url=NEW52_PAGE)
        _insert(urls_table, "https://getcomics.org/dc/52-2-2006/", "52",
                "52 #2 (2006)", issue_num="2", year=2006)
        urls_table.commit()

        result, _ = try_scrape_index("52", "2", 2006, None, 2006)

        assert result["title"] == "52 #2 (2006)"

    def test_a_same_series_trade_range_is_not_a_direct_match(self, urls_table):
        """A correctly-filed row was overturned downstream too: a trade's
        "(#1-6)" is not issue #3 of the series."""
        from models.getcomics import try_scrape_index

        _insert(urls_table, NEW52_PAGE + "#1-6-batgirl", "batgirl", BATGIRL_TITLE,
                issue_num="1-6", issue_range="(1, 6)", full_url=NEW52_PAGE)
        urls_table.commit()

        assert try_scrape_index("Batgirl", "3", 2012, None, 2011) == (None, 0)

    def test_an_alias_titled_row_needs_the_alias(self, urls_table):
        from models.getcomics import try_scrape_index

        _insert(urls_table, "https://getcomics.org/other/2000ad-2400-2024/", "2000 AD",
                "2000AD #2400 (2024)", issue_num="2400", year=2024)
        urls_table.commit()

        assert try_scrape_index("2000 AD", "2400", 2024) == (None, 0)
        result, _ = try_scrape_index("2000 AD", "2400", 2024, series_aliases=["2000AD"])
        assert result["title"] == "2000AD #2400 (2024)"


class TestEntrySeriesKey:
    @pytest.mark.parametrize("title, hint, expected", [
        # The hint survives when the title names it...
        ("52 #2 (2006)", "52", "52"),
        ("Punisher #4 (2026)", "The Punisher", "The Punisher"),
        ("Spider Man #5 (2019)", "Spider-Man", "Spider-Man"),
        ("2000AD #2400 (2024)", "2000 AD", "2000 AD"),
        ("Top 10 #1 - 12", "Top 10", "Top 10"),
        # ...and is dropped when it does not.
        (BATGIRL_TITLE, "52", "Batgirl"),
        ("Batgirl Vol. 4 (New 52 TPB Collection) (2011-2016)", "52", "Batgirl"),
        ("Green Lantern Vol. 3: The End (#13-20)", "52", "Green Lantern"),
        ("The New 52 - Futures End #2 (2014)", "52", "The New 52"),
        # No hint: the title speaks for itself.
        ("Daredevil #4 (2026)", "", "Daredevil"),
    ])
    def test_key(self, title, hint, expected):
        from models.getcomics import _entry_series_key

        assert _entry_series_key(title, hint) == expected

    def test_a_number_inside_another_word_is_not_the_series(self):
        from models.getcomics import _title_names_series

        assert not _title_names_series("520 Days #1", "52")
        assert not _title_names_series("X-52 #1", "52")


LISTING_HTML = f"""\
<html><head><title>Batgirl Vol. 4 (New 52 TPB Collection) (2011-2016) - GetComics</title></head><body>
<div class="post-content"><h5><a href="https://getcomics.org/a">{BATGIRL_TITLE}</a></h5>
<a class="aio-red" href="https://getcomics.org/dlds/batgirl">Download</a></div>
<div class="post-content"><h5><a href="https://getcomics.org/b">52 #2 (2006)</a></h5>
<a class="aio-red" href="https://getcomics.org/dlds/52">Download</a></div>
</body></html>
"""


def _fake_scraper():
    resp = MagicMock(status_code=200, text=LISTING_HTML)
    resp.headers = {}
    scraper = MagicMock()
    scraper.get.return_value = resp
    return scraper


class TestIndexerFilesByTitle:
    def test_scrape_does_not_stamp_the_page_hint_on_every_entry(self, urls_table):
        from models.getcomics import _scrape_url_to_index

        with patch("models.getcomics.cloudscraper.create_scraper", return_value=_fake_scraper()):
            _scrape_url_to_index(NEW52_PAGE, series_norm="52")

        tags = _tags(urls_table)
        assert tags[BATGIRL_TITLE] == "batgirl"
        assert tags["52 #2 (2006)"] == "52"
        assert tags["Batgirl Vol. 4 (New 52 TPB Collection) (2011-2016)"] == "batgirl"

    def test_live_results_are_not_filed_under_the_search(self, urls_table):
        """The path that produced the report: index_live_results(series_norm=<search>)."""
        from models.getcomics import index_live_results

        with patch("models.getcomics.cloudscraper.create_scraper", return_value=_fake_scraper()):
            index_live_results([NEW52_PAGE], series_norm="52", rate_limit=0)

        tags = _tags(urls_table)
        assert [t for t, tag in tags.items() if tag == "52"] == ["52 #2 (2006)"]

    def test_indexed_page_no_longer_answers_for_the_search(self, urls_table):
        """End to end: index the page for a "52" search, then ask for 52 #3."""
        from models.getcomics import index_live_results, try_scrape_index

        with patch("models.getcomics.cloudscraper.create_scraper", return_value=_fake_scraper()):
            index_live_results([NEW52_PAGE], series_norm="52", rate_limit=0)

        assert try_scrape_index("52", "3", 2006, None, 2006) == (None, 0)


class TestRepairSeriesTags:
    def test_refiles_only_what_the_title_contradicts(self, urls_table):
        from core.database import get_user_preference
        from models.getcomics import SERIES_TAGS_REPAIRED_FLAG, repair_series_tags

        _insert(urls_table, NEW52_PAGE + "#1-6-batgirl", "52", BATGIRL_TITLE,
                issue_num="1-6", issue_range="(1, 6)", full_url=NEW52_PAGE)
        _insert(urls_table, "https://getcomics.org/dc/52-2-2006/", "52",
                "52 #2 (2006)", issue_num="2", year=2006)
        _insert(urls_table, "https://getcomics.org/marvel/punisher-4/", "The Punisher",
                "Punisher #4 (2026)", issue_num="4", year=2026)
        # A sitemap row nobody has scraped: no title, nothing to check against.
        urls_table.execute(
            "INSERT INTO getcomics_urls (url, full_url, series_norm, series_norm_norm) "
            "VALUES ('https://getcomics.org/dc/x/', 'https://getcomics.org/dc/x/', '52', '52')"
        )
        urls_table.commit()

        assert repair_series_tags() == 1

        rows = dict(urls_table.execute(
            "SELECT url, series_norm_norm FROM getcomics_urls").fetchall())
        assert rows[NEW52_PAGE + "#1-6-batgirl"] == "batgirl"
        assert rows["https://getcomics.org/dc/52-2-2006/"] == "52"
        assert rows["https://getcomics.org/marvel/punisher-4/"] == "the punisher"
        assert rows["https://getcomics.org/dc/x/"] == "52"
        assert get_user_preference(SERIES_TAGS_REPAIRED_FLAG)

    def test_runs_once(self, urls_table):
        from models.getcomics import repair_series_tags

        assert repair_series_tags() == 0
        _insert(urls_table, NEW52_PAGE + "#1-6-batgirl", "52", BATGIRL_TITLE,
                issue_num="1-6", issue_range="(1, 6)", full_url=NEW52_PAGE)
        urls_table.commit()

        assert repair_series_tags() == 0
        assert _tags(urls_table)[BATGIRL_TITLE] == "52"

    def test_a_failed_write_leaves_the_flag_unset(self, urls_table):
        from core.database import get_user_preference
        from models import getcomics

        _insert(urls_table, NEW52_PAGE + "#1-6-batgirl", "52", BATGIRL_TITLE,
                issue_num="1-6", issue_range="(1, 6)", full_url=NEW52_PAGE)
        urls_table.commit()

        with patch.object(getcomics, "_norm_series_key", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                getcomics.repair_series_tags()

        assert not get_user_preference(getcomics.SERIES_TAGS_REPAIRED_FLAG)
        assert getcomics.repair_series_tags() == 1
