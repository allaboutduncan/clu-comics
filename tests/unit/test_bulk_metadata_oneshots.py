"""Unit tests for oneshots-folder handling in the bulk metadata orchestrator.

A "oneshots" folder (configured via ONESHOT_FOLDERS) holds unrelated single
issues. Unlike a normal folder, each file is resolved independently from its own
filename and NO cvinfo/series.json sidecar is written. Providers are mocked at
the module level so no real APIs are hit.
"""
import os
from unittest.mock import patch, MagicMock

import pytest

from core import bulk_metadata as bm
from core.database import create_bulk_job, get_bulk_job, get_review_queue
from models.providers import ProviderType
from models.providers.base import SearchResult, IssueResult


def _make_search(provider, title, year, **kw):
    return SearchResult(
        provider=provider,
        id=str(kw.get('id', '1')),
        title=title,
        year=year,
        publisher=kw.get('publisher'),
        issue_count=kw.get('issue_count'),
        cover_url=None,
        description=None,
    )


def _make_issue(provider, series_id, number, issue_id='1'):
    return IssueResult(
        provider=provider,
        id=str(issue_id),
        series_id=str(series_id),
        issue_number=str(number),
        title=None,
        cover_date=None,
        store_date=None,
        cover_url=None,
        summary=None,
    )


def _new_job():
    import uuid
    job_id = uuid.uuid4().hex
    create_bulk_job(job_id, 'folder', [])
    return job_id


# ---------------------------------------------------------------------------
# _is_oneshot_folder
# ---------------------------------------------------------------------------
class TestIsOneshotFolder:

    def test_matches_configured_name_case_insensitive(self):
        with patch('core.settings.get_setting', return_value='oneshots,one-shots,specials'):
            assert bm._is_oneshot_folder('/data/Comics/Oneshots') is True
            assert bm._is_oneshot_folder('/data/Comics/ONESHOTS') is True
            assert bm._is_oneshot_folder('/data/Comics/Specials/') is True

    def test_ignores_leading_slash_in_config_value(self):
        # User may type "/oneshots" in the config field.
        with patch('core.settings.get_setting', return_value='/oneshots, Standalone'):
            assert bm._is_oneshot_folder('/data/Comics/oneshots') is True
            assert bm._is_oneshot_folder('/data/Comics/standalone') is True

    def test_unlisted_folder_is_false(self):
        with patch('core.settings.get_setting', return_value='oneshots,one-shots,specials'):
            assert bm._is_oneshot_folder('/data/Comics/Batman (2016)') is False

    def test_empty_config_is_false(self):
        with patch('core.settings.get_setting', return_value=''):
            assert bm._is_oneshot_folder('/data/Comics/oneshots') is False


