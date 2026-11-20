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


# ---------------------------------------------------------------------------
# Yearly reading goal
# ---------------------------------------------------------------------------

# One user_settings row holding {"2026": 150}. It is personal data with no site
# default, so it is read with get_user_setting_override (no fall-through to a
# global preference) and is deliberately not in SETTINGS_SCHEMA.
GOALS_KEY = "reading_goals"
MAX_GOAL = 100000


def get_reading_goal(year, user_id=None):
    """The user's goal (issues) for *year*, or None when they have not set one."""
    from core.database import get_user_setting_override

    goals = get_user_setting_override(GOALS_KEY, user_id=user_id, default=None)
    if not isinstance(goals, dict):
        return None
    try:
        goal = int(goals.get(str(year)))
    except (TypeError, ValueError):
        return None
    return goal if 0 < goal <= MAX_GOAL else None


def set_reading_goal(year, goal, user_id=None):
    """Store *goal* for *year*; ``None`` or 0 removes it. Returns True on success."""
    from core.database import get_user_setting_override, set_user_setting

    goals = get_user_setting_override(GOALS_KEY, user_id=user_id, default=None)
    goals = dict(goals) if isinstance(goals, dict) else {}
    if goal:
        goals[str(year)] = int(goal)
    else:
        goals.pop(str(year), None)
    return bool(set_user_setting(GOALS_KEY, goals, user_id=user_id))


def goal_progress(read, goal, year, today):
    """Where *read* issues stand against *goal* for *year*, as of *today*.

    Pace is linear over the calendar year: ``expected`` is the share of the
    goal that should be done by today, ``delta`` how far ahead (positive) or
    behind that is, and ``projected`` the year-end total at the rate so far.
    A past year is complete, so its projection is simply what was read; a
    future year has no pace at all.

    Works without a goal (``goal`` None): the projection is still worth
    showing, and it is what suggests a goal in the first place.
    """
    start = date(year, 1, 1)
    days_in_year = (date(year + 1, 1, 1) - start).days
    if today.year > year:
        elapsed = days_in_year
    elif today.year < year:
        elapsed = 0
    else:
        elapsed = (today - start).days + 1
    remaining_days = days_in_year - elapsed
    fraction = elapsed / days_in_year

    projected = round(read / fraction) if fraction > 0 else 0
    result = {
        "year": year,
        "read": read,
        "goal": goal,
        "projected": projected,
        "days_remaining": remaining_days,
        "complete": elapsed >= days_in_year,
        "pct": None, "expected": None, "delta": None,
        "per_week_needed": None, "on_track": None, "reached": None,
    }
    if not goal:
        return result

    expected = goal * fraction
    to_go = max(goal - read, 0)
    weeks_left = remaining_days / 7
    result.update({
        "pct": round(read / goal * 100, 1),
        "expected": round(expected),
        "delta": round(read - expected),
        "reached": read >= goal,
        "on_track": read >= goal or read >= round(expected),
        # Nothing left to need once the goal is met or the year is over.
        "per_week_needed": round(to_go / weeks_left, 1) if to_go and weeks_left > 0 else None,
    })
    return result


# ---------------------------------------------------------------------------
# This week, last week, and the last five
# ---------------------------------------------------------------------------

OTHER_SERIES = "Other"


def week_start(day):
    """The Monday of *day*'s week."""
    return day - timedelta(days=day.weekday())


def _week_totals(events):
    by_day = [0] * 7
    for event in events:
        by_day[event.day.weekday()] += 1
    return {
        "issues": len(events),
        "pages": sum(e.pages for e in events),
        "seconds": sum(e.seconds for e in events),
        "by_day": by_day,
    }


