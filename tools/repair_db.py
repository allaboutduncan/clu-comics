#!/usr/bin/env python3
"""
Salvage a malformed SQLite comic_utils.db into a clean, integrity-verified copy.

Use this when the app logs "database disk image is malformed" and the Database
tab shows a red "Corrupted" badge. It reads whatever is still readable out of the
damaged file and writes a fresh, structurally-clean database you can swap in.

- NEVER modifies the input file.
- Recovery strategy, in order of quality:
    1. sqlite3 CLI ".recover"  (best; used only if that dot-command exists AND
                                actually produces a populated database)
    2. pure-Python salvage     (no dependencies; works anywhere Python runs,
                                including inside the app container with the system
                                Python). Copies each table's readable rows; when a
                                table hits a corrupt page it cursors forward by
                                rowid, stepping past the bad page so only the rows
                                physically on it are lost, not the whole table.
- Prints a full integrity_check of the input (so you see exactly what is
  damaged), a quick_check of the output (to confirm it is clean), and per-table
  row counts before and after so you can gauge any data loss.

Usage:
    python repair_db.py INPUT_DB [OUTPUT_DB]

Default OUTPUT_DB = INPUT_DB + ".recovered"
Exit code 0 = output is CLEAN, 1 = still not clean / failed, 2 = bad usage.

Typical run against a containerised deployment (from the Docker host):
    docker cp tools/repair_db.py <container>:/tmp/repair_db.py
    docker exec <container> python3 /tmp/repair_db.py \
        /config/comic_utils.db /config/comic_utils.salvaged
Then stop the app, move the .salvaged file over comic_utils.db, delete the stale
comic_utils.db-wal / comic_utils.db-shm / .db_backup_hash, and restart.
"""
import os
import sys
import sqlite3
import subprocess


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def cli_has_recover():
    """True only if the sqlite3 CLI exists AND supports the .recover command."""
    try:
        if sh(["sqlite3", "-version"]).returncode != 0:
            return False
    except FileNotFoundError:
        return False
    help_out = sh(["sqlite3", ":memory:", ".help"])
    return ".recover" in (help_out.stdout + help_out.stderr)


def _user_table_count(path):
    try:
        c = sqlite3.connect(path)
        try:
            return c.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'").fetchone()[0]
        finally:
            c.close()
    except sqlite3.DatabaseError:
        return 0


def integrity(path, pragma="integrity_check"):
    try:
        c = sqlite3.connect(path)
        try:
            return [r[0] for r in c.execute(f"PRAGMA {pragma}").fetchall()]
        finally:
            c.close()
    except sqlite3.DatabaseError as e:
        return [f"<error: {e}>"]


def table_counts(path):
    out = {}
    try:
        c = sqlite3.connect(path)
        try:
            names = [r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()]
            for n in names:
                try:
                    out[n] = c.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
                except sqlite3.DatabaseError as e:
                    out[n] = f"ERR({e})"
        finally:
            c.close()
    except sqlite3.DatabaseError as e:
        out["<db>"] = f"ERR({e})"
    return out


def recover_cli(src, dst):
    rec = sh(["sqlite3", src, ".recover"])
    if not rec.stdout:
        # .recover can fail to even generate SQL on a badly damaged file
        # (prints "sql error: ..." to stderr). Fall back to the Python salvage.
        print("  .recover produced no output:", (rec.stderr or "")[:500])
        return False
    load = subprocess.run(["sqlite3", dst], input=rec.stdout, text=True,
                          capture_output=True)
    if load.returncode != 0 and load.stderr:
        print("  (loader notes)", load.stderr.strip()[:500])
    # A partial/aborted load can leave an empty-but-valid DB; treat that as a
    # failure so the caller falls back to the Python salvage.
    if _user_table_count(dst) == 0:
        print("  .recover output loaded no tables; falling back.")
        return False
    return os.path.exists(dst)


def _insert_rows(d, name, rows):
    if not rows:
        return 0
    ph = ",".join(["?"] * len(rows[0]))
    ok = 0
    for r in rows:
        try:
            d.execute(f'INSERT INTO "{name}" VALUES ({ph})', r)
            ok += 1
        except sqlite3.Error:
            pass
    return ok


