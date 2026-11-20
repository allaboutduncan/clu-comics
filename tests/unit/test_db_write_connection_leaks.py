"""Write functions must release their connection on every path (#723).

The dominant pattern in ``core/database.py`` is::

    try:
        conn = get_db_connection()
        ...
        conn.commit()
        conn.close()
    except Exception as e:
        app_logger.error(...)

When a statement raises after Python's sqlite3 has opened its implicit
transaction -- a UNIQUE violation, a "database is locked" -- the ``close()`` is
skipped and the connection is abandoned *with the transaction open*. It does
not die with the frame: it lives on in a reference cycle until the cyclic GC
runs, and while it lives it holds the WAL write lock. Every other writer then
waits out the 30s ``busy_timeout`` and fails. One support report logged 93
"database is locked" failures in twelve minutes from a single
``UNIQUE constraint failed: file_index.path`` in ``update_file_index_entry``.

A writer is leak-free when it uses ``with db_conn() as conn:`` or closes in a
``finally``. This is a ratchet: the existing leak-shaped writers are being
converted where they are hot, and the count may only go down. Lower
``BASELINE`` when you convert one.
"""

import ast
import os

DATABASE_PY = os.path.join(
    os.path.dirname(__file__), "..", "..", "core", "database.py"
)

# Leak-shaped writers on the day this test was written. Only ever lower it.
BASELINE = 91

# Converted for #723 -- they are on the TARGET -> library path, which runs once
# per completed download, and must never regress.
# #722 added the index rebuilds (save_file_index_to_db could raise UNIQUE
# after committing its DELETE) and the reading-list import/sync writers, which
# five concurrent imports drive.
MUST_BE_LEAK_FREE = {
    "update_file_index_entry",
    "add_file_index_entry",
    "delete_file_index_entry",
    "set_directory_has_thumbnail",
    "save_file_index_to_db",
    "sync_file_index_incremental",
    "create_reading_list",
    "add_reading_list_entry",
    "add_reading_list_entries",
    "update_reading_list_description",
    "update_reading_list_source_version",
    "sync_reading_list_entries",
    "update_reading_list_entry_match",
    # Per-file across the whole library: the metadata scan, ComicInfo writes,
    # the browse cache and the reader's position saves.
    "update_file_metadata",
    "update_file_index_from_comicinfo",
    "save_browse_cache",
    "invalidate_browse_cache",
    "save_reading_position",
    # A mapping row with no library_id raised after the DELETE and locked the
    # database for the schedule save that follows it.
    "save_komga_config",
    "save_komga_library_mappings",
}


def _calls(node, attr):
    """Names X for every ``X.<attr>()`` call under *node*."""
    out = set()
    for n in ast.walk(node):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == attr
            and isinstance(n.func.value, ast.Name)
        ):
            out.add(n.func.value.id)
    return out


def _leak_shaped_writers(path=None):
    with open(path or DATABASE_PY, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    leaky = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # Names bound straight from get_db_connection().
        conns = set()
        for n in ast.walk(fn):
            if (
                isinstance(n, ast.Assign)
                and isinstance(n.value, ast.Call)
                and isinstance(n.value.func, ast.Name)
                and n.value.func.id == "get_db_connection"
            ):
                conns |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        if not conns:
            continue

        committed = conns & _calls(fn, "commit")
        if not committed:
            continue  # a reader: costs a WAL read mark, not the write lock

        closed_in_finally = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Try):
                for stmt in n.finalbody:
                    closed_in_finally |= _calls(stmt, "close")
        if committed - closed_in_finally:
            leaky.add(fn.name)
    return leaky


def test_hot_file_index_writers_release_their_connection():
    assert not (_leak_shaped_writers() & MUST_BE_LEAK_FREE)


def test_no_new_leak_shaped_writers():
    found = _leak_shaped_writers()
    assert len(found) <= BASELINE, (
        f"{len(found)} write functions in core/database.py can abandon an open "
        f"write transaction (baseline {BASELINE}). Use `with db_conn() as conn:` "
        f"or close in a `finally`."
    )


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
# .claude holds agent worktrees: full copies of the repo, at other commits.
SKIP_DIRS = {"tests", ".git", ".claude", "venv", ".venv", "node_modules", "__pycache__"}


def _modules_outside_database_py():
    for base, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            if os.path.abspath(path) == os.path.abspath(DATABASE_PY):
                continue
            yield os.path.relpath(path, REPO_ROOT).replace(os.sep, "/"), path


def test_no_leak_shaped_writer_outside_database_py():
    """The backlog is in core/database.py only; everywhere else the count is 0.

    The ratchet used to read that one file, while the same shape sat in
    models/getcomics.py (five writers on the sweep's own tables), app.py and
    routes/series.py. A writer that leaks holds the WAL write lock whichever
    module it lives in.
    """
    found = {}
    scanned = 0
    for rel, path in _modules_outside_database_py():
        scanned += 1
        leaky = _leak_shaped_writers(path)
        if leaky:
            found[rel] = sorted(leaky)
    assert scanned > 50, "the walk found almost no modules"
    assert not found, (
        f"write functions that can abandon an open write transaction: {found}. "
        "Use `with db_conn() as conn:` or close in a `finally`."
    )


def test_detector_sees_the_leak_shape():
    """Guard the guard: the detector must flag the pattern it exists for."""
    sample = ast.parse(
        "def f():\n"
        "    try:\n"
        "        conn = get_db_connection()\n"
        "        conn.execute('x')\n"
        "        conn.commit()\n"
        "        conn.close()\n"
        "    except Exception:\n"
        "        pass\n"
        "def g():\n"
        "    conn = get_db_connection()\n"
        "    try:\n"
        "        conn.commit()\n"
        "    finally:\n"
        "        conn.close()\n"
    )
    import tests.unit.test_db_write_connection_leaks as mod

    original = mod.DATABASE_PY
    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(ast.unparse(sample))
    try:
        mod.DATABASE_PY = fh.name
        assert _leak_shaped_writers() == {"f"}
    finally:
        mod.DATABASE_PY = original
        os.unlink(fh.name)
