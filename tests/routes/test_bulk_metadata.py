"""
Route tests for /api/bulk-metadata.

Covers:
- /start with scope='files' kicks off a job, returns job_id + op_id.
- /progress reports counts after the background job has run.
- /review surfaces queued items, /resolve writes metadata + audit row.
- /audit/<id>/revert restores prior ComicInfo.xml bytes.

The orchestrator is mocked at the provider level so we don't hit any
real APIs; the actual ComicInfo.xml write / revert logic runs against
real CBZ files in tmp_path.
"""
import io
import os
import time
import zipfile
from unittest.mock import patch, MagicMock

import pytest
from flask import Flask

from models.providers import ProviderType
from models.providers.base import SearchResult, IssueResult


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def _make_cbz(path, with_comicinfo=False):
    """Create a tiny CBZ at `path`. Returns the path."""
    with zipfile.ZipFile(str(path), 'w') as zf:
        zf.writestr('page1.png', b'fake-image')
        if with_comicinfo:
            zf.writestr('ComicInfo.xml', b'<ComicInfo><Series>Old</Series></ComicInfo>')
    return str(path)


def _read_xml_from_cbz(path):
    with zipfile.ZipFile(str(path), 'r') as zf:
        for n in zf.namelist():
            if os.path.basename(n).lower() == 'comicinfo.xml':
                return zf.read(n)
    return None


@pytest.fixture
def bulk_client(db_connection, tmp_path):
    """Minimal Flask app with just the bulk_metadata blueprint registered."""
    from routes.bulk_metadata import bulk_metadata_bp

    app = Flask(__name__, template_folder=os.path.join(PROJECT_ROOT, 'templates'))
    app.config['TESTING'] = True
    app.register_blueprint(bulk_metadata_bp)

    # Patch is_valid_library_path so tmp_path counts as a library.
    with patch('routes.bulk_metadata.is_valid_library_path', return_value=True):
        yield app.test_client()


