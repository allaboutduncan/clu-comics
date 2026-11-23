"""The startup thumbnail scan: walk every library, queue the covers that need
(re)generating.

``app.scan_library_task`` is a thin caller of :func:`scan_library_thumbnails`;
the walk, the decision and the batching live here because app.py cannot be
imported in tests.

**No transaction is ever open across file I/O.** The scan used to open a write
transaction with a directory's first upsert and keep it open while it stat'd
every file in that directory and submitted the jobs, committing only at the end
of the directory. On a slow NAS, or a folder of thousands of comics, that held
the WAL write lock for the whole of that I/O, so every other writer in the app
waited out its 30s ``busy_timeout`` and failed with "database is locked" -- the
scan's own jobs included, whose terminal ``set_job_status`` had to wait for the
directory's commit. Now, per directory and per chunk of at most
``SCAN_BATCH_SIZE`` files:

1. stat the files -- no connection open at all;
2. read their existing rows -- one short read (:func:`get_job_rows`);
3. decide in memory what to queue (:func:`needs_thumbnail`);
4. write the ``processing`` rows -- one short transaction, committed
   (:func:`mark_processing_batch`);
5. only then submit the jobs.

Step 5 after step 4 keeps the route's guarantee: a ``processing`` row is
committed before its job can run, so a fast worker's terminal write is never
overwritten by it, and never queues behind the scan's lock.

What it enumerates must equal what ``build_file_index`` enumerates -- the
``is_hidden`` prune on directories and the ``.``/``_`` guard on files, ahead of
the extension test. It is the only library walker whose output never reaches
``file_index``, so nothing downstream catches its mistakes (the AppleDouble
``._Foo.cbz`` sidecars, see ``.claude/rules/thumbnails.md``).
"""

import os
import sqlite3

from core.app_logging import app_logger
from core.db_health import is_corruption_error
from core.thumbnail_cache import (
    get_job_rows,
    is_in_flight,
    mark_processing_batch,
    prune_hidden_jobs,
)
from helpers import is_hidden

# Files per read + write round. Bounds the write transaction, the IN (...)
# list and how many jobs wait on one commit, whatever the folder holds.
SCAN_BATCH_SIZE = 200

SCAN_EXTENSIONS = (".cbz", ".cbr", ".zip", ".rar", ".pdf")


def needs_thumbnail(existing, current_mtime, in_flight):
    """Should the scan queue a job for this file?

    ``existing`` is the row's ``(status, file_mtime)``, or None for a file with
    no row. ``in_flight`` is whether a job for it is queued or running in this
    process.
    """
    if in_flight:
        # Already being handled; its own terminal write will settle the row.
        return False
    if existing is None:
        return True  # New file
    status, stored_mtime = existing
    # A NULL mtime means the row predates the column (or was written by the old
    # raw upsert): "we cannot tell", read as retry once -- the row then carries
    # a real mtime and settles.
    if stored_mtime is None or current_mtime > stored_mtime:
        return True
    if status == "error":
        # Retry. A thumbnail fails for transient reasons far more often than
        # permanent ones -- an unwritable /cache, a full disk, a file still
        # being copied -- and treating the first failure as final left those
        # comics showing error.svg forever with no way back (#548). This scan
        # runs once per startup, so the retry is bounded to one attempt per
        # restart.
        return True
    if status == "processing":
        # Orphaned: not in flight (checked above), so the job was queued by an
        # earlier process and died with it (a restart, a worker kill). Nothing
        # else will ever settle this row.
        return True
    # 'completed' and up to date, or 'skipped' -- which is permanent: there is
    # no reader for this type, and a type cannot change while the path does not.
    return False


def iter_candidate_dirs(library_root, walk=os.walk):
    """Yield ``(dir, [file paths])`` for every library directory, holding
    exactly the comics ``build_file_index`` would index there."""
    from helpers.library import staging_matcher

    staging = staging_matcher()
    for root, dirs, files in walk(library_root):
        # The prunes build_file_index applies to directories: hidden folders,
        # and WATCH/TARGET nested in the library (downloads in transit).
        dirs[:] = [
            d for d in dirs
            if not is_hidden(os.path.join(root, d))
            and not staging(os.path.join(root, d), is_dir=True)
        ]
        paths = []
        for file in files:
            if staging(os.path.join(root, file)):
                continue
            # The '.'/'_' guard first: an AppleDouble sidecar carries a comic
            # extension, so an extension-first test lets it through.
            if file.startswith(".") or file.startswith("_"):
                continue
            if file.lower().endswith(SCAN_EXTENSIONS):
                paths.append(os.path.join(root, file))
        yield root, paths


