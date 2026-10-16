"""Re-tag a folder from one chosen provider (#626) and the provider-mix route."""
import os
import zipfile
from unittest.mock import patch, MagicMock

from models.providers import ProviderType
from models.providers.base import SearchResult, IssueResult

# Reuse the fixtures and helpers of the existing bulk-metadata route tests.
from tests.routes.test_bulk_metadata import (  # noqa: F401
    bulk_client, _wait_for_job, _read_xml_from_cbz,
)


def _tagged_cbz(path, notes, extra=b''):
    xml = (b'<ComicInfo><Series>Old</Series>' + extra +
           b'<Notes>' + notes.encode() + b'</Notes></ComicInfo>')
    with zipfile.ZipFile(str(path), 'w') as zf:
        zf.writestr('page1.png', b'fake-image')
        zf.writestr('ComicInfo.xml', xml)
    return str(path)


def _cv_provider():
    provider = MagicMock()
    series = SearchResult(provider=ProviderType.COMICVINE, id='7', title='Batman',
                          year=2020, publisher='DC', issue_count=12)
    provider.search_series.return_value = [series]
    provider.get_series.return_value = series
    provider.get_issues.return_value = [IssueResult(
        provider=ProviderType.COMICVINE, id='99', series_id='7', issue_number='1',
        title='Beginnings', cover_date='2020-01-01')]
    provider.to_comicinfo.return_value = {
        'Series': 'Batman', 'Number': '1', 'Year': 2020,
        'Notes': 'Metadata from ComicVine. Issue ID: 99',
    }
    return provider


def _folder(tmp_path):
    folder = tmp_path / 'Batman (2020)'
    folder.mkdir()
    return folder


class TestRetagWithProvider:

    def test_replace_drops_old_providers_tags(self, bulk_client, tmp_path):
        cbz = _tagged_cbz(_folder(tmp_path) / 'Batman 001 (2020).cbz',
                          'Metadata from Metron. Issue ID: 5',
                          b'<Genre>Horror</Genre><MetronId>5</MetronId>')

        with patch('core.bulk_metadata._instantiate_provider',
                   return_value=_cv_provider()) as inst:
            r = bulk_client.post('/api/bulk-metadata/start', json={
                'scope': 'files', 'paths': [cbz],
                'provider': 'comicvine', 'replace': True,
            })
            assert r.status_code == 200
            final = _wait_for_job(bulk_client, r.get_json()['job_id'])

        # Only the forced provider was ever instantiated: no fallback.
        assert {c.args[0] for c in inst.call_args_list} == {'comicvine'}
        assert final['job']['auto_accepted'] == 1
        xml = _read_xml_from_cbz(cbz)
        assert b'Metadata from ComicVine' in xml
        assert b'Horror' not in xml and b'MetronId' not in xml

    def test_without_replace_merges(self, bulk_client, tmp_path):
        cbz = _tagged_cbz(_folder(tmp_path) / 'Batman 001 (2020).cbz',
                          'Metadata from Metron. Issue ID: 5', b'<Genre>Horror</Genre>')
        with patch('core.bulk_metadata._instantiate_provider', return_value=_cv_provider()):
            r = bulk_client.post('/api/bulk-metadata/start', json={
                'scope': 'files', 'paths': [cbz], 'provider': 'comicvine'})
            _wait_for_job(bulk_client, r.get_json()['job_id'])
        assert b'Horror' in _read_xml_from_cbz(cbz)

    def test_files_already_tagged_by_target_are_skipped(self, bulk_client, tmp_path):
        folder = _folder(tmp_path)
        done = _tagged_cbz(folder / 'Batman 001 (2020).cbz', 'Metadata from ComicVine. Issue ID: 1')
        todo = _tagged_cbz(folder / 'Batman 002 (2020).cbz', 'Metadata from Metron. Issue ID: 2')
        before = _read_xml_from_cbz(done)
        with patch('core.bulk_metadata._instantiate_provider', return_value=_cv_provider()):
            r = bulk_client.post('/api/bulk-metadata/start', json={
                'scope': 'files', 'paths': [done, todo], 'provider': 'comicvine'})
            final = _wait_for_job(bulk_client, r.get_json()['job_id'])
        assert final['job']['total_files'] == 1
        assert _read_xml_from_cbz(done) == before

    def test_only_from_limits_to_those_providers(self, bulk_client, tmp_path):
        folder = _folder(tmp_path)
        gcd = _tagged_cbz(folder / 'Batman 001 (2020).cbz', 'Metadata from GCD (Grand Comics Database).')
        metron = _tagged_cbz(folder / 'Batman 002 (2020).cbz', 'Metadata from Metron. Issue ID: 2')
        with patch('core.bulk_metadata._instantiate_provider', return_value=_cv_provider()):
            r = bulk_client.post('/api/bulk-metadata/start', json={
                'scope': 'files', 'paths': [gcd, metron],
                'provider': 'comicvine', 'only_from': ['metron']})
            final = _wait_for_job(bulk_client, r.get_json()['job_id'])
        assert final['job']['total_files'] == 1

    def test_unknown_provider_rejected(self, bulk_client, tmp_path):
        r = bulk_client.post('/api/bulk-metadata/start', json={
            'scope': 'files', 'paths': [str(tmp_path / 'x.cbz')], 'provider': 'nope'})
        assert r.status_code == 400

    def test_provider_not_enabled_for_library_rejected(self, bulk_client, tmp_path):
        with patch('core.bulk_metadata._enabled_providers_for_library', return_value=['metron']):
            r = bulk_client.post('/api/bulk-metadata/start', json={
                'scope': 'files', 'paths': [str(tmp_path / 'x.cbz')],
                'provider': 'comicvine', 'library_id': 1})
        assert r.status_code == 400

    def test_replace_without_provider_rejected(self, bulk_client, tmp_path):
        r = bulk_client.post('/api/bulk-metadata/start', json={
            'scope': 'files', 'paths': [str(tmp_path / 'x.cbz')], 'replace': True})
        assert r.status_code == 400

    def test_path_outside_library_rejected(self, bulk_client, tmp_path):
        with patch('routes.bulk_metadata.is_valid_library_path', return_value=False):
            r = bulk_client.post('/api/bulk-metadata/start', json={
                'scope': 'files', 'paths': [str(tmp_path / 'x.cbz')], 'provider': 'comicvine'})
        assert r.status_code == 403

    def test_cvinfo_of_another_provider_is_ignored(self, tmp_path):
        from core.bulk_metadata import _try_cvinfo
        (tmp_path / 'cvinfo').write_text('x')
        with patch('models.metron.parse_cvinfo_for_metron_id', return_value=123), \
             patch('models.comicvine.parse_cvinfo_volume_id', return_value=456):
            assert _try_cvinfo(str(tmp_path)) == ('metron', '123')
            assert _try_cvinfo(str(tmp_path), only_provider='comicvine') == ('comicvine', '456')
            assert _try_cvinfo(str(tmp_path), only_provider='gcd') is None