def _wait_for_job(client, job_id, timeout=8.0):
    """Poll /progress until the bulk job hits a terminal state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f'/api/bulk-metadata/progress/{job_id}')
        data = r.get_json()
        if data and data.get('success'):
            status = data.get('status') or data.get('job', {}).get('status')
            if status in ('completed', 'error'):
                return data
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


class TestStartAndProgress:

    def test_invalid_scope_rejected(self, bulk_client):
        r = bulk_client.post(
            '/api/bulk-metadata/start',
            json={'scope': 'nope', 'paths': ['/x']}
        )
        assert r.status_code == 400

    def test_empty_paths_rejected(self, bulk_client):
        r = bulk_client.post(
            '/api/bulk-metadata/start',
            json={'scope': 'files', 'paths': []}
        )
        assert r.status_code == 400

    def test_auto_match_writes_metadata(self, bulk_client, tmp_path):
        """End-to-end: one folder, exact-name+year match -> ComicInfo written."""
        folder = tmp_path / 'Batman (2020)'
        folder.mkdir()
        cbz = _make_cbz(folder / 'Batman 001 (2020).cbz')

        provider = MagicMock()
        provider.search_series.return_value = [
            SearchResult(
                provider=ProviderType.METRON,
                id='42', title='Batman', year=2020, publisher='DC',
                issue_count=12, cover_url=None, description=None,
            )
        ]
        provider.get_series.return_value = SearchResult(
            provider=ProviderType.METRON,
            id='42', title='Batman', year=2020, publisher='DC',
            issue_count=12, cover_url=None, description=None,
        )
        provider.get_issues.return_value = [
            IssueResult(
                provider=ProviderType.METRON,
                id='999', series_id='42', issue_number='1',
                title='Beginnings', cover_date='2020-01-01',
                store_date=None, cover_url=None, summary=None,
            )
        ]
        provider.to_comicinfo.return_value = {
            'Series': 'Batman', 'Number': '1', 'Year': 2020, 'Publisher': 'DC',
        }

        with patch('core.bulk_metadata._instantiate_provider', return_value=provider):
            r = bulk_client.post(
                '/api/bulk-metadata/start',
                json={'scope': 'files', 'paths': [cbz]},
            )
            assert r.status_code == 200
            job_id = r.get_json()['job_id']
            final = _wait_for_job(bulk_client, job_id)

        job = final['job']
        assert job['status'] == 'completed'
        assert job['total_files'] == 1
        assert job['auto_accepted'] == 1
        assert job['needs_review'] == 0

        # The CBZ now has a ComicInfo.xml
        new_xml = _read_xml_from_cbz(cbz)
        assert new_xml is not None
        assert b'<Series>Batman</Series>' in new_xml

    def test_ambiguous_match_goes_to_review(self, bulk_client, tmp_path):
        folder = tmp_path / 'Batman'  # no year — auto-accept impossible
        folder.mkdir()
        cbz = _make_cbz(folder / 'Batman 001.cbz')

        provider = MagicMock()
        provider.search_series.return_value = [
            SearchResult(provider=ProviderType.METRON, id='1', title='Batman', year=2020),
            SearchResult(provider=ProviderType.METRON, id='2', title='Batman', year=2011),
        ]

        with patch('core.bulk_metadata._instantiate_provider', return_value=provider):
            r = bulk_client.post(
                '/api/bulk-metadata/start',
                json={'scope': 'files', 'paths': [cbz]},
            )
            job_id = r.get_json()['job_id']
            final = _wait_for_job(bulk_client, job_id)

        assert final['job']['auto_accepted'] == 0
        assert final['job']['needs_review'] == 1

        # Review endpoint surfaces the queued folder.
        r = bulk_client.get(f'/api/bulk-metadata/review/{job_id}')
        data = r.get_json()
        assert data['success']
        assert data['count'] == 1
        item = data['items'][0]
        assert item['reason'] in ('series_ambiguous', 'series_no_match')
        assert len(item['candidates']) >= 1

    def test_skips_files_with_existing_comicinfo(self, bulk_client, tmp_path):
        folder = tmp_path / 'Batman (2020)'
        folder.mkdir()
        cbz = _make_cbz(folder / 'Batman 001 (2020).cbz', with_comicinfo=True)

        provider = MagicMock()
        provider.search_series.return_value = [
            SearchResult(provider=ProviderType.METRON, id='42', title='Batman', year=2020),
        ]
        # get_series / get_issues should not need to do anything because file is skipped,
        # but provide stubs anyway to avoid AttributeError if reached.
        provider.get_series.return_value = SearchResult(
            provider=ProviderType.METRON, id='42', title='Batman', year=2020,
        )
        provider.get_issues.return_value = []

        with patch('core.bulk_metadata._instantiate_provider', return_value=provider):
            r = bulk_client.post(
                '/api/bulk-metadata/start',
                json={'scope': 'files', 'paths': [cbz]},
            )
            job_id = r.get_json()['job_id']
            final = _wait_for_job(bulk_client, job_id)

        # Pre-existing XML preserved.
        assert b'<Series>Old</Series>' in _read_xml_from_cbz(cbz)
        assert final['job']['skipped'] == 1
        assert final['job']['auto_accepted'] == 0


class TestRevert:

    def test_revert_restores_prior_xml(self, bulk_client, tmp_path):
        folder = tmp_path / 'Batman (2020)'
        folder.mkdir()
        # Start with prior metadata. We want bulk to overwrite it, then revert.
        cbz = _make_cbz(folder / 'Batman 001 (2020).cbz', with_comicinfo=True)

        provider = MagicMock()
        provider.search_series.return_value = [
            SearchResult(provider=ProviderType.METRON, id='42', title='Batman', year=2020),
        ]
        provider.get_series.return_value = SearchResult(
            provider=ProviderType.METRON, id='42', title='Batman', year=2020,
        )
        provider.get_issues.return_value = [
            IssueResult(
                provider=ProviderType.METRON,
                id='999', series_id='42', issue_number='1',
            )
        ]
        provider.to_comicinfo.return_value = {'Series': 'Batman', 'Number': '1'}

        with patch('core.bulk_metadata._instantiate_provider', return_value=provider):
            r = bulk_client.post(
                '/api/bulk-metadata/start',
                json={'scope': 'files', 'paths': [cbz], 'overwrite_existing': True},
            )
            job_id = r.get_json()['job_id']
            _wait_for_job(bulk_client, job_id)

        assert b'<Series>Batman</Series>' in _read_xml_from_cbz(cbz)

        # Find the audit row we just created.
        r = bulk_client.get('/api/bulk-metadata/audit')
        rows = r.get_json()['items']
        assert rows, "expected at least one audit row"
        audit_id = rows[0]['id']

        r = bulk_client.post(f'/api/bulk-metadata/audit/{audit_id}/revert')
        assert r.status_code == 200, r.get_json()

        restored = _read_xml_from_cbz(cbz)
        assert restored is not None
        assert b'<Series>Old</Series>' in restored

    def test_revert_strips_when_no_prior_xml(self, bulk_client, tmp_path):
        folder = tmp_path / 'Batman (2020)'
        folder.mkdir()
        cbz = _make_cbz(folder / 'Batman 001 (2020).cbz')  # no prior XML

        provider = MagicMock()
        provider.search_series.return_value = [
            SearchResult(provider=ProviderType.METRON, id='42', title='Batman', year=2020),
        ]
        provider.get_series.return_value = SearchResult(
            provider=ProviderType.METRON, id='42', title='Batman', year=2020,
        )
        provider.get_issues.return_value = [
            IssueResult(provider=ProviderType.METRON, id='99', series_id='42', issue_number='1')
        ]
        provider.to_comicinfo.return_value = {'Series': 'Batman', 'Number': '1'}

        with patch('core.bulk_metadata._instantiate_provider', return_value=provider):
            r = bulk_client.post(
                '/api/bulk-metadata/start',
                json={'scope': 'files', 'paths': [cbz]},
            )
            _wait_for_job(bulk_client, r.get_json()['job_id'])

        assert _read_xml_from_cbz(cbz) is not None

        r = bulk_client.get('/api/bulk-metadata/audit')
        audit_id = r.get_json()['items'][0]['id']
        r = bulk_client.post(f'/api/bulk-metadata/audit/{audit_id}/revert')
        assert r.status_code == 200

        # Revert removes the XML entirely since there was none prior.
        assert _read_xml_from_cbz(cbz) is None


class TestRevertMultiple:
    """`/api/bulk-metadata/audit/revert-multiple` — bulk-revert endpoint with
    an aggregate result envelope (matches /api/reading-lists/bulk-delete)."""

    def _seed_audit_rows(self, tmp_path, count=3):
        """Create ``count`` CBZs (each with a prior ComicInfo) + matching
        audit rows. Returns (cbz_paths, audit_ids)."""
        import uuid as _uuid
        from core.database import create_bulk_job, log_bulk_audit

        folder = tmp_path / 'Audit'
        folder.mkdir(exist_ok=True)
        cbz_paths = []
        for i in range(1, count + 1):
            cbz_paths.append(_make_cbz(folder / f'A 00{i}.cbz', with_comicinfo=True))

        job_id = _uuid.uuid4().hex
        create_bulk_job(job_id=job_id, scope_type='files', scope_payload={'paths': cbz_paths})
        audit_ids = []
        for fp in cbz_paths:
            new_xml = b'<?xml version="1.0"?><ComicInfo><Series>New</Series></ComicInfo>'
            # Mimic what _write_metadata does: capture prior, apply new.
            import zipfile
            prior_bytes = b'<?xml version="1.0"?><ComicInfo><Series>Old</Series></ComicInfo>'
            # Re-pack with the new xml so the on-disk state matches an audit row.
            tmp_zip = fp + '.tmp'
            with zipfile.ZipFile(fp, 'r') as src:
                with zipfile.ZipFile(tmp_zip, 'w', zipfile.ZIP_DEFLATED) as dst:
                    for info in src.infolist():
                        if os.path.basename(info.filename).lower() == 'comicinfo.xml':
                            continue
                        dst.writestr(info, src.read(info.filename))
                    dst.writestr('ComicInfo.xml', new_xml)
            os.replace(tmp_zip, fp)
            aid = log_bulk_audit(
                job_id=job_id,
                file_path=fp,
                folder_path=str(folder),
                provider='metron',
                series_id='1',
                issue_id='1',
                series_name='A',
                issue_number='1',
                year=2020,
                matched_via='manual_review',
                prior_xml=prior_bytes,
                new_xml=new_xml,
            )
            audit_ids.append(aid)
        return cbz_paths, audit_ids

    def test_handles_mixed_outcomes(self, bulk_client, tmp_path):
        from core.database import mark_audit_reverted
        cbz_paths, audit_ids = self._seed_audit_rows(tmp_path, count=3)
        good_id = audit_ids[0]
        already_id = audit_ids[1]
        mark_audit_reverted(already_id)  # simulate prior revert
        missing_id = 999_999

        r = bulk_client.post(
            '/api/bulk-metadata/audit/revert-multiple',
            json={'ids': [good_id, already_id, missing_id]},
        )
        assert r.status_code == 200
        d = r.get_json()
        assert d['success'] is True
        assert d['reverted'] == [good_id]
        # Failures mention the appropriate errors.
        failed_by_id = {f['id']: f['error'] for f in d['failed']}
        assert already_id in failed_by_id
        assert 'already reverted' in failed_by_id[already_id]
        assert missing_id in failed_by_id
        assert 'not found' in failed_by_id[missing_id]
        # And the good file actually got reverted on disk (prior XML restored).
        assert b'<Series>Old</Series>' in _read_xml_from_cbz(cbz_paths[0])

    def test_rejects_empty_ids(self, bulk_client):
        r = bulk_client.post(
            '/api/bulk-metadata/audit/revert-multiple',
            json={'ids': []},
        )
        assert r.status_code == 400

    def test_rejects_non_list_ids(self, bulk_client):
        r = bulk_client.post(
            '/api/bulk-metadata/audit/revert-multiple',
            json={'ids': 'not-a-list'},
        )
        assert r.status_code == 400

    def test_invalid_id_format_recorded_as_failure(self, bulk_client, tmp_path):
        """Non-numeric ids land in the failed list with 'invalid id' — they
        shouldn't break the whole batch."""
        _, audit_ids = self._seed_audit_rows(tmp_path, count=1)
        r = bulk_client.post(
            '/api/bulk-metadata/audit/revert-multiple',
            json={'ids': [audit_ids[0], 'bogus']},
        )
        assert r.status_code == 200
        d = r.get_json()
        assert audit_ids[0] in d['reverted']
        assert any(f.get('id') == 'bogus' for f in d['failed'])


