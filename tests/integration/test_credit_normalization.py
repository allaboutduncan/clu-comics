"""
Integration: provider-ID stripping across the ci_* write paths, the
file_metadata_tags projection, issues_read, and the one-time backfill.
"""

import pytest


def _insert_file(conn, path, name="Issue.cbz", **ci):
    cols = {
        "name": name,
        "path": path,
        "type": "file",
        "size": 1024,
        "parent": "/data",
        "has_comicinfo": 1,
    }
    cols.update(ci)
    conn.execute(
        f"INSERT INTO file_index ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        list(cols.values()),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM file_index WHERE path = ?", (path,)
    ).fetchone()[0]


def _tags(conn, path, kind):
    return sorted(
        r[0]
        for r in conn.execute(
            "SELECT value FROM file_metadata_tags WHERE file_path = ? AND kind = ?",
            (path, kind),
        )
    )


class TestWritePathsNormalize:
    def test_update_file_metadata_strips_ids(self, db_connection):
        from core.database import update_file_metadata

        fid = _insert_file(db_connection, "/data/A.cbz")
        assert update_file_metadata(
            fid,
            {
                "ci_writer": "Roger Stern [41502]",
                "ci_penciller": "Ron Lim [3258], Joe Sinnott [77]",
                "ci_characters": "Aquaman [2357]",
                "ci_publisher": "Marvel [31]",
                # Non-credit columns must survive their brackets untouched.
                "ci_number": "3 [Variant Cover]",
                "ci_series": "Silver Surfer [1987]",
            },
            123.0,
            1,
        )

        row = db_connection.execute(
            "SELECT ci_writer, ci_penciller, ci_characters, ci_publisher,"
            " ci_number, ci_series FROM file_index WHERE id = ?",
            (fid,),
        ).fetchone()
        assert row["ci_writer"] == "Roger Stern"
        assert row["ci_penciller"] == "Ron Lim, Joe Sinnott"
        assert row["ci_characters"] == "Aquaman"
        assert row["ci_publisher"] == "Marvel"
        assert row["ci_number"] == "3 [Variant Cover]"
        assert row["ci_series"] == "Silver Surfer [1987]"

    def test_update_file_metadata_syncs_clean_tags(self, db_connection):
        from core.database import update_file_metadata

        fid = _insert_file(db_connection, "/data/B.cbz")
        update_file_metadata(
            fid, {"ci_penciller": "Ron Lim [3258]"}, 123.0, 1
        )
        assert _tags(db_connection, "/data/B.cbz", "penciller") == ["Ron Lim"]

    def test_update_file_index_from_comicinfo_strips_ids(self, db_connection):
        from core.database import update_file_index_from_comicinfo

        _insert_file(db_connection, "/data/C.cbz")
        assert update_file_index_from_comicinfo(
            "/data/C.cbz",
            {
                "Writer": "Roger Stern [41502]",
                "Characters": "Aquaman [2357], Batman [uncredited]",
                "Publisher": "DC [10]",
            },
        )
        row = db_connection.execute(
            "SELECT ci_writer, ci_characters, ci_publisher FROM file_index"
            " WHERE path = ?",
            ("/data/C.cbz",),
        ).fetchone()
        assert row["ci_writer"] == "Roger Stern"
        # Non-numeric brackets are meaningful and must be preserved.
        assert row["ci_characters"] == "Aquaman, Batman [uncredited]"
        assert row["ci_publisher"] == "DC"

    def test_update_ci_field_strips_and_syncs_tags(self, db_connection):
        """Source Wall edits must land in file_metadata_tags — browse reads it."""
        from core.database import update_file_index_ci_field

        _insert_file(db_connection, "/data/D.cbz", ci_penciller="Old Name")
        assert update_file_index_ci_field(
            "/data/D.cbz", "ci_penciller", "Ron Lim [3258]"
        )

        assert db_connection.execute(
            "SELECT ci_penciller FROM file_index WHERE path = ?", ("/data/D.cbz",)
        ).fetchone()[0] == "Ron Lim"
        assert _tags(db_connection, "/data/D.cbz", "penciller") == ["Ron Lim"]

    def test_update_ci_field_replaces_stale_tags(self, db_connection):
        from core.database import update_file_index_ci_field

        _insert_file(db_connection, "/data/E.cbz")
        update_file_index_ci_field("/data/E.cbz", "ci_penciller", "First Artist")
        update_file_index_ci_field("/data/E.cbz", "ci_penciller", "Second Artist")
        assert _tags(db_connection, "/data/E.cbz", "penciller") == ["Second Artist"]

    def test_mark_issue_read_strips_ids(self, db_connection):
        from core.database import mark_issue_read

        assert mark_issue_read(
            "/data/F.cbz",
            writer="Roger Stern [41502]",
            penciller="Ron Lim [3258]",
            characters="Aquaman [2357]",
            publisher="Marvel [31]",
        )
        row = db_connection.execute(
            "SELECT writer, penciller, characters, publisher FROM issues_read"
            " WHERE issue_path = ?",
            ("/data/F.cbz",),
        ).fetchone()
        assert tuple(row) == ("Roger Stern", "Ron Lim", "Aquaman", "Marvel")


