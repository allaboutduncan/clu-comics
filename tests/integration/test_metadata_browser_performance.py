"""
Integration tests for the metadata browser performance layer:

* file_metadata_tags population by the real write paths
  (update_file_metadata, update_file_index_from_comicinfo)
* Backfill at init_db time when tags are missing
* Cache invalidation on add/delete/update
"""
import time


def _count_tags(conn, **filters):
    where = " AND ".join(f"{k} = ?" for k in filters)
    params = list(filters.values())
    c = conn.cursor()
    sql = "SELECT COUNT(*) FROM file_metadata_tags"
    if where:
        sql += " WHERE " + where
    c.execute(sql, params)
    return c.fetchone()[0]


def test_update_file_metadata_populates_tags(db_connection):
    from core.database import add_file_index_entry, update_file_metadata
    add_file_index_entry(
        name="x.cbz", path="/data/x.cbz", entry_type="file",
        size=1, parent="/data",
    )
    c = db_connection.cursor()
    c.execute("SELECT id FROM file_index WHERE path = ?", ("/data/x.cbz",))
    file_id = c.fetchone()[0]

    update_file_metadata(file_id, {
        "ci_title": "X", "ci_series": "S", "ci_number": "1",
        "ci_count": "", "ci_volume": "", "ci_year": "2020",
        "ci_writer": "Alice, Bob",
        "ci_penciller": "", "ci_inker": "", "ci_colorist": "",
        "ci_letterer": "", "ci_coverartist": "",
        "ci_publisher": "P",
        "ci_genre": "Action, Drama",
        "ci_characters": "Hero, Sidekick",
    }, scanned_at=time.time(), has_comicinfo=1)

    assert _count_tags(db_connection, file_path="/data/x.cbz", kind="writer") == 2
    assert _count_tags(db_connection, file_path="/data/x.cbz",
                       kind="characters", value="Hero") == 1
    assert _count_tags(db_connection, file_path="/data/x.cbz",
                       kind="genre", value="Drama") == 1


def test_update_file_metadata_replaces_stale_tags(db_connection):
    """Re-scanning a file should remove tags that are no longer in the new metadata."""
    from core.database import add_file_index_entry, update_file_metadata
    add_file_index_entry(
        name="y.cbz", path="/data/y.cbz", entry_type="file",
        size=1, parent="/data",
    )
    c = db_connection.cursor()
    c.execute("SELECT id FROM file_index WHERE path = ?", ("/data/y.cbz",))
    file_id = c.fetchone()[0]

    base = {k: "" for k in [
        "ci_title", "ci_series", "ci_number", "ci_count", "ci_volume", "ci_year",
        "ci_writer", "ci_penciller", "ci_inker", "ci_colorist",
        "ci_letterer", "ci_coverartist", "ci_publisher", "ci_genre",
        "ci_characters",
    ]}
    first = dict(base, ci_writer="Alice, Bob")
    update_file_metadata(file_id, first, scanned_at=1.0, has_comicinfo=1)
    assert _count_tags(db_connection, file_path="/data/y.cbz", kind="writer") == 2

    # Rescan with only Alice — Bob should disappear
    second = dict(base, ci_writer="Alice")
    update_file_metadata(file_id, second, scanned_at=2.0, has_comicinfo=1)
    c = db_connection.cursor()
    c.execute(
        "SELECT value FROM file_metadata_tags WHERE file_path = ? AND kind = 'writer'",
        ("/data/y.cbz",),
    )
    values = {r[0] for r in c.fetchall()}
    assert values == {"Alice"}


def test_delete_file_index_entry_removes_tags(db_connection):
    from core.database import (
        add_file_index_entry, update_file_metadata, delete_file_index_entry,
    )
    add_file_index_entry(
        name="z.cbz", path="/data/z.cbz", entry_type="file",
        size=1, parent="/data",
    )
    c = db_connection.cursor()
    c.execute("SELECT id FROM file_index WHERE path = ?", ("/data/z.cbz",))
    file_id = c.fetchone()[0]
    base = {k: "" for k in [
        "ci_title", "ci_series", "ci_number", "ci_count", "ci_volume", "ci_year",
        "ci_writer", "ci_penciller", "ci_inker", "ci_colorist",
        "ci_letterer", "ci_coverartist", "ci_publisher", "ci_genre",
        "ci_characters",
    ]}
    update_file_metadata(file_id, dict(base, ci_characters="Hero"),
                         scanned_at=1.0, has_comicinfo=1)
    assert _count_tags(db_connection, file_path="/data/z.cbz") == 1

    delete_file_index_entry("/data/z.cbz")
    assert _count_tags(db_connection, file_path="/data/z.cbz") == 0


