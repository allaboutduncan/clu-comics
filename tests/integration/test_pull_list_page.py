"""The Pull List pages in SQL: get_pull_list_page, get_pull_list_matching_ids
and get_pull_list_facets.

The page used to load every mapped series and every issue count, render them
all, and hide all but 50 in the browser. These pin that the filters, sort and
LIMIT run on the server and give the same answers the browser used to.
"""
import pytest

from tests.factories.db_factories import create_publisher, create_series, create_issue

TODAY = "2026-01-01"


def _mark_found(series_id, issue_id, issue_number, found):
    from core.database import save_collection_status_bulk
    save_collection_status_bulk([{
        "series_id": series_id,
        "issue_id": issue_id,
        "issue_number": str(issue_number),
        "found": 1 if found else 0,
        "file_path": "/data/x.cbz" if found else None,
        "file_mtime": 0.0,
        "matched_via": "filename",
    }])


@pytest.fixture
def library(db_connection):
    """Five mapped series, one of each colour, across two publishers.

    complete:    Batman        (DC)     1 issue, found
    missing:     Aquaman       (DC)     2 issues, 1 found, 1 past missing
    upcoming:    Spider-Man    (Marvel) 1 found, 1 future missing
    unmonitored: X-Men         (Marvel) monitored off
    unknown:     2000 AD       (Marvel) no issues
    """
    from core.database import set_series_monitored

    dc = create_publisher(publisher_id=10, name="DC Comics")
    marvel = create_publisher(publisher_id=20, name="Marvel")

    create_series(series_id=1, name="Batman", publisher_id=dc)
    create_issue(issue_id=11, series_id=1, number="1", store_date="2020-01-01")
    _mark_found(1, 11, 1, True)

    create_series(series_id=2, name="Aquaman", publisher_id=dc)
    create_issue(issue_id=21, series_id=2, number="1", store_date="2020-01-01")
    create_issue(issue_id=22, series_id=2, number="2", store_date="2020-02-01")
    _mark_found(2, 21, 1, True)
    _mark_found(2, 22, 2, False)

    create_series(series_id=3, name="Spider-Man", publisher_id=marvel)
    create_issue(issue_id=31, series_id=3, number="1", store_date="2020-01-01")
    create_issue(issue_id=32, series_id=3, number="2", store_date="2099-01-01")
    _mark_found(3, 31, 1, True)

    create_series(series_id=4, name="X-Men", publisher_id=marvel)
    create_issue(issue_id=41, series_id=4, number="1", store_date="2020-01-01")
    set_series_monitored(4, False)

    create_series(series_id=5, name="2000 AD", publisher_id=marvel)
    return {"dc": dc, "marvel": marvel}


def _page(**kw):
    from core.database import get_pull_list_page
    return get_pull_list_page(TODAY, **kw)


def _names(result):
    return [r["name"] for r in result["rows"]]


class TestFiltersAndSort:

    def test_default_is_every_series_by_name(self, library):
        r = _page()
        assert r["total"] == 5
        assert _names(r) == ["2000 AD", "Aquaman", "Batman", "Spider-Man", "X-Men"]

    def test_row_carries_status_and_counts(self, library):
        rows = {r["id"]: r for r in _page()["rows"]}
        assert rows[1]["row_status"] == "complete"
        assert rows[2]["row_status"] == "missing" and rows[2]["missing_count"] == 1
        assert rows[3]["row_status"] == "upcoming" and rows[3]["missing_future"] == 1
        assert rows[4]["row_status"] == "unmonitored"
        assert rows[5]["row_status"] == "unknown"
        assert rows[1]["publisher_name"] == "DC Comics"

    @pytest.mark.parametrize("status,expected", [
        ("complete", ["Batman"]),
        ("missing", ["Aquaman"]),
        ("upcoming", ["Spider-Man"]),
        ("unmonitored", ["X-Men"]),
    ])
    def test_status_filter_agrees_with_the_row_colour(self, library, status, expected):
        """A row filtered as "missing" must also render as missing (both
        select _PULL_LIST_ROW_STATUS_SQL)."""
        r = _page(status=status)
        assert _names(r) == expected
        assert all(row["row_status"] == status for row in r["rows"])

    def test_unknown_status_value_is_ignored(self, library):
        assert _page(status="bogus")["total"] == 5

    def test_search_is_a_case_blind_substring(self, library):
        assert _names(_page(q="MAN")) == ["Aquaman", "Batman", "Spider-Man"]

    def test_search_folds_non_ascii_case(self, library):
        """SQLite's LIKE folds ASCII only; the old in-browser search used
        toLowerCase(), so "élite" found "Élite"."""
        create_series(series_id=6, name="Élite Strike Force")
        create_series(series_id=7, name="STRASSE")
        assert _names(_page(q="élite")) == ["Élite Strike Force"]
        assert _names(_page(q="ÉLITE")) == ["Élite Strike Force"]
        assert _names(_page(q="straße")) == ["STRASSE"]

    def test_search_treats_like_wildcards_literally(self, library):
        assert _page(q="%")["total"] == 0
        assert _page(q="_")["total"] == 0

    def test_publisher_filter_is_by_id(self, library):
        assert _names(_page(publisher_id=library["dc"])) == ["Aquaman", "Batman"]

    def test_letter_filter(self, library):
        assert _names(_page(letter="b")) == ["Batman"]
        assert _names(_page(letter="#")) == ["2000 AD"]

    def test_a_non_ascii_letter_is_the_hash_bucket(self, library):
        """The bar shows É under "#"; a hand-made ?letter=É must not turn
        the filter off."""
        assert _names(_page(letter="É")) == ["2000 AD"]

    def test_sort_by_collection_reads_the_whole_library_counts(self, library):
        r = _page(sort="collection", descending=True)
        # Aquaman, Spider-Man and X-Men each have one missing issue; ties by name.
        assert _names(r)[:3] == ["Aquaman", "Spider-Man", "X-Men"]

    def test_sort_descending_by_name(self, library):
        assert _names(_page(sort="name", descending=True))[0] == "X-Men"

    def test_filters_combine(self, library):
        r = _page(publisher_id=library["marvel"], q="men")
        assert _names(r) == ["X-Men"]