def _salvage_table(s, d, name):
    """Copy as many rows as possible from one table. Returns rows_recovered.

    Fast path reads the whole table. On a malformed page it cursors forward by
    rowid, stepping *past* the corrupt page(s) so only the rows physically on a
    bad page are lost. Requires an ordinary rowid table (the norm here); a
    WITHOUT ROWID table that won't read whole is skipped.
    """
    # Fast path: whole table reads cleanly.
    try:
        rows = s.execute(f'SELECT * FROM "{name}"').fetchall()
        return _insert_rows(d, name, rows)
    except sqlite3.DatabaseError as e:
        print(f"  [data] {name}: full read failed ({e}); cursoring by rowid...")

    # Lower bound (leftmost leaf is usually intact).
    start = 1
    try:
        v = s.execute(f'SELECT MIN(rowid) FROM "{name}"').fetchone()[0]
        if v is not None:
            start = v
    except sqlite3.DatabaseError:
        pass
    # Optional upper bound: MAX(rowid), else AUTOINCREMENT high-water mark.
    end_bound = None
    for q, args in ((f'SELECT MAX(rowid) FROM "{name}"', ()),
                    ("SELECT seq FROM sqlite_sequence WHERE name=?", (name,))):
        try:
            row = s.execute(q, args).fetchone()
            if row and row[0] is not None:
                end_bound = row[0]
                break
        except sqlite3.DatabaseError:
            pass

    recovered = 0
    x = start
    consecutive_fail = 0
    FAIL_CAP = 20000          # stop after this many contiguous unreadable rowids
    while True:
        if end_bound is not None and x > end_bound:
            break
        try:
            chunk = s.execute(
                f'SELECT rowid, * FROM "{name}" WHERE rowid >= ? '
                f'ORDER BY rowid LIMIT 500', (x,)).fetchall()
        except sqlite3.DatabaseError:
            # Corrupt leaf at this rowid region — step past it one rowid at a time.
            x += 1
            consecutive_fail += 1
            if end_bound is None and consecutive_fail > FAIL_CAP:
                break
            continue
        if not chunk:
            break
        consecutive_fail = 0
        # row[0] is rowid; row[1:] is the table's own columns (matches INSERT *).
        recovered += _insert_rows(d, name, [r[1:] for r in chunk])
        x = chunk[-1][0] + 1
    print(f"  [data] {name}: recovered {recovered} rows via rowid cursor"
          + ("" if end_bound is not None else " (no upper bound)"))
    return recovered


def recover_python(src, dst):
    s = sqlite3.connect(src)
    d = sqlite3.connect(dst)
    objs = s.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
    tables = [(n, sql) for (t, n, sql) in objs
              if t == "table" and not n.startswith("sqlite_")]
    extras = [sql for (t, n, sql) in objs
              if t in ("index", "trigger", "view") and sql]

    for n, sql in tables:
        try:
            d.execute(sql)
        except sqlite3.Error as e:
            print(f"  [schema] {n}: {e}")
    for n, _ in tables:
        _salvage_table(s, d, n)
        d.commit()
    # Rebuild indexes/triggers/views last so they don't slow inserts or choke
    # on rows that violate a corrupt unique index.
    for sql in extras:
        try:
            d.execute(sql)
        except sqlite3.Error as e:
            print(f"  [index] skipped: {e}")
    d.commit()
    d.close()
    s.close()
    return True


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else src + ".recovered"
    if not os.path.exists(src):
        print("Input not found:", src)
        sys.exit(2)
    if os.path.exists(dst):
        print("Output already exists, refusing to overwrite:", dst)
        sys.exit(2)

    print(f"== Input integrity_check: {src} ==")
    for line in integrity(src)[:60]:
        print("   ", line)
    print("\n== Input table row counts ==")
    for k, v in table_counts(src).items():
        print(f"    {k}: {v}")

    use_cli = cli_has_recover()
    print(f"\n== Recovering -> {dst} ==")
    print("    method:", "sqlite3 CLI .recover" if use_cli
          else "pure-Python salvage (no .recover in CLI)")
    ok = False
    if use_cli:
        ok = recover_cli(src, dst)
        if not ok and os.path.exists(dst):
            os.remove(dst)
    if not ok:
        if os.path.exists(dst):
            os.remove(dst)
        if use_cli:
            print("    method: pure-Python salvage (fallback)")
        ok = recover_python(src, dst)
    if not ok:
        print("\nRecovery FAILED to produce an output file.")
        sys.exit(1)

    print(f"\n== Output quick_check: {dst} ==")
    qc = integrity(dst, "quick_check")
    for line in qc[:60]:
        print("   ", line)
    print("\n== Output table row counts ==")
    for k, v in table_counts(dst).items():
        print(f"    {k}: {v}")

    clean = qc == ["ok"]
    print("\nRESULT:",
          "CLEAN - compare the row counts above, then swap it in."
          if clean else "STILL NOT CLEAN - inspect output above.")
    sys.exit(0 if clean else 1)


if __name__ == "__main__":
    main()
