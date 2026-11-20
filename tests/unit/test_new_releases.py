"""Tests for get_new_release_items(): the New Releases dashboard section."""
import os
import re

import pytest

MARVEL = "/lib/Marvel"
MARVEL_2 = "/lib/Marvel_2"


def _own(series_id, issue_id, number, path, store_date, found=1, indexed=True):
    """One issue with a store date, its collection_status row and its file."""
    from tests.factories.db_factories import create_issue, create_file_index_entry
    from core.database import save_collection_status_bulk

    create_issue(issue_id=issue_id, series_id=series_id, number=number,
                 store_date=store_date)
    save_collection_status_bulk([{
        "series_id": series_id, "issue_id": issue_id, "issue_number": number,
        "found": found, "file_path": path, "file_mtime": 1700000000.0,
        "matched_via": "exact",
    }])
    if indexed:
        create_file_index_entry(name=os.path.basename(path), path=path,
                                parent=os.path.dirname(path))


@pytest.fixture
def releases_db(db_connection):
    from tests.factories.db_factories import (
        create_publisher, create_series, reset_counters,
    )

    reset_counters()
    pub_id = create_publisher(publisher_id=10, name="Marvel")
    create_series(series_id=100, name="Avengers", publisher_id=pub_id,
                  mapped_path=f"{MARVEL}/Avengers")
    create_series(series_id=200, name="X-Men", publisher_id=pub_id,
                  mapped_path=f"{MARVEL_2}/X-Men")

    _own(100, 1001, "1", f"{MARVEL}/Avengers/Avengers 001.cbz", "2026-09-02")
    _own(100, 1002, "2", f"{MARVEL}/Avengers/Avengers 002.cbz", "2026-10-07")
    _own(100, 1003, "3", f"{MARVEL}/Avengers/Avengers 003.cbz", "2026-11-04")
    _own(200, 2001, "1", f"{MARVEL_2}/X-Men/X-Men 001.cbz", "2026-10-21")
    return db_connection


def _paths(items):
    return [os.path.basename(i["file_path"]) for i in items]


class TestGetNewReleaseItems:

    def test_newest_store_date_first_across_series(self, releases_db):
        from core.database import get_new_release_items

        items = get_new_release_items(limit=10)

        assert _paths(items) == [
            "Avengers 003.cbz", "X-Men 001.cbz",
            "Avengers 002.cbz", "Avengers 001.cbz",
        ]
        first = items[0]
        assert first["series_name"] == "Avengers"
        assert first["issue_number"] == "3"
        assert first["store_date"] == "2026-11-04"
        assert first["file_name"] == "Avengers 003.cbz"
        assert first["file_size"] == 1024

    def test_limit(self, releases_db):
        from core.database import get_new_release_items

        assert _paths(get_new_release_items(limit=2)) == [
            "Avengers 003.cbz", "X-Men 001.cbz",
        ]

    def test_missing_issue_is_excluded(self, releases_db):
        from core.database import get_new_release_items

        _own(100, 1004, "4", f"{MARVEL}/Avengers/Avengers 004.cbz",
             "2026-11-18", found=0)

        assert "Avengers 004.cbz" not in _paths(get_new_release_items())

    @pytest.mark.parametrize("store_date", [None, ""])
    def test_issue_without_store_date_is_excluded(self, releases_db, store_date):
        from core.database import get_new_release_items

        _own(100, 1005, "5", f"{MARVEL}/Avengers/Avengers 005.cbz", store_date)

        assert "Avengers 005.cbz" not in _paths(get_new_release_items())

    def test_stale_collection_status_path_is_excluded(self, releases_db):
        """collection_status.file_path does not follow a move; file_index does."""
        from core.database import get_new_release_items

        _own(100, 1006, "6", f"{MARVEL}/Avengers/Moved Away 006.cbz",
             "2026-11-18", indexed=False)

        assert "Moved Away 006.cbz" not in _paths(get_new_release_items())

    def test_unmapped_series_is_excluded(self, releases_db):
        from tests.factories.db_factories import create_series
        from core.database import get_new_release_items

        # The factory fills in a mapped_path, so clear it afterwards.
        create_series(series_id=300, name="Thor", publisher_id=10)
        releases_db.execute("UPDATE series SET mapped_path = NULL WHERE id = 300")
        releases_db.commit()
        _own(300, 3001, "1", f"{MARVEL}/Thor/Thor 001.cbz", "2026-11-18")

        assert "Thor 001.cbz" not in _paths(get_new_release_items())

    def test_file_matched_under_two_series_is_listed_once(self, releases_db):
        from tests.factories.db_factories import create_issue, create_series
        from core.database import get_new_release_items, save_collection_status_bulk

        path = f"{MARVEL}/Avengers/Avengers 003.cbz"
        create_series(series_id=400, name="Avengers Reprint", publisher_id=10,
                      mapped_path=f"{MARVEL}/Avengers")
        create_issue(issue_id=4001, series_id=400, number="3",
                     store_date="2026-11-04")
        save_collection_status_bulk([{
            "series_id": 400, "issue_id": 4001, "issue_number": "3",
            "found": 1, "file_path": path, "file_mtime": 1700000000.0,
            "matched_via": "exact",
        }])

        assert _paths(get_new_release_items()).count("Avengers 003.cbz") == 1

    def test_folder_scope_does_not_leak_a_sibling_prefix(self, releases_db):
        """/lib/Marvel must not take in /lib/Marvel_2."""
        from core.database import get_new_release_items

        items = get_new_release_items(limit=10, allowed_prefixes=[MARVEL])

        assert _paths(items) == [
            "Avengers 003.cbz", "Avengers 002.cbz", "Avengers 001.cbz",
        ]

    def test_folder_scope_applies_before_the_limit(self, releases_db):
        from core.database import get_new_release_items

        items = get_new_release_items(limit=1, allowed_prefixes=[MARVEL_2])

        assert _paths(items) == ["X-Men 001.cbz"]

    def test_no_granted_folders_returns_nothing(self, releases_db):
        from core.database import get_new_release_items

        assert get_new_release_items(limit=10, allowed_prefixes=[]) == []


