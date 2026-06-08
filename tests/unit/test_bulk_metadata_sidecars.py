"""Unit tests for ensure_folder_sidecars — cvinfo + series.json creation.

Verifies the bulk metadata process drops folder sidecars from a resolved
series, with provider-correct cvinfo content, sensible series.json id routing,
and without clobbering existing files.
"""
import json
import os

from core.bulk_metadata import ensure_folder_sidecars, _try_cvinfo
from models.providers import ProviderType
from models.providers.base import SearchResult


def _series(provider, **kw):
    return SearchResult(
        provider=provider,
        id=str(kw.get('id', '42')),
        title=kw.get('title', 'Batman'),
        year=kw.get('year', 2016),
        publisher=kw.get('publisher', 'DC Comics'),
        issue_count=kw.get('issue_count', 50),
        cover_url=kw.get('cover_url'),
        description=kw.get('description'),
    )


def _read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def _meta(folder):
    with open(os.path.join(folder, 'series.json'), encoding='utf-8') as f:
        return json.load(f)['metadata']


class TestEnsureFolderSidecars:

    def test_metron_writes_both_files(self, tmp_path):
        folder = str(tmp_path)
        ensure_folder_sidecars(folder, 'metron', _series(ProviderType.METRON, id='100'))

        cvinfo = _read(os.path.join(folder, 'cvinfo'))
        assert 'series_id: 100' in cvinfo
        assert '4050-' not in cvinfo  # no ComicVine URL for a Metron match
        assert 'publisher_name: DC Comics' in cvinfo
        assert 'start_year: 2016' in cvinfo

        meta = _meta(folder)
        assert meta['metron_id'] == '100'
        assert meta['comicid'] is None
        assert meta['name'] == 'Batman'
        assert meta['year'] == 2016
        assert meta['publisher'] == 'DC Comics'
        assert meta['total_issues'] == 50

    def test_comicvine_writes_url_without_series_id(self, tmp_path):
        folder = str(tmp_path)
        ensure_folder_sidecars(folder, 'comicvine', _series(ProviderType.COMICVINE, id='12345'))

        cvinfo = _read(os.path.join(folder, 'cvinfo'))
        assert 'https://comicvine.gamespot.com/volume/4050-12345/' in cvinfo
        # Must NOT emit a series_id line — _try_cvinfo would misread it as Metron.
        assert 'series_id:' not in cvinfo

        meta = _meta(folder)
        assert meta['comicid'] == '12345'
        assert meta['metron_id'] is None

    def test_gcd_skips_cvinfo_but_writes_series_json(self, tmp_path):
        folder = str(tmp_path)
        ensure_folder_sidecars(folder, 'gcd', _series(ProviderType.GCD, id='7'))

        assert not os.path.exists(os.path.join(folder, 'cvinfo'))
        meta = _meta(folder)
        assert meta['name'] == 'Batman'
        assert meta['metron_id'] is None
        assert meta['comicid'] is None

    def test_does_not_overwrite_existing_sidecars(self, tmp_path):
        folder = str(tmp_path)
        cvinfo_path = os.path.join(folder, 'cvinfo')
        sj_path = os.path.join(folder, 'series.json')
        with open(cvinfo_path, 'w', encoding='utf-8') as f:
            f.write('SENTINEL CVINFO')
        with open(sj_path, 'w', encoding='utf-8') as f:
            f.write('{"metadata": {"name": "SENTINEL"}}')

        ensure_folder_sidecars(folder, 'metron', _series(ProviderType.METRON, id='100'))

        assert _read(cvinfo_path) == 'SENTINEL CVINFO'
        assert _meta(folder)['name'] == 'SENTINEL'

    def test_cvinfo_round_trips_through_try_cvinfo(self, tmp_path):
        metron_dir = tmp_path / 'metron'
        cv_dir = tmp_path / 'cv'
        metron_dir.mkdir()
        cv_dir.mkdir()

        ensure_folder_sidecars(str(metron_dir), 'metron', _series(ProviderType.METRON, id='100'))
        ensure_folder_sidecars(str(cv_dir), 'comicvine', _series(ProviderType.COMICVINE, id='12345'))

        assert _try_cvinfo(str(metron_dir)) == ('metron', '100')
        assert _try_cvinfo(str(cv_dir)) == ('comicvine', '12345')

    def test_none_series_is_noop(self, tmp_path):
        folder = str(tmp_path)
        ensure_folder_sidecars(folder, 'metron', None)
        assert not os.path.exists(os.path.join(folder, 'cvinfo'))
        assert not os.path.exists(os.path.join(folder, 'series.json'))


class TestOneShotFolderNoSidecars:
    """One-shot folders (oneshots/specials/...) must never get folder sidecars,
    on any metadata write — both via ensure_folder_sidecars and the low-level
    cvinfo writers."""

    def test_ensure_folder_sidecars_skips_oneshot(self, tmp_path):
        folder = tmp_path / "oneshots"
        folder.mkdir()
        ensure_folder_sidecars(str(folder), 'metron', _series(ProviderType.METRON, id='100'))
        assert not os.path.exists(os.path.join(str(folder), 'cvinfo'))
        assert not os.path.exists(os.path.join(str(folder), 'series.json'))

    def test_low_level_cvinfo_writers_noop_in_oneshot(self, tmp_path):
        from models import metron, comicvine
        folder = tmp_path / "specials"
        folder.mkdir()
        cvinfo = str(folder / "cvinfo")

        assert metron.create_cvinfo_file(cvinfo, cv_id=1, series_id=2) is False
        assert comicvine.write_cvinfo_fields(cvinfo, "DC Comics", 2016) is False
        assert not os.path.exists(cvinfo)

    def test_writers_still_work_in_normal_folder(self, tmp_path):
        from models import metron
        folder = tmp_path / "Batman (2016)"
        folder.mkdir()
        cvinfo = str(folder / "cvinfo")
        assert metron.create_cvinfo_file(cvinfo, cv_id=1, series_id=2) is True
        assert os.path.exists(cvinfo)


class TestIsOneShotFolderPredicate:

    def test_matches_default_names(self):
        from core.config import is_oneshot_folder
        assert is_oneshot_folder("/data/Adult/oneshots")
        assert is_oneshot_folder("/x/specials")
        assert is_oneshot_folder("/x/one-shots/")

    def test_rejects_normal_and_empty(self):
        from core.config import is_oneshot_folder
        assert not is_oneshot_folder("/x/Batman (2016)")
        assert not is_oneshot_folder("")
        assert not is_oneshot_folder(None)
