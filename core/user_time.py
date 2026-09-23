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