class TestGetNewReleasesPage:

    def test_pages_through_everything_once(self, releases_db):
        from core.database import get_new_releases_page

        first, total = get_new_releases_page(offset=0, limit=3)
        second, total_again = get_new_releases_page(offset=3, limit=3)

        assert total == total_again == 4
        assert _paths(first) == [
            "Avengers 003.cbz", "X-Men 001.cbz", "Avengers 002.cbz",
        ]
        assert _paths(second) == ["Avengers 001.cbz"]

    def test_oldest_first(self, releases_db):
        from core.database import get_new_releases_page

        items, _ = get_new_releases_page(sort="oldest")

        assert _paths(items) == [
            "Avengers 001.cbz", "Avengers 002.cbz",
            "X-Men 001.cbz", "Avengers 003.cbz",
        ]

    def test_items_carry_the_publisher(self, releases_db):
        from core.database import get_new_releases_page

        items, _ = get_new_releases_page(limit=1)

        assert items[0]["publisher_name"] == "Marvel"

    def test_search_matches_the_series_name(self, releases_db):
        from core.database import get_new_releases_page

        items, total = get_new_releases_page(search="x-men")

        assert total == 1
        assert _paths(items) == ["X-Men 001.cbz"]

    def test_search_wildcards_are_literal(self, releases_db):
        """'_' must not match any character: X_Men is not X-Men."""
        from core.database import get_new_releases_page

        assert get_new_releases_page(search="X_Men") == ([], 0)
        assert get_new_releases_page(search="%") == ([], 0)

    def test_publisher_filter(self, releases_db):
        from tests.factories.db_factories import create_publisher, create_series
        from core.database import get_new_releases_page

        create_publisher(publisher_id=20, name="DC Comics")
        create_series(series_id=500, name="Batman", publisher_id=20,
                      mapped_path="/lib/DC/Batman")
        _own(500, 5001, "1", "/lib/DC/Batman/Batman 001.cbz", "2026-11-11")

        items, total = get_new_releases_page(publisher_id=20)

        assert total == 1
        assert _paths(items) == ["Batman 001.cbz"]

    def test_year_filter(self, releases_db):
        from core.database import get_new_releases_page

        _own(100, 1009, "0", f"{MARVEL}/Avengers/Avengers 000.cbz", "2025-12-31")

        items, total = get_new_releases_page(year="2025")

        assert total == 1
        assert _paths(items) == ["Avengers 000.cbz"]

    def test_total_counts_a_doubly_matched_file_once(self, releases_db):
        from tests.factories.db_factories import create_issue, create_series
        from core.database import get_new_releases_page, save_collection_status_bulk

        create_series(series_id=400, name="Avengers Reprint", publisher_id=10,
                      mapped_path=f"{MARVEL}/Avengers")
        create_issue(issue_id=4001, series_id=400, number="3",
                     store_date="2026-11-04")
        save_collection_status_bulk([{
            "series_id": 400, "issue_id": 4001, "issue_number": "3",
            "found": 1, "file_path": f"{MARVEL}/Avengers/Avengers 003.cbz",
            "file_mtime": 1700000000.0, "matched_via": "exact",
        }])

        items, total = get_new_releases_page()

        assert total == 4
        assert len(items) == 4

    def test_folder_scope_limits_the_total(self, releases_db):
        from core.database import get_new_releases_page

        assert get_new_releases_page(allowed_prefixes=[MARVEL_2])[1] == 1
        assert get_new_releases_page(allowed_prefixes=[]) == ([], 0)


