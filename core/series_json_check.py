"""Find series.json files with blank values and hand them to Problem Files.

Komga reads a series.json into a class in which most fields are required, and a
blank ``name`` / ``description_text`` / ``comic_image`` leaves a series with no
title, summary or cover. Older CLU versions wrote ``null`` for these;
:func:`models.series_json._finalize` now writes ``""``, which is just as blank.

Things that look arbitrary and are not:

- **This is the one scan-driven problem source.** Every other writer records a
  failure that happened while an operation ran; nothing "failed" here, so the
  Problem Files page offers a Scan button that calls :func:`scan_library`. It is
  deliberately not scheduled — the page promises it never scans on its own.
- **"Blank" is wider than null.** A missing key, ``null``, whitespace and a
  non-string all count: each renders the same in Komga.
- **The row's path is the series.json itself**, with the folder as its parent,
  so the existing ``(path, source)`` key, dismissal-by-mtime and pruning all
  work unchanged. Rewriting the file lifts a dismissal if it is still blank.
- **A passing file clears its own row** (the table is a ledger), and the scan
  reads the open rows once instead of issuing a DELETE per library folder.
- **One-shot folders are skipped.** They hold unrelated issues and never get a
  series.json; the same rule ``/api/series-json/refresh`` applies.
"""

import os

from core.app_logging import app_logger
from core.problem_files import (
    CLASS_SERIES_JSON_BLANK,
    CLASS_SERIES_JSON_INVALID,
    SOURCE_SERIES_JSON,
    clear_problem,
    record_problem,
)

# Mylar field names. The page's "series" and "title" are both ``name``.
CHECKED_FIELDS = ("name", "description_text", "comic_image")
# Fields where an empty string is acceptable and only null/missing is flagged.
NULL_ONLY_FIELDS = ("comic_image", "description_text")

# How often the scan reports progress. A library has thousands of folders.
_PROGRESS_EVERY = 25


def blank_fields(data):
    """Names of :data:`CHECKED_FIELDS` that are blank in a parsed series.json.

    Returns ``None`` when ``data`` is not a Mylar file at all (not a dict, or no
    ``metadata`` object) — a different problem from a blank value, and it needs
    a different fix, so the caller must tell them apart.
    """
    if not isinstance(data, dict):
        return None
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return None
    blanks = []
    for field in CHECKED_FIELDS:
        value = metadata.get(field)
        if field in NULL_ONLY_FIELDS:
            # "" is a legitimate value: a series with no cover or summary is
            # written with an empty string (Komga rejects null). Only null/missing is wrong.
            if not isinstance(value, str):
                blanks.append(field)
        elif not isinstance(value, str) or not value.strip():
            blanks.append(field)
    return blanks


def _read(path):
    """Parse ``path``; returns ``(data, error)`` where exactly one is not None."""
    import json

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except Exception as e:
        return None, str(e)


def check_file(path):
    """``None`` when the file is complete, else ``(error_class, message)``."""
    data, error = _read(path)
    if error is not None:
        return CLASS_SERIES_JSON_INVALID, f"Could not parse series.json: {error}"
    blanks = blank_fields(data)
    if blanks is None:
        return CLASS_SERIES_JSON_INVALID, 'series.json has no "metadata" object'
    if blanks:
        return CLASS_SERIES_JSON_BLANK, "Blank: " + ", ".join(blanks)
    return None


def recheck_file(path):
    """Re-evaluate one series.json, recording or clearing its row.

    Returns True when the file is complete. Declines anything that is not a
    ``series.json`` (False, no row written): the retry route takes an arbitrary
    path from the client.
    """
    from models.series_json import SERIES_JSON_FILENAME

    if os.path.basename(path) != SERIES_JSON_FILENAME or not os.path.isfile(path):
        return False
    problem = check_file(path)
    if problem is None:
        clear_problem(path, SOURCE_SERIES_JSON)
        return True
    record_problem(
        path, SOURCE_SERIES_JSON, error_class=problem[0], error_message=problem[1]
    )
    return False


def _open_paths():
    """Paths that currently have a series.json row, or ``None`` if unreadable."""
    try:
        from core.database import db_conn

        with db_conn() as conn:
            if not conn:
                return None
            return {
                r[0]
                for r in conn.execute(
                    "SELECT path FROM problem_files WHERE source = ?",
                    (SOURCE_SERIES_JSON,),
                ).fetchall()
            }
    except Exception as e:
        app_logger.error(f"Could not read series.json problem rows: {e}")
        return None


def scan_library(op_id=None):
    """Check every series.json under the enabled library roots.

    Records a row per incomplete file and clears rows for files that now pass.
    Never raises. Returns ``{folders, checked, flagged, cleared}``.
    """
    from core.app_state import update_operation
    from core.config import is_oneshot_folder
    from helpers import is_hidden
    from helpers.library import get_library_roots
    from models.series_json import SERIES_JSON_FILENAME

    stats = {"folders": 0, "checked": 0, "flagged": 0, "cleared": 0}
    seen = set()
    # Rows are only cleared for a root that was actually walked: an unmounted
    # library looks exactly like "every series.json was deleted".
    walked_roots = []

    try:
        for root in get_library_roots():
            if not os.path.isdir(root):
                continue
            walked_roots.append(root)
            for dirpath, dirnames, filenames in os.walk(root):
                # Full path, not the bare name: is_hidden also asks the OS for
                # the Windows hidden attribute, which needs a real path.
                dirnames[:] = [
                    d for d in dirnames if not is_hidden(os.path.join(dirpath, d))
                ]
                stats["folders"] += 1
                if stats["folders"] % _PROGRESS_EVERY == 0 and op_id:
                    update_operation(
                        op_id,
                        detail=f"Checked {stats['folders']} folders, "
                               f"{stats['flagged']} flagged",
                    )
                if SERIES_JSON_FILENAME not in filenames:
                    continue
                if is_oneshot_folder(dirpath):
                    continue
                path = os.path.join(dirpath, SERIES_JSON_FILENAME)
                seen.add(path)
                stats["checked"] += 1
                problem = check_file(path)
                if problem is None:
                    if clear_problem(path, SOURCE_SERIES_JSON):
                        stats["cleared"] += 1
                else:
                    stats["flagged"] += 1
                    record_problem(
                        path, SOURCE_SERIES_JSON,
                        error_class=problem[0], error_message=problem[1],
                    )

        # Rows for files that vanished (or sit in a now-skipped folder) under a
        # root we did walk. Rows under roots we could not reach stay put.
        existing = _open_paths() or set()
        prefixes = tuple(os.path.join(r, "") for r in walked_roots)
        for path in existing - seen:
            if prefixes and path.startswith(prefixes) and not os.path.isfile(path):
                if clear_problem(path, SOURCE_SERIES_JSON):
                    stats["cleared"] += 1
    except Exception as e:
        app_logger.error(f"series.json scan failed: {e}")

    app_logger.info(
        f"series.json scan: {stats['checked']} files in {stats['folders']} folders, "
        f"{stats['flagged']} flagged, {stats['cleared']} cleared"
    )
    return stats
