"""
Integration: get_files_by_metadata / get_files_by_metadata_grouped.

These were previously untested. Covers the switch from substring LIKE to an
exact, case-insensitive, indexed match against file_metadata_tags.
"""

import pytest


def _seed(conn, name, penciller=None, publisher="Marvel", series="Silver Surfer",
          number="1", writer=None, characters=None):
    conn.execute(
        "INSERT INTO file_index (name, path, type, size, parent, has_comicinfo,"
        " ci_penciller, ci_writer, ci_characters, ci_publisher, ci_series, ci_number)"
        " VALUES (?,?,'file',1024,'/data',1,?,?,?,?,?,?)",
        (name, "/data/" + name, penciller, writer, characters,
         publisher, series, number),
    )
    conn.commit()


def _sync_all_tags(conn):
    """Mirror the production backfill order: tags first, then normalization."""
    from core.database import (
        _backfill_file_metadata_tags,
        _backfill_normalize_credits,
    )
    _backfill_file_metadata_tags(conn)
    _backfill_normalize_credits(conn)


def _all_names(result):
    names = []
    for group in result["groups"]:
        if result["nested"]:
            for s in group["series"]:
                names.extend(f["name"] for f in s["files"])
        else:
            names.extend(f["name"] for f in group["files"])
    return sorted(names)


@pytest.fixture
def library(db_connection):
    _seed(db_connection, "A1.cbz", penciller="Ron Lim [3258]")
    _seed(db_connection, "A2.cbz", penciller="Ron Lim")
    _seed(db_connection, "A3.cbz", penciller="Ron Limbaugh", series="Other")
    _seed(db_connection, "A4.cbz", penciller="Roger Stern [41502]",
          publisher="DC [10]", series="Avengers")
    _sync_all_tags(db_connection)
    return db_connection


class TestGrouped:
    def test_merges_dirty_and_clean_spellings(self, library):
        from core.database import get_files_by_metadata_grouped

        r = get_files_by_metadata_grouped("penciller", "Ron Lim")
        assert r["total"] == 2
        assert _all_names(r) == ["A1.cbz", "A2.cbz"]

    def test_stale_link_with_provider_id_still_resolves(self, library):
        from core.database import get_files_by_metadata_grouped

        r = get_files_by_metadata_grouped("penciller", "Ron Lim [3258]")
        assert r["total"] == 2
        assert _all_names(r) == ["A1.cbz", "A2.cbz"]

    def test_match_is_case_insensitive(self, library):
        """The padded LIKE this replaced was case-insensitive; keep that."""
        from core.database import get_files_by_metadata_grouped

        assert get_files_by_metadata_grouped("penciller", "ron lim")["total"] == 2
        assert get_files_by_metadata_grouped("penciller", "RON LIM")["total"] == 2

    def test_no_longer_substring_matches_a_different_person(self, library):
        """'Ron Lim' used to match 'Ron Limbaugh' via LIKE '%Ron Lim%'."""
        from core.database import get_files_by_metadata_grouped

        r = get_files_by_metadata_grouped("penciller", "Ron Limbaugh")
        assert _all_names(r) == ["A3.cbz"]
        assert "A3.cbz" not in _all_names(
            get_files_by_metadata_grouped("penciller", "Ron Lim")
        )

    def test_unknown_name_returns_empty(self, library):
        from core.database import get_files_by_metadata_grouped

        r = get_files_by_metadata_grouped("penciller", "Nobody At All")
        assert r == {"groups": [], "total": 0, "nested": True}

    def test_publisher_matches_scalar_column(self, library):
        from core.database import get_files_by_metadata_grouped

        r = get_files_by_metadata_grouped("publisher", "Marvel")
        assert r["total"] == 3
        assert r["nested"] is False
        r = get_files_by_metadata_grouped("publisher", "dc [10]")
        assert _all_names(r) == ["A4.cbz"]

    def test_nested_shape_for_writer_and_penciller(self, library):
        from core.database import get_files_by_metadata_grouped

        r = get_files_by_metadata_grouped("penciller", "Ron Lim")
        assert r["nested"] is True
        group = r["groups"][0]
        assert set(group) == {"name", "count", "series"}
        assert group["name"] == "Marvel"
        assert set(group["series"][0]) == {"name", "count", "files"}

    def test_flat_shape_for_characters_and_publisher(self, db_connection):
        from core.database import get_files_by_metadata_grouped

        _seed(db_connection, "C1.cbz", characters="Aquaman [2357]")
        _seed(db_connection, "C2.cbz", characters="Aquaman")
        _sync_all_tags(db_connection)

        r = get_files_by_metadata_grouped("characters", "Aquaman")
        assert r["nested"] is False
        assert r["total"] == 2
        assert set(r["groups"][0]) == {"name", "count", "files"}

    def test_invalid_field(self, library):
        from core.database import get_files_by_metadata_grouped

        assert get_files_by_metadata_grouped("nope", "x") == {
            "groups": [], "total": 0, "nested": False
        }

    def test_bare_provider_id_matches_nothing(self, library):
        from core.database import get_files_by_metadata_grouped

        assert get_files_by_metadata_grouped("penciller", "[3258]")["total"] == 0


