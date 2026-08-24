"""The date check as bulk_metadata applies it.

Covers the gate itself (_date_conflicted) across all three modes, which is what
decides whether a match is written or diverted to the review queue.
"""
import pytest

from core.bulk_metadata import _date_conflicted
from core.metadata_dates import MODE_ENFORCE, MODE_LOG, MODE_OFF
from models.providers import ProviderType
from models.providers.base import IssueResult


def _issue(cover_date=None, store_date=None):
    return IssueResult(
        provider=ProviderType.GCD,
        id='500',
        series_id='200',
        issue_number='1',
        title='The Beginning',
        cover_date=cover_date,
        store_date=store_date,
    )


@pytest.fixture
def mode(monkeypatch):
    def _set(value, tolerance=2):
        monkeypatch.setattr("core.metadata_dates.date_check_mode", lambda: value)
        monkeypatch.setattr("core.metadata_dates.date_check_tolerance", lambda: tolerance)
    return _set


# The real case this exists for: an Italian 2014 part-work matching the
# unrelated 1999 American series of the same name.
CONFLICTING = '/comics/Diabolik - Nero Su Nero #001 (2014).cbz'


class TestDateConflicted:

    def test_off_never_diverts(self, mode):
        mode(MODE_OFF)
        assert _date_conflicted(CONFLICTING, _issue(cover_date='1999-06-01')) is False

    def test_log_reports_but_does_not_divert(self, mode):
        """'log' must write exactly what 'off' writes."""
        mode(MODE_LOG)
        assert _date_conflicted(CONFLICTING, _issue(cover_date='1999-06-01')) is False

    def test_enforce_diverts_a_conflicting_match(self, mode):
        mode(MODE_ENFORCE)
        assert _date_conflicted(CONFLICTING, _issue(cover_date='1999-06-01')) is True

    def test_enforce_allows_an_agreeing_match(self, mode):
        mode(MODE_ENFORCE)
        assert _date_conflicted(CONFLICTING, _issue(cover_date='2014-01-01')) is False

    def test_falls_back_to_store_date(self, mode):
        mode(MODE_ENFORCE)
        assert _date_conflicted(CONFLICTING, _issue(store_date='1999-06-01')) is True

    def test_issue_without_any_date_is_allowed(self, mode):
        """A provider that carries no dates must not have every match rejected."""
        mode(MODE_ENFORCE)
        assert _date_conflicted(CONFLICTING, _issue()) is False

    def test_filename_without_a_year_is_allowed(self, mode):
        mode(MODE_ENFORCE)
        assert _date_conflicted('/comics/Diabolik 001.cbz',
                                _issue(cover_date='1999-06-01')) is False

    def test_tolerance_is_respected(self, mode):
        mode(MODE_ENFORCE, tolerance=20)
        assert _date_conflicted(CONFLICTING, _issue(cover_date='1999-06-01')) is False
