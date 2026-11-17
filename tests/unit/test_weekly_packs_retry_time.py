"""The weekly-packs retry fires "tomorrow at the same time" -- in UTC.

Every schedule time in the ``schedules`` table is UTC. ``schedule_weekly_packs_retry``
used to take that "HH:MM" and apply it to a naive local ``datetime.now()``:

    tomorrow = datetime.now() + timedelta(days=1)
    run_time = tomorrow.replace(hour=hour, minute=minute, ...)

A naive ``run_date`` is read by APScheduler in the scheduler's zone, which is
the host's. So on a host at UTC-5 a 14:00 UTC schedule retried at 14:00 local
= 19:00 UTC, five hours late, and near midnight "tomorrow" was the host's
tomorrow rather than UTC's.

The calculation now lives in ``core.user_time.next_utc_day_at``. The clock is
injected as an aware datetime in a non-UTC zone, which is what the host's
local "now" is -- no ``time.tzset`` (it does not exist on Windows) and no
dependence on the machine running the tests.
"""

import ast
import os
from datetime import datetime, timedelta, timezone

import pytest

import core.user_time as user_time
from core.user_time import next_utc_day_at, parse_schedule_time

UTC = timezone.utc
US_CENTRAL_SUMMER = timezone(timedelta(hours=-5))
AEST = timezone(timedelta(hours=10))
INDIA = timezone(timedelta(hours=5, minutes=30))

APP_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "app.py",
)


def _old_naive_local_retry(schedule_time, local_now):
    """The removed app.py computation, run against a host clock of *local_now*.

    Returns the absolute instant APScheduler would have fired at: the naive
    wall-clock result interpreted in the host's zone.
    """
    hour, minute = (int(p) for p in schedule_time.split(":"))
    naive_now = local_now.replace(tzinfo=None)
    tomorrow = naive_now + timedelta(days=1)
    naive_run = tomorrow.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return naive_run.replace(tzinfo=local_now.tzinfo).astimezone(UTC)


