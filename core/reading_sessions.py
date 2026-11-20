"""Reading sessions: when, and for how long, a comic was actually open.

``issues_read`` holds one row per finished issue and ``reading_positions`` one
bookmark per comic. Neither can say *when* reading happened: a re-read
overwrites the row, an unfinished comic has no row at all, and the only time
figure is a cumulative total that includes a tab left open over lunch. This
table is the missing record -- one row per sitting.

**Sessions are derived on the server from saves the reader already makes.**
There is no heartbeat endpoint, no timer and no extra unload handler: the
reader posts its position on page turns (throttled) and on unload, and each of
those posts is evidence that the comic was being read at that moment. Rules
that look arbitrary and are not:

- **Every timestamp is the server's.** The client sends a session id and
  nothing about time, so a wrong device clock cannot place a session in the
  wrong day. The reader's own ``time_spent`` accounting is untouched.
- **A gap longer than ``IDLE_CAP_SECONDS`` adds only the cap.** Two saves 40
  minutes apart are not 40 minutes of reading; they are one page turn before
  the kettle and one after. This is the idle detection the reader lacks.
- **A gap longer than ``SESSION_GAP_SECONDS`` starts a new session**, even for
  the same reader open. A tab left open overnight and picked up the next
  evening is two sittings, and one row spanning both would draw an 20-hour bar
  on the day view.
- **Only a page turn creates a session** (the position route calls this for
  ``page_number > 1`` only). Opening a comic to look at the cover is not
  reading, and the route already treats page 1 as "no bookmark".
- **Finishing closes a session; it never opens one unless the reader says
  so.** ``/api/mark-comic-read`` is also what the grid's "Mark as read" calls,
  and ``POST /api/v1/issues/read`` is how a client bulk-imports history. A
  session is recorded there only for a known reader session (``session_id``)
  or to close one already open.
- ``comic_path`` is stored byte-exact, like ``reading_positions.comic_path``:
  it is joined against ``file_index.path`` and followed on rename by
  ``move_path_references``. Do not normalise it.
"""
import re
from datetime import timedelta

from core.app_logging import app_logger
from core.database import current_user_id, db_conn
from core.user_time import DISPLAY_FORMAT, parse_utc, utc_now

# Longest stretch between two saves that still counts in full as reading.
IDLE_CAP_SECONDS = 300
# A longer silence than this is a new sitting.
SESSION_GAP_SECONDS = 30 * 60

# A reader-generated id (crypto.randomUUID, or the fallback's hex). Anything
# else is ignored rather than stored: it is an opaque key, not user content.
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")