# ---------------------------------------------------------------------------
# _process_oneshot_folder — per-file resolution, no sidecars
# ---------------------------------------------------------------------------
class TestProcessOneshotFolder:

    def _run(self, db_connection, tmp_path, files, resolve_results, issues):
        """Run _process_oneshot_folder over `files`, mocking resolution + write.

        Returns (job_id, write_mock).
        """
        folder = tmp_path / 'oneshots'
        folder.mkdir()
        file_paths = [str(folder / f) for f in files]

        job_id = _new_job()
        progress = {"done": 0}

        provider = MagicMock()
        provider.get_issues.return_value = issues

        write_mock = MagicMock(return_value=True)

        with patch.object(bm, '_resolve_series_auto', side_effect=resolve_results), \
             patch.object(bm, '_instantiate_provider', return_value=provider), \
             patch.object(bm, '_write_metadata', write_mock):
            bm._process_oneshot_folder(
                job_id=job_id,
                op_id='op-test',
                folder_path=str(folder),
                files=file_paths,
                providers=['metron', 'comicvine'],
                overwrite_existing=True,  # skip the on-disk ComicInfo check
                progress=progress,
            )

        return job_id, str(folder), write_mock

    def test_two_unrelated_files_each_resolve_independently(self, db_connection, tmp_path):
        series_a = _make_search(ProviderType.METRON, 'Batman - Damned', 2017, id='10')
        series_b = _make_search(ProviderType.COMICVINE, 'Saga Special', 2014, id='20')
        issues = [
            _make_issue(ProviderType.METRON, '10', '1', issue_id='A1'),
            _make_issue(ProviderType.COMICVINE, '20', '3', issue_id='B3'),
        ]
        job_id, folder, write_mock = self._run(
            db_connection, tmp_path,
            files=['Batman - Damned (2017).cbz', 'Saga Special 003 (2014).cbz'],
            resolve_results=[('metron', series_a, []), ('comicvine', series_b, [])],
            issues=issues,
        )

        # _write_metadata called once per file, with that file's own series.
        assert write_mock.call_count == 2
        written_series = {c.kwargs['series'].id for c in write_mock.call_args_list}
        assert written_series == {'10', '20'}
        assert all(c.kwargs['matched_via'] == 'oneshot_filename' for c in write_mock.call_args_list)

        # No folder-level sidecars written.
        assert not os.path.exists(os.path.join(folder, 'cvinfo'))
        assert not os.path.exists(os.path.join(folder, 'series.json'))

        job = get_bulk_job(job_id)
        assert job['auto_accepted'] == 2
        assert job['needs_review'] == 0

    def test_file_without_issue_number_defaults_to_one(self, db_connection, tmp_path):
        series_a = _make_search(ProviderType.METRON, 'Batman - Damned', 2017, id='10')
        issues = [_make_issue(ProviderType.METRON, '10', '1', issue_id='A1')]
        job_id, folder, write_mock = self._run(
            db_connection, tmp_path,
            files=['Batman - Damned (2017).cbz'],  # no issue number
            resolve_results=[('metron', series_a, [])],
            issues=issues,
        )
        # Treated as #1 -> matched + written, not queued.
        assert write_mock.call_count == 1
        assert write_mock.call_args.kwargs['issue'].id == 'A1'
        assert get_review_queue(job_id=job_id) == []
        assert not os.path.exists(os.path.join(folder, 'series.json'))

    def test_unresolvable_file_queues_file_level_review_no_sidecar(self, db_connection, tmp_path):
        job_id, folder, write_mock = self._run(
            db_connection, tmp_path,
            files=['Totally Unknown Comic (1999).cbz'],
            resolve_results=[(None, None, [])],  # nothing matched
            issues=[],
        )
        write_mock.assert_not_called()

        queue = get_review_queue(job_id=job_id)
        assert len(queue) == 1
        # File-level review item -> file_path is populated.
        assert queue[0]['file_path'] == os.path.join(folder, 'Totally Unknown Comic (1999).cbz')
        assert queue[0]['reason'] == 'series_no_match'

        assert not os.path.exists(os.path.join(folder, 'cvinfo'))
        assert not os.path.exists(os.path.join(folder, 'series.json'))


# ---------------------------------------------------------------------------
# _process_folder routing — oneshot vs normal
# ---------------------------------------------------------------------------
class TestProcessFolderRouting:

    def test_oneshot_folder_routes_to_per_file_and_skips_sidecars(self, db_connection, tmp_path):
        folder = tmp_path / 'oneshots'
        folder.mkdir()
        files = [str(folder / 'Some Comic (2017).cbz')]

        with patch.object(bm, '_is_oneshot_folder', return_value=True), \
             patch.object(bm, '_process_oneshot_folder') as oneshot_mock, \
             patch.object(bm, 'ensure_folder_sidecars') as sidecar_mock, \
             patch.object(bm, '_resolve_series_auto') as resolve_mock:
            bm._process_folder(
                job_id=_new_job(), op_id='op', folder_path=str(folder),
                files=files, providers=['metron'], overwrite_existing=True,
                progress={"done": 0},
            )

        oneshot_mock.assert_called_once()
        sidecar_mock.assert_not_called()
        resolve_mock.assert_not_called()  # folder-level resolution skipped

    def test_normal_folder_writes_sidecars(self, db_connection, tmp_path):
        folder = tmp_path / 'Batman (2016)'
        folder.mkdir()
        files = [str(folder / 'Batman 001 (2016).cbz')]

        series = _make_search(ProviderType.METRON, 'Batman', 2016, id='42')
        provider = MagicMock()
        provider.get_issues.return_value = [
            _make_issue(ProviderType.METRON, '42', '1', issue_id='I1'),
        ]

        with patch.object(bm, '_is_oneshot_folder', return_value=False), \
             patch.object(bm, '_process_oneshot_folder') as oneshot_mock, \
             patch.object(bm, '_resolve_series_auto', return_value=('metron', series, [])), \
             patch.object(bm, '_instantiate_provider', return_value=provider), \
             patch.object(bm, '_write_metadata', return_value=True), \
             patch.object(bm, 'ensure_folder_sidecars') as sidecar_mock:
            bm._process_folder(
                job_id=_new_job(), op_id='op', folder_path=str(folder),
                files=files, providers=['metron'], overwrite_existing=True,
                progress={"done": 0},
            )

        oneshot_mock.assert_not_called()
        sidecar_mock.assert_called_once()


