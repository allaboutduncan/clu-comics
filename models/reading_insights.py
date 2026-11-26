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
from datetime import date, datetime, timedelta, timezone

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


# ---------------------------------------------------------------------------
# Reading clock and the day view
# ---------------------------------------------------------------------------

# A finished issue with no session behind it only says when it was *finished*
# and how long it took in total, possibly over several sittings. Its time is
# laid back from the finish, but never further than this: a comic that took
# six hours across a week was not being read for the six hours before it ended.
FINISH_TIME_CAP_SECONDS = 2 * 60 * 60


def _spread_over_hours(start, end, seconds, hours):
    """Add *seconds* of reading between *start* and *end* (aware, local) to the
    24 *hours* buckets, in proportion to how much of the span falls in each."""
    span = (end - start).total_seconds()
    if seconds <= 0:
        return
    if span <= 0:
        hours[start.hour] += seconds
        return
    cursor = start
    while cursor < end:
        next_hour = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        piece_end = min(next_hour, end)
        hours[cursor.hour] += seconds * (piece_end - cursor).total_seconds() / span
        cursor = piece_end


def reading_clock(events, sessions, tz):
    """When reading happens: seconds per hour of day and per weekday.

    A comic that has sessions is described by them. One that has none (all
    history from before sessions were recorded, and reads from clients that
    do not report progress) falls back to its finished-issue row, using the
    finish time and the time spent -- the same rule ``day_detail`` draws by.
    Issues with no reading time at all -- a manual "mark as read", an import
    -- say nothing about the clock and are only counted in ``untimed``.

    ``source`` tells the page how much to trust the picture: ``sessions``,
    ``finish_times``, ``mixed`` or ``none``.
    """
    hours = [0.0] * 24
    weekdays = [0.0] * 7
    session_paths = set()
    from_sessions = from_finishes = untimed = 0

    for session in sessions:
        seconds = session.get("active_seconds") or 0
        start = session["started_at"].astimezone(tz)
        end = session["ended_at"].astimezone(tz)
        session_paths.add(session["comic_path"])
        if seconds <= 0:
            continue
        _spread_over_hours(start, end, seconds, hours)
        weekdays[start.weekday()] += seconds
        from_sessions += 1

    for event in events:
        if event.path in session_paths:
            continue
        if not event.timed:
            untimed += 1
            continue
        seconds = min(event.seconds, FINISH_TIME_CAP_SECONDS)
        _spread_over_hours(event.at - timedelta(seconds=seconds), event.at, seconds, hours)
        weekdays[event.day.weekday()] += seconds
        from_finishes += 1

    if from_sessions and from_finishes:
        source = "mixed"
    elif from_sessions:
        source = "sessions"
    elif from_finishes:
        source = "finish_times"
    else:
        source = "none"

    def peak(values):
        return max(range(len(values)), key=values.__getitem__) if any(values) else None

    return {
        "hours": [round(v) for v in hours],
        "weekdays": [round(v) for v in weekdays],
        "peak_hour": peak(hours),
        "peak_weekday": peak(weekdays),
        "total_seconds": round(sum(weekdays)),
        "source": source,
        "sessions": from_sessions,
        "finishes": from_finishes,
        "untimed": untimed,
    }


def clock_for_period(user_id=None, year=None, tz_hours=None):
    """``reading_clock`` for one local year, or for all time when *year* is None."""
    from core.reading_sessions import load_sessions
    from core.user_time import local_period_bounds_utc

    uid = current_user_id() if user_id is None else user_id
    hours = user_offset_hours() if tz_hours is None else tz_hours
    start, end = local_period_bounds_utc(year, hours=hours) if year else (None, None)
    return reading_clock(
        load_read_events(uid, start, end, tz_hours=hours),
        load_sessions(uid, start, end),
        user_tzinfo(hours),
    )


def _basename(path):
    return str(path).replace("\\", "/").rsplit("/", 1)[-1]