class TestPaged:
    def test_totals_and_pagination(self, library):
        from core.database import get_files_by_metadata

        r = get_files_by_metadata("penciller", "Ron Lim", limit=1, offset=0)
        assert r["total"] == 2
        assert len(r["files"]) == 1

        r2 = get_files_by_metadata("penciller", "Ron Lim", limit=1, offset=1)
        assert r2["total"] == 2
        assert r2["files"][0]["name"] != r["files"][0]["name"]

    def test_stale_link_resolves(self, library):
        from core.database import get_files_by_metadata

        assert get_files_by_metadata("penciller", "Ron Lim [3258]")["total"] == 2

    def test_file_shape_unchanged(self, library):
        from core.database import get_files_by_metadata

        f = get_files_by_metadata("penciller", "Ron Lim")["files"][0]
        assert set(f) == {
            "name", "path", "size", "series", "number", "year", "publisher"
        }

    def test_like_wildcards_in_name_are_escaped(self, db_connection):
        """A '%' in a credit name must not turn into a match-everything query."""
        from core.database import get_files_by_metadata

        _seed(db_connection, "W1.cbz", penciller="Ron Lim")
        _sync_all_tags(db_connection)
        assert get_files_by_metadata("penciller", "%")["total"] == 0

    def test_invalid_field(self, library):
        from core.database import get_files_by_metadata

        assert get_files_by_metadata("nope", "x") == {"files": [], "total": 0}


class TestBackfillWindowFallback:
    def test_falls_back_to_substring_while_tags_incomplete(self, db_connection):
        """Mid-backfill the tag table is partial; browse must return a superset
        rather than a silent fraction of the library."""
        import core.database as db

        _seed(db_connection, "B1.cbz", penciller="Ron Lim [3258]")
        _seed(db_connection, "B2.cbz", penciller="Ron Lim")
        # Deliberately no tag rows — simulates the backfill still running.
        db._tags_table_complete = False

        r = db.get_files_by_metadata_grouped("penciller", "Ron Lim")
        assert r["total"] == 2, "fallback must find both spellings"

    def test_uses_exact_match_once_tags_complete(self, db_connection):
        import core.database as db

        _seed(db_connection, "B1.cbz", penciller="Ron Lim")
        _seed(db_connection, "B2.cbz", penciller="Ron Limbaugh")
        _sync_all_tags(db_connection)
        db._tags_table_complete = False  # force a fresh probe

        r = db.get_files_by_metadata_grouped("penciller", "Ron Lim")
        assert _all_names(r) == ["B1.cbz"], "exact match must exclude Ron Limbaugh"
