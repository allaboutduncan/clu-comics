"""Unmatched reading-list entries as wanted issues.

``core.wanted_reading_lists`` stores nothing: an entry is wanted exactly while
it has no file. These tests pin that equivalence, because it is the whole
reason the feature needs no hooks in the import, sync, map or delete paths --
if the derived query ever drifts from "unmatched", every one of those paths
silently needs one.

The cooldown is tested here too. A reading-list entry has no ``mapped_path``,
so ``process_incoming_wanted_issues`` cannot file a finished download back onto
it; without the stamp the sweep re-queues the same issue every night forever.
"""
from datetime import datetime, timedelta

import pytest

from tests.factories.db_factories import (
    create_reading_list,
    create_reading_list_entry,
)


def _track(list_id, enabled=True):
    from core.database import set_reading_list_track_wanted

    assert set_reading_list_track_wanted(list_id, enabled)


def _rows(**kwargs):
    from core.wanted_reading_lists import get_reading_list_wanted_items

    return get_reading_list_wanted_items(**kwargs)


def _names(rows):
    return sorted((r["series"], r["issue_number"]) for r in rows)


class TestOptIn:

    def test_a_list_not_opted_in_contributes_nothing(self, db_connection):
        """Default OFF: importing a 300-issue arc must not flood the page."""
        list_id = create_reading_list(name="Untracked")
        create_reading_list_entry(list_id, series="Batman", issue_number="1")

        assert _rows() == []

    def test_an_opted_in_list_contributes_its_unmatched_entries(self, db_connection):
        list_id = create_reading_list(name="Crisis")
        create_reading_list_entry(list_id, series="Batman", issue_number="1")
        _track(list_id)

        assert _names(_rows()) == [("Batman", "1")]

    def test_opting_back_out_removes_them_again(self, db_connection):
        list_id = create_reading_list(name="Crisis")
        create_reading_list_entry(list_id, series="Batman", issue_number="1")
        _track(list_id)
        assert len(_rows()) == 1

        _track(list_id, False)
        assert _rows() == []

    def test_the_list_name_and_id_come_back_for_the_link(self, db_connection):
        list_id = create_reading_list(name="Crisis on Infinite Earths")
        create_reading_list_entry(list_id, series="Batman", issue_number="1")
        _track(list_id)

        row = _rows()[0]
        assert row["reading_list_id"] == list_id
        assert row["reading_list_name"] == "Crisis on Infinite Earths"


class TestWantedMeansUnmatched:
    """Both halves of the feature, and neither needs a hook to work."""

    def test_a_matched_entry_is_not_wanted(self, db_connection):
        list_id = create_reading_list(name="Crisis")
        create_reading_list_entry(
            list_id, series="Batman", issue_number="1",
            matched_file_path="/data/DC/Batman 001.cbz",
        )
        _track(list_id)

        assert _rows() == []

    def test_mapping_by_hand_removes_it(self, db_connection):
        """The user's half: map_entry writes manual_override_path."""
        from core.database import update_reading_list_entry_match

        list_id = create_reading_list(name="Crisis")
        entry_id = create_reading_list_entry(list_id, series="Batman", issue_number="1")
        _track(list_id)
        assert len(_rows()) == 1

        update_reading_list_entry_match(entry_id, "/data/DC/hand picked.cbz")
        assert _rows() == []

    def test_clearing_a_mapping_brings_it_back(self, db_connection):
        """Clearing nulls BOTH path columns, so the entry is wanted again."""
        from core.database import update_reading_list_entry_match

        list_id = create_reading_list(name="Crisis")
        entry_id = create_reading_list_entry(
            list_id, series="Batman", issue_number="1",
            matched_file_path="/data/DC/Batman 001.cbz",
        )
        _track(list_id)
        assert _rows() == []

        update_reading_list_entry_match(entry_id, None)
        assert len(_rows()) == 1

    def test_a_rematch_that_finds_a_file_removes_it(self, db_connection):
        """The matcher's half: set_reading_list_entry_auto_match."""
        from core.database import set_reading_list_entry_auto_match

        list_id = create_reading_list(name="Crisis")
        entry_id = create_reading_list_entry(list_id, series="Batman", issue_number="1")
        _track(list_id)
        assert len(_rows()) == 1

        set_reading_list_entry_auto_match(entry_id, "/data/DC/Batman 001.cbz")
        assert _rows() == []

    def test_a_rematch_that_rejects_a_file_adds_it(self, db_connection):
        from core.database import set_reading_list_entry_auto_match

        list_id = create_reading_list(name="Crisis")
        entry_id = create_reading_list_entry(
            list_id, series="Batman", issue_number="1",
            matched_file_path="/data/DC/wrong volume.cbz",
        )
        _track(list_id)
        assert _rows() == []

        set_reading_list_entry_auto_match(entry_id, None)
        assert len(_rows()) == 1

    def test_deleting_the_entry_removes_it_with_no_cleanup_hook(self, db_connection):
        from core.database import delete_reading_list_entry

        list_id = create_reading_list(name="Crisis")
        entry_id = create_reading_list_entry(list_id, series="Batman", issue_number="1")
        _track(list_id)
        assert len(_rows()) == 1

        delete_reading_list_entry(entry_id)
        assert _rows() == []