def day_detail(local_day, user_id=None, tz_hours=None):
    """Everything read on one local day, for the day view's time ruler.

    ``sessions`` are sittings that started that day (with start and end);
    ``reads`` are the issues finished that day. A hidden read is left out of
    both -- this view names comics, and hiding a read is asking for it not to
    be shown -- but ``hidden`` says how many were, so the total can still be
    explained against the charts, which count them.

    Times are ISO strings in the user's offset, and ``minutes`` gives each
    block's position on a 0..1440 ruler so the page does no date arithmetic.
    """
    from core.reading_sessions import load_sessions, session_pages

    uid = current_user_id() if user_id is None else user_id
    hours = user_offset_hours() if tz_hours is None else tz_hours
    tz = user_tzinfo(hours)
    start_local = datetime(local_day.year, local_day.month, local_day.day, tzinfo=tz)
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = start_utc + timedelta(days=1)

    def minute_of_day(moment):
        return max(0, min(1440, round((moment - start_local).total_seconds() / 60)))

    events = load_read_events(uid, start_utc, end_utc, tz_hours=hours)
    hidden_paths = {e.path for e in events if e.hidden}
    series_by_path = {e.path: e.series for e in events}

    sessions = []
    for session in load_sessions(uid, start_utc, end_utc):
        path = session["comic_path"]
        if path in hidden_paths:
            continue
        started = session["started_at"].astimezone(tz)
        ended = session["ended_at"].astimezone(tz)
        sessions.append({
            "path": path,
            "name": _basename(path),
            "series": series_by_path.get(path) or series_from_path(path),
            "start": started.isoformat(),
            "end": ended.isoformat(),
            "minutes": [minute_of_day(started), minute_of_day(ended)],
            "active_seconds": session.get("active_seconds") or 0,
            "pages": session_pages(session),
            "finished": bool(session.get("finished")),
        })

    with_session = {s["path"] for s in sessions}
    reads = []
    for event in events:
        if event.hidden:
            continue
        item = {
            "path": event.path,
            "name": _basename(event.path),
            "series": event.series,
            "at": event.at.isoformat(),
            "pages": event.pages,
            "seconds": event.seconds,
            "timed": event.timed,
            "has_session": event.path in with_session,
            "minutes": None,
        }
        # Drawn on the ruler only when nothing better describes it: a session
        # is the real record, and an untimed mark has no place on a clock.
        if event.timed and not item["has_session"]:
            spent = min(event.seconds, FINISH_TIME_CAP_SECONDS)
            item["minutes"] = [
                minute_of_day(event.at - timedelta(seconds=spent)), minute_of_day(event.at)]
        reads.append(item)

    return {
        "date": local_day.isoformat(),
        "sessions": sessions,
        "reads": reads,
        "hidden": len(hidden_paths),
        "issues": len(reads),
        "pages": sum(r["pages"] for r in reads),
        "active_seconds": (
            sum(s["active_seconds"] for s in sessions)
            + sum(r["seconds"] for r in reads if r["minutes"])
        ),
    }


# ---------------------------------------------------------------------------
# Constellation: who appears with whom
# ---------------------------------------------------------------------------

CONSTELLATION_KINDS = ("characters", "creators")
CONSTELLATION_NODE_LIMIT = 40
CONSTELLATION_EDGES_PER_NODE = 5


def _graph_members(event, kind):
    """``[(name, role)]`` for one read. *role* is the /browse category the
    name links to."""
    if kind == "creators":
        members = [(name, "writer") for name in event.writers]
        known = {name.casefold() for name in event.writers}
        # Someone who both wrote and drew an issue is one node, not two.
        members += [(name, "penciller") for name in event.pencillers
                    if name.casefold() not in known]
        return members
    return [(name, "characters") for name in event.characters]