class TestApplyCvinfo:
    """The /apply-cvinfo endpoint: direct ID entry writes cvinfo + applies
    metadata to every CBZ in the folder and cascade-resolves sibling reviews."""

    def _seed_review_rows(self, folder, files, parsed_year=None):
        """Create a bulk job + a pending review row per file. Returns
        (job_id, [review_id, ...])."""
        import uuid
        from core.database import create_bulk_job, add_review_item

        job_id = uuid.uuid4().hex
        create_bulk_job(
            job_id=job_id,
            scope_type='files',
            scope_payload={'paths': files},
        )
        ids = []
        for fp in files:
            rid = add_review_item(
                job_id=job_id,
                folder_path=str(folder),
                file_path=fp,
                parsed_series='Batman',
                parsed_issue='1',
                parsed_year=parsed_year,
                reason='issue_no_match',
                candidates=[],
            )
            ids.append(rid)
        return job_id, ids

    def _provider_returning(self, series, issues):
        p = MagicMock()
        p.get_series.return_value = series
        p.get_issues.return_value = issues
        # to_comicinfo must return a real dict so generate_comicinfo_xml gets
        # actual strings to serialise (otherwise MagicMock.get(...) yields
        # mocks and the XML ends up filled with <MagicMock id=...> repr text).
        def _to_ci(issue, ser=None):
            return {
                'Series': ser.title if ser else series.title,
                'Number': issue.issue_number,
                'Year': (ser.year if ser else series.year),
                'Publisher': (ser.publisher if ser else series.publisher),
            }
        p.to_comicinfo.side_effect = _to_ci
        return p

    def test_metron_id_writes_cvinfo_and_metadata(self, bulk_client, tmp_path):
        folder = tmp_path / 'Batman'
        folder.mkdir()
        files = [
            _make_cbz(folder / f'Batman 00{i}.cbz') for i in (1, 2, 3)
        ]
        job_id, review_ids = self._seed_review_rows(folder, files, parsed_year=2020)

        series = SearchResult(
            provider=ProviderType.METRON,
            id='42', title='Batman', year=2020, publisher='DC',
        )
        issues = [
            IssueResult(provider=ProviderType.METRON, id=str(100+i), series_id='42',
                        issue_number=str(i)) for i in (1, 2, 3)
        ]

        with patch('core.bulk_metadata._instantiate_provider',
                   return_value=self._provider_returning(series, issues)):
            r = bulk_client.post(
                f'/api/bulk-metadata/review/{review_ids[0]}/apply-cvinfo',
                json={'metron_series_id': '42'},
            )

        assert r.status_code == 200, r.get_json()
        data = r.get_json()
        assert data['success']
        assert data['written'] == 3
        assert data['provider'] == 'metron'
        assert sorted(data['resolved_review_ids']) == sorted(review_ids)

        # cvinfo written to folder.
        cvinfo = folder / 'cvinfo'
        assert cvinfo.exists()
        content = cvinfo.read_text(encoding='utf-8')
        assert 'series_id: 42' in content

        # All three CBZs got ComicInfo.
        for fp in files:
            xml = _read_xml_from_cbz(fp)
            assert xml is not None
            assert b'<Series>Batman</Series>' in xml

    def test_cascades_resolve_to_sibling_reviews(self, bulk_client, tmp_path):
        from core.database import get_review_item, get_bulk_job

        folder = tmp_path / 'Saga'
        folder.mkdir()
        files = [_make_cbz(folder / f'Saga 00{i}.cbz') for i in (1, 2)]
        job_id, review_ids = self._seed_review_rows(folder, files)

        series = SearchResult(
            provider=ProviderType.METRON, id='9', title='Saga', year=2012,
        )
        issues = [
            IssueResult(provider=ProviderType.METRON, id=str(900+i), series_id='9',
                        issue_number=str(i)) for i in (1, 2)
        ]

        # Sanity: job starts with 2 needs_review.
        job_before = get_bulk_job(job_id)
        # needs_review is incremented inside the orchestrator, not by seeding,
        # so for this test we don't assert the initial counter — we only
        # verify the cascade behaviour against the review-row table.

        with patch('core.bulk_metadata._instantiate_provider',
                   return_value=self._provider_returning(series, issues)):
            bulk_client.post(
                f'/api/bulk-metadata/review/{review_ids[0]}/apply-cvinfo',
                json={'metron_series_id': '9'},
            )

        # All rows now resolved.
        for rid in review_ids:
            row = get_review_item(rid)
            assert row['status'] == 'resolved', f"row {rid} still {row['status']}"

    def test_comicvine_only_writes_cv_url(self, bulk_client, tmp_path):
        folder = tmp_path / 'Hellboy'
        folder.mkdir()
        files = [_make_cbz(folder / 'Hellboy 001.cbz')]
        job_id, review_ids = self._seed_review_rows(folder, files)

        series = SearchResult(
            provider=ProviderType.COMICVINE, id='12345', title='Hellboy', year=1994,
        )
        issues = [
            IssueResult(provider=ProviderType.COMICVINE, id='5', series_id='12345',
                        issue_number='1'),
        ]

        def factory(name):
            # The endpoint tries 'metron' first when only metron_id is given;
            # when only cv id is given it goes straight to 'comicvine'.
            return self._provider_returning(series, issues) if name == 'comicvine' else None

        with patch('core.bulk_metadata._instantiate_provider', side_effect=factory):
            r = bulk_client.post(
                f'/api/bulk-metadata/review/{review_ids[0]}/apply-cvinfo',
                json={'cv_volume_id': '12345'},
            )

        assert r.status_code == 200, r.get_json()
        assert r.get_json()['provider'] == 'comicvine'

        cvinfo = folder / 'cvinfo'
        assert cvinfo.exists()
        content = cvinfo.read_text(encoding='utf-8')
        # CV URL line is canonical.
        assert 'comicvine.gamespot.com/volume/4050-12345' in content

    def test_rejects_when_no_ids(self, bulk_client, tmp_path):
        folder = tmp_path / 'Empty'
        folder.mkdir()
        files = [_make_cbz(folder / 'x.cbz')]
        _, review_ids = self._seed_review_rows(folder, files)

        r = bulk_client.post(
            f'/api/bulk-metadata/review/{review_ids[0]}/apply-cvinfo',
            json={},
        )
        assert r.status_code == 400

    def test_mark_applied_resolves_without_provider(self, bulk_client, tmp_path):
        """When /api/search-metadata writes metadata directly (single-match
        success), the bulk modal calls /mark-applied to drop the review row
        without re-doing the write or requiring a provider/series_id."""
        from core.database import get_review_item, get_bulk_job

        folder = tmp_path / 'Done'
        folder.mkdir()
        files = [_make_cbz(folder / 'd.cbz')]
        job_id, review_ids = self._seed_review_rows(folder, files)

        r = bulk_client.post(
            f'/api/bulk-metadata/review/{review_ids[0]}/mark-applied'
        )
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['success'] is True

        row = get_review_item(review_ids[0])
        assert row['status'] == 'resolved'

    def test_mark_applied_rejects_already_resolved(self, bulk_client, tmp_path):
        from core.database import update_review_status

        folder = tmp_path / 'Done2'
        folder.mkdir()
        files = [_make_cbz(folder / 'd.cbz')]
        _, review_ids = self._seed_review_rows(folder, files)
        update_review_status(review_ids[0], 'resolved')

        r = bulk_client.post(
            f'/api/bulk-metadata/review/{review_ids[0]}/mark-applied'
        )
        assert r.status_code == 409

    def test_rejects_when_review_already_resolved(self, bulk_client, tmp_path):
        from core.database import update_review_status

        folder = tmp_path / 'Done'
        folder.mkdir()
        files = [_make_cbz(folder / 'd.cbz')]
        _, review_ids = self._seed_review_rows(folder, files)
        update_review_status(review_ids[0], 'resolved')

        r = bulk_client.post(
            f'/api/bulk-metadata/review/{review_ids[0]}/apply-cvinfo',
            json={'metron_series_id': '1'},
        )
        assert r.status_code == 409

    def test_resolve_file_level_without_issue_id(self, bulk_client, tmp_path):
        """Regression test for the Apply-500 bug.

        After a manual search the user picks a SERIES candidate (no issue_id
        in the payload). resolve_review's file-level branch must look up the
        issue by parsed number — that path was broken by a bad lazy import
        and returned an HTML 500 instead of JSON.
        """
        folder = tmp_path / 'Avengers Spotlight'
        folder.mkdir()
        cbz = _make_cbz(folder / 'Avengers Spotlight 021 (1989).cbz')
        _, review_ids = self._seed_review_rows(folder, [cbz], parsed_year=1989)

        series = SearchResult(
            provider=ProviderType.COMICVINE,
            id='4218', title='Avengers Spotlight', year=1989, publisher='Marvel',
        )
        issues = [
            IssueResult(
                provider=ProviderType.COMICVINE, id='100', series_id='4218',
                issue_number=str(i),
            ) for i in (19, 20, 21, 22)
        ]
        provider = self._provider_returning(series, issues)

        with patch('core.bulk_metadata._instantiate_provider', return_value=provider):
            r = bulk_client.post(
                f'/api/bulk-metadata/review/{review_ids[0]}/resolve',
                json={'provider': 'comicvine', 'series_id': '4218'},
            )

        # Critical: response is JSON, not an HTML 500 error page.
        assert r.content_type.startswith('application/json'), (
            f"expected JSON response, got {r.content_type} — "
            "this means an uncaught exception bubbled to Flask"
        )
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['success'] is True
        # And the CBZ actually got metadata.
        assert _read_xml_from_cbz(cbz) is not None

    def test_resolve_relaxes_variant_descriptor(self, bulk_client, tmp_path):
        """GCD-API returns issue descriptors like '21 [Cover by X]'. The
        file-level resolve must still match issue 21 from a filename like
        'Avengers Spotlight 021 (1989).cbz'."""
        folder = tmp_path / 'Avengers Spotlight'
        folder.mkdir()
        cbz = _make_cbz(folder / 'Avengers Spotlight 021 (1989).cbz')
        _, review_ids = self._seed_review_rows(folder, [cbz], parsed_year=1989)

        series = SearchResult(
            provider=ProviderType.GCD_API, id='3829', title='Avengers Spotlight', year=1989,
        )
        issues = [
            IssueResult(
                provider=ProviderType.GCD_API, id=str(900 + n), series_id='3829',
                issue_number=label,
            ) for n, label in enumerate(['19', '20 [Cover by X]', '21A', '21', '22'])
        ]
        with patch('core.bulk_metadata._instantiate_provider',
                   return_value=self._provider_returning(series, issues)):
            r = bulk_client.post(
                f'/api/bulk-metadata/review/{review_ids[0]}/resolve',
                json={'provider': 'gcd_api', 'series_id': '3829'},
            )
        assert r.status_code == 200, r.get_json()
        assert _read_xml_from_cbz(cbz) is not None

    def test_resolve_synthesizes_issue_when_provider_has_no_match(self, bulk_client, tmp_path):
        """When the user has explicitly picked a series via manual search and
        the provider's issue list doesn't contain a matching issue at all
        (e.g. GCD-API only ingested a partial run), we synthesize an
        IssueResult so the metadata write still succeeds with series-level
        info — better than refusing to write after a confirmed manual pick."""
        folder = tmp_path / 'Foo'
        folder.mkdir()
        cbz = _make_cbz(folder / 'Foo 099.cbz')
        _, review_ids = self._seed_review_rows(folder, [cbz])

        series = SearchResult(
            provider=ProviderType.GCD_API, id='1', title='Foo', year=2000,
        )
        # No issues match issue 99.
        issues = [
            IssueResult(provider=ProviderType.GCD_API, id='a', series_id='1', issue_number='1'),
            IssueResult(provider=ProviderType.GCD_API, id='b', series_id='1', issue_number='2'),
        ]
        with patch('core.bulk_metadata._instantiate_provider',
                   return_value=self._provider_returning(series, issues)):
            r = bulk_client.post(
                f'/api/bulk-metadata/review/{review_ids[0]}/resolve',
                json={'provider': 'gcd_api', 'series_id': '1'},
            )
        assert r.status_code == 200, r.get_json()
        xml = _read_xml_from_cbz(cbz)
        assert xml is not None
        assert b'<Series>Foo</Series>' in xml
        # The parsed issue number from the filename makes it into the XML.
        assert b'<Number>99</Number>' in xml

    def test_resolve_defaults_to_issue_one_when_unparseable(self, bulk_client, tmp_path):
        """A one-shot whose filename has no parseable issue number must fall back
        to issue #1 instead of erroring with
        'could not parse issue number from filename'."""
        folder = tmp_path / "Demis Wild Kingdom"
        folder.mkdir()
        cbz = _make_cbz(folder / "Demis Wild Kingdom.cbz")  # no issue number
        _, review_ids = self._seed_review_rows(folder, [cbz])

        series = SearchResult(
            provider=ProviderType.GCD_API, id='30644', title="Demi's Wild Kingdom", year=2009,
        )
        issues = [
            IssueResult(provider=ProviderType.GCD_API, id='1', series_id='30644', issue_number='1'),
        ]
        with patch('core.bulk_metadata._instantiate_provider',
                   return_value=self._provider_returning(series, issues)):
            r = bulk_client.post(
                f'/api/bulk-metadata/review/{review_ids[0]}/resolve',
                json={'provider': 'gcd_api', 'series_id': '30644'},
            )
        assert r.status_code == 200, r.get_json()
        assert r.get_json()['success'] is True
        xml = _read_xml_from_cbz(cbz)
        assert xml is not None
        assert b'<Number>1</Number>' in xml

    def test_flips_file_index_has_comicinfo(self, bulk_client, tmp_path):
        """The bug fix: after applying metadata, file_index.has_comicinfo
        must flip to 1 so the file drops off the Missing XML view."""
        from core.database import add_file_index_entry, get_db_connection

        folder = tmp_path / 'Hawkeye'
        folder.mkdir()
        cbz = _make_cbz(folder / 'Hawkeye 001.cbz')

        # Seed the file_index row in the "missing XML" state.
        add_file_index_entry(
            name='Hawkeye 001.cbz', path=cbz, entry_type='file',
            size=os.path.getsize(cbz), parent=str(folder),
        )
        from core.database import set_has_comicinfo
        set_has_comicinfo(cbz, 0)

        # Sanity: row is flagged as missing.
        conn = get_db_connection()
        row = conn.execute(
            "SELECT has_comicinfo FROM file_index WHERE path = ?", (cbz,)
        ).fetchone()
        conn.close()
        assert row is not None and row['has_comicinfo'] == 0

        _, review_ids = self._seed_review_rows(folder, [cbz])

        series = SearchResult(
            provider=ProviderType.METRON, id='7', title='Hawkeye', year=2012,
        )
        issues = [
            IssueResult(provider=ProviderType.METRON, id='71', series_id='7',
                        issue_number='1'),
        ]
        with patch('core.bulk_metadata._instantiate_provider',
                   return_value=self._provider_returning(series, issues)):
            r = bulk_client.post(
                f'/api/bulk-metadata/review/{review_ids[0]}/apply-cvinfo',
                json={'metron_series_id': '7'},
            )
        assert r.status_code == 200, r.get_json()

        # The fix: file_index row now reflects has_comicinfo = 1.
        conn = get_db_connection()
        row = conn.execute(
            "SELECT has_comicinfo, ci_series, ci_number FROM file_index WHERE path = ?",
            (cbz,),
        ).fetchone()
        conn.close()
        assert row is not None
        assert row['has_comicinfo'] == 1
        # The ci_* columns are populated too so the Missing XML query stops returning it
        # and the metadata browser picks it up.
        assert row['ci_series'] == 'Hawkeye'
        assert row['ci_number'] == '1'


