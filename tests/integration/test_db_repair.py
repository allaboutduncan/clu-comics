"""Salvage: produce a candidate, show the loss, then swap on request.

Salvage always loses rows. The point of the two-phase design is that the user
sees how many, per table, *before* their library database is replaced -- so the
diff is as much the feature as the recovery is, and it is tested as such.
"""
import os
import sqlite3

import pytest

from core import database, db_repair


def _build(path, rows=4000):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA page_size=4096")
    conn.execute("CREATE TABLE file_index (id INTEGER PRIMARY KEY, path TEXT)")
    conn.execute("CREATE TABLE issues_read (id INTEGER PRIMARY KEY, note TEXT)")
    conn.executemany(
        "INSERT INTO file_index (path) VALUES (?)",
        [(f"/data/x/{i}-" + "y" * 60,) for i in range(rows)],
    )
    conn.executemany(
        "INSERT INTO issues_read (note) VALUES (?)",
        [(f"read-{i}",) for i in range(25)],
    )
    conn.commit()
    conn.close()


def _corrupt_leaf(path):
    pages = os.path.getsize(path) // 4096
    with open(path, "r+b") as f:
        f.seek((pages // 2) * 4096)
        f.write(b"\x99" * 4096)


@pytest.fixture
def broken_db(tmp_path, monkeypatch):
    path = str(tmp_path / "comic_utils.db")
    _build(path)
    _corrupt_leaf(path)
    monkeypatch.setattr(database, "get_db_path", lambda: path)
    yield path
    db_repair.discard_candidate()


class TestSalvage:
    def test_produces_a_clean_candidate_with_a_diff(self, broken_db):
        # Sanity: the source really is damaged.
        ok, _ = database.check_integrity(broken_db)
        assert ok is False

        result = db_repair.start_salvage()
        assert result["success"] is True, result.get("error")

        candidate = result["candidate"]
        assert candidate["integrity_ok"] is True, candidate["integrity_message"]
        assert os.path.exists(candidate["path"])
        assert candidate["path"] != broken_db, "must never touch the original"

        diff = candidate["diff"]
        rows = {row["name"]: row for row in diff["tables"]}
        assert {"file_index", "issues_read"} <= set(rows)
        assert diff["total_after"] > 0

        # issues_read was readable on both sides and survived intact.
        assert rows["issues_read"]["before"] == 25
        assert rows["issues_read"]["delta"] == 0

        # file_index is the damaged table: SELECT COUNT(*) fails on it, so its
        # "before" is genuinely unknown. The diff must say so rather than
        # implying nothing was lost -- roughly 480 rows really are gone here,
        # and a naive total-vs-total would have reported a loss of zero.
        assert rows["file_index"]["before"] is None
        assert rows["file_index"]["delta"] is None
        assert "file_index" in diff["unknown_before"]
        assert rows["file_index"]["after"] > 0

    def test_original_is_untouched(self, broken_db):
        before = open(broken_db, "rb").read()
        db_repair.start_salvage()
        assert open(broken_db, "rb").read() == before

    def test_apply_swaps_it_in_and_snapshots_the_old_one(self, broken_db):
        result = db_repair.start_salvage()
        candidate = result["candidate"]

        applied = db_repair.apply_candidate(candidate["token"])
        assert applied["success"] is True
        assert applied["requires_restart"] is True
        # backup_database quarantines rather than rotating when the source is
        # corrupt, so the snapshot is a comic_utils_corrupt_*.zip here.
        assert applied["pre_swap_backup"]

        ok, message = database.check_integrity(broken_db, quick=False)
        assert ok, f"the live database should now be clean: {message}"

        conn = sqlite3.connect(broken_db)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM file_index"
            ).fetchone()[0] > 0
            assert conn.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0].lower() == "wal"
        finally:
            conn.close()

        # The candidate is consumed.
        assert db_repair.get_candidate() is None

    def test_apply_rejects_a_stale_token(self, broken_db):
        db_repair.start_salvage()
        with pytest.raises(RuntimeError, match="changed since it was inspected"):
            db_repair.apply_candidate("not-the-right-token")

    def test_apply_without_a_candidate(self, broken_db):
        with pytest.raises(FileNotFoundError):
            db_repair.apply_candidate("anything")

    def test_discard_removes_the_file(self, broken_db):
        result = db_repair.start_salvage()
        path = result["candidate"]["path"]
        assert os.path.exists(path)
        db_repair.discard_candidate()
        assert not os.path.exists(path)
        assert db_repair.get_candidate() is None

    def test_refuses_without_free_space(self, broken_db, monkeypatch):
        class _Usage:
            free = 1

        monkeypatch.setattr("shutil.disk_usage", lambda p: _Usage())
        result = db_repair.start_salvage()
        assert result["success"] is False
        assert "free space" in result["error"].lower()


class TestDiff:
    def test_counts_and_losses(self):
        diff = db_repair._diff_counts(
            {"a": 100, "b": 50, "c": 10},
            {"a": 97, "b": 50},
        )
        rows = {r["name"]: r for r in diff["tables"]}
        assert rows["a"]["delta"] == -3
        assert rows["b"]["delta"] == 0
        assert rows["c"]["missing"] is True
        assert diff["rows_lost"] == 3, (
            "only comparable tables count: c's rows are lost but its own "
            "before/after pair is what tables_lost reports"
        )
        assert diff["tables_lost"] == ["c"]
        assert diff["unknown_before"] == []

    def test_unreadable_before_is_not_zero(self):
        """A table that could not be counted in the damaged database is
        unknown, not empty -- reporting it as 0 would show a recovery as a
        gain."""
        diff = db_repair._diff_counts({"a": "ERR(malformed)"}, {"a": 5})
        row = diff["tables"][0]
        assert row["before"] is None
        assert row["before_error"] is True
        assert row["delta"] is None
        assert diff["unknown_before"] == ["a"]
        # Never reported as a gain, and never as a confident zero loss.
        assert diff["rows_lost"] == 0


class TestRescueScriptStaysStandalone:
    """tools/repair_db.py must keep working when the app will not start.

    That is its entire reason to exist: it is run by `docker exec` against a
    database too damaged for CLU to open. Importing anything from core/ would
    pull in the config parser, the logger and a /config/config.ini read, and
    destroy that property -- so the dependency points core -> tools, never back.
    """

    def test_has_no_project_imports(self):
        import ast

        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        source = open(os.path.join(root, "tools", "repair_db.py"),
                      encoding="utf-8").read()
        banned = {"core", "routes", "models", "helpers", "app", "api",
                  "flask", "cbz_ops"}
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert name.split(".")[0] not in banned, (
                    f"tools/repair_db.py must stay standalone; it imports {name}"
                )

    def test_log_sink_defaults_to_print(self):
        import inspect

        from tools import repair_db

        for fn in (repair_db.recover_cli, repair_db.recover_python,
                   repair_db._salvage_table):
            assert inspect.signature(fn).parameters["log"].default is print