class TestUnmatchableEntriesAreExcluded:
    """An entry match_file can never satisfy would be searched every night."""

    @pytest.mark.parametrize("series,number", [
        ("", "1"),
        ("   ", "1"),
        ("Batman", ""),
        ("Batman", "   "),
    ])
    def test_blank_series_or_issue_is_not_wanted(self, db_connection, series, number):
        list_id = create_reading_list(name="Crisis")
        create_reading_list_entry(list_id, series=series, issue_number=number)
        _track(list_id)

        assert _rows() == []


class TestReleaseGate:

    def test_a_future_year_is_not_yet_wanted(self, db_connection):
        list_id = create_reading_list(name="Crisis")
        create_reading_list_entry(
            list_id, series="Batman", issue_number="1", year=2030,
        )
        _track(list_id)

        assert _rows(released_only=True, today="2026-01-01") == []

    def test_a_past_year_is_wanted(self, db_connection):
        list_id = create_reading_list(name="Crisis")
        create_reading_list_entry(
            list_id, series="Batman", issue_number="1", year=1986,
        )
        _track(list_id)

        assert len(_rows(released_only=True, today="2026-01-01")) == 1

    def test_a_missing_year_counts_as_released(self, db_connection):
        """Opposite of the mapped-series rule, deliberately.

        issue_year is NULL on every row imported before that column existed,
        which is most CBL lists. Skipping NULL would make the feature quietly
        do nothing for exactly the lists people import most.
        """
        list_id = create_reading_list(name="Crisis")
        create_reading_list_entry(
            list_id, series="Batman", issue_number="1", volume=None, year=None,
        )
        _track(list_id)

        assert len(_rows(released_only=True, today="2026-01-01")) == 1

    def test_the_gate_is_off_for_the_wanted_page(self, db_connection):
        """The page shows everything; only the sweep gates on release."""
        list_id = create_reading_list(name="Crisis")
        create_reading_list_entry(
            list_id, series="Batman", issue_number="1", year=2030,
        )
        _track(list_id)

        assert len(_rows(today="2026-01-01")) == 1


class TestDeduplication:

    def test_the_same_issue_in_two_lists_appears_once(self, db_connection):
        a = create_reading_list(name="Arc")
        b = create_reading_list(name="Best Of")
        create_reading_list_entry(a, series="Batman", issue_number="1")
        create_reading_list_entry(b, series="Batman", issue_number="1")
        _track(a)
        _track(b)

        assert len(_rows()) == 1

    def test_punctuation_differences_collapse(self, db_connection):
        """"Batman: Year One" and "Batman - Year One" are the same series."""
        a = create_reading_list(name="Arc")
        b = create_reading_list(name="Best Of")
        create_reading_list_entry(a, series="Batman: Year One", issue_number="1")
        create_reading_list_entry(b, series="Batman - Year One", issue_number="1")
        _track(a)
        _track(b)

        assert len(_rows()) == 1

    def test_padded_issue_numbers_collapse(self, db_connection):
        a = create_reading_list(name="Arc")
        b = create_reading_list(name="Best Of")
        create_reading_list_entry(a, series="Batman", issue_number="007")
        create_reading_list_entry(b, series="Batman", issue_number="7")
        _track(a)
        _track(b)

        assert len(_rows()) == 1

    def test_suffixed_issues_stay_distinct(self, db_connection):
        """issue_number_to_int returns None for both of these; collapsing them
        would silently drop one."""
        list_id = create_reading_list(name="Arc")
        create_reading_list_entry(list_id, series="Batman", issue_number="1.MU")
        create_reading_list_entry(list_id, series="Batman", issue_number="1.HU")
        _track(list_id)

        assert len(_rows()) == 2