def test_delete_directory_removes_descendant_tags(db_connection):
    from core.database import (
        add_file_index_entry, update_file_metadata, delete_file_index_entry,
    )
    add_file_index_entry(name="Dir", path="/data/Dir", entry_type="directory",
                         parent="/data")
    add_file_index_entry(name="a.cbz", path="/data/Dir/a.cbz",
                         entry_type="file", size=1, parent="/data/Dir")
    c = db_connection.cursor()
    c.execute("SELECT id FROM file_index WHERE path = ?", ("/data/Dir/a.cbz",))
    fid = c.fetchone()[0]
    base = {k: "" for k in [
        "ci_title", "ci_series", "ci_number", "ci_count", "ci_volume", "ci_year",
        "ci_writer", "ci_penciller", "ci_inker", "ci_colorist",
        "ci_letterer", "ci_coverartist", "ci_publisher", "ci_genre",
        "ci_characters",
    ]}
    update_file_metadata(fid, dict(base, ci_writer="Writer"),
                         scanned_at=1.0, has_comicinfo=1)
    assert _count_tags(db_connection, file_path="/data/Dir/a.cbz") == 1

    delete_file_index_entry("/data/Dir")
    assert _count_tags(db_connection, file_path="/data/Dir/a.cbz") == 0


def test_backfill_runs_when_tags_empty_but_ci_columns_present(db_connection, db_path):
    """init_db should kick off a background backfill from existing ci_* data."""
    # Seed file_index with comma-separated ci_* data directly
    c = db_connection.cursor()
    c.execute(
        "INSERT INTO file_index(name, path, type, size, parent,"
        " ci_writer, ci_characters, ci_genre)"
        " VALUES (?, ?, 'file', 1, '/data', ?, ?, ?)",
        ("bf.cbz", "/data/bf.cbz", "Alice, Bob", "Hero", "Action"),
    )
    db_connection.commit()
    # Clear any tags that might have been added by the write path
    c.execute("DELETE FROM file_metadata_tags")
    db_connection.commit()
    assert _count_tags(db_connection) == 0

    # Re-run init_db; it should spawn a background backfill thread.
    from unittest.mock import patch
    import core.database as database_mod
    with patch("core.database.get_db_path", return_value=db_path):
        database_mod.init_db()

        # Wait for the background thread (short timeout — only one file).
        t = database_mod._backfill_thread
        if t is not None:
            t.join(timeout=5.0)

    # Reconnect via the patched path to observe the backfill
    with patch("core.database.get_db_path", return_value=db_path):
        from core.database import get_db_connection
        fresh = get_db_connection()
        try:
            assert _count_tags(fresh, file_path="/data/bf.cbz", kind="writer") == 2
            assert _count_tags(fresh, file_path="/data/bf.cbz",
                               kind="characters", value="Hero") == 1
        finally:
            fresh.close()


def test_backfill_not_started_when_nothing_to_do(db_connection, db_path):
    """A clean DB with no ci_* data should not spawn a backfill thread."""
    from unittest.mock import patch
    import core.database as database_mod
    database_mod._backfill_thread = None
    with patch("core.database.get_db_path", return_value=db_path):
        database_mod.init_db()
    assert database_mod._backfill_thread is None


def test_cache_invalidates_on_add(db_connection):
    """Adding a file_index row should invalidate the metadata browser cache."""
    from core.database import (
        metadata_browse, add_file_index_entry, update_file_metadata,
    )

    add_file_index_entry(
        name="a.cbz", path="/data/a.cbz", entry_type="file",
        size=1, parent="/data",
    )
    c = db_connection.cursor()
    c.execute("SELECT id FROM file_index WHERE path = ?", ("/data/a.cbz",))
    fid = c.fetchone()[0]
    base = {k: "" for k in [
        "ci_title", "ci_series", "ci_number", "ci_count", "ci_volume", "ci_year",
        "ci_writer", "ci_penciller", "ci_inker", "ci_colorist",
        "ci_letterer", "ci_coverartist", "ci_publisher", "ci_genre",
        "ci_characters",
    ]}
    update_file_metadata(fid, dict(base, ci_publisher="P1", ci_series="S1"),
                         scanned_at=1.0, has_comicinfo=1)

    first = metadata_browse("publisher", {})
    assert {i["name"] for i in first["items"]} == {"P1"}

    # Add a second publisher — cache must be invalidated
    add_file_index_entry(
        name="b.cbz", path="/data/b.cbz", entry_type="file",
        size=1, parent="/data",
    )
    c.execute("SELECT id FROM file_index WHERE path = ?", ("/data/b.cbz",))
    fid2 = c.fetchone()[0]
    update_file_metadata(fid2, dict(base, ci_publisher="P2", ci_series="S2"),
                         scanned_at=2.0, has_comicinfo=1)

    second = metadata_browse("publisher", {})
    assert {i["name"] for i in second["items"]} == {"P1", "P2"}
