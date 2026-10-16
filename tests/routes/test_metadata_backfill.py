"""Manual backfill (#579): fill blanks from the file's own provider, never overwrite."""
import zipfile
from unittest.mock import patch, MagicMock

import pytest

from core.metadata_backfill import extract_issue_id
from models.providers import ProviderType
from models.providers.base import SearchResult, IssueResult

from tests.routes.test_bulk_metadata import (  # noqa: F401
    bulk_client, _wait_for_job, _read_xml_from_cbz,
)


def _cbz(path, xml: bytes):
    with zipfile.ZipFile(str(path), 'w') as zf:
        zf.writestr('page1.png', b'fake-image')
        zf.writestr('ComicInfo.xml', xml)
    return str(path)


def _provider(info):
    provider = MagicMock()
    provider.get_issue.return_value = IssueResult(
        provider=ProviderType.METRON, id='5', series_id='42', issue_number='1')
    provider.get_series.return_value = SearchResult(
        provider=ProviderType.METRON, id='42', title='Batman', year=2020)
    provider.to_comicinfo.return_value = info
    return provider


class TestExtractIssueId:
    def test_metron_prefers_metronid_tag(self):
        assert extract_issue_id('metron', {'MetronId': '77', 'Notes': 'Issue ID: 5'}) == '77'

    def test_notes_issue_id(self):
        assert extract_issue_id('comicvine', {'Notes': 'Metadata from ComicVine. Issue ID: 99'}) == '99'

    def test_metron_resource_url(self):
        notes = 'Metadata from Metron. Resource URL: https://metron.cloud/issue/1234/ — modified x'
        assert extract_issue_id('metron', {'Notes': notes}) == '1234'

    def test_none_when_absent(self):
        assert extract_issue_id('gcd', {'Notes': 'Metadata from GCD.'}) is None


class TestBackfillJob:

    OLD = (b'<ComicInfo><Series>Batman</Series><Number>1</Number>'
           b'<Summary>My own words</Summary><Writer></Writer>'
           b'<Notes>Metadata from Metron. Issue ID: 5</Notes></ComicInfo>')
    NEW = {
        'Series': 'Batman', 'Number': '1', 'Summary': 'Provider words',
        'Writer': 'Tom King', 'Genre': 'Superhero',
        'Notes': 'Metadata from Metron. Issue ID: 5 — modified later',
    }

    def _run(self, client, folder, **body):
        with patch('core.bulk_metadata._instantiate_provider',
                   return_value=_provider(self.NEW)):
            r = client.post('/api/bulk-metadata/backfill', json={'paths': [str(folder)], **body})
            assert r.status_code == 200
            return _wait_for_job(client, r.get_json()['job_id'])

    def test_fills_blanks_and_keeps_hand_edits(self, bulk_client, tmp_path):
        cbz = _cbz(tmp_path / 'Batman 001.cbz', self.OLD)
        final = self._run(bulk_client, tmp_path)
        xml = _read_xml_from_cbz(cbz)
        assert b'Tom King' in xml and b'Superhero' in xml
        assert b'My own words' in xml and b'Provider words' not in xml
        # Provenance is not rewritten.
        assert b'modified later' not in xml
        assert final['job']['auto_accepted'] == 1

    def test_dry_run_writes_nothing(self, bulk_client, tmp_path):
        cbz = _cbz(tmp_path / 'Batman 001.cbz', self.OLD)
        before = _read_xml_from_cbz(cbz)
        final = self._run(bulk_client, tmp_path, dry_run=True)
        assert _read_xml_from_cbz(cbz) == before
        assert final['job']['auto_accepted'] == 1

    def test_second_run_finds_nothing_to_fill(self, bulk_client, tmp_path):
        _cbz(tmp_path / 'Batman 001.cbz', self.OLD)
        self._run(bulk_client, tmp_path)
        final = self._run(bulk_client, tmp_path)
        assert final['job']['auto_accepted'] == 0 and final['job']['skipped'] == 1

    def test_untagged_and_manual_files_are_skipped_not_guessed(self, bulk_client, tmp_path):
        a = _cbz(tmp_path / 'a.cbz', b'<ComicInfo><Series>X</Series></ComicInfo>')
        b = _cbz(tmp_path / 'b.cbz', b'<ComicInfo><Notes>Metadata entered manually in CLU on 2024-01-01.</Notes></ComicInfo>')
        before = (_read_xml_from_cbz(a), _read_xml_from_cbz(b))
        final = self._run(bulk_client, tmp_path)
        assert (_read_xml_from_cbz(a), _read_xml_from_cbz(b)) == before
        assert final['job']['skipped'] == 2

    def test_backfill_is_revertable(self, bulk_client, tmp_path):
        cbz = _cbz(tmp_path / 'Batman 001.cbz', self.OLD)
        before = _read_xml_from_cbz(cbz)
        final = self._run(bulk_client, tmp_path)
        rows = bulk_client.get('/api/bulk-metadata/audit').get_json()
        items = rows.get('items') or rows.get('rows') or []
        assert any(i.get('matched_via') == 'backfill' for i in items)
        audit_id = next(i['id'] for i in items if i.get('matched_via') == 'backfill')
        r = bulk_client.post(f'/api/bulk-metadata/audit/{audit_id}/revert')
        assert r.status_code == 200
        assert _read_xml_from_cbz(cbz).strip() == before.strip()

    def test_rejects_bad_input(self, bulk_client):
        assert bulk_client.post('/api/bulk-metadata/backfill', json={}).status_code == 400
        assert bulk_client.post('/api/bulk-metadata/backfill', json={'paths': [1]}).status_code == 400

    def test_outside_library_forbidden(self, bulk_client, tmp_path):
        with patch('routes.bulk_metadata.is_valid_library_path', return_value=False):
            r = bulk_client.post('/api/bulk-metadata/backfill', json={'paths': [str(tmp_path)]})
        assert r.status_code == 403
