"""Series folder conflicts: a second folder for a series that is already mapped.

A series holds exactly one folder (``series.mapped_path``), and matching,
downloads and the series page all read only that one. When Scan Library finds a
sidecar folder that resolves to a series already mapped elsewhere (or two
scanned folders resolve to the same series), the comics in that folder are not
counted as owned. This module keeps those findings in
``series_folder_conflicts`` so they outlive the scan job; the job used to hold
them only in memory, so closing the dialog lost them.

Things that look arbitrary and are not:

- **Pruning needs positive evidence** (as for ``problem_files``). A row not
  reported by a scan is dropped only when its library root is mounted, and a
  vanished folder only when ``is_path_provably_gone`` says so. An asleep NAS is
  an empty directory, and treating that as "resolved" would empty the table.
- **Ignore is keyed on the folder's file list** (``files_sig``), not on a flag:
  a new comic in an ignored folder re-opens it, for the same reason a dismissed
  duplicate group re-opens when another copy turns up.
- **The mapped folder is read from ``series`` at list time.** The stored
  ``mapped_folder`` is what the scan saw; the user may have switched folders
  since, so the live value wins when there is one.
"""

import hashlib
import os

from core.app_logging import app_logger

COMIC_EXTENSIONS = (".cbz", ".cbr", ".zip", ".rar", ".cb7", ".7z", ".pdf")


def _norm(path):
    if not path:
        return ""
    return os.path.normpath(path).replace("\\", "/").rstrip("/")


def folder_files(folder):
    """Sorted comic file names directly inside ``folder`` (``[]`` if unreadable)."""
    try:
        return sorted(
            name for name in os.listdir(folder)
            if name.lower().endswith(COMIC_EXTENSIONS)
            and os.path.isfile(os.path.join(folder, name))
        )
    except OSError:
        return []


def files_signature(names):
    return hashlib.sha1("\n".join(names).encode("utf-8", "surrogateescape")).hexdigest()


def _under(path, root):
    path, root = _norm(path), _norm(root)
    return bool(root) and (path == root or path.startswith(root + "/"))


def _row_out(row):
    item = dict(row)
    live = item.pop("live_mapped_path", None)
    if live:
        item["mapped_folder"] = live
    item["ignored"] = bool(item.get("ignored_sig")) and item.get("ignored_sig") == item.get("files_sig")
    item.pop("ignored_sig", None)
    item.pop("files_sig", None)
    return item


