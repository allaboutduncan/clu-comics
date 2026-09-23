"""core/user_time.py -- the one parser for the `timezone` preference.

The preference is a fixed UTC offset with no zone name, so there is no DST. It
used to be parsed independently in models/timeline.py and models/stats.py, and
the server never applied it to a next-run time at all -- which is why one line
on the Schedules page could read "Last sync: <UTC> | Next: <host zone>" while
the input box above it showed a third value.

Nothing in the module may raise: it is called from GET routes that render a
page and from bare APScheduler threads, so a corrupt preference has to degrade
to UTC rather than take the page down.
"""

from datetime import datetime, timedelta, timezone

import pytest

from core.user_time import (
    describe_age,
    format_user_time,
    host_offset_label,
    parse_offset_hours,
    parse_utc,
    to_user_time,
    user_offset_hours,
    user_offset_label,
    user_tzinfo,
    utc_now,
    utc_now_iso,
)


def _set_offset(monkeypatch, value):
    """Point the preference read at *value* for the duration of a test."""
    import core.database

    monkeypatch.setattr(
        core.database, "get_user_preference", lambda key, default=None: value
    )


class TestParseOffsetHours:
    """Pure: no database, no clock, and no exception for any input."""

    @pytest.mark.parametrize("raw,expected", [
        ("UTC", 0.0),
        ("utc", 0.0),
        ("0", 0.0),
        ("-5", -5.0),
        ("10", 10.0),
        ("5.5", 5.5),
        ("  -3  ", -3.0),
        (-5, -5.0),
        (5.5, 5.5),
    ])
    def test_valid_values(self, raw, expected):
        assert parse_offset_hours(raw) == expected

    @pytest.mark.parametrize("raw", [
        None, "", "   ", "banana", "5:30", [], {},
        True,   # a bool is an int in Python; it is not an offset
        False,
        float("nan"),
    ])
    def test_unreadable_values_read_as_utc(self, raw):
        assert parse_offset_hours(raw) == 0.0

    @pytest.mark.parametrize("raw", ["99", "-99", "400", 1000])
    def test_out_of_range_is_clamped_to_utc(self, raw):
        """A corrupt preference must not shift every timestamp by 400 hours.

        Real fixed offsets run -12..+14, so anything outside that is a typo or
        a damaged row, and ignoring it is strictly safer than honouring it.
        """
        assert parse_offset_hours(raw) == 0.0

    @pytest.mark.parametrize("raw", ["-12", "14"])
    def test_the_range_bounds_themselves_are_accepted(self, raw):
        assert parse_offset_hours(raw) == float(raw)


class TestUserOffsetHours:

    def test_reads_the_preference(self, monkeypatch):
        _set_offset(monkeypatch, "-5")
        assert user_offset_hours() == -5.0

    def test_a_raising_preference_read_is_utc_not_an_error(self, monkeypatch):
        """get_user_preference swallows its own errors, but an import failure in
        a bare scheduler thread would not -- and a clock is never worth a 500."""
        import core.database

        def boom(key, default=None):
            raise RuntimeError("no database yet")

        monkeypatch.setattr(core.database, "get_user_preference", boom)
        assert user_offset_hours() == 0.0


class TestUserOffsetLabel:
    """Wording has to be honest: a fixed offset, not "your local time"."""

    @pytest.mark.parametrize("hours,expected", [
        (0, "UTC"),
        (-5, "UTC-05:00"),
        (10, "UTC+10:00"),
        (5.5, "UTC+05:30"),
        (-3.5, "UTC-03:30"),
    ])
    def test_label(self, hours, expected):
        assert user_offset_label(hours) == expected


class TestUserTzinfo:

    def test_zero_is_utc(self):
        assert user_tzinfo(0) is timezone.utc

    def test_fractional_offset(self):
        assert user_tzinfo(5.5).utcoffset(None) == timedelta(hours=5, minutes=30)


class TestParseUtc:

    def test_sqlite_current_timestamp_is_read_as_utc(self):
        parsed = parse_utc("2026-01-15 12:00:00")
        assert parsed == datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)

    def test_a_naive_datetime_is_read_as_utc(self):
        parsed = parse_utc(datetime(2026, 1, 15, 12, 0))
        assert parsed == datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)

    def test_an_aware_datetime_is_normalised_not_assumed(self):
        """This is the load-bearing case.

        APScheduler hands back job.next_run_time as an aware datetime in the
        *scheduler's* zone -- the host's, since nothing sets TZ. Converting it
        with astimezone first is what makes the next-run display correct on a
        non-UTC host instead of only on a UTC one.
        """
        tokyo = timezone(timedelta(hours=9))
        parsed = parse_utc(datetime(2026, 1, 15, 21, 0, tzinfo=tokyo))
        assert parsed == datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)

    def test_iso_with_a_z_suffix(self):
        assert parse_utc("2026-01-15T12:00:00Z") == datetime(
            2026, 1, 15, 12, 0, tzinfo=timezone.utc
        )

    @pytest.mark.parametrize("raw", [None, "", "   ", "banana", "not a date"])
    def test_unreadable_values_are_none(self, raw):
        assert parse_utc(raw) is None