def character_graph(events, kind="characters", limit=CONSTELLATION_NODE_LIMIT,
                    edges_per_node=CONSTELLATION_EDGES_PER_NODE):
    """Who appears together across the issues read.

    A node is a character (or, for ``kind="creators"``, a writer or artist)
    sized by the number of issues they are in; an edge joins two who share an
    issue, weighted by how many. Only the *limit* most frequent are kept, and
    each keeps only its *edges_per_node* strongest ties, so a team book with
    thirty names in every issue does not turn the picture into a solid ball.

    Hidden reads are left out: the graph names what is in them.
    Names are matched case-insensitively and shown as first spelled.
    """
    spelling, role_of, counts = {}, {}, {}
    per_issue = []
    for event in events:
        if event.hidden:
            continue
        keys = []
        for name, role in _graph_members(event, kind):
            key = name.casefold()
            if key in keys:
                continue
            spelling.setdefault(key, name)
            role_of.setdefault(key, role)
            counts[key] = counts.get(key, 0) + 1
            keys.append(key)
        if keys:
            per_issue.append(keys)

    top = sorted(counts, key=lambda key: (-counts[key], key))[:limit]
    index = {key: i for i, key in enumerate(top)}

    weights = {}
    for keys in per_issue:
        present = sorted(index[key] for key in keys if key in index)
        for i, a in enumerate(present):
            for b in present[i + 1:]:
                weights[(a, b)] = weights.get((a, b), 0) + 1

    # Each node nominates its strongest ties; an edge survives if either end
    # nominates it.
    by_node = {}
    for (a, b), weight in weights.items():
        by_node.setdefault(a, []).append((weight, a, b))
        by_node.setdefault(b, []).append((weight, a, b))
    kept = set()
    for ties in by_node.values():
        ties.sort(key=lambda tie: (-tie[0], tie[1], tie[2]))
        kept.update((a, b) for _, a, b in ties[:edges_per_node])

    return {
        "nodes": [
            {"id": i, "name": spelling[key], "count": counts[key], "role": role_of[key]}
            for i, key in enumerate(top)
        ],
        "edges": [
            {"source": a, "target": b, "weight": weights[(a, b)]}
            for a, b in sorted(kept)
        ],
        "issues": len(per_issue),
        "total": len(counts),
    }


# The card the graph is drawn in is wider than tall (clu-constellation.js draws
# 1000 x 560). Spacing is enforced in that shape, not in the unit square.
CONSTELLATION_ASPECT = 0.56
# Closest two node centres may sit, as a share of the drawing's width. The
# largest node is 44px across in a 1000px drawing, so 0.105 leaves room
# between any two for a line to be seen.
CONSTELLATION_MIN_GAP = 0.105
CONSTELLATION_MARGIN = 0.05