class TestNextUtcDayAt:

    def test_result_is_aware_utc(self):
        now = datetime(2026, 11, 18, 14, 0, 5, tzinfo=UTC)
        result = next_utc_day_at("14:00", now=now)
        assert result.tzinfo is not None
        assert result.utcoffset() == timedelta(0)
        assert result == datetime(2026, 11, 19, 14, 0, tzinfo=UTC)

    def test_non_utc_host_just_after_the_scheduled_time(self):
        """The usual case: the scheduled run finds packs not ready and retries."""
        # 14:00 UTC schedule fires at 09:00 local on a UTC-5 host.
        local_now = datetime(2026, 11, 18, 9, 0, 30, tzinfo=US_CENTRAL_SUMMER)
        result = next_utc_day_at("14:00", now=local_now)
        assert result == datetime(2026, 11, 19, 14, 0, tzinfo=UTC)
        # Exactly one day after the run, not one day plus the host offset.
        assert result - local_now.astimezone(UTC) == timedelta(hours=23, minutes=59, seconds=30)

    def test_the_old_computation_was_out_by_the_host_offset(self):
        """Pins the bug this replaces, so the example in the PR is honest."""
        local_now = datetime(2026, 11, 18, 9, 0, 30, tzinfo=US_CENTRAL_SUMMER)
        old = _old_naive_local_retry("14:00", local_now)
        assert old == datetime(2026, 11, 19, 19, 0, tzinfo=UTC)
        assert old - next_utc_day_at("14:00", now=local_now) == timedelta(hours=5)

    def test_non_utc_host_before_the_scheduled_time(self):
        """A manual Run Now earlier in the UTC day still targets the next UTC day."""
        local_now = datetime(2026, 11, 18, 3, 0, tzinfo=US_CENTRAL_SUMMER)  # 08:00 UTC
        result = next_utc_day_at("14:00", now=local_now)
        assert result == datetime(2026, 11, 19, 14, 0, tzinfo=UTC)

    def test_host_ahead_of_utc_across_the_day_boundary(self):
        """Local date is already the 19th while UTC is still the 18th."""
        # 22:00 UTC on the 18th is 08:00 on the 19th in AEST.
        local_now = datetime(2026, 11, 19, 8, 0, tzinfo=AEST)
        result = next_utc_day_at("22:00", now=local_now)
        assert result == datetime(2026, 11, 19, 22, 0, tzinfo=UTC)
        # The local-date version skipped a whole day.
        assert _old_naive_local_retry("22:00", local_now) == datetime(
            2026, 11, 20, 12, 0, tzinfo=UTC
        )

    def test_host_behind_utc_across_the_day_boundary(self):
        """UTC has already rolled over to the 19th while the host is on the 18th."""
        # 01:00 UTC on the 19th is 20:00 on the 18th at UTC-5.
        local_now = datetime(2026, 11, 18, 20, 0, tzinfo=US_CENTRAL_SUMMER)
        result = next_utc_day_at("01:00", now=local_now)
        assert result == datetime(2026, 11, 20, 1, 0, tzinfo=UTC)
        assert _old_naive_local_retry("01:00", local_now) == datetime(
            2026, 11, 19, 6, 0, tzinfo=UTC
        )

    def test_fractional_offset_host(self):
        local_now = datetime(2026, 11, 18, 15, 30, tzinfo=INDIA)  # 10:00 UTC
        assert next_utc_day_at("10:00", now=local_now) == datetime(
            2026, 11, 19, 10, 0, tzinfo=UTC
        )

    def test_month_and_year_rollover(self):
        now = datetime(2026, 12, 31, 23, 30, tzinfo=UTC)
        assert next_utc_day_at("23:30", now=now) == datetime(2027, 1, 1, 23, 30, tzinfo=UTC)

    def test_naive_now_is_read_as_utc(self):
        now = datetime(2026, 11, 18, 23, 59)
        assert next_utc_day_at("00:05", now=now) == datetime(2026, 11, 19, 0, 5, tzinfo=UTC)

    def test_default_clock_is_utc_now(self, monkeypatch):
        fixed = datetime(2026, 11, 18, 14, 0, tzinfo=UTC)
        monkeypatch.setattr(user_time, "utc_now", lambda: fixed)
        assert next_utc_day_at("14:00") == datetime(2026, 11, 19, 14, 0, tzinfo=UTC)

    @pytest.mark.parametrize("bad", [None, "", "14", "14:xx", "24:00", "12:60", "-1:00"])
    def test_unreadable_time_is_none_not_an_exception(self, bad):
        now = datetime(2026, 11, 18, 14, 0, tzinfo=UTC)
        assert next_utc_day_at(bad, now=now) is None

    def test_unreadable_now_is_none(self):
        assert next_utc_day_at("14:00", now="not a time") is None


class TestParseScheduleTime:

    @pytest.mark.parametrize(
        "value, expected",
        [("00:00", (0, 0)), ("9:05", (9, 5)), ("23:59", (23, 59)), (" 10:00 ", (10, 0))],
    )
    def test_valid(self, value, expected):
        assert parse_schedule_time(value) == expected


class TestAppPyWiring:
    """app.py cannot be imported in tests, so the call site is asserted on its AST."""

    @pytest.fixture(scope="class")
    def func(self):
        with open(APP_PY, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename="app.py")
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "schedule_weekly_packs_retry":
                return node
        raise AssertionError("app.py no longer defines schedule_weekly_packs_retry()")

    @staticmethod
    def _call_names(node):
        names = []
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                target = child.func
                names.append(getattr(target, "id", None) or getattr(target, "attr", None))
        return names

    def test_run_time_comes_from_the_shared_helper(self, func):
        assert "next_utc_day_at" in self._call_names(func)

    def test_reads_no_local_clock(self, func):
        """A naive local now is exactly the bug; there is no reason to read one here."""
        offenders = [
            child.lineno
            for child in ast.walk(func)
            if isinstance(child, ast.Call)
            and getattr(child.func, "attr", None) in ("now", "utcnow", "today")
        ]
        assert not offenders, f"schedule_weekly_packs_retry reads a clock at app.py:{offenders}"

    def test_does_not_rebuild_the_time_by_hand(self, func):
        assert "replace" not in self._call_names(func), (
            "replace(hour=, minute=) on a datetime is the removed computation"
        )