class TestGroupCascade:
    """Cascade-resolution must respect (folder_path, parsed_series) — Annuals
    or other sibling series in the same folder must NOT be swept up when a
    different series is resolved."""

    def _seed(self, folder, files_with_series, parsed_year=None):
        """Each item in files_with_series is (path, parsed_series)."""
        import uuid
        from core.database import create_bulk_job, add_review_item

        job_id = uuid.uuid4().hex
        create_bulk_job(
            job_id=job_id,
            scope_type='files',
            scope_payload={'paths': [p for p, _ in files_with_series]},
        )
        ids = []
        for fp, series in files_with_series:
            rid = add_review_item(
                job_id=job_id,
                folder_path=str(folder),
                file_path=fp,
                parsed_series=series,
                parsed_issue=None,
                parsed_year=parsed_year,
                reason='issue_no_match',
                candidates=[],
            )
            ids.append(rid)
        return job_id, ids

    def _provider(self, series, issues):
        p = MagicMock()
        p.get_series.return_value = series
        p.get_issues.return_value = issues
        def _to_ci(issue, ser=None):
            return {
                'Series': ser.title if ser else series.title,
                'Number': issue.issue_number,
            }
        p.to_comicinfo.side_effect = _to_ci
        return p

    def test_apply_cvinfo_cascade_respects_parsed_series(self, bulk_client, tmp_path):
        """Two 'Foo' rows + one 'Foo Annual' row in the same folder. Resolving
        'Foo' must NOT mark the 'Foo Annual' row resolved."""
        from core.database import get_review_item

        folder = tmp_path / 'Foo'
        folder.mkdir()
        foo_a = _make_cbz(folder / 'Foo 001.cbz')
        foo_b = _make_cbz(folder / 'Foo 002.cbz')
        annual = _make_cbz(folder / 'Foo Annual 001.cbz')
        _, rids = self._seed(folder, [
            (foo_a, 'Foo'),
            (foo_b, 'Foo'),
            (annual, 'Foo Annual'),
        ])

        series = SearchResult(provider=ProviderType.METRON, id='1', title='Foo', year=2000)
        issues = [
            IssueResult(provider=ProviderType.METRON, id=str(i), series_id='1', issue_number=str(i))
            for i in (1, 2)
        ]

        with patch('core.bulk_metadata._instantiate_provider', return_value=self._provider(series, issues)):
            r = bulk_client.post(
                f'/api/bulk-metadata/review/{rids[0]}/apply-cvinfo',
                json={'metron_series_id': '1'},
            )
        assert r.status_code == 200, r.get_json()

        # Foo rows resolved, Annual stays pending.
        assert get_review_item(rids[0])['status'] == 'resolved'
        assert get_review_item(rids[1])['status'] == 'resolved'
        assert get_review_item(rids[2])['status'] == 'pending', (
            "Foo Annual row was wrongly cascaded — should respect parsed_series filter"
        )

    def test_resolve_cascades_across_group(self, bulk_client, tmp_path):
        """Two pending file-level rows with the same (folder, parsed_series).
        Resolving one via /resolve must also resolve the other and write
        metadata to both files."""
        from core.database import get_review_item

        folder = tmp_path / 'Bar'
        folder.mkdir()
        files = [_make_cbz(folder / f'Bar 00{i}.cbz') for i in (1, 2)]
        _, rids = self._seed(folder, [(files[0], 'Bar'), (files[1], 'Bar')])

        series = SearchResult(provider=ProviderType.METRON, id='9', title='Bar', year=2010)
        issues = [
            IssueResult(provider=ProviderType.METRON, id=str(i), series_id='9', issue_number=str(i))
            for i in (1, 2)
        ]

        with patch('core.bulk_metadata._instantiate_provider', return_value=self._provider(series, issues)):
            r = bulk_client.post(
                f'/api/bulk-metadata/review/{rids[0]}/resolve',
                json={'provider': 'metron', 'series_id': '9'},
            )
        assert r.status_code == 200, r.get_json()
        data = r.get_json()
        assert data['success']
        # Both rows resolved, both CBZs written.
        for rid in rids:
            assert get_review_item(rid)['status'] == 'resolved'
        for fp in files:
            xml = _read_xml_from_cbz(fp)
            assert xml is not None
            assert b'<Series>Bar</Series>' in xml

    def test_skip_series_dismisses_only_matching_group(self, bulk_client, tmp_path):
        """/skip-series must dismiss only rows in the same (folder, series)."""
        from core.database import get_review_item, get_bulk_job

        folder = tmp_path / 'Baz'
        folder.mkdir()
        x1 = _make_cbz(folder / 'X 001.cbz')
        x2 = _make_cbz(folder / 'X 002.cbz')
        y1 = _make_cbz(folder / 'Y 001.cbz')
        job_id, rids = self._seed(folder, [
            (x1, 'X'),
            (x2, 'X'),
            (y1, 'Y'),
        ])

        r = bulk_client.post(f'/api/bulk-metadata/review/{rids[0]}/skip-series')
        assert r.status_code == 200, r.get_json()
        data = r.get_json()
        assert data['count'] == 2
        assert sorted(data['dismissed']) == sorted([rids[0], rids[1]])

        assert get_review_item(rids[0])['status'] == 'dismissed'
        assert get_review_item(rids[1])['status'] == 'dismissed'
        assert get_review_item(rids[2])['status'] == 'pending'

        # Job counts reflect the skipped pair.
        job = get_bulk_job(job_id)
        assert job['skipped'] == 2

    def test_review_queue_enriches_cover_path(self, bulk_client, tmp_path):
        """Folder-level review rows (file_path=NULL) need cover_path resolved
        to the first CBZ in the folder — /api/thumbnail can't render folders.
        File-level rows pass file_path through unchanged."""
        import uuid
        from core.database import create_bulk_job, add_review_item

        folder = tmp_path / 'Mixed'
        folder.mkdir()
        # Two real CBZs in the folder + a stray file.
        cbz_first = _make_cbz(folder / 'A 001.cbz')
        _make_cbz(folder / 'A 002.cbz')
        (folder / 'README.txt').write_text('ignore me')

        job_id = uuid.uuid4().hex
        create_bulk_job(job_id=job_id, scope_type='files', scope_payload={'paths': []})
        # Folder-level row (file_path None).
        rid_folder = add_review_item(
            job_id=job_id, folder_path=str(folder), file_path=None,
            parsed_series='A', parsed_issue=None, parsed_year=None,
            reason='series_no_match', candidates=[],
        )
        # File-level row.
        rid_file = add_review_item(
            job_id=job_id, folder_path=str(folder), file_path=cbz_first,
            parsed_series='A', parsed_issue='1', parsed_year=None,
            reason='issue_no_match', candidates=[],
        )

        r = bulk_client.get(f'/api/bulk-metadata/review/{job_id}')
        assert r.status_code == 200
        items = {it['id']: it for it in r.get_json()['items']}

        # Folder-level row got the first CBZ as its cover.
        assert items[rid_folder]['cover_path'] == cbz_first
        # File-level row's cover is the file itself.
        assert items[rid_file]['cover_path'] == cbz_first

    def test_review_queue_cover_path_null_for_empty_folder(self, bulk_client, tmp_path):
        """When the folder has no CBZs (e.g. user moved them away after the
        job started), cover_path is None and the JS falls back to a placeholder."""
        import uuid
        from core.database import create_bulk_job, add_review_item

        folder = tmp_path / 'EmptyDir'
        folder.mkdir()

        job_id = uuid.uuid4().hex
        create_bulk_job(job_id=job_id, scope_type='files', scope_payload={'paths': []})
        rid = add_review_item(
            job_id=job_id, folder_path=str(folder), file_path=None,
            parsed_series='X', parsed_issue=None, parsed_year=None,
            reason='series_no_match', candidates=[],
        )

        r = bulk_client.get(f'/api/bulk-metadata/review/{job_id}')
        items = r.get_json()['items']
        assert len(items) == 1
        assert items[0]['cover_path'] is None

    def test_register_operation_accepts_custom_op_id(self, db_connection):
        """Smoke test for the app_state extension — custom op_id is honored
        so /api/bulk-metadata/op-progress/<op_id> can be polled by a
        client-generated token."""
        import core.app_state as app_state
        token = 'bm-test-' + str(id(self))
        returned = app_state.register_operation(
            'unit_test', 'label', total=3, op_id=token
        )
        assert returned == token
        app_state.update_operation(token, current=2, total=3, detail='file.cbz')
        active = {o['id']: o for o in app_state.get_active_operations()}
        assert token in active
        assert active[token]['current'] == 2
        assert active[token]['detail'] == 'file.cbz'
        app_state.complete_operation(token)

    def test_op_progress_endpoint_returns_state(self, bulk_client, db_connection):
        """The endpoint surfaces an in-flight op so the modal can render
        per-file progress while the Apply request is still pending."""
        import core.app_state as app_state
        token = 'bm-progress-test'
        app_state.register_operation(
            'bulk_review_apply', 'Test', total=5, op_id=token
        )
        app_state.update_operation(token, current=3, total=5, detail='Captain America 134 (1971).cbz')

        r = bulk_client.get(f'/api/bulk-metadata/op-progress/{token}')
        assert r.status_code == 200
        d = r.get_json()
        assert d['success'] is True
        assert d['current'] == 3
        assert d['total'] == 5
        assert d['detail'] == 'Captain America 134 (1971).cbz'
        assert d['status'] == 'running'

    def test_op_progress_endpoint_404_when_missing(self, bulk_client):
        r = bulk_client.get('/api/bulk-metadata/op-progress/nonexistent-token')
        assert r.status_code == 404

    def test_applies_custom_rename_after_metadata_write(self, bulk_client, tmp_path):
        """The user-configured Custom Naming pattern (config.html /
        user_preferences) must be applied to bulk writes the same way the
        legacy single-file flow applies it — otherwise files keep their
        pre-metadata names like 'Be Not Afraid 005.cbz' instead of becoming
        'Be Not Afraid 005 (2026).cbz'."""
        import uuid as _uuid
        from core.database import (
            set_user_preference, create_bulk_job, add_review_item,
            add_file_index_entry,
        )

        # Enable Custom Naming for this test.
        set_user_preference('enable_custom_rename', True, category='rename')
        set_user_preference(
            'custom_rename_pattern',
            '{series_name} {issue_number} ({year})',
            category='rename',
        )

        folder = tmp_path / 'Be Not Afraid (2025)'
        folder.mkdir()
        old_cbz = _make_cbz(folder / 'Be Not Afraid 005.cbz')
        add_file_index_entry(
            name='Be Not Afraid 005.cbz', path=old_cbz, entry_type='file',
            size=1, parent=str(folder),
        )

        job_id = _uuid.uuid4().hex
        create_bulk_job(job_id=job_id, scope_type='files', scope_payload={'paths': [old_cbz]})
        review_id = add_review_item(
            job_id=job_id, folder_path=str(folder), file_path=old_cbz,
            parsed_series='Be Not Afraid', parsed_issue='5', parsed_year=2025,
            reason='issue_no_match', candidates=[],
        )

        series = SearchResult(
            provider=ProviderType.METRON, id='12087', title='Be Not Afraid',
            year=2025, publisher='Boom! Studios',
        )
        issues = [
            IssueResult(provider=ProviderType.METRON, id='1', series_id='12087', issue_number='5')
        ]
        provider = self._provider(series, issues)
        # Override the default _provider's to_comicinfo so Year=2026 (the
        # issue's cover year) lands in the metadata dict, mirroring the
        # user's log where the issue's cover_date year differed from the
        # series start year.
        def _to_ci(issue, ser=None):
            return {
                'Series': 'Be Not Afraid', 'Number': issue.issue_number,
                'Year': 2026, 'Publisher': 'Boom! Studios',
            }
        provider.to_comicinfo.side_effect = _to_ci

        with patch('core.bulk_metadata._instantiate_provider', return_value=provider):
            r = bulk_client.post(
                f'/api/bulk-metadata/review/{review_id}/resolve',
                json={'provider': 'metron', 'series_id': '12087'},
            )
        assert r.status_code == 200, r.get_json()

        # Old file no longer exists; new file at the expected path.
        expected = folder / 'Be Not Afraid 005 (2026).cbz'
        assert not os.path.exists(old_cbz), "original filename should be gone after rename"
        assert expected.exists(), f"expected renamed file at {expected}"
        # And it carries the metadata.
        xml = _read_xml_from_cbz(str(expected))
        assert xml is not None
        assert b'<Series>Be Not Afraid</Series>' in xml

    def test_skip_rename_when_pattern_disabled(self, bulk_client, tmp_path):
        """When Custom Naming is OFF the bulk writer must leave filenames alone."""
        import uuid as _uuid
        from core.database import (
            set_user_preference, create_bulk_job, add_review_item,
        )

        set_user_preference('enable_custom_rename', False, category='rename')

        folder = tmp_path / 'Quiet'
        folder.mkdir()
        cbz = _make_cbz(folder / 'Quiet 001.cbz')
        job_id = _uuid.uuid4().hex
        create_bulk_job(job_id=job_id, scope_type='files', scope_payload={'paths': [cbz]})
        review_id = add_review_item(
            job_id=job_id, folder_path=str(folder), file_path=cbz,
            parsed_series='Quiet', parsed_issue='1', parsed_year=2020,
            reason='issue_no_match', candidates=[],
        )

        series = SearchResult(provider=ProviderType.METRON, id='1', title='Quiet', year=2020)
        issues = [IssueResult(provider=ProviderType.METRON, id='1', series_id='1', issue_number='1')]

        with patch('core.bulk_metadata._instantiate_provider', return_value=self._provider(series, issues)):
            r = bulk_client.post(
                f'/api/bulk-metadata/review/{review_id}/resolve',
                json={'provider': 'metron', 'series_id': '1'},
            )
        assert r.status_code == 200, r.get_json()
        # Filename unchanged.
        assert os.path.exists(cbz)

    def test_apply_cvinfo_skips_files_with_existing_comicinfo(self, bulk_client, tmp_path):
        """Regression: when the user resolves a review row via apply-cvinfo,
        sibling files in the folder that already have ComicInfo.xml MUST NOT
        be overwritten. Previously _apply_series_to_folder happily clobbered
        hand-curated metadata."""
        import uuid as _uuid
        from core.database import create_bulk_job, add_review_item

        folder = tmp_path / 'CapAm v1968'
        folder.mkdir()
        # Pre-existing metadata on three files — these must NOT be touched.
        preserved = []
        for n in (196, 197, 198):
            fp = _make_cbz(folder / f'Captain America {n} (1976).cbz',
                           with_comicinfo=True)
            preserved.append(fp)
        # Two new files without metadata — these get written.
        new_files = [
            _make_cbz(folder / f'Captain America {n} (1976).cbz')
            for n in (199, 200)
        ]

        # Seed review rows only for the new files (mirrors what the
        # orchestrator's initial pass does — files with metadata are skipped
        # at that stage so no review row is created for them).
        job_id = _uuid.uuid4().hex
        create_bulk_job(job_id=job_id, scope_type='files', scope_payload={'paths': new_files})
        review_id = add_review_item(
            job_id=job_id, folder_path=str(folder), file_path=new_files[0],
            parsed_series='Captain America', parsed_issue='199', parsed_year=1976,
            reason='issue_no_match', candidates=[],
        )
        add_review_item(
            job_id=job_id, folder_path=str(folder), file_path=new_files[1],
            parsed_series='Captain America', parsed_issue='200', parsed_year=1976,
            reason='issue_no_match', candidates=[],
        )

        series = SearchResult(provider=ProviderType.METRON, id='1', title='Captain America', year=1968)
        issues = [
            IssueResult(provider=ProviderType.METRON, id=str(n), series_id='1', issue_number=str(n))
            for n in (196, 197, 198, 199, 200)
        ]
        with patch('core.bulk_metadata._instantiate_provider', return_value=self._provider(series, issues)):
            r = bulk_client.post(
                f'/api/bulk-metadata/review/{review_id}/apply-cvinfo',
                json={'metron_series_id': '1'},
            )
        assert r.status_code == 200, r.get_json()
        # Only the 2 new files should have been written.
        assert r.get_json()['written'] == 2

        # The preserved CBZs still carry their original <Series>Old</Series>.
        for fp in preserved:
            xml = _read_xml_from_cbz(fp)
            assert xml is not None
            assert b'<Series>Old</Series>' in xml, (
                f"file with existing ComicInfo was overwritten: {fp}"
            )

        # The new files got the real series.
        for fp in new_files:
            xml = _read_xml_from_cbz(fp)
            assert xml is not None
            assert b'<Series>Captain America</Series>' in xml

    def test_apply_cvinfo_reports_progress_via_op_token(self, bulk_client, tmp_path):
        """End-to-end: client supplies op_token; after the request completes
        the op exists in app_state with current==total==written count."""
        import uuid as _uuid
        import core.app_state as app_state

        folder = tmp_path / 'CapAm'
        folder.mkdir()
        files = [_make_cbz(folder / f'Cap {i:03d}.cbz') for i in (1, 2, 3)]
        job_id, rids = self._seed(folder, [(f, 'Cap') for f in files], parsed_year=1968)

        token = 'bm-' + _uuid.uuid4().hex
        series = SearchResult(provider=ProviderType.METRON, id='1', title='Cap', year=1968)
        issues = [
            IssueResult(provider=ProviderType.METRON, id=str(i), series_id='1', issue_number=str(i))
            for i in (1, 2, 3)
        ]
        with patch('core.bulk_metadata._instantiate_provider', return_value=self._provider(series, issues)):
            r = bulk_client.post(
                f'/api/bulk-metadata/review/{rids[0]}/apply-cvinfo',
                json={'metron_series_id': '1', 'op_token': token},
            )
        assert r.status_code == 200, r.get_json()

        # The op was registered and progressed through all three files; final
        # state is current==total==3.
        active = {o['id']: o for o in app_state.get_active_operations()}
        assert token in active
        op = active[token]
        assert op['total'] == 3
        assert op['current'] == 3
        # Completed status, last detail is the last file processed.
        assert op['status'] == 'completed'

    def test_skip_series_rejects_already_resolved(self, bulk_client, tmp_path):
        from core.database import update_review_status

        folder = tmp_path / 'Z'
        folder.mkdir()
        files = [_make_cbz(folder / 'z.cbz')]
        _, rids = self._seed(folder, [(files[0], 'Z')])
        update_review_status(rids[0], 'resolved')

        r = bulk_client.post(f'/api/bulk-metadata/review/{rids[0]}/skip-series')
        assert r.status_code == 409


