"""The date check as bulk_metadata applies it.

Covers both gates across all three modes: _date_conflicted, which decides
whether one file's match is written or diverted to review, and
_series_year_conflicted, which decides the same for a whole folder when the
folder name carries the year and the filenames do not.
"""
import zipfile
from unittest.mock import MagicMock, patch

import pytest

from core.bulk_metadata import _date_conflicted, _series_year_conflicted
from core.metadata_dates import MODE_ENFORCE, MODE_LOG, MODE_OFF
from models.providers import ProviderType
from models.providers.base import IssueResult, SearchResult


def _issue(cover_date=None, store_date=None, provider=ProviderType.GCD,
           issue_number='1'):
    return IssueResult(
        provider=provider,
        id='500',
        series_id='200',
        issue_number=issue_number,
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
        assert _date_conflicted(CONFLICTING, _issue(cover_date='1999-06-01'), '1') is False

    def test_log_reports_but_does_not_divert(self, mode):
        """'log' must write exactly what 'off' writes."""
        mode(MODE_LOG)
        assert _date_conflicted(CONFLICTING, _issue(cover_date='1999-06-01'), '1') is False

    def test_enforce_diverts_a_conflicting_match(self, mode):
        mode(MODE_ENFORCE)
        assert _date_conflicted(CONFLICTING, _issue(cover_date='1999-06-01'), '1') is True

    def test_enforce_allows_an_agreeing_match(self, mode):
        mode(MODE_ENFORCE)
        assert _date_conflicted(CONFLICTING, _issue(cover_date='2014-01-01'), '1') is False

    def test_falls_back_to_store_date(self, mode):
        mode(MODE_ENFORCE)
        assert _date_conflicted(CONFLICTING, _issue(store_date='1999-06-01'), '1') is True

    def test_issue_without_any_date_is_allowed(self, mode):
        """A provider that carries no dates must not have every match rejected."""
        mode(MODE_ENFORCE)
        assert _date_conflicted(CONFLICTING, _issue(), '1') is False

    def test_filename_without_a_year_is_allowed(self, mode):
        mode(MODE_ENFORCE)
        assert _date_conflicted('/comics/Diabolik 001.cbz',
                                _issue(cover_date='1999-06-01'), '1') is False

    def test_tolerance_is_respected(self, mode):
        mode(MODE_ENFORCE, tolerance=20)
        assert _date_conflicted(CONFLICTING, _issue(cover_date='1999-06-01'), '1') is False

    def test_uses_the_filename_issue_number_not_the_providers(self, mode):
        """The safety argument for discarding a year that matches the issue
        number rests on that number being one the caller read from the
        filename. The provider's own `issue.issue_number` is only usually
        equal to it (both callers select `issue` via `issues_by_norm`, keyed
        on the filename's number) -- passing it directly instead would say
        the wrong thing even where the values happen to agree.

        Here they deliberately disagree: the provider's record is issue '1',
        but the caller matched it against the filename's issue number, which
        is what "Topolino 1904.cbz" actually reads as. Passing that number
        discards the filename's only year candidate and the check abstains.
        """
        mode(MODE_ENFORCE)
        issue = _issue(cover_date='1992-05-24', issue_number='1')
        assert _date_conflicted('/comics/Topolino 1904.cbz', issue, '1904') is False
        # The provider's issue_number alone ('1') does not collide with the
        # filename's year, so if the call site regressed to passing
        # issue.issue_number this would report a conflict instead.
        assert _date_conflicted('/comics/Topolino 1904.cbz', issue, '1') is True


class TestProviderExemption:
    """Providers whose Year/date is series-level are exempt here too."""

    def test_manga_provider_is_never_diverted(self, mode):
        """MangaDex leaves cover_date None today, so this is belt and braces —
        but if it ever reported a series-level date, every volume of a long
        series would otherwise become a conflict."""
        mode(MODE_ENFORCE)
        issue = _issue(cover_date='1989-01-01', provider=ProviderType.MANGADEX)
        assert _date_conflicted(CONFLICTING, issue, '1') is False

    def test_comic_provider_is_still_checked(self, mode):
        mode(MODE_ENFORCE)
        issue = _issue(cover_date='1999-06-01', provider=ProviderType.COMICVINE)
        assert _date_conflicted(CONFLICTING, issue, '1') is True


def _series(year, title='Diabolik', provider=ProviderType.GCD):
    return SearchResult(
        provider=provider,
        id='200',
        title=title,
        year=year,
        publisher=None,
        issue_count=None,
        cover_url=None,
        description=None,
    )


# The folder from #510: an Italian 2014 part-work whose files are numbered
# 001.cbz, 002.cbz — nothing in any filename to check.
CONFLICTING_FOLDER = 'Diabolik - Nero Su Nero (2014)'


class TestSeriesYearConflicted:
    """The folder-level half: folder year vs the series' start year."""

    def test_enforce_diverts_a_folder_whose_year_contradicts_the_series(self, mode):
        mode(MODE_ENFORCE)
        assert _series_year_conflicted(CONFLICTING_FOLDER, _series(1999)) is True

    def test_agreeing_year_is_allowed(self, mode):
        mode(MODE_ENFORCE)
        assert _series_year_conflicted(CONFLICTING_FOLDER, _series(2014)) is False

    def test_log_reports_but_does_not_divert(self, mode):
        mode(MODE_LOG)
        assert _series_year_conflicted(CONFLICTING_FOLDER, _series(1999)) is False

    def test_off_never_diverts(self, mode):
        mode(MODE_OFF)
        assert _series_year_conflicted(CONFLICTING_FOLDER, _series(1999)) is False

    def test_folder_without_a_year_is_allowed(self, mode):
        mode(MODE_ENFORCE)
        assert _series_year_conflicted('Diabolik', _series(1999)) is False

    def test_series_without_a_start_year_is_allowed(self, mode):
        """A provider that carries no start year is not evidence of anything."""
        mode(MODE_ENFORCE)
        assert _series_year_conflicted(CONFLICTING_FOLDER, _series(None)) is False

    def test_unresolved_series_is_allowed(self, mode):
        mode(MODE_ENFORCE)
        assert _series_year_conflicted(CONFLICTING_FOLDER, None) is False

    def test_tolerance_is_respected(self, mode):
        mode(MODE_ENFORCE, tolerance=20)
        assert _series_year_conflicted(CONFLICTING_FOLDER, _series(1999)) is False

    def test_volume_marker_folders_are_read_too(self, mode):
        """extract_year_from_name accepts vYYYY, and folders use it."""
        mode(MODE_ENFORCE)
        assert _series_year_conflicted('Captain Marvel v2002', _series(1968)) is True

    def test_a_long_run_is_not_a_conflict(self, mode):
        """The comparison is folder-year vs *series start*, never vs an issue
        date — otherwise issue #200 of a 1962 series would fail against its own
        folder."""
        mode(MODE_ENFORCE)
        assert _series_year_conflicted('Amazing Spider-Man (1962)', _series(1962)) is False


class TestFolderConflictShortCircuitsTheFolder:
    """A wrong series must not be written to the folder — or to a sidecar."""

    def _run(self, tmp_path, folder_name, series_year, mode_fixture):
        from core import bulk_metadata as bm

        mode_fixture(MODE_ENFORCE)
        folder = tmp_path / folder_name
        folder.mkdir()
        cbz = folder / '001.cbz'
        with zipfile.ZipFile(cbz, 'w') as z:
            z.writestr('001.jpg', b'x')

        fake_provider = MagicMock()
        fake_provider.get_series.return_value = _series(series_year)
        fake_provider.get_issues.return_value = []

        progress = {"done": 0}
        with patch.object(bm, '_is_oneshot_folder', return_value=False), \
                patch.object(bm, '_try_cvinfo', return_value=('gcd', '200')), \
                patch.object(bm, '_instantiate_provider', return_value=fake_provider), \
                patch.object(bm, 'ensure_folder_sidecars') as sidecars, \
                patch.object(bm, 'add_review_item') as review, \
                patch.object(bm, 'update_bulk_job_counts') as counts, \
                patch.object(bm, 'app_state'):
            bm._process_folder(
                job_id='job-1',
                op_id='op-1',
                folder_path=str(folder),
                files=[str(cbz)],
                providers=['gcd'],
                overwrite_existing=False,
                progress=progress,
            )
        return sidecars, review, counts, progress

    def test_conflicting_series_queues_the_folder(self, mode, tmp_path):
        sidecars, review, counts, progress = self._run(
            tmp_path, CONFLICTING_FOLDER, 1999, mode
        )

        review.assert_called_once()
        kwargs = review.call_args.kwargs
        assert kwargs['reason'] == 'date_conflict'
        # Folder-level, like series_ambiguous — the whole folder is suspect,
        # not one file in it.
        assert kwargs['file_path'] is None
        assert kwargs['candidates'][0]['year'] == 1999
        counts.assert_called_once_with('job-1', needs_review=1)
        # The bar still reaches 100%.
        assert progress['done'] == 1

    def test_no_sidecar_is_written_for_a_rejected_series(self, mode, tmp_path):
        """Writing cvinfo here would persist the bad match and exempt the
        folder from ever being re-resolved."""
        sidecars, _, _, _ = self._run(tmp_path, CONFLICTING_FOLDER, 1999, mode)
        sidecars.assert_not_called()

    def test_agreeing_series_proceeds_as_before(self, mode, tmp_path):
        sidecars, review, _, _ = self._run(tmp_path, CONFLICTING_FOLDER, 2014, mode)
        sidecars.assert_called_once()
        # It got as far as matching issues; the folder was not short-circuited.
        assert review.call_args.kwargs['reason'] != 'date_conflict'
