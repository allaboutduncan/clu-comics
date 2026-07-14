"""Unit tests for tools/repair_db.py — the offline DB salvage utility."""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "tools"))

import repair_db  # noqa: E402


def _build_db(path, rows=2000):
    c = sqlite3.connect(path)
    c.execute("PRAGMA page_size=4096")
    c.execute("CREATE TABLE file_index(id INTEGER PRIMARY KEY, path TEXT)")
    c.execute("CREATE TABLE issues_read(id INTEGER PRIMARY KEY, note TEXT)")
    c.executemany("INSERT INTO file_index(path) VALUES(?)",
                  [(f"/data/x/{i}-" + "y" * 60,) for i in range(rows)])
    c.executemany("INSERT INTO issues_read(note) VALUES(?)",
                  [(f"read-{i}",) for i in range(25)])
    c.execute("CREATE INDEX ix_path ON file_index(path)")
    c.commit()
    c.close()


def _corrupt_leaf(path):
    """Overwrite an interior page so the file becomes 'malformed' but readable."""
    pages = os.path.getsize(path) // 4096
    with open(path, "r+b") as f:
        f.seek((pages // 2) * 4096)
        f.write(b"\x99" * 4096)


def test_recover_python_clean_db_full_copy(tmp_path):
    src = str(tmp_path / "clean.db")
    dst = str(tmp_path / "clean.recovered")
    _build_db(src, rows=500)

    assert repair_db.recover_python(src, dst) is True
    assert repair_db.integrity(dst, "quick_check") == ["ok"]
    counts = repair_db.table_counts(dst)
    assert counts["file_index"] == 500
    assert counts["issues_read"] == 25


def test_recover_python_salvages_corrupt_db(tmp_path):
    src = str(tmp_path / "bad.db")
    dst = str(tmp_path / "bad.recovered")
    _build_db(src, rows=2000)
    _corrupt_leaf(src)

    # Sanity: the source really is malformed now.
    assert repair_db.integrity(src) != ["ok"]

    assert repair_db.recover_python(src, dst) is True
    # Output must be structurally clean regardless of how many rows survived.
    assert repair_db.integrity(dst, "quick_check") == ["ok"]
    counts = repair_db.table_counts(dst)
    # The untouched table is fully recovered; the damaged one keeps most rows.
    assert counts["issues_read"] == 25
    assert isinstance(counts["file_index"], int)
    assert counts["file_index"] > 0


def test_integrity_reports_missing_and_bad(tmp_path):
    # A non-database file reports an error rather than raising.
    junk = str(tmp_path / "junk.db")
    with open(junk, "wb") as f:
        f.write(b"not a database" * 100)
    result = repair_db.integrity(junk, "quick_check")
    assert result != ["ok"]


def test_input_is_never_modified(tmp_path):
    src = str(tmp_path / "keep.db")
    dst = str(tmp_path / "keep.recovered")
    _build_db(src, rows=300)
    before = os.path.getsize(src), os.path.getmtime(src)

    repair_db.recover_python(src, dst)

    after = os.path.getsize(src), os.path.getmtime(src)
    assert before == after
