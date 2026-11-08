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
BASELINE = 107

# Converted for #723 -- they are on the TARGET -> library path, which runs once
# per completed download, and must never regress.
MUST_BE_LEAK_FREE = {
    "update_file_index_entry",
    "add_file_index_entry",
    "delete_file_index_entry",
    "set_directory_has_thumbnail",
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


def _leak_shaped_writers():
    with open(DATABASE_PY, encoding="utf-8") as fh:
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
