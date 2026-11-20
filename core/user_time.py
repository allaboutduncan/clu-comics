"""One answer to "what time is it for the user?".

The ``timezone`` preference is a fixed UTC offset -- the literal "UTC" or a
signed number of hours ("-5", "10", "5.5"). There is no zone name, so there is
no DST: the offset the user picked is the offset applied all year.

Every timestamp CLU stores is UTC (SQLite ``CURRENT_TIMESTAMP``), and every
schedule time in the ``schedules`` table is UTC by convention -- that is what
the Schedules page writes and reads through. Three places parsed that
preference independently before this module existed (``models/timeline.py`` as
a timedelta, ``models/stats.py`` as a SQLite '+N hours' modifier, and the
Schedules page's own JS) and the server never applied it to a next-run time at
all, which is why one sentence on that page could quote three different clocks.

Nothing here raises. A missing, blank or corrupt preference reads as UTC: a
wrong clock on a status line is cosmetic, while an exception would take out the
Schedules page, the reading timeline and the stats cache. It is also called
from bare APScheduler threads with no application context, so it must not
touch ``current_app``.
"""

import re
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
DISPLAY_FORMAT = "%Y-%m-%d %H:%M:%S"

# Real fixed offsets run -12..+14. Anything outside that is a typo or a
# corrupted preference, and shifting a timestamp by 400 hours is worse than
# ignoring the value.
_MIN_OFFSET = -12.0
_MAX_OFFSET = 14.0


def parse_offset_hours(raw):
    """Offset in hours for a stored preference value.

    Pure: no database, no clock, no exceptions. "UTC", None, "", a
    non-numeric string and an out-of-range number all read as 0.0.
    """
    if raw is None or isinstance(raw, bool):
        return 0.0
    if isinstance(raw, (int, float)):
        hours = float(raw)
    else:
        text = str(raw).strip()
        if not text or text.upper() == "UTC":
            return 0.0
        try:
            hours = float(text)
        except (TypeError, ValueError):
            return 0.0
    if hours != hours:  # NaN
        return 0.0
    return hours if _MIN_OFFSET <= hours <= _MAX_OFFSET else 0.0


def user_offset_hours():
    """The user's configured offset in hours. 0.0 when unset or unreadable."""
    try:
        # Imported here, not at module scope: core.database is a very large
        # module and this helper is imported from app.py's module body and from
        # unit tests, which must be able to load it on its own.
        from core.database import get_user_preference

        raw = get_user_preference("timezone", default="UTC")
    except Exception:
        return 0.0
    return parse_offset_hours(raw)


def user_tzinfo(hours=None):
    """A fixed-offset tzinfo for the user's preference."""
    hours = user_offset_hours() if hours is None else hours
    return UTC if hours == 0 else timezone(timedelta(hours=hours))


def sql_offset_modifier(hours=None):
    """The user's offset as a SQLite date modifier: "+0.0 hours", "-5.0 hours".

    The one place that turns the preference into SQL text. The value has been
    through ``parse_offset_hours`` (a float clamped to -12..+14), so what is
    formatted here is a number and nothing else -- safe to concatenate.
    """
    hours = user_offset_hours() if hours is None else parse_offset_hours(hours)
    return f"{hours:+.2f} hours"


_READ_AT_BUCKET = re.compile(
    r"\b(strftime\('%[A-Za-z]',\s*|date\()((?:\w+\.)?read_at)\)"
)


def localise_read_at_sql(sql, hours=None):
    """Make every ``strftime('%X', read_at)`` / ``date(read_at)`` in *sql*
    bucket by the user's local day instead of the UTC one.

    ``read_at`` is stored in UTC. Bucketing it raw put a comic read at 9pm in
    UTC-5 into the next day -- and, on 31 December, into the next year's
    Wrapped. The reading-history chart already shifted by the offset; Wrapped,
    the heatmap and the Timeline filters did not, so the same read could sit in
    two different months on one page.

    ``datetime()`` also normalises the two shapes the column holds
    ("2024-06-01T10:00:00" from a caller-supplied stamp, "2024-06-01 10:00:00"
    from CURRENT_TIMESTAMP). Only bucketing expressions are rewritten: an
    ``ORDER BY read_at`` is left alone, because a fixed shift cannot change an
    order.
    """
    modifier = sql_offset_modifier(hours)
    return _READ_AT_BUCKET.sub(
        lambda m: f"{m.group(1)}datetime({m.group(2)}, '{modifier}'))", sql
    )


def user_today(now=None, hours=None):
    """Today's date where the user is -- not the server's, and not UTC's."""
    reference = utc_now() if now is None else parse_utc(now)
    if reference is None:
        reference = utc_now()
    return reference.astimezone(user_tzinfo(hours)).date()


def local_date(value, hours=None):
    """The user's calendar date for a stored UTC timestamp, or None."""
    as_utc = parse_utc(value)
    if as_utc is None:
        return None
    return as_utc.astimezone(user_tzinfo(hours)).date()


def local_period_bounds_utc(year, month=None, hours=None):
    """``(start, end)`` of a local year or month as naive UTC datetimes.

    Half-open: ``start <= t < end``. Compare against ``datetime(read_at)`` in
    SQL (not the raw column -- see ``localise_read_at_sql`` for its two shapes)
    using ``strftime(DISPLAY_FORMAT)`` of these values.
    """
    tz = user_tzinfo(hours)
    if month is None:
        start = datetime(year, 1, 1, tzinfo=tz)
        end = datetime(year + 1, 1, 1, tzinfo=tz)
    else:
        start = datetime(year, month, 1, tzinfo=tz)
        end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=tz)
    return (
        start.astimezone(UTC).replace(tzinfo=None),
        end.astimezone(UTC).replace(tzinfo=None),
    )


