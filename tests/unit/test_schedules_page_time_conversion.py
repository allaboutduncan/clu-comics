"""Every schedule time input converts, and they all share one implementation.

Schedule times are stored as UTC. Before this, four of the five cards on the
Schedules page converted them for display, the Reading List Sync card did not
convert at all, and the Komga card in Settings and the Weekly Packs input were
on the raw value too -- five conventions across three pages. The JS helpers
themselves existed twice, in schedules.html and config.html, and the config.html
copy had drifted into being dead code while the input right above it converted
nothing.

So: one copy, in base.html's CLU block, and every schedule time input goes
through it in both directions.
"""

import os
import re

import pytest

TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "templates",
)


def _read(name):
    with open(os.path.join(TEMPLATES, name), encoding="utf-8") as handle:
        return handle.read()


@pytest.fixture(scope="module")
def schedules():
    return _read("schedules.html")


@pytest.fixture(scope="module")
def base():
    return _read("base.html")


class TestSharedHelpers:

    def test_base_defines_all_three(self, base):
        for name in ("CLU.tzOffsetHours", "CLU.utcToLocal", "CLU.localToUtc"):
            assert f"{name} = function" in base, f"base.html must define {name}"

    @pytest.mark.parametrize("template", ["schedules.html", "config.html",
                                          "weekly_packs.html"])
    def test_no_page_carries_its_own_copy(self, template):
        """A second copy is how config.html's went dead without anyone noticing."""
        source = _read(template)
        for dead in ("function utcToLocal", "function localToUtc",
                     "function getTimezoneOffset"):
            assert dead not in source, (
                f"{template} defines {dead}; the shared copy in base.html is the "
                "only one"
            )


class TestEverySchedulesInputConverts:
    """The five cards, both directions. rlSyncTime is the one that did neither."""

    INPUTS = ["rebuildTime", "scrapeIndexTime", "syncTime", "getcomicsTime",
              "rlSyncTime"]

    @pytest.mark.parametrize("input_id", INPUTS)
    def test_loaded_through_utc_to_local(self, schedules, input_id):
        assert f"getElementById('{input_id}')" in schedules
        # The load either assigns .value from CLU.utcToLocal(...), or reads the
        # element first and then assigns; both spellings put the id and the call
        # within a few lines of each other.
        assert self._converted_near(schedules, input_id, "CLU.utcToLocal("), (
            f"{input_id} is filled from the stored UTC value without conversion"
        )

    @pytest.mark.parametrize("input_id", INPUTS)
    def test_posted_through_local_to_utc(self, schedules, input_id):
        assert self._converted_near(schedules, input_id, "CLU.localToUtc("), (
            f"{input_id} is posted back without being converted to UTC"
        )

    @staticmethod
    def _converted_near(source, input_id, call, window=4):
        lines = source.splitlines()
        hits = [i for i, line in enumerate(lines) if f"'{input_id}'" in line]
        for i in hits:
            chunk = "\n".join(lines[max(0, i - window):i + window + 1])
            if call in chunk:
                return True
        return False


class TestOffsetHolderIsPresent:
    """The rule that stops a sixth convention appearing.

    CLU.tzOffsetHours() reads an element with id="timezone" and returns 0 when
    it is absent -- which fails silently, showing raw UTC. So any template with
    a schedule time input has to carry the holder.
    """

    TIME_INPUT = re.compile(r'<input[^>]*type="time"', re.IGNORECASE)

    def test_every_template_with_a_time_input_has_the_holder(self):
        missing = []
        for name in sorted(os.listdir(TEMPLATES)):
            if not name.endswith(".html"):
                continue
            source = _read(name)
            if not self.TIME_INPUT.search(source):
                continue
            if 'id="timezone"' not in source:
                missing.append(name)
        assert not missing, (
            f"{missing} render a time input but no id=\"timezone\" holder, so "
            "CLU.tzOffsetHours() silently reads 0 and the field shows raw UTC"
        )


class TestPageNamesItsZone:

    def test_the_intro_names_the_offset(self, schedules):
        assert "{{ timezone_label }}" in schedules

    def test_no_card_still_claims_your_timezone(self, schedules):
        """The preference is a fixed offset, so it does not follow DST."""
        assert "in your timezone" not in schedules


class TestServerClock:
    """The page shows the server's own clock, so a wrong offset is visible.

    A user in US Central set the offset to UTC-06:00 in September, when Central
    is really UTC-05:00. Every displayed time was self-consistent -- the input
    box and "Next" agreed -- and every schedule was still an hour out, and the
    only symptom was a job that appeared not to fire. The fixed offset cannot
    follow DST, so the page has to make the discrepancy readable instead.
    """

    def test_the_bar_carries_the_server_instant(self, schedules):
        assert 'id="serverTimeBar"' in schedules
        assert 'data-utc="{{ server_now_utc }}"' in schedules

    def test_it_shows_the_configured_offset_utc_and_the_host_clock(self, schedules):
        assert 'id="serverTimeDisplay"' in schedules
        assert 'id="serverTimeUtc"' in schedules
        assert "{{ host_offset_label }}" in schedules

    def test_the_clock_starts_on_page_init(self, schedules):
        assert "function startServerClock" in schedules
        assert "startServerClock();" in schedules

    def test_it_ticks_locally_rather_than_polling(self, schedules):
        """There is nothing to fetch; CLU.startPoll is for requests."""
        block = schedules[schedules.index("function startServerClock"):]
        block = block[:block.index("// Page Init")]
        assert "fetch(" not in block
        assert "CLU.startPoll" not in block
        assert "setInterval(tick" in block

    def test_it_never_reads_the_browsers_own_timezone(self, schedules):
        """The page shows the server's clock in the configured offset.

        Formatting through a local-time getter would make the same page read
        differently on a phone in another country.
        """
        block = schedules[schedules.index("function startServerClock"):]
        block = block[:block.index("// Page Init")]
        for banned in ("getTimezoneOffset", "toLocaleString", "toLocaleTimeString",
                       "getHours()", "getMinutes()"):
            assert banned not in block, f"{banned} reads the viewer's zone"

    def test_the_dst_caveat_is_spelled_out(self, schedules):
        assert "daylight saving" in schedules
