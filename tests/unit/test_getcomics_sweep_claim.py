"""One GetComics sweep per scope.

The sweep's de-duplication (`queued_download_urls`, `downloaded_ranges`) lives
in locals, so it dies with the call: a second concurrent sweep starts with both
empty and re-queues everything the first is still working through. One reported
run took 9,408s (2h37m) -- long enough for the nightly cron to fire on top of
it -- while /api/run-getcomics-now had no in-progress check at all.

The registry lives in core.app_state rather than app.py so the routes can ask
"is one already running?" without importing app.
"""
import pytest

import core.app_state as app_state


@pytest.fixture(autouse=True)
def _clean_registry():
    app_state._getcomics_sweeps.clear()
    yield
    app_state._getcomics_sweeps.clear()


class TestScopeKeys:

    def test_a_full_sweep_and_a_scoped_run_have_different_keys(self):
        assert app_state.getcomics_sweep_scope() == "all"
        assert app_state.getcomics_sweep_scope(7) == "series:7"

    def test_two_series_do_not_share_a_key(self):
        assert app_state.getcomics_sweep_scope(7) != app_state.getcomics_sweep_scope(8)


class TestClaiming:

    def test_the_second_claim_on_a_scope_stands_down(self):
        assert app_state.claim_getcomics_sweep("all") is True
        assert app_state.claim_getcomics_sweep("all") is False

    def test_releasing_lets_the_next_run_through(self):
        app_state.claim_getcomics_sweep("all")
        app_state.release_getcomics_sweep("all")
        assert app_state.claim_getcomics_sweep("all") is True

    def test_releasing_a_scope_nobody_holds_is_harmless(self):
        app_state.release_getcomics_sweep("series:99")  # must not raise

    def test_a_full_sweep_does_not_block_the_per_series_button(self):
        """Clicking "Check for Missing Issues" is an explicit request about one
        series; making the user wait out a sweep with hours left is worse than
        the single duplicate it can cost."""
        assert app_state.claim_getcomics_sweep("all") is True
        assert app_state.claim_getcomics_sweep("series:7") is True

    def test_two_checks_of_one_series_do_not_overlap(self):
        assert app_state.claim_getcomics_sweep("series:7") is True
        assert app_state.claim_getcomics_sweep("series:7") is False


class TestRunningPredicate:

    def test_it_reports_only_its_own_scope(self):
        app_state.claim_getcomics_sweep("series:7")
        assert app_state.getcomics_sweep_running(only_series_id=7) is True
        assert app_state.getcomics_sweep_running(only_series_id=8) is False
        assert app_state.getcomics_sweep_running() is False

    def test_age_is_none_for_a_scope_nobody_holds(self):
        assert app_state.getcomics_sweep_age("all") is None

    def test_age_is_reported_once_claimed(self):
        app_state.claim_getcomics_sweep("all")
        age = app_state.getcomics_sweep_age("all")
        assert age is not None and age >= 0
