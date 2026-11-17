"""``thumbnail_jobs`` rows are written by ``core/thumbnail_cache.py`` alone.

``set_job_status`` is the only correct way to write one, because it is what
stamps ``file_mtime``. ``/api/thumbnail`` kept a raw
``INSERT OR REPLACE INTO thumbnail_jobs (path, status)`` for its 'processing'
row: that leaves ``file_mtime`` NULL *and*, since ``OR REPLACE`` deletes the old
row first, wipes a value already there. ``scan_library_task`` reads NULL as
"migrated, mtime unknown" and re-queues. The job's own terminal write usually
re-stamped it, which hid the defect; a job that never reached that write (a
restart with the queue full, a terminal write that hit a locked database) left
the NULL behind.

The ratchet is structural -- app.py cannot be imported in tests -- and scans
the SQL string literals of every non-test module. ``ALLOWED`` is every write
still permitted outside the owner; it may only shrink.
"""

import ast
import os
import re
import time

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OWNER = "core/thumbnail_cache.py"

# (module, enclosing function) -> number of writes still allowed there.
ALLOWED = {
    # The startup scan's batched 'processing' upsert. Not the defect: it is an
    # ON CONFLICT DO UPDATE that stamps file_mtime itself (asserted below). It
    # runs on the scan's own connection inside a per-directory transaction, so
    # routing it through set_job_status -- which opens a second connection --
    # would wait out the 30s busy timeout behind the scan's own write lock.
    ("app.py", "scan_library_task"): 1,
}

_PRUNE_DIRS = {"tests", "node_modules", "venv", "env", "build", "dist",
               "site-packages", "__pycache__"}

_WRITE = re.compile(
    r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+\w+)?|DELETE\s+FROM)"
    r"\s+[\"'`\[]?thumbnail_jobs\b",
    re.IGNORECASE,
)


def _modules():
    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and d not in _PRUNE_DIRS
        )
        for name in sorted(files):
            if name.endswith(".py"):
                path = os.path.join(root, name)
                yield os.path.relpath(path, PROJECT_ROOT).replace(os.sep, "/"), path


def _string_value(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            v.value for v in node.values
            if isinstance(v, ast.Constant) and isinstance(v.value, str)
        )
    return None


def _writes(path):
    """Yield (enclosing function name, sql) for every thumbnail_jobs write."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)

    def visit(node, func):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node.name
        if isinstance(node, ast.JoinedStr):
            text = _string_value(node)
            if text and _WRITE.search(text):
                yield func, text
            return  # its parts are not separate strings
        text = _string_value(node)
        if text and _WRITE.search(text):
            yield func, text
        for child in ast.iter_child_nodes(node):
            yield from visit(child, func)

    yield from visit(tree, None)


def _all_writes():
    found = {}
    for rel, path in _modules():
        if rel == OWNER:
            continue
        for func, sql in _writes(path):
            found.setdefault((rel, func), []).append(sql)
    return found


class TestOnlyTheOwnerWritesJobRows:

    def test_the_scan_sees_the_owner(self):
        """Guard against a scan that silently checks nothing."""
        owner = os.path.join(PROJECT_ROOT, *OWNER.split("/"))
        assert any(True for _ in _writes(owner)), (
            "the pattern no longer recognises the owner's own writes"
        )
        assert ("app.py", "scan_library_task") in _all_writes()

    def test_no_new_raw_writes(self):
        offenders = {
            key: len(sqls) for key, sqls in _all_writes().items()
            if len(sqls) > ALLOWED.get(key, 0)
        }
        assert not offenders, (
            "thumbnail_jobs is written outside core/thumbnail_cache.py: "
            f"{offenders}. Use set_job_status -- it stamps file_mtime; a raw "
            "(path, status) write leaves it NULL and the startup scan re-queues."
        )

    def test_the_allow_list_only_shrinks(self):
        found = _all_writes()
        stale = {k: v for k, v in ALLOWED.items() if len(found.get(k, [])) < v}
        assert not stale, f"lower ALLOWED to match the code: {stale}"

    def test_the_allowed_write_stamps_file_mtime(self):
        for sql in _all_writes()[("app.py", "scan_library_task")]:
            assert "file_mtime" in sql and "excluded.file_mtime" in sql, (
                "the scan's upsert no longer stamps file_mtime"
            )
            assert "OR REPLACE" not in sql.upper()

    def test_the_serving_route_goes_through_the_owner(self):
        path = os.path.join(PROJECT_ROOT, "app.py")
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        fn = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef) and n.name == "get_thumbnail"
        )
        calls = [
            c for c in ast.walk(fn)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == "set_job_status"
        ]
        assert any(
            len(c.args) == 2 and isinstance(c.args[1], ast.Constant)
            and c.args[1].value == "processing"
            for c in calls
        ), "get_thumbnail no longer records 'processing' through set_job_status"

        # The row is recorded before the job is queued, so a fast worker's
        # 'completed' is never overwritten by it.
        submit = next(
            c for c in ast.walk(fn)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
            and c.func.id == "submit_thumbnail_job"
        )
        assert min(c.lineno for c in calls) < submit.lineno


class TestSetJobStatusStampsTheMtime:
    """The behaviour the ratchet protects, at the level tests can reach."""

    def _row(self, conn, path):
        return conn.execute(
            "SELECT status, file_mtime FROM thumbnail_jobs WHERE path = ?", (path,)
        ).fetchone()

    def test_processing_row_carries_the_files_mtime(self, db_connection, tmp_path):
        from core.thumbnail_cache import set_job_status

        comic = tmp_path / "Batman 001.cbr"
        comic.write_bytes(b"x")
        set_job_status(str(comic), "processing")

        row = self._row(db_connection, str(comic))
        assert row["status"] == "processing"
        assert row["file_mtime"] == pytest.approx(os.path.getmtime(comic))

    def test_moving_a_completed_row_to_processing_keeps_an_mtime(
        self, db_connection, tmp_path
    ):
        """What the route does to a row whose cache file went stale: the old
        raw upsert turned a stamped row into a NULL one."""
        from core.thumbnail_cache import set_job_status

        comic = tmp_path / "Batman 002.cbz"
        comic.write_bytes(b"x")
        set_job_status(str(comic), "completed")
        assert self._row(db_connection, str(comic))["file_mtime"] is not None

        later = time.time() + 60
        os.utime(comic, (later, later))
        set_job_status(str(comic), "processing")

        row = self._row(db_connection, str(comic))
        assert row["status"] == "processing"
        assert row["file_mtime"] == pytest.approx(later)

    def test_the_raw_upsert_it_replaced_did_lose_it(self, db_connection, tmp_path):
        """Pins why the ratchet exists, so nobody 'simplifies' it back."""
        from core.thumbnail_cache import set_job_status

        comic = tmp_path / "Batman 003.cbz"
        comic.write_bytes(b"x")
        set_job_status(str(comic), "completed")

        db_connection.execute(
            "INSERT OR REPLACE INTO thumbnail_jobs (path, status) VALUES (?, ?)",
            (str(comic), "processing"),
        )
        db_connection.commit()
        assert self._row(db_connection, str(comic))["file_mtime"] is None
