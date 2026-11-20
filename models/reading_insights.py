"""Reading insights: the numbers Insights and Wrapped are both built from.

Two layers, on purpose:

* **Loaders** read ``issues_read`` once for a period and turn each row into a
  ``ReadEvent`` whose timestamp is already in the user's local time.
* **Pure functions** take events (or plain dates) and return numbers. They
  touch no database and no clock, so every rule here is a table-driven test.

Local time matters because ``read_at`` is stored in UTC and the user's day is
not UTC's: a comic finished at 9pm in UTC-5 belongs to that evening, not to the
next morning, and on 31 December not to next year's Wrapped. Bucketing is done
in Python from ``core.user_time`` rather than in SQL, which also sidesteps the
two shapes the column holds (see ``localise_read_at_sql``).
"""
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from core.app_logging import app_logger
from core.database import current_user_id, db_conn
from core.metadata_normalize import split_credit_list
from core.user_time import DISPLAY_FORMAT, parse_utc, user_offset_hours, user_tzinfo

# Wrapped has always named a series after its folder, minus a " v2016"-style
# volume suffix. Kept so a read with no ComicInfo lands in the same series it
# did before.
_VOLUME_SUFFIX = re.compile(r"\s*v\d{4}$")


@dataclass(frozen=True)
class ReadEvent:
    """One finished issue, in the user's local time."""

    path: str
    at: datetime            # aware, in the user's offset
    pages: int = 0
    seconds: int = 0
    series: str = ""
    publisher: str = ""
    writers: tuple = field(default_factory=tuple)
    pencillers: tuple = field(default_factory=tuple)
    characters: tuple = field(default_factory=tuple)
    hidden: bool = False

    @property
    def day(self):
        return self.at.date()

    @property
    def timed(self):
        """Whether ``at`` is when the comic was actually being read.

        A manual "mark as read", a Komga import and a folder-level mark all
        write a row with no reading time: the stamp is when the button was
        pressed (or an imported date), not when anyone read anything. Those
        count as reads everywhere, but must not feed an hour-of-day chart.
        """
        return self.seconds > 0


def series_from_path(path):
    """The series a path belongs to when ComicInfo does not say: its folder."""
    parts = str(path or "").replace("\\", "/").rstrip("/").split("/")
    folder = parts[-2] if len(parts) >= 2 else ""
    return _VOLUME_SUFFIX.sub("", folder) or "Unknown"


def load_read_events(user_id=None, start_utc=None, end_utc=None,
                     include_hidden=True, tz_hours=None):
    """Read events for one user, oldest first.

    *start_utc* / *end_utc* are naive UTC datetimes forming a half-open range
    (``core.user_time.local_period_bounds_utc`` builds them for a local year or
    month); either may be None. Returns ``[]`` when the database cannot be
    read: every caller renders a page, and an empty chart is the honest
    rendering of "nothing could be loaded" there.
    """
    uid = current_user_id() if user_id is None else user_id
    hours = user_offset_hours() if tz_hours is None else tz_hours
    tz = user_tzinfo(hours)

    clauses = ["r.user_id = ?", "r.read_at IS NOT NULL"]
    params = [uid]
    # datetime() rather than the raw column: read_at holds both
    # "2024-06-01T10:00:00" and "2024-06-01 10:00:00", and 'T' sorts after ' '.
    if start_utc is not None:
        clauses.append("datetime(r.read_at) >= ?")
        params.append(start_utc.strftime(DISPLAY_FORMAT))
    if end_utc is not None:
        clauses.append("datetime(r.read_at) < ?")
        params.append(end_utc.strftime(DISPLAY_FORMAT))
    if not include_hidden:
        clauses.append("r.hide = 0")

    sql = (  # nosec B608 - clauses are fixed strings with ? placeholders
        "SELECT r.issue_path, r.read_at, r.page_count, r.time_spent, r.writer,"
        " r.penciller, r.characters, r.publisher, r.hide,"
        " f.ci_series AS ci_series, f.ci_publisher AS ci_publisher"
        " FROM issues_read r"
        " LEFT JOIN file_index f ON f.path = r.issue_path"
        " WHERE " + " AND ".join(clauses) +
        " ORDER BY datetime(r.read_at) ASC, r.id ASC"
    )

    events = []
    try:
        with db_conn() as conn:
            if not conn:
                return []
            rows = conn.execute(sql, params).fetchall()
    except Exception as e:
        app_logger.error(f"Failed to load read events: {e}")
        return []

    for row in rows:
        stamp = parse_utc(row["read_at"])
        if stamp is None:
            continue
        path = row["issue_path"]
        events.append(ReadEvent(
            path=path,
            at=stamp.astimezone(tz),
            pages=int(row["page_count"] or 0),
            seconds=int(row["time_spent"] or 0),
            series=(row["ci_series"] or "").strip() or series_from_path(path),
            publisher=(row["publisher"] or row["ci_publisher"] or "").strip(),
            writers=split_credit_list(row["writer"]),
            pencillers=split_credit_list(row["penciller"]),
            characters=split_credit_list(row["characters"]),
            hidden=bool(row["hide"]),
        ))
    return events


def reading_days(events):
    """The set of local calendar days on which anything was read."""
    return {event.day for event in events}


def compute_streaks(days, today):
    """Current and longest run of consecutive reading days.

    *days* is any iterable of ``date``; *today* is the user's local date
    (``core.user_time.user_today()``), passed in so this stays pure.

    The current streak is still alive when the last read was **yesterday**:
    someone who reads every evening has not broken anything by opening the
    page at lunchtime. It counts back from today if today has a read,
    otherwise from yesterday, and is 0 once a whole day has been missed.

    Returns ``{current, longest, longest_start, longest_end}`` with the two
    dates as ISO strings (None when there is no reading at all).
    """
    ordered = sorted({d for d in days if isinstance(d, date)})
    if not ordered:
        return {"current": 0, "longest": 0, "longest_start": None, "longest_end": None}

    one_day = timedelta(days=1)

    longest = run = 1
    run_start = ordered[0]
    longest_start = longest_end = ordered[0]
    for previous, current in zip(ordered, ordered[1:]):
        if current - previous == one_day:
            run += 1
        else:
            run = 1
            run_start = current
        if run > longest:
            longest, longest_start, longest_end = run, run_start, current

    known = set(ordered)
    cursor = today if today in known else today - one_day
    current_streak = 0
    while cursor in known:
        current_streak += 1
        cursor -= one_day

    return {
        "current": current_streak,
        "longest": longest,
        "longest_start": longest_start.isoformat(),
        "longest_end": longest_end.isoformat(),
    }