def clean_session_id(value):
    """*value* if it is a plausible client session id, else None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if _SESSION_ID.fullmatch(value) else None


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def record_reading_activity(comic_path, page_number=None, total_pages=None,
                            session_id=None, finished=False, source="web",
                            user_id=None, now=None):
    """Note that *comic_path* was being read just now.

    Extends the open session for this reader (or, with no ``session_id``, the
    most recent session on this comic) when the last activity was within
    ``SESSION_GAP_SECONDS``; otherwise starts a new one. Returns the session
    row id, or None when nothing was recorded.

    Raises on a database error. Callers on a request path use
    ``note_reading_activity``, which does not.
    """
    if not comic_path:
        return None

    uid = current_user_id() if user_id is None else user_id
    session_id = clean_session_id(session_id)
    page = _as_int(page_number)
    total = _as_int(total_pages)
    moment = (utc_now() if now is None else parse_utc(now)).replace(microsecond=0)
    stamp = moment.strftime(DISPLAY_FORMAT)

    with db_conn() as conn:
        if not conn:
            return None
        # Take the write lock before reading: an autosave and an unload beacon
        # can arrive together, and two readers of "is there an open session?"
        # would both answer no and both insert.
        conn.execute("BEGIN IMMEDIATE")
        try:
            if session_id:
                row = conn.execute(
                    "SELECT id, ended_at, max_page FROM reading_sessions"
                    " WHERE user_id = ? AND client_session_id = ? AND comic_path = ?"
                    " ORDER BY ended_at DESC, id DESC LIMIT 1",
                    (uid, session_id, comic_path),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id, ended_at, max_page FROM reading_sessions"
                    " WHERE user_id = ? AND comic_path = ? AND client_session_id IS NULL"
                    " ORDER BY ended_at DESC, id DESC LIMIT 1",
                    (uid, comic_path),
                ).fetchone()

            gap = None
            if row is not None:
                last = parse_utc(row["ended_at"])
                if last is not None:
                    gap = (moment - last).total_seconds()

            if row is not None and gap is not None and gap <= SESSION_GAP_SECONDS:
                # A negative gap is a clock step backwards: extend nothing.
                credit = int(min(max(gap, 0), IDLE_CAP_SECONDS))
                max_page = row["max_page"]
                if page is not None:
                    max_page = page if max_page is None else max(max_page, page)
                conn.execute(
                    "UPDATE reading_sessions SET ended_at = ?,"
                    " active_seconds = active_seconds + ?,"
                    " end_page = COALESCE(?, end_page), max_page = ?,"
                    " total_pages = COALESCE(?, total_pages),"
                    " finished = MAX(finished, ?)"
                    " WHERE id = ?",
                    (stamp, credit, page, max_page, total, int(bool(finished)), row["id"]),
                )
                session_row_id = row["id"]
            elif finished and not session_id:
                # Nothing open to close, and no reader vouching for a sitting:
                # this is a manual or imported "mark as read".
                session_row_id = None
            else:
                cursor = conn.execute(
                    "INSERT INTO reading_sessions (user_id, comic_path,"
                    " client_session_id, started_at, ended_at, active_seconds,"
                    " start_page, end_page, max_page, total_pages, finished, source)"
                    " VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?)",
                    (uid, comic_path, session_id, stamp, stamp, page, page, page,
                     total, int(bool(finished)), source),
                )
                session_row_id = cursor.lastrowid
            conn.commit()
            return session_row_id
        except Exception:
            conn.rollback()
            raise


def note_reading_activity(*args, **kwargs):
    """``record_reading_activity`` for a request path: it never raises.

    The save or the mark-read it rides on is the thing the user asked for. A
    session that could not be written (a locked database, most often) costs one
    data point on a chart and must not turn a saved bookmark into an error.
    """
    try:
        return record_reading_activity(*args, **kwargs)
    except Exception as e:
        app_logger.debug(f"Reading session not recorded: {e}")
        return None


def load_sessions(user_id=None, start_utc=None, end_utc=None):
    """Sessions that *started* in ``[start_utc, end_utc)``, oldest first.

    Rows are plain dicts with ``started_at`` / ``ended_at`` as aware UTC
    datetimes. Returns ``[]`` when the database cannot be read.
    """
    uid = current_user_id() if user_id is None else user_id
    clauses = ["user_id = ?"]
    params = [uid]
    if start_utc is not None:
        clauses.append("started_at >= ?")
        params.append(start_utc.strftime(DISPLAY_FORMAT))
    if end_utc is not None:
        clauses.append("started_at < ?")
        params.append(end_utc.strftime(DISPLAY_FORMAT))
    sql = (  # nosec B608 - clauses are fixed strings with ? placeholders
        "SELECT id, comic_path, started_at, ended_at, active_seconds, start_page,"
        " end_page, max_page, total_pages, finished, source"
        " FROM reading_sessions WHERE " + " AND ".join(clauses) +
        " ORDER BY started_at ASC, id ASC"
    )
    try:
        with db_conn() as conn:
            if not conn:
                return []
            rows = conn.execute(sql, params).fetchall()
    except Exception as e:
        app_logger.error(f"Failed to load reading sessions: {e}")
        return []

    sessions = []
    for row in rows:
        started = parse_utc(row["started_at"])
        ended = parse_utc(row["ended_at"])
        if started is None or ended is None:
            continue
        item = dict(row)
        item["started_at"] = started
        item["ended_at"] = max(ended, started)
        item["finished"] = bool(row["finished"])
        sessions.append(item)
    return sessions


def session_pages(session):
    """Pages covered by a session: from where it started to the furthest page."""
    start, furthest = session.get("start_page"), session.get("max_page")
    if start is None or furthest is None:
        return 0
    return max(0, furthest - start + 1)


def session_span(session):
    """Wall-clock length of a session as a timedelta (never negative)."""
    return max(session["ended_at"] - session["started_at"], timedelta(0))
