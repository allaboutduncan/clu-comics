"""The Schedules page and the scheduler must agree on one clock.

Schedule times are stored as UTC -- that is the convention the page writes
(localToUtc) and reads (utcToLocal). Three things on the server used to ignore
it, so the page showed a different time in every place it showed a time:

* `CronTrigger(hour=, minute=)` was built with no `timezone=`, so APScheduler
  fell back to the scheduler's zone -- whatever tzlocal resolved for the host,
  and nothing in the Dockerfile, compose file or entrypoint sets TZ. A UTC
  value therefore fired at that wall clock in the *host's* zone, a different
  absolute instant from the one the page promised.
* `get_next_run_for_job` formatted `job.next_run_time` raw, so "Next" quoted
  the scheduler's zone while the input box quoted the user's offset.
* `/api/file-index-status` subtracted a UTC timestamp from a naive local
  `datetime.now()`, so "3 hour(s) ago" was out by the host offset.

app.py cannot be imported in tests, so this is asserted against its AST.
"""

import ast
import os

import pytest

APP_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "app.py",
)


@pytest.fixture(scope="module")
def tree():
    with open(APP_PY, encoding="utf-8") as handle:
        return ast.parse(handle.read(), filename="app.py")


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"app.py no longer defines {name}()")


def _calls(node, func_name):
    found = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        target = child.func
        name = getattr(target, "id", None) or getattr(target, "attr", None)
        if name == func_name:
            found.append(child)
    return found


def _names_used(node):
    used = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            used.add(child.id)
        elif isinstance(child, ast.Attribute):
            used.add(child.attr)
    return used


class TestConfigureSchedule:

    def test_every_cron_trigger_pins_a_timezone(self, tree):
        """Both branches -- daily and weekly -- or one frequency drifts."""
        configure = _function(tree, "configure_schedule")
        triggers = _calls(configure, "CronTrigger")
        assert len(triggers) >= 2, "expected a daily and a weekly CronTrigger"
        for call in triggers:
            keywords = {kw.arg for kw in call.keywords}
            assert "timezone" in keywords, (
                f"CronTrigger at app.py:{call.lineno} has no timezone= -- it will "
                "inherit the host's zone and fire at the wrong instant"
            )

    def test_add_job_is_passed_no_jitter(self, tree):
        """`jitter=` was dead here for its whole life, and must not come back.

        It was passed to add_job() alongside a CronTrigger *instance*, where
        APScheduler discards it: _create_trigger returns the instance before
        trigger_args is ever read. A live jitter lands in job.next_run_time, so
        re-enabling it would make the "Next" call-out read up to 30 minutes
        after the time in the input box -- indistinguishable, to a user, from
        this bug being unfixed.
        """
        configure = _function(tree, "configure_schedule")
        for call in _calls(configure, "add_job"):
            keywords = {kw.arg for kw in call.keywords}
            assert "jitter" not in keywords, (
                f"add_job at app.py:{call.lineno} passes jitter=; it has no effect "
                "with a trigger instance, and enabling it would shift 'Next'"
            )


class TestGetNextRunForJob:
    """The single next-run formatter; seven GET routes call it inline."""

    def test_formats_through_the_shared_helper(self, tree):
        func = _function(tree, "get_next_run_for_job")
        assert "format_user_time" in _names_used(func), (
            "get_next_run_for_job must format through core.user_time, which "
            "normalises to UTC with astimezone before applying the offset"
        )

    def test_does_not_strftime_the_raw_value(self, tree):
        func = _function(tree, "get_next_run_for_job")
        assert not _calls(func, "strftime"), (
            "a raw strftime prints the scheduler's own zone"
        )

    def test_keeps_its_swallow_everything_contract(self, tree):
        """A clock that cannot be read must not 500 a page."""
        func = _function(tree, "get_next_run_for_job")
        assert any(isinstance(n, ast.ExceptHandler) for n in ast.walk(func))
        returns = [
            n.value.value
            for n in ast.walk(func)
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
        ]
        assert "Not scheduled" in returns


class TestFileIndexStatus:

    def test_age_is_computed_by_the_shared_helper(self, tree):
        func = _function(tree, "api_file_index_status")
        assert "describe_age" in _names_used(func)

    def test_does_not_mix_a_local_now_with_a_utc_stamp(self, tree):
        func = _function(tree, "api_file_index_status")
        for call in _calls(func, "now"):
            assert False, (
                f"datetime.now() at app.py:{call.lineno} -- last_rebuild is a UTC "
                "CURRENT_TIMESTAMP, so subtracting it from a local now is out by "
                "the host offset"
            )


class TestNoDuplicateGetcomicsScheduleRoutes:
    """routes/downloads.py owns these two.

    app.py defined them a second time and, because the blueprint registers long
    before this module body runs, Werkzeug matched the blueprint copies -- so
    the app.py pair was unreachable and had already drifted (no last_run, no
    time-format validation, configure_schedule("getcomics") instead of
    configure_getcomics_schedule()). A fix landing in the dead half would look
    applied and do nothing.
    """

    def test_app_py_declares_no_getcomics_schedule_route(self, tree):
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                for arg in decorator.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if "getcomics-schedule" in arg.value:
                            offenders.append((node.name, arg.value))
        assert not offenders, (
            f"app.py re-declares {offenders}; these belong to routes/downloads.py"
        )


class TestSchedulesPageNamesItsZone:

    def test_the_page_is_given_the_offset_label(self, tree):
        func = _function(tree, "schedules_page")
        assert "timezone_label" in {
            kw.arg for call in _calls(func, "render_template") for kw in call.keywords
        }, "the page has to name the fixed offset it is displaying"