class TestGetNewReleaseFacets:

    def test_publishers_and_years(self, releases_db):
        from tests.factories.db_factories import create_publisher, create_series
        from core.database import get_new_release_facets

        create_publisher(publisher_id=20, name="DC Comics")
        create_series(series_id=500, name="Batman", publisher_id=20,
                      mapped_path="/lib/DC/Batman")
        _own(500, 5001, "1", "/lib/DC/Batman/Batman 001.cbz", "2025-06-04")

        facets = get_new_release_facets()

        assert facets["publishers"] == [
            {"id": 20, "name": "DC Comics", "count": 1},
            {"id": 10, "name": "Marvel", "count": 4},
        ]
        assert facets["years"] == ["2026", "2025"]

    def test_facets_respect_folder_scope(self, releases_db):
        from core.database import get_new_release_facets

        assert get_new_release_facets(allowed_prefixes=[]) == {
            "publishers": [], "years": [],
        }
        scoped = get_new_release_facets(allowed_prefixes=[MARVEL_2])
        assert scoped["publishers"] == [{"id": 10, "name": "Marvel", "count": 1}]


class TestNewReleasesWiring:
    """What ties the Home carousel, the nav and /recent together. None of it
    is visible without reading the files."""

    ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    @classmethod
    def _read(cls, *parts):
        with open(os.path.join(cls.ROOT, *parts), encoding="utf-8") as fh:
            return fh.read()

    def test_section_is_registered_and_view_all_opens_the_page(self):
        from routes.collection import DASHBOARD_SECTION_DEFS, DEFAULT_DASHBOARD_ORDER

        section = DASHBOARD_SECTION_DEFS["new_releases"]
        assert section["swiper_id"] == "newReleasesSwiper"
        assert section["view_all_type"] == "link"
        assert section["view_all_href"] == "/recent"
        assert "new_releases" in DEFAULT_DASHBOARD_ORDER

    def test_carousel_is_loaded_with_the_other_dashboard_rows(self):
        src = self._read("static", "js", "collection.js")
        assert re.search(r"^\s+loadNewReleasesSwiper\(\);$", src, re.M)
        assert "fetch('/api/new-releases?limit=10')" in src
        assert "#newReleasesSwiper .swiper-wrapper" in src

    def test_home_grid_has_no_new_releases_mode(self):
        """View All is the /recent page; a second in-grid copy must not return."""
        src = self._read("static", "js", "collection.js")
        assert "isNewReleasesMode" not in src
        assert "loadNewReleases(" not in src

    def test_page_script_uses_the_shared_pager_and_search(self):
        src = self._read("static", "js", "recent.js")
        assert "CLU.createServerPager({" in src
        assert "endpoint: '/api/new-releases/page'" in src
        assert "CLU.bindSearch(" in src
        assert "CLU.setCount(" in src
        assert "CLU.noMatches(" in src

    def test_page_script_keeps_the_reader_contract(self):
        """A page hosting reader.js publishes the read Set from the fetch
        callback and defines the marked-read hook."""
        src = self._read("static", "js", "recent.js")
        assert "window._readerOnMarkedRead = " in src
        fetch_at = src.index("fetch('/api/issues-read-paths')")
        assert src.index("window._readerReadIssuesSet = readIssues", fetch_at) > fetch_at
        assert "window._readerAllItems = " in src

    def test_badge_is_shown_for_an_unnumbered_issue(self):
        src = self._read("static", "js", "recent.js")
        assert "issue-badge-unnumbered" in src
        assert "padStart" not in src

    def test_nav_links_the_page_under_home(self):
        src = self._read("templates", "header.html")
        assert src.count("url_for('collection.new_releases_page')") == 3