class TestWorkItems:
    """The shape the GetComics sweep's search body reads."""

    def _items(self, **kwargs):
        from core.wanted_reading_lists import build_reading_list_work_items

        return build_reading_list_work_items(**kwargs)

    def test_carries_the_fields_the_search_needs(self, db_connection):
        list_id = create_reading_list(name="Crisis")
        create_reading_list_entry(
            list_id, series="Batman", issue_number="7", volume=2011, year=2011,
        )
        _track(list_id)

        item = self._items(today="2026-01-01")[0]
        assert item["series_name"] == "Batman"
        assert item["issue_num"] == "7"
        assert item["series_year"] == 2011
        assert item["source"] == "reading_list"

    def test_series_volume_is_never_taken_from_the_volume_column(self, db_connection):
        """That column is a YEAR for CBL imports and a volume NUMBER for Metron.

        Feeding a year to score_getcomics_result(series_volume=...) fails the
        volume check against every result and tanks the score.
        """
        list_id = create_reading_list(name="Crisis")
        create_reading_list_entry(
            list_id, series="Batman", issue_number="7", volume=1986, year=1986,
        )
        _track(list_id)

        assert self._items(today="2026-01-01")[0]["series_volume"] is None

    def test_no_cover_date_so_the_scrape_index_refresh_no_ops(self, db_connection):
        list_id = create_reading_list(name="Crisis")
        create_reading_list_entry(list_id, series="Batman", issue_number="7")
        _track(list_id)

        assert self._items(today="2026-01-01")[0]["cover_date"] is None

    def test_an_item_the_mapped_sweep_already_has_is_dropped(self, db_connection):
        """The mapped-series item carries a publisher and a real volume
        number, so it searches better -- it wins."""
        list_id = create_reading_list(name="Crisis")
        create_reading_list_entry(list_id, series="Batman", issue_number="7")
        _track(list_id)

        existing = [{"series_name": "Batman", "issue_num": "007"}]
        assert self._items(existing=existing, today="2026-01-01") == []

    def test_a_different_issue_of_the_same_series_survives(self, db_connection):
        list_id = create_reading_list(name="Crisis")
        create_reading_list_entry(list_id, series="Batman", issue_number="8")
        _track(list_id)

        existing = [{"series_name": "Batman", "issue_num": "7"}]
        assert len(self._items(existing=existing, today="2026-01-01")) == 1


class TestQueueCooldown:
    """Without this the sweep re-downloads the same issue every night."""

    def _items(self, **kwargs):
        from core.wanted_reading_lists import build_reading_list_work_items

        return build_reading_list_work_items(**kwargs)

    def test_a_freshly_queued_entry_is_not_searched_again(self, db_connection):
        from core.database import mark_reading_list_entries_queued

        list_id = create_reading_list(name="Crisis")
        entry_id = create_reading_list_entry(list_id, series="Batman", issue_number="1")
        _track(list_id)
        assert len(self._items(today="2026-01-01")) == 1

        assert mark_reading_list_entries_queued([entry_id]) == 1
        assert self._items(today="2026-01-01") == []

    def test_it_is_searched_again_once_the_cooldown_lapses(self, db_connection):
        from core.database import mark_reading_list_entries_queued

        list_id = create_reading_list(name="Crisis")
        entry_id = create_reading_list_entry(list_id, series="Batman", issue_number="1")
        _track(list_id)
        mark_reading_list_entries_queued([entry_id])

        later = datetime.now() + timedelta(days=8)
        assert len(self._items(today="2026-01-01", now=later)) == 1

    def test_the_cooldown_never_reaches_the_wanted_page(self, db_connection):
        """A queued issue is still missing -- it must keep showing."""
        from core.database import mark_reading_list_entries_queued

        list_id = create_reading_list(name="Crisis")
        entry_id = create_reading_list_entry(list_id, series="Batman", issue_number="1")
        _track(list_id)
        mark_reading_list_entries_queued([entry_id])

        assert len(_rows()) == 1


class TestSweepRematch:
    """The pass that actually closes the download loop.

    A reading-list download lands in TARGET and is filed by the normal
    WATCH/TARGET pipeline. Nothing then tells the entry about it, so the sweep
    re-matches tracked lists before deciding what is still wanted.
    """

    def test_only_tracked_lists_are_rematched(self, db_connection):
        from core.reading_list_match import rematch_tracked_lists

        tracked = create_reading_list(name="Tracked")
        create_reading_list_entry(tracked, series="Batman", issue_number="1")
        _track(tracked)

        untracked = create_reading_list(name="Untracked")
        create_reading_list_entry(untracked, series="Flash", issue_number="1")

        assert rematch_tracked_lists()["lists"] == 1

    def test_it_runs_without_an_application_context(self, db_connection):
        """The sweep is an APScheduler job, so current_app is unavailable.

        Reading the rename pattern through current_app would raise, be
        swallowed, and silently match against a pattern the user does not use.
        """
        import threading

        from core.reading_list_match import rematch_tracked_lists

        list_id = create_reading_list(name="Crisis")
        create_reading_list_entry(list_id, series="Batman", issue_number="1")
        _track(list_id)

        out = {}
        t = threading.Thread(target=lambda: out.update(result=rematch_tracked_lists()))
        t.start()
        t.join()

        assert out["result"]["lists"] == 1

    def test_a_rematch_that_finds_the_download_takes_it_off_the_sweep(self, db_connection):
        from core.database import set_reading_list_entry_auto_match
        from core.wanted_reading_lists import build_reading_list_work_items

        list_id = create_reading_list(name="Crisis")
        entry_id = create_reading_list_entry(list_id, series="Batman", issue_number="1")
        _track(list_id)
        assert len(build_reading_list_work_items(today="2026-01-01")) == 1

        # What the re-match does once the file has been filed into the library.
        set_reading_list_entry_auto_match(entry_id, "/data/DC/Batman 001.cbz")
        assert build_reading_list_work_items(today="2026-01-01") == []