def layout_graph(node_count, edges, iterations=220):
    """Positions in the unit square for a small graph, as ``[(x, y), ...]``.

    Two passes:

    1. A Fruchterman-Reingold spring layout decides the *shape*: every pair of
       nodes repels, every edge pulls, and a pull to the centre keeps separate
       groups from drifting to the corners. An edge's pull is divided by how
       many ties its ends have, because real data is cliques -- every
       character in a team book shares every issue with every other -- and
       with an undivided pull a ten-node clique collapses onto one point.
    2. A spacing pass then guarantees *legibility*: any two nodes closer than
       ``CONSTELLATION_MIN_GAP`` are pushed apart, measured in the drawing's
       real proportions. The spring pass alone cannot promise that, and
       stretching its result to fill the card (which this used to do) kept
       tight groups tight while moving them further from each other.

    Computed here rather than in the browser so that it is **deterministic**
    -- the same reading history draws the same sky on every load and every
    device -- and so the rules have tests. There is no randomness: nodes start
    on a golden-angle spiral in id order.
    """
    import math

    if node_count == 0:
        return []
    if node_count == 1:
        return [(0.5, 0.5)]

    golden = math.pi * (3 - math.sqrt(5))
    xs, ys = [], []
    for i in range(node_count):
        radius = 0.45 * math.sqrt((i + 0.5) / node_count)
        xs.append(radius * math.cos(i * golden))
        ys.append(radius * math.sin(i * golden))

    degree = [0] * node_count
    for edge in edges:
        degree[edge["source"]] += 1
        degree[edge["target"]] += 1

    k = math.sqrt(1.0 / node_count)
    heaviest = max((e["weight"] for e in edges), default=1)
    pulls = [
        (e["source"], e["target"],
         (0.3 + 0.7 * e["weight"] / heaviest) / math.sqrt(degree[e["source"]] * degree[e["target"]]))
        for e in edges
    ]

    for step in range(iterations):
        temperature = 0.1 * (1 - step / iterations)
        dx = [0.0] * node_count
        dy = [0.0] * node_count
        for i in range(node_count):
            for j in range(i + 1, node_count):
                ox, oy = xs[i] - xs[j], ys[i] - ys[j]
                distance = math.hypot(ox, oy) or 0.001
                force = k * k / distance
                fx, fy = ox / distance * force, oy / distance * force
                dx[i] += fx
                dy[i] += fy
                dx[j] -= fx
                dy[j] -= fy
        for a, b, pull in pulls:
            ox, oy = xs[a] - xs[b], ys[a] - ys[b]
            distance = math.hypot(ox, oy) or 0.001
            force = distance * distance / k * pull
            fx, fy = ox / distance * force, oy / distance * force
            dx[a] -= fx
            dy[a] -= fy
            dx[b] += fx
            dy[b] += fy
        for i in range(node_count):
            dx[i] -= xs[i] * 1.2 * k
            dy[i] -= ys[i] * 1.2 * k
            length = math.hypot(dx[i], dy[i]) or 0.001
            move = min(length, temperature)
            xs[i] += dx[i] / length * move
            ys[i] += dy[i] / length * move

    # Into the drawing's proportions: u across (0..1), v down (0..ASPECT).
    margin = CONSTELLATION_MARGIN
    width, height = 1 - 2 * margin, CONSTELLATION_ASPECT - 2 * margin

    def fit(values, size):
        low, high = min(values), max(values)
        if high - low < 1e-9:
            return [margin + size / 2] * len(values)
        return [margin + size * (v - low) / (high - low) for v in values]

    us, vs = fit(xs, width), fit(ys, height)

    # Spacing pass. The gap shrinks if the card cannot hold this many nodes at
    # the full gap (it can, at the 40-node limit; this is for a caller who
    # raises the limit).
    gap = min(CONSTELLATION_MIN_GAP, 0.8 * math.sqrt(width * height / node_count))
    for _ in range(300):
        moved = False
        for i in range(node_count):
            for j in range(i + 1, node_count):
                ou, ov = us[i] - us[j], vs[i] - vs[j]
                distance = math.hypot(ou, ov)
                if distance >= gap:
                    continue
                if distance < 1e-6:
                    # Exactly coincident: part them along a direction fixed by
                    # their ids, so the result stays deterministic.
                    angle = (i * 7 + j * 13) * golden
                    ou, ov, distance = math.cos(angle), math.sin(angle), 1.0
                push = (gap - distance) / 2 + 1e-4
                us[i] += ou / distance * push
                vs[i] += ov / distance * push
                us[j] -= ou / distance * push
                vs[j] -= ov / distance * push
                moved = True
        for i in range(node_count):
            us[i] = min(max(us[i], margin), margin + width)
            vs[i] = min(max(vs[i], margin), margin + height)
        if not moved:
            break

    return [(round(u, 4), round(v / CONSTELLATION_ASPECT, 4)) for u, v in zip(us, vs)]