class TestLongCharacterList:
    """A real Superman issue: 23 characters, every one carrying a provider ID."""

    RAW = (
        "Adam Zeller [13042], Allie [5557], Angie [13041], Bibbo [12960], "
        "Cat Grant [8346], Dabney Donovan [13038], Guardian [7644], "
        "Gunn [13046], Halloran [13044], Jimmy Olsen [3213], "
        "Johnny Dakota [13045], Lex Luthor [41952], Lois Lane [1808], "
        "Maggie Sawyer [5379], Mr. Drysdale [13043], Noose [13037], "
        "Perry White [1809], Rough House [13035], Simone DeNiege [13028], "
        "Superman [1807], The Mob [9948], Toby Raines [71342], Torcher [13036]"
    )

    def test_every_character_is_split_and_cleaned(self, db_connection):
        from core.database import update_file_metadata

        fid = _insert_file(db_connection, "/data/superman.cbz")
        update_file_metadata(fid, {"ci_characters": self.RAW}, 1.0, 1)

        tags = _tags(db_connection, "/data/superman.cbz", "characters")
        assert len(tags) == 23
        # Spot-check the awkward ones: a period, a leading article, two words.
        for expected in ("Superman", "Lex Luthor", "Mr. Drysdale", "The Mob",
                         "Simone DeNiege", "Toby Raines"):
            assert expected in tags
        assert not any("[" in t for t in tags)

    def test_each_character_is_individually_browsable(self, db_connection):
        """Every name in the list must resolve on /browse/characters/<name>."""
        from core.database import (
            get_files_by_metadata_grouped,
            update_file_metadata,
        )

        fid = _insert_file(db_connection, "/data/superman.cbz",
                           name="Superman 001.cbz")
        update_file_metadata(fid, {"ci_characters": self.RAW}, 1.0, 1)

        for name in ("Superman", "Lois Lane", "Mr. Drysdale", "The Mob"):
            r = get_files_by_metadata_grouped("characters", name)
            assert r["total"] == 1, f"{name} should resolve to the issue"

        # And the stale link form, straight off the XML, resolves identically.
        assert get_files_by_metadata_grouped(
            "characters", "Superman [1807]")["total"] == 1

    def test_shared_character_merges_across_differing_ids(self, db_connection):
        """Two files whose taggers disagree on the ID still count as one
        character — the case that fragments Insights' Top Characters."""
        from core.database import (
            get_files_by_metadata_grouped,
            update_file_metadata,
        )

        for i, value in enumerate(
            ["Superman [1807]", "Superman", "Superman [99999]"]
        ):
            fid = _insert_file(db_connection, f"/data/s{i}.cbz", name=f"s{i}.cbz")
            update_file_metadata(fid, {"ci_characters": value}, 1.0, 1)

        assert get_files_by_metadata_grouped("characters", "Superman")["total"] == 3


class TestSplitTagValues:
    def test_strips_ids_and_dedupes_case_insensitively(self):
        from core.database import _split_tag_values

        assert _split_tag_values("Ron Lim [3258], Ron Lim, RON LIM") == ("Ron Lim",)

    def test_empty(self):
        from core.database import _split_tag_values

        assert _split_tag_values(None) == ()
        assert _split_tag_values("") == ()