class TestProviderMixRoute:

    def test_returns_mix_and_enabled_providers(self, bulk_client, tmp_path):
        mix = {'providers': {'metron': 2}, 'untagged': 0, 'no_xml': 0, 'unscanned': 0, 'total': 2}
        with patch('core.database.get_provider_mix', return_value=mix), \
             patch('core.bulk_metadata._enabled_providers_for_library',
                   return_value=['metron', 'comicvine']), \
             patch('helpers.library.resolve_provider_library_id', return_value=1):
            r = bulk_client.get('/api/bulk-metadata/provider-mix',
                                query_string={'path': str(tmp_path)})
        data = r.get_json()
        assert r.status_code == 200 and data['mix'] == mix
        assert data['enabled_providers'] == ['metron', 'comicvine']

    def test_outside_library_forbidden(self, bulk_client, tmp_path):
        with patch('routes.bulk_metadata.is_valid_library_path', return_value=False):
            r = bulk_client.get('/api/bulk-metadata/provider-mix',
                                query_string={'path': str(tmp_path)})
        assert r.status_code == 403

    def test_read_failure_is_an_error_not_empty(self, bulk_client, tmp_path):
        with patch('core.database.get_provider_mix', return_value=None):
            r = bulk_client.get('/api/bulk-metadata/provider-mix',
                                query_string={'path': str(tmp_path)})
        assert r.status_code == 500


class TestRetagUsesCvinfoAndOverwrites:
    """A folder with a cvinfo must not be sent to review, and resolving a
    review of a re-tag must overwrite files that already have ComicInfo.xml."""

    def test_local_db_choice_reads_the_cvinfo_volume_id(self, tmp_path):
        from core.bulk_metadata import _try_cvinfo
        (tmp_path / 'cvinfo').write_text('https://comicvine.gamespot.com/volume/4050-6824/')
        assert _try_cvinfo(str(tmp_path), only_provider='comicvine_sqlite') == ('comicvine_sqlite', '6824')
        assert _try_cvinfo(str(tmp_path), only_provider='comicvine') == ('comicvine', '6824')

    def test_retag_skips_review_when_cvinfo_exists(self, bulk_client, tmp_path):
        folder = _folder(tmp_path)
        (folder / 'cvinfo').write_text('https://comicvine.gamespot.com/volume/4050-7/')
        fp = _tagged_cbz(folder / 'Batman 001 (2020).cbz', 'Metadata from Metron. Issue ID: 1')
        with patch('core.bulk_metadata._instantiate_provider', return_value=_cv_provider()), \
             patch('routes.bulk_metadata.is_valid_library_path', return_value=True):
            r = bulk_client.post('/api/bulk-metadata/start', json={
                'scope': 'files', 'paths': [fp], 'provider': 'comicvine_sqlite', 'replace': True})
            job = _wait_for_job(bulk_client, r.get_json()['job_id'])
        assert job['job']['needs_review'] == 0
        assert b'Metadata from ComicVine' in _read_xml_from_cbz(fp)

    def test_resolving_a_retag_review_overwrites_existing_xml(self, bulk_client, tmp_path):
        import uuid as _uuid
        from core.database import create_bulk_job, add_review_item
        folder = _folder(tmp_path)
        fp = _tagged_cbz(folder / 'Batman 001 (2020).cbz', 'Metadata from Metron. Issue ID: 1')
        job_id = _uuid.uuid4().hex
        create_bulk_job(job_id=job_id, scope_type='files',
                        scope_payload={'paths': [fp], 'provider': 'comicvine', 'replace': True})
        review_id = add_review_item(
            job_id=job_id, folder_path=str(folder), file_path=None, parsed_series='Batman',
            parsed_issue=None, parsed_year=2020, reason='series_no_match', candidates=[])
        with patch('core.bulk_metadata._instantiate_provider', return_value=_cv_provider()):
            r = bulk_client.post(f'/api/bulk-metadata/review/{review_id}/apply-cvinfo',
                                 json={'cv_volume_id': '7'})
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['written'] == 1
        assert b'Metadata from ComicVine' in _read_xml_from_cbz(fp)
