"""What each automatic download run did: searched, queued, not found, and how
the downloads it queued turned out.

The GetComics sweep and the weekly-pack job used to leave one INFO summary line
and a ``schedules.last_run`` stamp, so "did last night's run do anything?" could
only be answered from the logs. This module records one ``download_runs`` row
per run and one ``download_run_items`` row per issue the run actually searched
for, and the three download settle points (api.py, models/usenet.py,
models/dcpp.py) write each queued download's outcome back onto its item through
:func:`set_download_outcome`.

Things that look arbitrary and are not:

- **Skips are counted, never listed.** "Already owned", "not monitored",
  "unreleased" and "in cooldown" are most of every sweep -- thousands of issues
  a night on a large library -- and listing them would make this table the
  largest in the database for no information. They go in ``counts_json``.
- **Bounded three ways.** At most ``RUN_ITEM_CAP`` items per run (past that only
  the counters move and ``items_truncated`` is set), at most
  ``download_run_history_keep`` runs per kind, and nothing older than
  ``MAX_AGE_DAYS``. Pruning happens when a run starts, so no scheduler job is
  needed and the table can never outgrow one run's worth of slack.
- **Recording never raises.** Every call here sits inside a sweep or a download
  worker; a diagnostic must never break the thing it is diagnosing (the same
  contract as :func:`core.problem_files.record_problem`). Failures are DEBUG.
- **Items are buffered and flushed in batches**, not written one per issue: the
  sweep runs for hours against a database the rest of the app is using, and a
  write per searched issue is a write lock per searched issue.
- **A dry run records nothing** -- it queues nothing, and the simulation page is
  its report. Callers pass :data:`NULL_RECORDER` for it so the hook sites need
  no ``if``.
"""

import json
from datetime import datetime, timedelta, timezone

from core.app_logging import app_logger

KIND_GETCOMICS = "getcomics"
KIND_WEEKLY_PACKS = "weekly_packs"
KINDS = (KIND_GETCOMICS, KIND_WEEKLY_PACKS)

TRIGGER_SCHEDULED = "scheduled"
TRIGGER_MANUAL = "manual"
TRIGGER_SERIES = "series"

STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_ERROR = "error"

# Item outcomes: what the run did for one issue it searched for.
OUTCOME_QUEUED = "queued"
OUTCOME_DUPLICATE = "duplicate"
OUTCOME_NO_RESULTS = "no_results"
OUTCOME_NO_MATCH = "no_match"
OUTCOME_NO_PART = "no_part_matched"
OUTCOME_PACK_SKIPPED = "pack_skipped"
OUTCOME_NO_LINK = "no_link"
OUTCOME_ERROR = "error"
QUEUED_OUTCOMES = (OUTCOME_QUEUED, OUTCOME_DUPLICATE)

# Skip counters (counted, never listed).
COUNT_OWNED = "skipped_owned"
COUNT_MANUAL = "skipped_manual_status"
COUNT_UNRELEASED = "skipped_unreleased"
COUNT_COOLDOWN = "skipped_cooldown"
COUNT_UNMONITORED = "skipped_unmonitored_series"
COUNT_NO_FOLDER = "skipped_missing_folder"
COUNT_RANGE = "skipped_covered_by_pack"
COUNT_CAP = "not_searched_cap_reached"
COUNT_ALREADY_DOWNLOADED = "skipped_already_downloaded"
COUNT_NOT_READY = "not_ready"

RUN_ITEM_CAP = 1000
MAX_AGE_DAYS = 90
DEFAULT_KEEP = 30
DETAIL_MAX = 300
FLUSH_EVERY = 100