class TestBackfill:
    def _seed_dirty(self, conn):
        _insert_file(
            conn, "/data/1.cbz", ci_penciller="Ron Lim [3258]",
            ci_characters="Aquaman [2357]", ci_publisher="Marvel [31]",
        )
        _insert_file(
            conn, "/data/2.cbz", ci_penciller="Ron Lim",
            ci_characters="Aquaman", ci_publisher="Marvel",
        )
        _insert_file(
            conn, "/data/3.cbz", ci_penciller="Roger Stern [41502]",
            ci_characters="Batman [uncredited]", ci_publisher="DC",
        )
        conn.execute(
            "INSERT INTO issues_read (user_id, issue_path, writer, publisher)"
            " VALUES (1, '/data/1.cbz', 'Roger Stern [41502]', 'Marvel [31]')"
        )
        conn.commit()

    def test_cleans_file_index_and_issues_read(self, db_connection):
        from core.database import _backfill_normalize_credits

        self._seed_dirty(db_connection)
        files, reads = _backfill_normalize_credits(db_connection)
        # Rows 1 and 3 are dirty; row 2 is already clean and must not be
        # rewritten (the backfill only touches rows whose values change).
        assert files == 2
        assert reads == 1

        pencillers = [
            r[0] for r in db_connection.execute(
                "SELECT ci_penciller FROM file_index ORDER BY path"
            )
        ]
        assert pencillers == ["Ron Lim", "Ron Lim", "Roger Stern"]
        assert db_connection.execute(
            "SELECT ci_characters FROM file_index WHERE path = '/data/3.cbz'"
        ).fetchone()[0] == "Batman [uncredited]"
        assert tuple(db_connection.execute(
            "SELECT writer, publisher FROM issues_read"
        ).fetchone()) == ("Roger Stern", "Marvel")

    def test_merges_two_spellings_into_one_tag(self, db_connection):
        """The WITHOUT ROWID PK on (file_path, kind, value) makes an in-place
        UPDATE of `value` collide; the backfill must delete-then-insert.

        Runs the two passes in the order _start_backfill_tags_async does —
        tags first, then normalization — since the normalize pass only
        re-syncs tags for rows it actually changed.
        """
        from core.database import (
            _backfill_file_metadata_tags,
            _backfill_normalize_credits,
        )

        self._seed_dirty(db_connection)
        _backfill_file_metadata_tags(db_connection)
        _backfill_normalize_credits(db_connection)

        rows = db_connection.execute(
            "SELECT value, COUNT(*) FROM file_metadata_tags"
            " WHERE kind = 'penciller' GROUP BY value ORDER BY value"
        ).fetchall()
        assert [tuple(r) for r in rows] == [("Roger Stern", 1), ("Ron Lim", 2)]

    def test_is_idempotent_and_sets_user_version(self, db_connection):
        from core.database import (
            CURRENT_DATA_MIGRATION_VERSION,
            _backfill_normalize_credits,
        )

        self._seed_dirty(db_connection)
        _backfill_normalize_credits(db_connection)
        assert db_connection.execute("PRAGMA user_version").fetchone()[0] == (
            CURRENT_DATA_MIGRATION_VERSION
        )
        assert _backfill_normalize_credits(db_connection) == (0, 0)

    def test_leaves_clean_library_untouched(self, db_connection):
        from core.database import _backfill_normalize_credits

        _insert_file(db_connection, "/data/clean.cbz", ci_penciller="Ron Lim")
        assert _backfill_normalize_credits(db_connection) == (0, 0)

    def test_launcher_runs_tags_before_normalization(self, db_connection):
        """Order matters: the normalize pass only re-syncs tags for rows it
        changed, so the tag backfill must populate everything else first."""
        import core.database as db

        calls = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(db, "_backfill_file_metadata_tags",
                       lambda conn, **kw: calls.append("tags"))
            mp.setattr(db, "_backfill_normalize_credits",
                       lambda conn, **kw: calls.append("normalize"))
            mp.setattr(db, "_backfill_thread", None)
            db._start_backfill_tags_async(normalize_credits=True)
            db._backfill_thread.join(timeout=10)

        assert calls == ["tags", "normalize"]

    def test_launcher_skips_normalization_when_not_needed(self, db_connection):
        import core.database as db

        calls = []
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(db, "_backfill_file_metadata_tags",
                       lambda conn, **kw: calls.append("tags"))
            mp.setattr(db, "_backfill_normalize_credits",
                       lambda conn, **kw: calls.append("normalize"))
            mp.setattr(db, "_backfill_thread", None)
            db._start_backfill_tags_async(normalize_credits=False)
            db._backfill_thread.join(timeout=10)

        assert calls == ["tags"]

    def test_empties_a_value_that_is_only_an_id(self, db_connection):
        from core.database import _backfill_normalize_credits

        _insert_file(db_connection, "/data/bare.cbz", ci_penciller="[3258]")
        _backfill_normalize_credits(db_connection)
        assert db_connection.execute(
            "SELECT ci_penciller FROM file_index WHERE path = '/data/bare.cbz'"
        ).fetchone()[0] == ""


class TestReadingTrends:
    def test_counts_merge_across_spellings(self, db_connection):
        from core.database import get_reading_trends

        for i, writer in enumerate(
            ["Roger Stern [41502]", "Roger Stern", "ROGER STERN"]
        ):
            db_connection.execute(
                "INSERT INTO issues_read (user_id, issue_path, writer)"
                " VALUES (1, ?, ?)",
                (f"/data/t{i}.cbz", writer),
            )
        db_connection.commit()

        trends = get_reading_trends("writer", user_id=1)
        assert len(trends) == 1
        assert trends[0]["name"] == "Roger Stern"
        assert trends[0]["count"] == 3
