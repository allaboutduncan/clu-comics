"""Filling tracked reading-list gaps when files land in the library.

``core.wanted_reading_lists._released()`` keeps a future-dated entry out of the
nightly GetComics sweep -- correctly, there is nothing to search for yet. That
makes the sweep's own ``rematch_tracked_lists`` useless for exactly that entry:
by the time the issue ships it arrives as a *mapped-series* wanted issue, and
the arrival is the only event that can close the gap. ``fill_gaps_for_series``
is the pass hooked onto that arrival.

The property that makes it safe to run on every TARGET move is that it only
ever sees entries that are already unmatched, so it can write a path but never
clear one. These tests pin that, the tracked-only scope, and the series
narrowing that keeps it cheap enough for the request thread.
"""
import pytest

from tests.factories.db_factories import (
    create_file_index_entry,
    create_reading_list,
    create_reading_list_entry,
)

PATTERN = "{series_name} {issue_number}"


def _track(list_id, enabled=True):
    from core.database import set_reading_list_track_wanted

    assert set_reading_list_track_wanted(list_id, enabled)


def _fill(series_names, **kwargs):
    from core.reading_list_match import fill_gaps_for_series

    kwargs.setdefault("rename_pattern", PATTERN)
    return fill_gaps_for_series(series_names, **kwargs)


def _paths(entry_id):
    from core.database import get_db_connection

    conn = get_db_connection()
    row = conn.execute(
        "SELECT matched_file_path, manual_override_path "
        "FROM reading_list_entries WHERE id = ?",
        (entry_id,),
    ).fetchone()
    conn.close()
    return dict(row)


def _seed_batman(year=2030, number="001"):
    """A library file the matcher's filename tier can find."""
    return create_file_index_entry(
        name=f"Batman {number} ({year}).cbz",
        parent=f"/data/DC/Batman ({year})",
    )


class TestTheMotivatingCase:

    def test_a_future_dated_gap_is_filled_by_an_arrival(self, db_connection):
        """The release gate governs the sweep; it must NOT govern this pass.

        A 2030 issue is never searched for, so nothing else would ever close
        this entry until the user pressed Re-match by hand.
        """
        path = _seed_batman(year=2030)
        list_id = create_reading_list(name="Future")
        entry_id = create_reading_list_entry(
            list_id, series="Batman", issue_number="1", volume=2030, year=2030,
        )
        _track(list_id)

        result = _fill(["Batman"])

        assert result["matched"] == 1
        assert _paths(entry_id)["matched_file_path"] == path


class TestScope:

    def test_an_untracked_list_is_never_touched(self, db_connection):
        _seed_batman()
        list_id = create_reading_list(name="Untracked")
        entry_id = create_reading_list_entry(
            list_id, series="Batman", issue_number="1", volume=2030, year=2030,
        )

        assert _fill(["Batman"])["entries"] == 0
        assert _paths(entry_id)["matched_file_path"] is None

    def test_only_the_series_that_moved_is_considered(self, db_connection):
        """The narrowing is what keeps this affordable on the request thread."""
        create_file_index_entry(
            name="Flash 001 (2030).cbz", parent="/data/DC/Flash (2030)",
        )
        list_id = create_reading_list(name="Mixed")
        flash = create_reading_list_entry(
            list_id, series="Flash", issue_number="1", volume=2030, year=2030,
        )
        _track(list_id)

        assert _fill(["Batman"])["entries"] == 0
        assert _paths(flash)["matched_file_path"] is None

    def test_punctuation_differences_still_match(self, db_connection):
        """The filed series name and the list's spelling rarely agree exactly."""
        from core.reading_list_match import unmatched_tracked_entries

        list_id = create_reading_list(name="Arc")
        create_reading_list_entry(
            list_id, series="Batman: Year One", issue_number="1",
        )
        _track(list_id)

        assert len(unmatched_tracked_entries(["Batman - Year One"])) == 1

    def test_no_series_names_is_a_no_op(self, db_connection):
        _seed_batman()
        list_id = create_reading_list(name="Future")
        create_reading_list_entry(
            list_id, series="Batman", issue_number="1", volume=2030, year=2030,
        )
        _track(list_id)

        assert _fill([]) == {"entries": 0, "matched": 0}
        assert _fill(None) == {"entries": 0, "matched": 0}