class _Counts:
    def __init__(self):
        self.queued = 0
        self.skipped = 0
        self.stat_errors = 0
        self.db_failed = 0
        self.batches = 0

    def as_dict(self):
        return {
            "queued": self.queued,
            "skipped": self.skipped,
            "stat_errors": self.stat_errors,
            "db_failed": self.db_failed,
            "batches": self.batches,
        }


def _process_chunk(paths, submit, stat, in_flight, counts):
    # 1. File I/O, with no connection open.
    stamped = []
    for path in paths:
        try:
            stamped.append((path, stat(path).st_mtime))
        except OSError as e:
            counts.stat_errors += 1
            log = app_logger.warning if counts.stat_errors == 1 else app_logger.debug
            log(f"Thumbnail scan: cannot stat {path}: {e}")
    if not stamped:
        return

    try:
        # 2. One short read.
        existing = get_job_rows(p for p, _ in stamped)
        if existing is None:
            raise RuntimeError("could not open the database")

        # 3. Decide in memory.
        to_queue = [
            (path, mtime)
            for path, mtime in stamped
            if needs_thumbnail(existing.get(path), mtime, in_flight(path))
        ]
        counts.skipped += len(stamped) - len(to_queue)
        if not to_queue:
            return

        # 4. One short write transaction, committed.
        mark_processing_batch(to_queue)
        counts.batches += 1
    except Exception as e:
        # Corruption ends the scan and is reported by the caller. A lock or a
        # missing connection costs this chunk only: nothing was submitted, so
        # no job runs without its row, and the next restart (or the route, on
        # first view) picks the files up.
        if isinstance(e, sqlite3.DatabaseError) and is_corruption_error(e):
            raise
        counts.db_failed += len(stamped)
        log = app_logger.warning if counts.db_failed == len(stamped) else app_logger.debug
        log(f"Thumbnail scan: could not record {len(stamped)} jobs: {e}")
        return

    # 5. Submit, only now that the rows are committed.
    for path, _ in to_queue:
        app_logger.debug(f"Thumbnail scan: queued {path}")
        if submit(path):
            counts.queued += 1
        else:
            counts.skipped += 1


def scan_library_thumbnails(
    library_roots,
    submit,
    *,
    walk=os.walk,
    stat=os.stat,
    in_flight=is_in_flight,
    batch_size=SCAN_BATCH_SIZE,
):
    """Queue a thumbnail job for every library comic that needs one.

    ``submit(path)`` queues one job and returns True when it did (the caller
    goes through ``submit_thumbnail_job``, so ``in_flight`` sees it). ``walk``
    and ``stat`` are injectable for tests.

    Returns the counts. Raises a corruption ``sqlite3.DatabaseError`` so the
    caller can latch it; every other database failure costs only its chunk.
    """
    # Clear the rows the pre-filter walk recorded -- see prune_hidden_jobs.
    # Before anything is read, so they cannot be considered one last time. Its
    # own short transaction; it never raises.
    prune_hidden_jobs()

    counts = _Counts()
    if not library_roots:
        app_logger.warning("No libraries configured, skipping thumbnail scan")
        return counts.as_dict()

    for library_root in library_roots:
        if not os.path.exists(library_root):
            app_logger.warning(f"Library path not found, skipping: {library_root}")
            continue
        app_logger.info(f"Scanning library for thumbnails: {library_root}")
        for _root, paths in iter_candidate_dirs(library_root, walk=walk):
            for i in range(0, len(paths), batch_size):
                _process_chunk(
                    paths[i:i + batch_size], submit, stat, in_flight, counts
                )

    summary = counts.as_dict()
    app_logger.info(
        f"Library scan complete. Queued {summary['queued']} thumbnails, "
        f"skipped {summary['skipped']}"
        + (f", {summary['stat_errors']} unreadable" if summary["stat_errors"] else "")
        + (
            f", {summary['db_failed']} not recorded (database busy)"
            if summary["db_failed"]
            else ""
        )
        + "."
    )
    return summary