def user_offset_label(hours=None):
    """"UTC", "UTC-05:00", "UTC+05:30" -- honest wording for a fixed offset."""
    hours = user_offset_hours() if hours is None else hours
    if hours == 0:
        return "UTC"
    sign = "+" if hours > 0 else "-"
    total_minutes = int(round(abs(hours) * 60))
    return f"UTC{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def utc_now():
    """Timezone-aware now, in UTC. The only clock this module reads."""
    return datetime.now(UTC)


def parse_utc(value):
    """*value* as an aware UTC datetime, or None if it cannot be read.

    A naive datetime or a bare "YYYY-MM-DD HH:MM:SS" string is read as UTC --
    that is what SQLite CURRENT_TIMESTAMP writes. An aware datetime is
    normalised with ``astimezone`` rather than assumed, which is what makes
    APScheduler's ``job.next_run_time`` convert correctly whatever zone the
    scheduler happens to be running in.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            return value.astimezone(UTC)
        return value.replace(tzinfo=UTC)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(UTC)
    return parsed.replace(tzinfo=UTC)


def to_user_time(value):
    """*value* as an aware datetime in the user's offset, or None."""
    as_utc = parse_utc(value)
    if as_utc is None:
        return None
    return as_utc.astimezone(user_tzinfo())


def format_user_time(value, fmt=DISPLAY_FORMAT, default=None):
    """*value* rendered in the user's offset, or *default* if unreadable."""
    shifted = to_user_time(value)
    if shifted is None:
        return default
    try:
        return shifted.strftime(fmt)
    except Exception:
        return default


def describe_age(value, now=None):
    """How long ago *value* was: "Just now", "5 minute(s) ago", and so on.

    Offset-independent by construction -- both sides are normalised to UTC.
    That is the bug this replaces: /api/file-index-status subtracted a UTC
    timestamp from a naive local ``datetime.now()``, so "3 hour(s) ago" was out
    by the host offset and a rebuild that had just finished could read as hours
    old. Returns None when *value* cannot be read, so the caller can fall back
    to showing the raw stamp.
    """
    stamp = parse_utc(value)
    if stamp is None:
        return None
    reference = utc_now() if now is None else parse_utc(now)
    if reference is None:
        return None
    seconds = (reference - stamp).total_seconds()
    # A negative delta means clock skew, not the future.
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} minute(s) ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} hour(s) ago"
    return f"{int(seconds // 86400)} day(s) ago"


def parse_schedule_time(value):
    """A stored schedule time ("HH:MM", UTC) as ``(hour, minute)``, or None.

    Pure and non-raising, like the rest of this module: a blank, malformed or
    out-of-range value is None so the caller can log and stand down.
    """
    try:
        parts = str(value).strip().split(":")
        hour, minute = int(parts[0]), int(parts[1])
    except (TypeError, ValueError, IndexError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def next_utc_day_at(schedule_time, now=None):
    """Tomorrow -- the next *UTC* day -- at *schedule_time*, as an aware UTC datetime.

    *schedule_time* is a ``schedules.time`` value, which is UTC by convention
    (see the module docstring). The weekly-packs retry used to apply it to a
    naive local ``datetime.now()``, so on a host at UTC-5 a 14:00 UTC schedule
    retried at 14:00 *local*, 19:00 UTC -- five hours late -- and the "tomorrow"
    was the host's tomorrow, which near midnight is a different calendar day
    from UTC's. Both halves are UTC here, and the result is aware, so
    APScheduler fires it at that instant whatever zone the scheduler runs in.

    *now* is injectable for tests; a naive value is read as UTC, an aware one
    is normalised. Returns None when either input cannot be read.
    """
    parsed = parse_schedule_time(schedule_time)
    if parsed is None:
        return None
    reference = utc_now() if now is None else parse_utc(now)
    if reference is None:
        return None
    hour, minute = parsed
    tomorrow = reference.date() + timedelta(days=1)
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, hour, minute, tzinfo=UTC)


def utc_now_iso():
    """Now, as "2026-09-23T16:47:05Z" -- parseable by JS `Date.parse`.

    The Schedules page renders its live clock from this one instant and ticks it
    forward locally, so it never has to poll and never reads the *browser's*
    zone.
    """
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def host_offset_label():
    """The offset of the clock this process is running on, e.g. "UTC-05:00".

    Not the user's preference: the two are different questions, and the gap
    between them is the whole reason the Schedules page shows a server clock.
    A container with TZ unset reports UTC, which is correct and expected; a
    bare-metal host reports its own zone, **including daylight saving**. The
    user's preference is a fixed offset and does not, so in summer a user in US
    Central has to pick -5 where they would pick -6 in winter -- and if they
    pick the wrong one, every schedule fires an hour out. Putting both on the
    page is what makes that visible.

    Returns "unknown" rather than raising: it decorates a page.
    """
    try:
        offset = datetime.now().astimezone().utcoffset()
        if offset is None:
            return "unknown"
        return user_offset_label(offset.total_seconds() / 3600.0)
    except Exception:
        return "unknown"