class TestApplySeriesOneShotFallback:
    """_apply_series_to_folder must fall back to issue #1 for an un-numbered
    file ONLY when the folder is a one-shot (single comic), never for multi-file
    folders (which would mis-map every file to #1)."""

    def _provider(self, issues):
        p = MagicMock()
        p.get_issues.return_value = issues
        return p

    def _series_and_issues(self):
        series = SearchResult(
            provider=ProviderType.COMICVINE, id='1', title='One Shot', year=2020,
        )
        issues = [IssueResult(provider=ProviderType.COMICVINE, id='i1', series_id='1', issue_number='1')]
        return series, issues

    def test_one_shot_unnumbered_uses_issue_one(self, tmp_path):
        from routes.bulk_metadata import _apply_series_to_folder
        folder = tmp_path / 'One Shot'
        folder.mkdir()
        _make_cbz(folder / 'One Shot Special.cbz')  # no issue number
        series, issues = self._series_and_issues()

        with patch('core.bulk_metadata._has_existing_comicinfo', return_value=False), \
             patch('core.bulk_metadata._write_metadata', return_value=True) as wm:
            written, errors = _apply_series_to_folder(
                job_id='j', folder_path=str(folder), provider_name='comicvine',
                provider=self._provider(issues), series=series, series_id='1',
                parsed_year=2020, matched_via='manual',
            )
        assert written == 1
        assert errors == 0
        # The single file was matched to issue #1.
        assert wm.call_args.kwargs['issue'].issue_number == '1'

    def test_multi_file_unnumbered_skips(self, tmp_path):
        from routes.bulk_metadata import _apply_series_to_folder
        folder = tmp_path / 'Multi'
        folder.mkdir()
        _make_cbz(folder / 'Multi One.cbz')  # no issue number
        _make_cbz(folder / 'Multi Two.cbz')  # no issue number
        series, issues = self._series_and_issues()

        with patch('core.bulk_metadata._has_existing_comicinfo', return_value=False), \
             patch('core.bulk_metadata._write_metadata', return_value=True) as wm:
            written, errors = _apply_series_to_folder(
                job_id='j', folder_path=str(folder), provider_name='comicvine',
                provider=self._provider(issues), series=series, series_id='1',
                parsed_year=2020, matched_via='manual',
            )
        # Neither un-numbered file is forced to #1 in a multi-file folder.
        assert written == 0
        assert not wm.called