def _now():
    """UTC, in SQLite's CURRENT_TIMESTAMP format, so both sort together."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _clip(text, limit=DETAIL_MAX):
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _keep_runs():
    try:
        from core.settings import get_int

        keep = get_int("DOWNLOAD_RUN_HISTORY_KEEP")
    except Exception:
        keep = DEFAULT_KEEP
    return max(1, keep or DEFAULT_KEEP)


def prune_runs(conn, kind, keep, now=None):
    """Drop runs of ``kind`` beyond the newest ``keep``, and any run older than
    ``MAX_AGE_DAYS``. Items go with their runs. Returns the runs removed."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=MAX_AGE_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        """
        SELECT id FROM download_runs
        WHERE kind = ? AND (
            started_at < ?
            OR id NOT IN (
                SELECT id FROM download_runs WHERE kind = ?
                ORDER BY started_at DESC, id DESC LIMIT ?
            )
        )
        """,
        (kind, cutoff, kind, keep),
    ).fetchall()
    ids = [r[0] for r in rows]
    if ids:
        marks = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM download_run_items WHERE run_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM download_runs WHERE id IN ({marks})", ids)
    return len(ids)


class _NullRecorder:
    """Accepts every call and records nothing (dry runs, failed starts)."""

    run_id = None

    def count(self, key, n=1):
        pass

    def set_scope(self, scope):
        pass

    def item(self, *args, **kwargs):
        pass

    def flush(self):
        pass

    def finish(self, status=STATUS_OK, error=None):
        pass


NULL_RECORDER = _NullRecorder()


class RunRecorder:
    """Collects one run's counters and items and writes them in batches."""

    def __init__(self, run_id):
        self.run_id = run_id
        self.counts = {}
        self._pending = []
        self._item_total = 0
        self._truncated = False
        self._finished = False
        self._scope = None

    def count(self, key, n=1):
        self.counts[key] = self.counts.get(key, 0) + n

    def set_scope(self, scope):
        """Name what a narrowed run covered (a series), written on next flush."""
        self._scope = _clip(scope, 200)

    def flush(self):
        """Write buffered items now -- before handing a download to a worker
        that could settle it before the next batch would be written."""
        if not self._finished:
            self._flush()

    def item(self, series_name, issue_number, outcome, *, series_id=None,
             source=None, provider=None, detail=None, download_id=None):
        """One searched issue. Past ``RUN_ITEM_CAP`` only the counters move."""
        self.count(f"outcome_{outcome}")
        if self._item_total >= RUN_ITEM_CAP:
            self._truncated = True
            return
        self._item_total += 1
        self._pending.append((
            self.run_id, series_id, _clip(series_name, 200),
            _clip(issue_number, 40), source, provider, outcome, _clip(detail),
            download_id, OUTCOME_QUEUED if download_id else None, _now(),
        ))
        if len(self._pending) >= FLUSH_EVERY:
            self._flush()

    def _flush(self, status=None, error=None):
        try:
            from core.database import db_conn

            with db_conn() as conn:
                if not conn:
                    return
                if self._pending:
                    conn.executemany(
                        """
                        INSERT INTO download_run_items (
                            run_id, series_id, series_name, issue_number, source,
                            provider, outcome, detail, download_id,
                            download_status, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        self._pending,
                    )
                sets = ["counts_json = ?", "items_truncated = ?"]
                args = [json.dumps(self.counts, sort_keys=True), int(self._truncated)]
                if self._scope:
                    sets.append("scope = ?")
                    args.append(self._scope)
                if status:
                    sets += ["status = ?", "finished_at = ?", "error = ?"]
                    args += [status, _now(), _clip(error)]
                conn.execute(
                    f"UPDATE download_runs SET {', '.join(sets)} WHERE id = ?",
                    args + [self.run_id],
                )
                conn.commit()
                self._pending = []
        except Exception as e:
            app_logger.debug(f"Download run {self.run_id}: flush failed: {e}")

    def finish(self, status=STATUS_OK, error=None):
        """Write what is buffered and close the run. Safe to call twice."""
        if self._finished:
            return
        self._finished = True
        self._flush(status=status, error=error)


def start_run(kind, trigger=TRIGGER_SCHEDULED, scope=None):
    """Open a run (pruning old ones first). Returns a recorder, never raises."""
    try:
        from core.database import db_conn

        with db_conn() as conn:
            if not conn:
                return NULL_RECORDER
            prune_runs(conn, kind, _keep_runs())
            cur = conn.execute(
                "INSERT INTO download_runs (kind, trigger, scope, started_at, status, "
                "counts_json, items_truncated) VALUES (?, ?, ?, ?, ?, '{}', 0)",
                (kind, trigger, _clip(scope, 200), _now(), STATUS_RUNNING),
            )
            conn.commit()
            return RunRecorder(cur.lastrowid)
    except Exception as e:
        app_logger.debug(f"Could not open a download run record: {e}")
        return NULL_RECORDER


def set_download_outcome(download_id, status, detail=None):
    """Record how a queued download settled (complete/failed/cancelled/...).

    Called for every download, scheduled or not; one indexed UPDATE that
    matches nothing for a manual grab. ``detail`` replaces the item's detail
    only when given (a failure's error message).
    """
    if not download_id:
        return
    try:
        from core.database import db_conn

        with db_conn() as conn:
            if not conn:
                return
            if detail is not None:
                conn.execute(
                    "UPDATE download_run_items SET download_status = ?, detail = ?, "
                    "updated_at = ? WHERE download_id = ?",
                    (status, _clip(detail), _now(), download_id),
                )
            else:
                conn.execute(
                    "UPDATE download_run_items SET download_status = ?, updated_at = ? "
                    "WHERE download_id = ?",
                    (status, _now(), download_id),
                )
            conn.commit()
    except Exception as e:
        app_logger.debug(f"Could not record outcome for download {download_id}: {e}")


def reconcile_interrupted_runs():
    """Close what a restart left open. Called once at startup, before any sweep
    can start, so every 'running' run is from a dead process.

    Downloads still 'queued'/'retrying' are marked 'interrupted' too: the
    download queue, the retry timers and the Usenet job table are all in
    memory. DC++ is the exception -- its jobs are persisted and re-adopted
    (models.dcpp.recover_dcpp_jobs), so its outcome still arrives.
    """
    try:
        from core.database import db_conn

        with db_conn() as conn:
            if not conn:
                return 0
            cur = conn.execute(
                "UPDATE download_runs SET status = ?, finished_at = ?, "
                "error = 'Interrupted (the app restarted during this run)' "
                "WHERE status = ?",
                (STATUS_ERROR, _now(), STATUS_RUNNING),
            )
            conn.execute(
                "UPDATE download_run_items SET download_status = 'interrupted', "
                "updated_at = ? WHERE download_status IN ('queued', 'retrying') "
                "AND COALESCE(provider, '') != 'dcpp'",
                (_now(),),
            )
            conn.commit()
            return cur.rowcount or 0
    except Exception as e:
        app_logger.debug(f"Could not reconcile interrupted download runs: {e}")
        return 0


# --------------------------------------------------------------------------
# Read side
# --------------------------------------------------------------------------

# Item filters the page offers. Each is a WHERE fragment over download_run_items.
ITEM_FILTERS = {
    "all": "1 = 1",
    "queued": "outcome IN ('queued', 'duplicate')",
    "downloaded": "download_status IN ('complete', 'complete_no_move')",
    "failed": "download_status IN ('failed', 'interrupted')",
    "not_found": "outcome NOT IN ('queued', 'duplicate')",
    # What the run did not get: never queued, or queued and then failed or was
    # cancelled. A download still in flight is not "missing" yet.
    "missing": (
        "(outcome NOT IN ('queued', 'duplicate') "
        "OR download_status IN ('failed', 'cancelled', 'interrupted'))"
    ),
}


def _summarise(conn, run):
    """A run row plus counters and its items' download tallies."""
    out = dict(run)
    try:
        out["counts"] = json.loads(out.pop("counts_json") or "{}")
    except (TypeError, ValueError):
        out["counts"] = {}
    out["items_truncated"] = bool(out.get("items_truncated"))
    tallies = conn.execute(
        """
        SELECT
            COUNT(*) AS items,
            SUM(outcome IN ('queued', 'duplicate')) AS queued,
            SUM(download_status IN ('complete', 'complete_no_move')) AS downloaded,
            SUM(download_status = 'failed') AS failed,
            SUM(download_status = 'cancelled') AS cancelled,
            SUM(download_status = 'interrupted') AS interrupted,
            SUM(outcome NOT IN ('queued', 'duplicate')) AS not_found
        FROM download_run_items WHERE run_id = ?
        """,
        (out["id"],),
    ).fetchone()
    out["tally"] = {k: (tallies[k] or 0) for k in tallies.keys()}
    counts = out["counts"]
    # Searched = every listed item plus any the cap kept off the list.
    out["tally"]["searched"] = sum(
        v for k, v in counts.items() if k.startswith("outcome_")
    ) or out["tally"]["items"]
    out["tally"]["skipped"] = sum(
        v for k, v in counts.items() if k.startswith("skipped_")
    )
    return out


def list_runs(kind=None, limit=DEFAULT_KEEP):
    """Newest first. Returns None on a read failure (never an empty list,
    which has to mean "no runs yet")."""
    try:
        import sqlite3
        from core.database import db_conn

        with db_conn() as conn:
            if not conn:
                return None
            conn.row_factory = sqlite3.Row
            sql = "SELECT * FROM download_runs"
            args = []
            if kind:
                sql += " WHERE kind = ?"
                args.append(kind)
            sql += " ORDER BY started_at DESC, id DESC LIMIT ?"
            args.append(max(1, min(int(limit), 200)))
            return [_summarise(conn, r) for r in conn.execute(sql, args).fetchall()]
    except Exception as e:
        app_logger.error(f"Could not list download runs: {e}")
        return None


def get_run(run_id, item_filter="all"):
    """One run with its items, or None if it does not exist / cannot be read.

    Raises ValueError for an unknown ``item_filter``.
    """
    where = ITEM_FILTERS.get(item_filter)
    if where is None:
        raise ValueError(f"Unknown filter {item_filter!r}")
    try:
        import sqlite3
        from core.database import db_conn

        with db_conn() as conn:
            if not conn:
                return None
            conn.row_factory = sqlite3.Row
            run = conn.execute(
                "SELECT * FROM download_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None:
                return None
            out = _summarise(conn, run)
            out["filter"] = item_filter
            out["items"] = [
                dict(r) for r in conn.execute(
                    f"""
                    SELECT series_id, series_name, issue_number, source, provider,
                           outcome, detail, download_id, download_status, updated_at
                    FROM download_run_items WHERE run_id = ? AND {where}
                    ORDER BY id
                    """,
                    (run_id,),
                ).fetchall()
            ]
            return out
    except Exception as e:
        app_logger.error(f"Could not read download run {run_id}: {e}")
        return None