class TestPaging:

    def test_limit_and_offset(self, library):
        r = _page(page=2, per_page=2)
        assert _names(r) == ["Batman", "Spider-Man"]
        assert r["total"] == 5 and r["total_pages"] == 3 and r["page"] == 2

    def test_a_page_past_the_end_shows_the_last_page(self, library):
        r = _page(page=99, per_page=2)
        assert r["page"] == 3 and _names(r) == ["X-Men"]

    def test_a_page_past_the_end_of_a_status_filter(self, library):
        r = _page(status="missing", page=5, per_page=2)
        assert r["page"] == 1 and _names(r) == ["Aquaman"]

    @pytest.mark.parametrize("status", ["", "missing"])
    def test_a_huge_page_number_is_the_last_page(self, library, status):
        """sqlite3 binds int64 only: an unclamped OFFSET raised OverflowError
        and the page said "No series match"."""
        r = _page(status=status, page=10 ** 20, per_page=2)
        assert r["error"] is None
        assert r["page"] == r["total_pages"] and r["rows"]

    def test_no_match_is_page_one_of_one(self, library):
        r = _page(q="nothing like this", page=4)
        assert r == {"rows": [], "total": 0, "page": 1, "per_page": 50,
                     "total_pages": 1, "error": None}

    @pytest.mark.parametrize("kw", [
        {"per_page": 2},
        {"status": "missing", "page": 99, "per_page": 2},
        {"sort": "collection", "page": 99, "per_page": 2},
    ])
    def test_the_whole_library_aggregate_runs_at_most_once(self, library, kw):
        """It was the 2.8 s this page set out to remove, and a stale ?page=
        under a status filter ran it three times. Only the page's own series
        may be aggregated a second time."""
        from unittest.mock import patch
        import core.database as db

        calls = []
        real = db._pull_list_counts_sql

        def spy(today, series_ids=None):
            calls.append(None if series_ids is None else list(series_ids))
            return real(today, series_ids)

        with patch.object(db, "_pull_list_counts_sql", spy):
            r = db.get_pull_list_page(TODAY, **kw)
        whole = [c for c in calls if c is None]
        expected = 1 if (kw.get("status") or kw.get("sort")) else 0
        assert len(whole) == expected
        assert [c for c in calls if c is not None] == [[row["id"] for row in r["rows"]]]

    def test_a_database_error_is_reported_not_shown_as_no_match(self, library):
        from unittest.mock import patch
        import core.database as db

        with patch.object(db, "_PULL_LIST_ROW_STATUS_SQL", "no_such_column"):
            r = db.get_pull_list_page(TODAY)
        assert r["rows"] == [] and r["error"]

    def test_unmapped_series_are_not_listed(self, library, db_connection):
        db_connection.execute("UPDATE series SET mapped_path = NULL WHERE id = 1")
        db_connection.commit()
        assert "Batman" not in _names(_page())


class TestCollectionCountsScope:

    def test_series_ids_narrows_the_aggregate(self, library):
        from core.database import get_pull_list_collection_counts
        assert set(get_pull_list_collection_counts(TODAY, series_ids=[2, 3])) == {2, 3}
        assert get_pull_list_collection_counts(TODAY, series_ids=[]) == {}
        assert set(get_pull_list_collection_counts(TODAY)) == {1, 2, 3, 4}


class TestMatchingIdsAndFacets:

    def test_ids_span_every_page(self, library):
        from core.database import get_pull_list_matching_ids
        assert get_pull_list_matching_ids(TODAY) == [1, 2, 3, 4, 5]
        assert get_pull_list_matching_ids(TODAY, publisher_id=library["dc"]) == [1, 2]
        assert get_pull_list_matching_ids(TODAY, status="upcoming") == [3]

    def test_ids_on_a_database_error_are_none_not_empty(self, library):
        from unittest.mock import patch
        import core.database as db

        with patch.object(db, "_PULL_LIST_ROW_STATUS_SQL", "no_such_column"):
            assert db.get_pull_list_matching_ids(TODAY, status="missing") is None


class TestCountsSqlHelper:

    def test_placeholders_match_params(self):
        from core.database import _pull_list_counts_sql
        for ids in (None, [], [1, 2, 3]):
            sql, params = _pull_list_counts_sql(TODAY, ids)
            assert sql.count("?") == len(params)
            assert params[:2] == [TODAY, TODAY]

    def test_facets(self, library):
        from core.database import get_pull_list_facets
        f = get_pull_list_facets()
        assert f["total"] == 5
        assert f["letters"] == ["#", "A", "B", "S", "X"]
        assert f["publishers"] == [
            {"id": library["dc"], "name": "DC Comics"},
            {"id": library["marvel"], "name": "Marvel"},
        ]

    def test_facets_skip_publishers_with_no_mapped_series(self, library):
        from core.database import get_pull_list_facets
        create_publisher(publisher_id=30, name="Image")
        assert [p["name"] for p in get_pull_list_facets()["publishers"]] == ["DC Comics", "Marvel"]