def constellation(user_id=None, year=None, kind="characters", tz_hours=None):
    """The laid-out graph for one user, a year (None = all time) and a kind.

    Cached in ``stats_cache`` under the user's prefix, which marking a comic
    read and the metadata backfill both clear -- the two things that change it.
    """
    from core.database import get_cached_stats, save_cached_stats
    from core.user_time import local_period_bounds_utc, sql_offset_modifier

    if kind not in CONSTELLATION_KINDS:
        kind = "characters"
    uid = current_user_id() if user_id is None else user_id
    hours = user_offset_hours() if tz_hours is None else tz_hours

    cache_key = f"u{uid}:constellation2:{kind}:{year or 'all'}:{sql_offset_modifier(hours)}"
    cached = get_cached_stats(cache_key)
    if cached:
        return cached

    start, end = local_period_bounds_utc(year, hours=hours) if year else (None, None)
    graph = character_graph(
        load_read_events(uid, start, end, include_hidden=False, tz_hours=hours), kind=kind)
    for node, (x, y) in zip(graph["nodes"], layout_graph(len(graph["nodes"]), graph["edges"])):
        node["x"], node["y"] = x, y
    graph["kind"] = kind
    graph["year"] = year
    if graph["nodes"]:
        save_cached_stats(cache_key, graph)
    return graph


# ---------------------------------------------------------------------------
# The story a Wrapped recap tells
# ---------------------------------------------------------------------------

# Thresholds for the reading personality. Module constants so the rule table
# in the tests reads against names, not magic numbers.
PERSONALITY_MIN_ISSUES = 5
RITUAL_ACTIVE_SHARE = 0.6
RITUAL_STREAK = {"yearly": 30, "monthly": 14}
BINGE_DAY_ISSUES = 5
BINGE_SHARE = 0.5
BINGE_MAX_ACTIVE_SHARE = 0.25
DEVOTEE_SERIES_SHARE = 0.5
EXPLORER_SERIES_RATIO = 0.6
EXPLORER_MIN_PUBLISHERS = 5
LOYALIST_PUBLISHER_SHARE = 0.8
LOYALIST_MIN_SERIES = 5
CLOCK_TRUST_SHARE = 0.6
NIGHT_SHARE = 0.5
MORNING_SHARE = 0.5
WEEKEND_SHARE = 0.6
MARATHON_ISSUES = {"yearly": 300, "monthly": 30}

PERSONALITIES = {
    "starting": ("Just Getting Started", "A few issues in. The story is only beginning."),
    "ritualist": ("The Daily Ritualist", "Reading is part of the day, most days."),
    "binger": ("The Binge Reader", "Quiet for a while, then a whole run in one sitting."),
    "devotee": ("The Devotee", "One series had your attention above all others."),
    "explorer": ("The Explorer", "Always another series, another publisher, another world."),
    "loyalist": ("The House Loyalist", "Many series, one publisher."),
    "night-owl": ("The Night Owl", "The best pages are read after dark."),
    "early-bird": ("The Early Bird", "Comics before the day gets going."),
    "weekender": ("The Weekend Warrior", "Saturdays and Sundays are for reading."),
    "marathoner": ("The Marathoner", "Sheer volume. Issue after issue after issue."),
    "steady": ("The Steady Reader", "A steady pace and a bit of everything."),
}


