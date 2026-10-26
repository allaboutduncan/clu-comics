"""The Reading Lists grid pages in SQL: get_reading_lists_page,
get_reading_list_matching_ids and count_reading_lists.

The grid used to run the entry/read counts and a cover query for every list,
then hide all but 40 in the browser. These pin the tag filter, sort and page
on the server, and that per-card work happens for the page only.
"""
import pytest

from core.database import (
    add_reading_list_entry,
    count_reading_lists,
    create_reading_list,
    get_reading_list_matching_ids,
    get_reading_lists_page,
    mark_issue_read,
    update_reading_list_tags,
)

A, B = 1, 2


def _set_created(conn, list_id, ts):
    conn.execute("UPDATE reading_lists SET created_at = ? WHERE id = ?", (ts, list_id))
    conn.commit()


@pytest.fixture
def lists(db_connection):
    """Four lists, created a day apart, with tags:

    Alpha   2026-01-01  [dc, event]
    beta    2026-01-02  [dc]
    Gamma   2026-01-03  [marvel, event]
    Delta   2026-01-04  []
    """
    ids = {}
    for n, (name, tags) in enumerate([
        ("Alpha", ["dc", "event"]),
        ("beta", ["dc"]),
        ("Gamma", ["marvel", "event"]),
        ("Delta", []),
    ]):
        list_id = create_reading_list(name, user_id=A)
        update_reading_list_tags(list_id, tags)
        _set_created(db_connection, list_id, f"2026-01-0{n + 1} 00:00:00")
        ids[name] = list_id
    return ids


def _names(result):
    return [lst["name"] for lst in result["lists"]]


class TestSortAndFilter:

    def test_default_is_newest_first(self, lists):
        r = get_reading_lists_page()
        assert _names(r) == ["Delta", "Gamma", "beta", "Alpha"]
        assert r["total"] == 4

    @pytest.mark.parametrize("sort,expected", [
        ("date-asc", ["Alpha", "beta", "Gamma", "Delta"]),
        ("name-asc", ["Alpha", "beta", "Delta", "Gamma"]),
        ("name-desc", ["Gamma", "Delta", "beta", "Alpha"]),
        ("not-a-sort", ["Delta", "Gamma", "beta", "Alpha"]),
    ])
    def test_sorts(self, lists, sort, expected):
        """Names sort case-blind, as the browser's localeCompare did."""
        assert _names(get_reading_lists_page(sort=sort)) == expected

    def test_one_tag(self, lists):
        assert _names(get_reading_lists_page(tags=["dc"])) == ["beta", "Alpha"]

    def test_tags_are_anded(self, lists):
        assert _names(get_reading_lists_page(tags=["dc", "event"])) == ["Alpha"]
        assert get_reading_lists_page(tags=["dc", "marvel"])["total"] == 0

    def test_tags_parse_into_lists(self, lists):
        by_name = {lst["name"]: lst for lst in get_reading_lists_page()["lists"]}
        assert by_name["Alpha"]["tags"] == ["dc", "event"]
        assert by_name["Delta"]["tags"] == []

    def test_malformed_tags_column_counts_as_no_tags(self, lists, db_connection):
        """json_each() raises on invalid JSON; one bad row must not take the
        whole grid down."""
        db_connection.execute(
            "UPDATE reading_lists SET tags = 'not json' WHERE id = ?", (lists["Delta"],)
        )
        db_connection.commit()
        assert _names(get_reading_lists_page(tags=["dc"])) == ["beta", "Alpha"]
        assert get_reading_lists_page()["total"] == 4

    def test_object_shaped_tags_are_not_tags(self, lists, db_connection):
        """json_each walks an object's values; only an array is a tag list."""
        db_connection.execute(
            """UPDATE reading_lists SET tags = '{"k": "dc"}' WHERE id = ?""", (lists["Delta"],)
        )
        db_connection.commit()
        assert _names(get_reading_lists_page(tags=["dc"])) == ["beta", "Alpha"]


    def test_only_string_elements_are_tags(self, lists, db_connection):
        """The tag bar and the filter must read the column the same way, or
        a button filters to an empty grid."""
        from core.database import get_all_reading_list_tags
        db_connection.execute(
            """UPDATE reading_lists SET tags = '["solo", 2024, null]' WHERE id = ?""",
            (lists["Delta"],),
        )
        db_connection.execute(
            """UPDATE reading_lists SET tags = '{"objtag": 1}' WHERE id = ?""",
            (lists["beta"],),
        )
        db_connection.commit()
        tags = get_all_reading_list_tags()
        assert "solo" in tags and "objtag" not in tags
        assert all(isinstance(t, str) for t in tags)
        for tag in tags:
            assert get_reading_lists_page(tags=[tag])["total"] > 0
        delta = next(rl for rl in get_reading_lists_page()["lists"] if rl["name"] == "Delta")
        assert delta["tags"] == ["solo"]


class TestPaging:

    def test_limit_and_offset(self, lists):
        r = get_reading_lists_page(page=2, per_page=3)
        assert _names(r) == ["Alpha"]
        assert (r["page"], r["total_pages"], r["total"]) == (2, 2, 4)

    def test_a_page_past_the_end_shows_the_last_page(self, lists):
        r = get_reading_lists_page(page=9, per_page=3)
        assert r["page"] == 2 and _names(r) == ["Alpha"]

    def test_no_lists(self, db_connection):
        r = get_reading_lists_page()
        assert r["lists"] == [] and r["total"] == 0 and r["total_pages"] == 1

    def test_covers_are_looked_up_for_the_page_only(self, lists):
        from unittest.mock import patch
        import core.database as db

        seen = []
        real = db._reading_list_covers

        def spy(cursor, list_id, thumbnail_path):
            seen.append(list_id)
            return real(cursor, list_id, thumbnail_path)

        with patch.object(db, "_reading_list_covers", spy):
            db.get_reading_lists_page(per_page=2)
        assert seen == [lists["Delta"], lists["Gamma"]]


class TestCounts:

    def test_entry_and_read_counts_are_the_viewers(self, lists):
        path = "/data/DC/Batman/Batman 001.cbz"
        add_reading_list_entry(lists["Alpha"], {
            "series": "Batman", "issue_number": "1", "matched_file_path": path,
        })
        add_reading_list_entry(lists["Alpha"], {"series": "Batman", "issue_number": "2"})
        mark_issue_read(path, user_id=A)

        def alpha(viewer):
            r = get_reading_lists_page(viewer_id=viewer, tags=["event"], sort="date-asc")
            return r["lists"][0]

        assert alpha(A)["name"] == "Alpha"
        assert (alpha(A)["entry_count"], alpha(A)["read_count"]) == (2, 1)
        assert (alpha(B)["entry_count"], alpha(B)["read_count"]) == (2, 0)
        assert alpha(A)["covers"] == [path]

    def test_every_users_lists_are_on_the_grid(self, lists):
        create_reading_list("B's list", user_id=B)
        assert get_reading_lists_page(viewer_id=A)["total"] == 5
        assert count_reading_lists() == 5


class TestMatchingIds:

    def test_ids_span_every_page(self, lists):
        assert get_reading_list_matching_ids() == sorted(lists.values())
        assert get_reading_list_matching_ids(["event"]) == sorted(
            [lists["Alpha"], lists["Gamma"]]
        )