# ---------------------------------------------------------------------------
# Cover match: a one-shot file is identified by its cover before its filename
# ---------------------------------------------------------------------------
class TestOneshotCoverMatch:

    HIT = {"id": 77, "series_id": 5, "series_name": "Batman - Damned", "series_year": 2018,
           "number": "1", "cover_year": 2018, "cover_hash": "0" * 16, "distance": 1}

    def _run(self, db_connection, tmp_path, hit, providers=('metron', 'comicvine'),
             resolve=(None, None, []), date_conflict=False):
        folder = tmp_path / 'oneshots'
        folder.mkdir()
        file_path = str(folder / 'Batman Damned (2018).cbz')
        job_id = _new_job()

        provider = MagicMock()
        provider.get_issue.return_value = _make_issue(ProviderType.METRON, '5', '1', issue_id='77')
        provider.get_issues.return_value = []
        write_mock = MagicMock(return_value=True)
        resolve_mock = MagicMock(return_value=resolve)

        with patch('models.metron.match_issue_by_cover', return_value=hit) as match, \
             patch.object(bm, '_resolve_series_auto', resolve_mock), \
             patch.object(bm, '_instantiate_provider', return_value=provider), \
             patch.object(bm, '_date_conflicted', return_value=date_conflict), \
             patch.object(bm, '_write_metadata', write_mock):
            bm._process_oneshot_folder(
                job_id=job_id, op_id='op-test', folder_path=str(folder),
                files=[file_path], providers=list(providers),
                overwrite_existing=True, progress={"done": 0},
            )
        return job_id, file_path, match, provider, write_mock, resolve_mock

    def test_cover_hit_is_written_without_a_name_search(self, db_connection, tmp_path):
        job_id, file_path, match, provider, write, resolve = self._run(
            db_connection, tmp_path, self.HIT)

        # Asked with what the filename gives: series, the one-shot "1", year.
        assert match.call_args.args[1:] == (file_path, 'Batman Damned', '1', 2018)
        provider.get_issue.assert_called_once_with('77')
        resolve.assert_not_called()

        kwargs = write.call_args.kwargs
        assert kwargs['provider_name'] == 'metron'
        assert kwargs['matched_via'] == 'oneshot_cover'
        assert kwargs['issue'].id == '77'
        assert (kwargs['series'].id, kwargs['series'].title, kwargs['series'].year) == (
            '5', 'Batman - Damned', 2018)

        job = get_bulk_job(job_id)
        assert job['auto_accepted'] == 1
        assert job['needs_review'] == 0
        # Still a one-shot folder: no sidecar from a per-file answer.
        assert not os.path.exists(os.path.join(os.path.dirname(file_path), 'cvinfo'))

    def test_no_cover_match_resolves_by_name_as_before(self, db_connection, tmp_path):
        job_id, _, _, _, write, resolve = self._run(db_connection, tmp_path, None)

        resolve.assert_called_once()
        write.assert_not_called()
        assert get_bulk_job(job_id)['needs_review'] == 1
        assert get_review_queue(job_id)[0]['reason'] == 'series_no_match'

    def test_cover_is_not_asked_when_metron_is_not_a_job_provider(self, db_connection, tmp_path):
        # A re-tag forced to ComicVine must not be answered by Metron.
        _, _, match, _, write, resolve = self._run(
            db_connection, tmp_path, self.HIT, providers=('comicvine',))

        match.assert_not_called()
        resolve.assert_called_once()
        write.assert_not_called()

    def test_a_date_conflict_sends_the_file_down_the_name_path(self, db_connection, tmp_path):
        job_id, _, _, _, write, resolve = self._run(
            db_connection, tmp_path, self.HIT, date_conflict=True)

        write.assert_not_called()
        resolve.assert_called_once()
        assert get_bulk_job(job_id)['needs_review'] == 1