class TestItOnlyEverFillsGaps:
    """The property that makes this safe to run on every arrival."""

    def test_an_existing_match_is_never_revisited(self, db_connection):
        """Not even to confirm it: clearing belongs to the sweep and the
        Re-match button, never to a file arriving."""
        _seed_batman()
        list_id = create_reading_list(name="Matched")
        entry_id = create_reading_list_entry(
            list_id, series="Batman", issue_number="1", volume=2030, year=2030,
            matched_file_path="/data/DC/gone/Batman 001.cbz",
        )
        _track(list_id)

        assert _fill(["Batman"])["entries"] == 0
        assert _paths(entry_id)["matched_file_path"] == "/data/DC/gone/Batman 001.cbz"

    def test_a_hand_picked_mapping_is_never_touched(self, db_connection):
        _seed_batman()
        list_id = create_reading_list(name="Manual")
        entry_id = create_reading_list_entry(
            list_id, series="Batman", issue_number="1", volume=2030, year=2030,
            manual_override_path="/data/DC/hand picked.cbz",
        )
        _track(list_id)

        assert _fill(["Batman"])["entries"] == 0
        assert _paths(entry_id)["manual_override_path"] == "/data/DC/hand picked.cbz"

    def test_a_miss_writes_nothing_and_leaves_the_entry_unmatched(self, db_connection):
        list_id = create_reading_list(name="Future")
        entry_id = create_reading_list_entry(
            list_id, series="Batman", issue_number="1", volume=2030, year=2030,
        )
        _track(list_id)

        assert _fill(["Batman"]) == {"entries": 1, "matched": 0}
        assert _paths(entry_id)["matched_file_path"] is None


class TestUnmatchableEntriesAreExcluded:
    """match_file returns None immediately for either, so they can never fill."""

    @pytest.mark.parametrize("series,number", [
        ("", "1"), ("   ", "1"), ("Batman", ""), ("Batman", "   "),
    ])
    def test_blank_series_or_issue_is_skipped(self, db_connection, series, number):
        from core.reading_list_match import unmatched_tracked_entries

        list_id = create_reading_list(name="Broken")
        create_reading_list_entry(list_id, series=series, issue_number=number)
        _track(list_id)

        assert unmatched_tracked_entries(None) == []


class TestBounds:

    def test_the_pass_is_capped(self, db_connection, monkeypatch):
        """A mount full of gaps must not turn one arrival into a scan of all
        of them on the request thread."""
        import core.reading_list_match as rlm

        monkeypatch.setattr(rlm, "MAX_GAP_ENTRIES_PER_PASS", 1)
        list_id = create_reading_list(name="Many")
        for n in ("1", "2", "3"):
            create_reading_list_entry(
                list_id, series="Batman", issue_number=n, volume=2030, year=2030,
            )
        _track(list_id)

        assert _fill(["Batman"])["entries"] == 1

    def test_a_second_pass_stands_down_while_one_is_running(self, db_connection):
        """Two callers drive a TARGET sweep and both walk the same gaps."""
        import core.reading_list_match as rlm

        _seed_batman()
        list_id = create_reading_list(name="Future")
        entry_id = create_reading_list_entry(
            list_id, series="Batman", issue_number="1", volume=2030, year=2030,
        )
        _track(list_id)

        assert rlm._gap_lock.acquire(blocking=False)
        try:
            assert _fill(["Batman"]) == {"entries": 0, "matched": 0}
        finally:
            rlm._gap_lock.release()

        assert _paths(entry_id)["matched_file_path"] is None
        assert _fill(["Batman"])["matched"] == 1


class TestFailureIsSwallowed:
    """A reading-list re-match must never break the import it is hooked onto."""

    def test_a_matcher_explosion_is_logged_not_raised(self, db_connection):
        from unittest.mock import patch

        list_id = create_reading_list(name="Future")
        create_reading_list_entry(
            list_id, series="Batman", issue_number="1", volume=2030, year=2030,
        )
        _track(list_id)

        with patch("core.reading_list_match.rematch_entries",
                   side_effect=RuntimeError("boom")):
            assert _fill(["Batman"]) == {"entries": 0, "matched": 0}
