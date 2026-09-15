"""prune_hidden_jobs: clearing the thumbnail_jobs rows the unfiltered scan left.

`app.scan_library_task` was the only library walker that enumerated by file
extension alone -- no is_hidden() on directories, no '.'/'_' guard on files.
macOS AppleDouble sidecars ("._X-Force 005 (2020).cbz", a 4KB resource fork,
not a zip) therefore got a thumbnail job each, failed with "File is not a zip
file", and -- once #548 made errored rows retryable on every startup -- were
re-queued at every boot against a two-worker executor.

The walk is fixed; this is what clears the backlog. The properties worth
pinning are the two ways an obvious implementation destroys a real library:

* matching on path *components* rather than the basename would wipe every row
  belonging to a library configured at "/mnt/_comics";
* doing the match in SQL would too -- '_' is a single-character wildcard in a
  LIKE pattern, so `path LIKE '%/_%'` matches every path containing a slash.
"""
from core.thumbnail_cache import prune_hidden_jobs


def _seed(conn, *paths, status="error"):
    conn.executemany(
        "INSERT INTO thumbnail_jobs (path, status) VALUES (?, ?)",
        [(p, status) for p in paths],
    )
    conn.commit()


def _remaining(conn):
    return {r[0] for r in conn.execute("SELECT path FROM thumbnail_jobs").fetchall()}


class TestRemovesJunk:

    def test_removes_an_appledouble_sidecar(self, db_connection):
        junk = "/data/Marvel/X-Force/v2020/._X-Force 005 (2020).cbz"
        _seed(db_connection, junk)

        assert prune_hidden_jobs(db_connection) == 1
        assert _remaining(db_connection) == set()

    def test_removes_an_underscore_prefixed_file(self, db_connection):
        _seed(db_connection, "/data/DC/_scratch.cbz")

        assert prune_hidden_jobs(db_connection) == 1
        assert _remaining(db_connection) == set()

    def test_removes_a_dotfile(self, db_connection):
        _seed(db_connection, "/data/DC/.DS_Store.cbz")

        assert prune_hidden_jobs(db_connection) == 1

    def test_removes_every_junk_row_in_one_pass(self, db_connection):
        paths = [f"/data/DC/._Batman {i:03d}.cbz" for i in range(25)]
        _seed(db_connection, *paths)

        assert prune_hidden_jobs(db_connection) == 25
        assert _remaining(db_connection) == set()


class TestLeavesRealComicsAlone:

    def test_keeps_an_ordinary_comic(self, db_connection):
        keep = "/data/DC/Batman 001.cbz"
        _seed(db_connection, keep, status="completed")

        assert prune_hidden_jobs(db_connection) == 0
        assert _remaining(db_connection) == {keep}

    def test_keeps_a_library_root_whose_own_name_starts_with_underscore(
        self, db_connection
    ):
        """The sharpest edge in the change. build_file_index walks *from* a
        library root and never tests the root itself, so /mnt/_comics is
        indexed normally -- a component-wise match here would empty its table.
        """
        keep = "/mnt/_comics/DC/Batman 001.cbz"
        _seed(db_connection, keep, status="completed")

        assert prune_hidden_jobs(db_connection) == 0
        assert _remaining(db_connection) == {keep}

    def test_keeps_a_comic_under_a_hidden_intermediate_directory(self, db_connection):
        """Same rule. The fixed walk is what stops new rows appearing here;
        deleting the existing ones is not this function's call to make."""
        keep = "/data/_incoming/Batman 001.cbz"
        _seed(db_connection, keep)

        assert prune_hidden_jobs(db_connection) == 0
        assert _remaining(db_connection) == {keep}

    def test_a_dotted_directory_component_is_not_enough(self, db_connection):
        keep = "/data/.staging/Batman 001.cbz"
        _seed(db_connection, keep)

        assert prune_hidden_jobs(db_connection) == 0

    def test_separates_junk_from_its_real_neighbours(self, db_connection):
        keep = "/data/Marvel/X-Force/v2020/X-Force 005 (2020).cbz"
        junk = "/data/Marvel/X-Force/v2020/._X-Force 005 (2020).cbz"
        _seed(db_connection, keep, junk)

        assert prune_hidden_jobs(db_connection) == 1
        assert _remaining(db_connection) == {keep}


class TestNeverRaises:
    """scan_library_task calls this inside the try that reports DB corruption;
    it must not become a new way for the scan to die."""

    def test_returns_zero_on_an_empty_table(self, db_connection):
        assert prune_hidden_jobs(db_connection) == 0

    def test_returns_zero_without_a_database(self, monkeypatch):
        monkeypatch.setattr(
            "core.database.get_db_connection", lambda *a, **kw: None, raising=False
        )
        assert prune_hidden_jobs() == 0

    def test_swallows_a_broken_connection(self):
        class _Broken:
            def execute(self, *a, **kw):
                raise RuntimeError("database disk image is malformed")

        assert prune_hidden_jobs(_Broken()) == 0

    def test_leaves_a_caller_supplied_connection_open(self, db_connection):
        """scan_library_task keeps using the connection it passes in."""
        _seed(db_connection, "/data/DC/._x.cbz")

        prune_hidden_jobs(db_connection)

        assert db_connection.execute("SELECT 1").fetchone()[0] == 1