def reading_personality(summary):
    """One label for how someone read in a period. First matching rule wins.

    *summary* is the dict ``period_story`` builds (``_personality_inputs``).
    The order is deliberate: habits that describe *when and how* someone reads
    (every day, in bursts) come before *what* they read, and sheer volume comes
    last so it only names a reader nothing more specific describes. The two
    time-of-day rules need most of the reading to carry a real reading time;
    without that the clock is guesswork (see ``ReadEvent.timed``).

    Returns ``{id, label, blurb, evidence}``; *evidence* is the one fact that
    earned the label, for the slide to show.
    """
    scope = summary.get("scope", "yearly")
    issues = summary.get("issues", 0)

    def result(key, evidence):
        label, blurb = PERSONALITIES[key]
        return {"id": key, "label": label, "blurb": blurb, "evidence": evidence}

    def pct(value):
        return f"{round(value * 100)}%"

    if issues < PERSONALITY_MIN_ISSUES:
        return result("starting", f"{issues} issue{'' if issues == 1 else 's'} read")

    active_share = summary.get("active_days", 0) / max(summary.get("period_days", 1), 1)
    streak = summary.get("longest_streak", 0)
    if active_share >= RITUAL_ACTIVE_SHARE or streak >= RITUAL_STREAK[scope]:
        if streak >= RITUAL_STREAK[scope]:
            return result("ritualist", f"A {streak}-day streak")
        return result("ritualist", f"Read on {pct(active_share)} of days")

    if summary.get("binge_share", 0) >= BINGE_SHARE and active_share < BINGE_MAX_ACTIVE_SHARE:
        return result("binger", f"{pct(summary['binge_share'])} read on days of "
                                f"{BINGE_DAY_ISSUES} or more")

    if summary.get("top_series_share", 0) >= DEVOTEE_SERIES_SHARE:
        return result("devotee", f"{pct(summary['top_series_share'])} of your reading was "
                                 f"{summary.get('top_series') or 'one series'}")

    series = summary.get("distinct_series", 0)
    publishers = summary.get("distinct_publishers", 0)
    if series / issues >= EXPLORER_SERIES_RATIO and publishers >= EXPLORER_MIN_PUBLISHERS:
        return result("explorer", f"{series} series from {publishers} publishers")

    if summary.get("top_publisher_share", 0) >= LOYALIST_PUBLISHER_SHARE \
            and series >= LOYALIST_MIN_SERIES:
        return result("loyalist", f"{pct(summary['top_publisher_share'])} from "
                                  f"{summary.get('top_publisher') or 'one publisher'}")

    if summary.get("timed_share", 0) >= CLOCK_TRUST_SHARE:
        if summary.get("night_share", 0) >= NIGHT_SHARE:
            return result("night-owl", f"{pct(summary['night_share'])} of reading time "
                                       "between 10pm and 4am")
        if summary.get("morning_share", 0) >= MORNING_SHARE:
            return result("early-bird", f"{pct(summary['morning_share'])} of reading time "
                                        "between 5am and 9am")

    if summary.get("weekend_share", 0) >= WEEKEND_SHARE:
        return result("weekender", f"{pct(summary['weekend_share'])} read at the weekend")

    if issues >= MARATHON_ISSUES[scope]:
        return result("marathoner", f"{issues:,} issues")

    return result("steady", f"{issues:,} issues across {series} series")


def _counted(names_per_event, limit):
    """Top names by how many reads they appear in, as ``[{name, count}]``.
    Case-insensitive, first spelling kept, ties by name."""
    spelling, counts = {}, {}
    for names in names_per_event:
        for key in {name.casefold(): name for name in names}:
            counts[key] = counts.get(key, 0) + 1
        for name in names:
            spelling.setdefault(name.casefold(), name)
    top = sorted(counts, key=lambda key: (-counts[key], key))[:limit]
    return [{"name": spelling[key], "count": counts[key]} for key in top]


def top_credits(events, limit=5):
    """The most-read writers, artists, characters and publishers."""
    return {
        "writers": _counted((e.writers for e in events), limit),
        "artists": _counted((e.pencillers for e in events), limit),
        "characters": _counted((e.characters for e in events), limit),
        "publishers": _counted(((e.publisher,) if e.publisher else () for e in events), limit),
    }