def week_compare(events, today):
    """This week (Monday to today) against last week.

    ``last_week_to_date`` is last week's count up to the same weekday: on a
    Wednesday, "3 this week vs 7 last week" reads as falling behind when last
    Wednesday the count was 2. Both figures are returned so the page can say
    the fair one and still draw the whole of last week.
    """
    this_start = week_start(today)
    last_start = this_start - timedelta(days=7)
    this_week = [e for e in events if this_start <= e.day <= today]
    last_week = [e for e in events if last_start <= e.day < this_start]
    cutoff = last_start + timedelta(days=today.weekday())
    return {
        "week_start": this_start.isoformat(),
        "weekday": today.weekday(),
        "this_week": _week_totals(this_week),
        "last_week": _week_totals(last_week),
        "last_week_to_date": sum(1 for e in last_week if e.day <= cutoff),
    }


def five_week_series_stack(events, today, weeks=5, top_n=5):
    """Issues per week for the last *weeks* weeks, split by series.

    The busiest *top_n* series over the whole window get a band each and the
    rest are folded into "Other", so the chart has a readable legend however
    widely someone reads. A hidden read is counted (the totals must match the
    stat tiles, which include hidden reads) but never named: it goes into
    "Other" whatever its series.
    """
    first = week_start(today) - timedelta(days=7 * (weeks - 1))
    starts = [first + timedelta(days=7 * n) for n in range(weeks)]
    window = [e for e in events if first <= e.day <= today]

    def label(event):
        return OTHER_SERIES if event.hidden else (event.series or OTHER_SERIES)

    totals = {}
    for event in window:
        totals[label(event)] = totals.get(label(event), 0) + 1
    named = sorted(
        (name for name in totals if name != OTHER_SERIES),
        key=lambda name: (-totals[name], name.casefold()),
    )[:top_n]

    counts = {name: [0] * weeks for name in named}
    other = [0] * weeks
    for event in window:
        index = (event.day - first).days // 7
        counts.get(label(event), other)[index] += 1

    series = [{"name": name, "counts": counts[name]} for name in named]
    if any(other):
        series.append({"name": OTHER_SERIES, "counts": other})
    return {
        "weeks": [{"start": s.isoformat(), "label": f"{s.strftime('%b')} {s.day}"} for s in starts],
        "series": series,
        "totals": [sum(row["counts"][i] for row in series) for i in range(weeks)],
    }


# ---------------------------------------------------------------------------
# The shelf, and the summary the Insights page loads
# ---------------------------------------------------------------------------

def finished_shelf(events, limit=12):
    """The most recently finished issues, newest first. Hidden reads are not
    shown: hiding a read is asking for it not to be displayed."""
    visible = [e for e in events if not e.hidden]
    return [
        {
            "path": e.path,
            "name": e.path.replace("\\", "/").rsplit("/", 1)[-1],
            "series": e.series,
            "read_at": e.at.isoformat(),
        }
        for e in reversed(visible[-limit:])
    ]


def open_shelf(user_id=None, limit=8):
    """Comics the user is part-way through, most recently opened first."""
    from core.database import get_continue_reading_items

    return [
        {
            "path": item["comic_path"],
            "name": item["file_name"],
            "page": item["page_number"],
            "total_pages": item["total_pages"],
            "percent": item["progress_percent"],
        }
        for item in get_continue_reading_items(limit=limit, user_id=user_id)
    ]


def insights_summary(user_id=None, year=None, today=None, tz_hours=None):
    """Everything the top of the Insights page shows, from one read of the
    history.

    Hidden reads are **counted** here (goal, streaks, week totals), matching
    the page's existing tiles, and never **named** (shelf, series bands).
    """
    from core.user_time import user_today

    uid = current_user_id() if user_id is None else user_id
    hours = user_offset_hours() if tz_hours is None else tz_hours
    today = user_today(hours=hours) if today is None else today
    year = today.year if year is None else year

    events = load_read_events(uid, tz_hours=hours)
    in_year = sum(1 for e in events if e.day.year == year)

    return {
        "today": today.isoformat(),
        "goal": goal_progress(in_year, get_reading_goal(year, uid), year, today),
        "streak": compute_streaks(reading_days(events), today),
        "weeks": week_compare(events, today),
        "five_weeks": five_week_series_stack(events, today),
        "shelf": {
            "finished": finished_shelf(events),
            "open": open_shelf(uid),
        },
    }