def record_scan(review, roots):
    """Store a scan's review items and prune rows the scan resolved.

    ``review`` is ``scan_library_for_automap()["review"]``; ``roots`` the library
    roots the scan walked. Returns ``{"open": n, "ignored": n, "ignored_folders":
    set}`` (``ignored_folders`` holds normalised paths), or None when the database
    could not be written -- the caller then reports the scan as before.
    """
    from core.database import db_conn
    from helpers.library import is_path_provably_gone, is_root_mounted

    mounted = [r for r in (roots or []) if r and is_root_mounted(r)]
    seen = set()
    with db_conn() as conn:
        if conn is None:
            return None
        try:
            c = conn.cursor()
            for it in review or []:
                series_id = it.get("metron_id")
                folder = it.get("folder")
                if not series_id or not folder:
                    continue
                names = folder_files(folder)
                c.execute(
                    """
                    INSERT INTO series_folder_conflicts
                        (series_id, folder, mapped_folder, series_name, year,
                         publisher_name, series_status, cv_id, source, file_count,
                         files_sig)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(series_id, folder) DO UPDATE SET
                        mapped_folder = excluded.mapped_folder,
                        series_name = excluded.series_name,
                        year = excluded.year,
                        publisher_name = excluded.publisher_name,
                        series_status = excluded.series_status,
                        cv_id = excluded.cv_id,
                        source = excluded.source,
                        file_count = excluded.file_count,
                        files_sig = excluded.files_sig,
                        last_seen_at = CURRENT_TIMESTAMP
                    """,
                    (
                        series_id, folder, it.get("conflict_with"),
                        it.get("series_name"),
                        None if it.get("year") is None else str(it.get("year")),
                        it.get("publisher_name"), it.get("status"), it.get("cv_id"),
                        it.get("source"), len(names), files_signature(names),
                    ),
                )
                seen.add((series_id, folder))

            c.execute("SELECT id, series_id, folder FROM series_folder_conflicts")
            stale = []
            mounted_cache = {}
            for row in c.fetchall():
                key = (row["series_id"], row["folder"])
                if key in seen:
                    continue
                if any(_under(row["folder"], r) for r in mounted) or is_path_provably_gone(
                    row["folder"], roots=roots, mounted=mounted_cache
                ):
                    stale.append(row["id"])
            c.executemany(
                "DELETE FROM series_folder_conflicts WHERE id = ?", [(i,) for i in stale]
            )
            conn.commit()

            c.execute(
                "SELECT folder, files_sig, ignored_sig FROM series_folder_conflicts"
            )
            ignored_folders = set()
            open_n = 0
            for row in c.fetchall():
                if row["ignored_sig"] and row["ignored_sig"] == row["files_sig"]:
                    ignored_folders.add(_norm(row["folder"]))
                else:
                    open_n += 1
        except Exception as e:
            app_logger.error(f"series conflicts: could not record scan results: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            return None

    if stale:
        app_logger.info(f"series conflicts: {len(stale)} resolved since the last scan")
    return {"open": open_n, "ignored": len(ignored_folders), "ignored_folders": ignored_folders}


_SELECT = """
    SELECT sfc.*, s.mapped_path AS live_mapped_path
    FROM series_folder_conflicts sfc
    LEFT JOIN series s ON s.id = sfc.series_id
"""


def list_conflicts(include_ignored=False):
    """Every conflict row, or None when the database could not be read.

    None is not ``[]``: an empty list has to mean "no conflicts" (the
    ``list_problems`` contract).
    """
    from core.database import db_conn

    with db_conn() as conn:
        if conn is None:
            return None
        try:
            c = conn.cursor()
            c.execute(_SELECT + " ORDER BY sfc.series_name COLLATE NOCASE, sfc.folder")
            rows = [_row_out(r) for r in c.fetchall()]
        except Exception as e:
            app_logger.error(f"series conflicts: could not list conflicts: {e}")
            return None
    if not include_ignored:
        rows = [r for r in rows if not r["ignored"]]
    return rows


def get_conflict(conflict_id):
    from core.database import db_conn

    with db_conn() as conn:
        if conn is None:
            return None
        try:
            c = conn.cursor()
            c.execute(_SELECT + " WHERE sfc.id = ?", (conflict_id,))
            row = c.fetchone()
        except Exception as e:
            app_logger.error(f"series conflicts: could not read conflict {conflict_id}: {e}")
            return None
    return _row_out(row) if row else None


def count_open():
    """Number of un-ignored conflicts (0 on a read failure: the badge is a hint)."""
    from core.database import db_conn

    with db_conn() as conn:
        if conn is None:
            return 0
        try:
            c = conn.cursor()
            c.execute(
                "SELECT COUNT(*) FROM series_folder_conflicts "
                "WHERE ignored_sig IS NULL OR files_sig IS NULL OR ignored_sig != files_sig"
            )
            return c.fetchone()[0]
        except Exception as e:
            app_logger.debug(f"series conflicts: count failed: {e}")
            return 0


def set_ignored(conflict_id, ignored=True):
    """Ignore (or un-ignore) a conflict at the folder's current file list.

    Re-reads the folder so the signature is the one on disk now, not the one
    from the last scan. Returns True when a row was updated.
    """
    from core.database import db_conn

    row = get_conflict(conflict_id)
    if not row:
        return False
    names = folder_files(row["folder"])
    sig = files_signature(names)
    with db_conn() as conn:
        if conn is None:
            return False
        try:
            c = conn.cursor()
            c.execute(
                "UPDATE series_folder_conflicts SET files_sig = ?, file_count = ?, "
                "ignored_sig = ? WHERE id = ?",
                (sig, len(names), sig if ignored else None, conflict_id),
            )
            conn.commit()
            return c.rowcount > 0
        except Exception as e:
            app_logger.error(f"series conflicts: could not update conflict {conflict_id}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            return False


def after_switch(conflict_id, old_mapped_folder):
    """The user made this row's folder the series' folder: the old one becomes the conflict.

    With no previous folder (the series was not actually mapped any more) the
    row simply goes away.
    """
    from core.database import db_conn

    row = get_conflict(conflict_id)
    if not row:
        return False
    with db_conn() as conn:
        if conn is None:
            return False
        try:
            c = conn.cursor()
            if not old_mapped_folder or _norm(old_mapped_folder) == _norm(row["folder"]):
                c.execute("DELETE FROM series_folder_conflicts WHERE id = ?", (conflict_id,))
            else:
                names = folder_files(old_mapped_folder)
                # The old folder may already have a row of its own (two scanned
                # folders and a mapped one); drop it so the UNIQUE key holds.
                c.execute(
                    "DELETE FROM series_folder_conflicts WHERE series_id = ? AND folder = ? AND id != ?",
                    (row["series_id"], old_mapped_folder, conflict_id),
                )
                c.execute(
                    "UPDATE series_folder_conflicts SET folder = ?, mapped_folder = ?, "
                    "file_count = ?, files_sig = ?, ignored_sig = NULL, "
                    "last_seen_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (old_mapped_folder, row["folder"], len(names),
                     files_signature(names), conflict_id),
                )
                # Every other row for this series now conflicts with the new folder.
                c.execute(
                    "UPDATE series_folder_conflicts SET mapped_folder = ? "
                    "WHERE series_id = ? AND id != ?",
                    (row["folder"], row["series_id"], conflict_id),
                )
            conn.commit()
            return True
        except Exception as e:
            app_logger.error(f"series conflicts: could not follow switch for {conflict_id}: {e}")
            try:
                conn.rollback()
            except Exception:
                pass
            return False


def refresh(conflict_id):
    """Re-read a row's file count and signature from disk (after a partial merge).

    An ignore stays only if the folder still holds exactly what was ignored.
    """
    from core.database import db_conn

    row = get_conflict(conflict_id)
    if not row:
        return False
    names = folder_files(row["folder"])
    with db_conn() as conn:
        if conn is None:
            return False
        try:
            c = conn.cursor()
            c.execute(
                "UPDATE series_folder_conflicts SET file_count = ?, files_sig = ?, "
                "last_seen_at = CURRENT_TIMESTAMP WHERE id = ?",
                (len(names), files_signature(names), conflict_id),
            )
            conn.commit()
            return c.rowcount > 0
        except Exception as e:
            app_logger.error(f"series conflicts: could not refresh conflict {conflict_id}: {e}")
            return False


def remove(conflict_id):
    """Forget one row (the page's Remove for a folder that is gone)."""
    from core.database import db_conn

    with db_conn() as conn:
        if conn is None:
            return False
        try:
            c = conn.cursor()
            c.execute("DELETE FROM series_folder_conflicts WHERE id = ?", (conflict_id,))
            conn.commit()
            return c.rowcount > 0
        except Exception as e:
            app_logger.error(f"series conflicts: could not remove conflict {conflict_id}: {e}")
            return False