def _personality_inputs(scope, events, clock, streak, period_days):
    issues = len(events)
    by_day, by_series, by_publisher = {}, {}, {}
    weekend = 0
    for event in events:
        by_day[event.day] = by_day.get(event.day, 0) + 1
        by_series[event.series] = by_series.get(event.series, 0) + 1
        if event.publisher:
            key = event.publisher.casefold()
            by_publisher.setdefault(key, [event.publisher, 0])[1] += 1
        if event.day.weekday() >= 5:
            weekend += 1
    top_series = max(by_series, key=lambda name: (by_series[name], name)) if by_series else None
    top_publisher = max(by_publisher.values(), key=lambda pair: pair[1]) if by_publisher else None

    clock_total = sum(clock["hours"]) or 0
    night = sum(clock["hours"][h] for h in (22, 23, 0, 1, 2, 3))
    morning = sum(clock["hours"][h] for h in (5, 6, 7, 8))
    with_time = sum(1 for e in events if e.timed)

    return {
        "scope": scope,
        "issues": issues,
        "active_days": len(by_day),
        "period_days": period_days,
        "longest_streak": streak,
        "binge_share": (sum(n for n in by_day.values() if n >= BINGE_DAY_ISSUES) / issues) if issues else 0,
        "top_series": top_series,
        "top_series_share": (by_series[top_series] / issues) if top_series else 0,
        "distinct_series": len(by_series),
        "distinct_publishers": len(by_publisher),
        "top_publisher": top_publisher[0] if top_publisher else None,
        "top_publisher_share": (top_publisher[1] / issues) if top_publisher else 0,
        "timed_share": (with_time / issues) if issues else 0,
        "night_share": (night / clock_total) if clock_total else 0,
        "morning_share": (morning / clock_total) if clock_total else 0,
        "weekend_share": (weekend / issues) if issues else 0,
    }


def period_story(scope, year, month=None, user_id=None, today=None, tz_hours=None):
    """Everything the Wrapped story slides say about a year or a month.

    One read of the period, and every slide draws from this dict, so two
    slides cannot disagree about a number. **Hidden reads are left out
    entirely**, as they are from the rest of Wrapped and the Timeline: a
    recap is something people share.

    The streak here is the longest run *inside the period*, and the clock is
    the period's, which is why neither is taken from ``insights_summary``.
    """
    from core.reading_sessions import load_sessions
    from core.user_time import local_period_bounds_utc, user_today

    uid = current_user_id() if user_id is None else user_id
    hours = user_offset_hours() if tz_hours is None else tz_hours
    tz = user_tzinfo(hours)
    today = user_today(hours=hours) if today is None else today
    start, end = local_period_bounds_utc(year, month, hours)

    everything = load_read_events(uid, start, end, tz_hours=hours)
    hidden_paths = {e.path for e in everything if e.hidden}
    events = [e for e in everything if not e.hidden]
    sessions = [s for s in load_sessions(uid, start, end) if s["comic_path"] not in hidden_paths]

    first_day = date(year, month or 1, 1)
    if month:
        last_day = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    else:
        last_day = date(year, 12, 31)
    # A period still in progress is judged on the days it has had so far.
    period_days = (min(last_day, max(today, first_day)) - first_day).days + 1

    days = reading_days(events)
    # "today" past the end of the period: there is no current streak to keep
    # alive, only the longest one.
    streaks = compute_streaks(days, last_day + timedelta(days=2))
    clock = reading_clock(events, sessions, tz)

    by_day = {}
    for event in events:
        by_day[event.day] = by_day.get(event.day, 0) + 1
    busiest = max(by_day, key=lambda d: (by_day[d], d)) if by_day else None
    seconds = sum(e.seconds for e in events)
    pages = sum(e.pages for e in events)

    story = {
        "scope": scope,
        "year": year,
        "month": month,
        "issues": len(events),
        "pages": pages,
        "seconds": seconds,
        "active_days": len(days),
        "period_days": period_days,
        "longest_streak": streaks["longest"],
        "longest_streak_start": streaks["longest_start"],
        "longest_streak_end": streaks["longest_end"],
        "busiest_day": busiest.isoformat() if busiest else None,
        "busiest_day_issues": by_day[busiest] if busiest else 0,
        "series": len({e.series for e in events}),
        "credits": top_credits(events),
        "clock": clock,
        "goal": None,
    }
    story["personality"] = reading_personality(
        _personality_inputs(scope, events, clock, streaks["longest"], period_days))
    if not month:
        goal = get_reading_goal(year, uid)
        if goal:
            # The goal counts hidden reads on Insights; a recap does not show
            # them, so it reports the visible count against the same goal.
            story["goal"] = goal_progress(len(events), goal, year, today)
    return story