class TestFormatUserTime:

    def test_utc_is_unchanged(self, monkeypatch):
        _set_offset(monkeypatch, "UTC")
        assert format_user_time("2026-01-15 12:00:00") == "2026-01-15 12:00:00"

    def test_negative_offset(self, monkeypatch):
        _set_offset(monkeypatch, "-5")
        assert format_user_time("2026-01-15 12:00:00") == "2026-01-15 07:00:00"

    def test_positive_offset_rolls_the_date(self, monkeypatch):
        """A positive offset near midnight is where an off-by-one day shows up."""
        _set_offset(monkeypatch, "10")
        assert format_user_time("2026-01-15 20:00:00") == "2026-01-16 06:00:00"

    def test_fractional_offset(self, monkeypatch):
        _set_offset(monkeypatch, "5.5")
        assert format_user_time("2026-01-15 12:00:00") == "2026-01-15 17:30:00"

    def test_an_aware_next_run_time_converts_from_its_own_zone(self, monkeypatch):
        """What get_next_run_for_job hands in: an aware datetime, not a string."""
        _set_offset(monkeypatch, "5.5")
        tokyo = timezone(timedelta(hours=9))
        next_run = datetime(2026, 1, 15, 21, 0, tzinfo=tokyo)   # 12:00 UTC
        assert format_user_time(next_run) == "2026-01-15 17:30:00"

    def test_none_returns_the_default(self, monkeypatch):
        _set_offset(monkeypatch, "UTC")
        assert format_user_time(None) is None
        assert format_user_time(None, default="Never") == "Never"
        assert format_user_time("banana", default="Never") == "Never"

    def test_to_user_time_carries_the_offset(self, monkeypatch):
        _set_offset(monkeypatch, "5.5")
        shifted = to_user_time("2026-01-15 12:00:00")
        assert shifted.utcoffset() == timedelta(hours=5, minutes=30)


class TestDescribeAge:
    """Offset-independent by construction: both sides normalise to UTC.

    /api/file-index-status used to subtract a UTC stamp from a naive local
    datetime.now(), so on a UTC+N host a rebuild that had just finished read as
    N hour(s) ago.
    """

    NOW = "2026-01-15 12:00:00"

    @pytest.mark.parametrize("stamp,expected", [
        ("2026-01-15 11:59:30", "Just now"),
        ("2026-01-15 11:55:00", "5 minute(s) ago"),
        ("2026-01-15 09:00:00", "3 hour(s) ago"),
        ("2026-01-13 12:00:00", "2 day(s) ago"),
        ("2026-01-15 12:01:00", "Just now"),   # clock skew, not the future
    ])
    def test_wording(self, stamp, expected):
        assert describe_age(stamp, now=self.NOW) == expected

    def test_an_unreadable_stamp_is_none_so_the_caller_can_fall_back(self):
        assert describe_age(None) is None
        assert describe_age("banana") is None

    def test_the_answer_does_not_depend_on_the_users_offset(self, monkeypatch):
        seen = []
        for offset in ("UTC", "-5", "10", "5.5"):
            _set_offset(monkeypatch, offset)
            seen.append(describe_age("2026-01-15 09:00:00", now=self.NOW))
        assert seen == ["3 hour(s) ago"] * 4


def test_utc_now_is_aware_and_utc():
    now = utc_now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


class TestServerClockHelpers:
    """What the Schedules page renders its live clock from."""

    def test_utc_now_iso_is_parseable_by_js_date_parse(self):
        """The page does Date.parse() on this, so the Z suffix is required."""
        stamp = utc_now_iso()
        assert stamp.endswith("Z")
        assert "T" in stamp
        # Round-trips through the module's own reader.
        assert parse_utc(stamp) is not None

    def test_utc_now_iso_has_second_resolution_and_no_offset(self):
        stamp = utc_now_iso()
        assert len(stamp) == len("2026-09-23T16:47:05Z")
        assert "+" not in stamp

    def test_host_offset_label_describes_the_process_clock(self):
        """Not the preference -- the gap between the two is the point.

        A container with TZ unset reads UTC; a bare-metal host reads its own
        zone *including* daylight saving, while the preference is a fixed
        offset that does not. Showing both is what catches a user who picked
        UTC-06:00 for US Central in September, when it is really UTC-05:00.
        """
        label = host_offset_label()
        assert label == "UTC" or label.startswith("UTC+") or label.startswith("UTC-")

    def test_host_offset_label_does_not_render_a_raw_timedelta(self):
        """str(utcoffset()) for a negative offset is "-1 day, 19:00:00"."""
        assert "day" not in host_offset_label()

    def test_host_offset_label_never_raises(self, monkeypatch):
        import core.user_time

        monkeypatch.setattr(core.user_time, "user_offset_label",
                            lambda hours=None: (_ for _ in ()).throw(RuntimeError()))
        assert host_offset_label() == "unknown"
