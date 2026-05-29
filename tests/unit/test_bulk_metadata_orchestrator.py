"""Unit tests for the bulk metadata orchestrator.

These verify the auto-accept gate (exact name + year match) without touching
real providers — providers are mocked at the module level.
"""
import os
import zipfile
from unittest.mock import patch, MagicMock

import pytest

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


class TestAutoAcceptGate:

    def test_exact_name_and_year_accepts(self, db_connection):
        from core import bulk_metadata as bm

        fake = MagicMock()
        fake.search_series.return_value = [
            _make_search(ProviderType.METRON, 'Batman', 2020, id='42'),
        ]
        with patch.object(bm, '_instantiate_provider', return_value=fake):
            provider, accepted, agg = bm._resolve_series_auto(['metron'], 'Batman', 2020)
        assert provider == 'metron'
        assert accepted is not None
        assert accepted.id == '42'

    def test_name_match_but_wrong_year_queues(self, db_connection):
        from core import bulk_metadata as bm

        fake = MagicMock()
        fake.search_series.return_value = [
            _make_search(ProviderType.METRON, 'Batman', 1985, id='1'),
        ]
        with patch.object(bm, '_instantiate_provider', return_value=fake):
            provider, accepted, agg = bm._resolve_series_auto(['metron'], 'Batman', 2020)
        assert accepted is None
        assert len(agg) == 1

    def test_missing_year_never_auto_accepts(self, db_connection):
        from core import bulk_metadata as bm

        fake = MagicMock()
        fake.search_series.return_value = [
            _make_search(ProviderType.METRON, 'Batman', 2020, id='1'),
        ]
        with patch.object(bm, '_instantiate_provider', return_value=fake):
            provider, accepted, agg = bm._resolve_series_auto(['metron'], 'Batman', None)
        assert accepted is None

    def test_multiple_exact_matches_queues(self, db_connection):
        from core import bulk_metadata as bm

        fake = MagicMock()
        fake.search_series.return_value = [
            _make_search(ProviderType.METRON, 'Batman', 2020, id='1'),
            _make_search(ProviderType.METRON, 'Batman', 2020, id='2'),
        ]
        with patch.object(bm, '_instantiate_provider', return_value=fake):
            provider, accepted, agg = bm._resolve_series_auto(['metron'], 'Batman', 2020)
        assert accepted is None
        assert len(agg) == 2

    def test_normalises_case_and_punctuation(self, db_connection):
        from core import bulk_metadata as bm

        fake = MagicMock()
        fake.search_series.return_value = [
            _make_search(ProviderType.METRON, 'The Amazing Spider-Man', 2018, id='5'),
        ]
        with patch.object(bm, '_instantiate_provider', return_value=fake):
            # Caller queries with a slightly different surface form.
            provider, accepted, agg = bm._resolve_series_auto(
                ['metron'], 'the amazing spider man', 2018
            )
        assert accepted is not None
        assert accepted.id == '5'

    def test_accepts_when_year_is_string(self, db_connection):
        """ComicVine's Simyan adapter returns start_year as a string. The
        auto-accept gate must coerce both sides before comparing — a raw
        ``"1989" == 1989`` is False and would wrongly send the file to review."""
        from core import bulk_metadata as bm

        fake = MagicMock()
        # year deliberately a string here (ComicVine path).
        fake.search_series.return_value = [
            _make_search(ProviderType.COMICVINE, 'Avengers Spotlight', '1989', id='4218'),
        ]
        with patch.object(bm, '_instantiate_provider', return_value=fake):
            provider, accepted, agg = bm._resolve_series_auto(
                ['comicvine'], 'Avengers Spotlight', 1989
            )
        assert accepted is not None
        assert accepted.id == '4218'

    def test_years_match_helper(self, db_connection):
        from core.bulk_metadata import _years_match
        assert _years_match(1989, 1989) is True
        assert _years_match('1989', 1989) is True
        assert _years_match(1989, '1989') is True
        assert _years_match('1989', '1989') is True
        assert _years_match(None, 1989) is False
        assert _years_match(1989, None) is False
        assert _years_match('not-a-year', 1989) is False
        assert _years_match(1988, 1989) is False

    def test_strips_embedded_year_from_candidate_title(self, db_connection):
        """Metron sometimes returns series names with the year embedded
        (e.g. 'Avengers Spotlight (1989)'). The auto-accept gate must still
        match against a parsed needle of 'Avengers Spotlight'."""
        from core import bulk_metadata as bm

        fake = MagicMock()
        fake.search_series.return_value = [
            _make_search(ProviderType.METRON, 'Avengers Spotlight (1989)', 1989, id='7963'),
        ]
        with patch.object(bm, '_instantiate_provider', return_value=fake):
            provider, accepted, _ = bm._resolve_series_auto(
                ['metron'], 'Avengers Spotlight', 1989
            )
        assert accepted is not None
        assert accepted.id == '7963'

    def test_normalize_series_handles_embedded_year(self, db_connection):
        from core.bulk_metadata import _normalize_series
        assert _normalize_series('Avengers Spotlight (1989)') == 'avengers spotlight'
        assert _normalize_series('Avengers Spotlight') == 'avengers spotlight'
        assert _normalize_series('The Flash (2016)') == 'the flash'


class TestSeriesYearForFolder:
    """Annuals/Specials live in a parent folder that doesn't carry the
    keyword. Provider lookups must search the *file's* full series name in
    those cases — 'Avengers West Coast Annual' is a distinct series at
    Metron and ComicVine, separate from 'Avengers West Coast'."""

    def test_promotes_filename_when_annual_suffix(self, tmp_path, db_connection):
        from core.bulk_metadata import _series_year_for_folder
        folder = tmp_path / 'Avengers West Coast'
        folder.mkdir()
        f = folder / 'Avengers West Coast Annual 004 (1989).cbz'
        f.touch()
        series, year = _series_year_for_folder(str(folder), [str(f)])
        assert series == 'Avengers West Coast Annual'
        assert year == 1989

    def test_no_promotion_when_no_suffix_match(self, tmp_path, db_connection):
        """If the filename adds an unrecognised token (e.g. 'v1') we keep the
        safer folder-derived name to avoid breaking provider lookups."""
        from core.bulk_metadata import _series_year_for_folder
        folder = tmp_path / 'Captain Britain'
        folder.mkdir()
        f = folder / 'Captain Britain v1 001.cbz'
        f.touch()
        series, _ = _series_year_for_folder(str(folder), [str(f)])
        assert series == 'Captain Britain'

    def test_no_promotion_when_filename_matches_folder(self, tmp_path, db_connection):
        from core.bulk_metadata import _series_year_for_folder
        folder = tmp_path / 'Batman'
        folder.mkdir()
        f = folder / 'Batman 001 (2020).cbz'
        f.touch()
        series, year = _series_year_for_folder(str(folder), [str(f)])
        assert series == 'Batman'
        assert year == 2020

    def test_promotes_for_special_suffix(self, tmp_path, db_connection):
        from core.bulk_metadata import _series_year_for_folder
        folder = tmp_path / 'X-Men'
        folder.mkdir()
        f = folder / 'X-Men Special 001 (1990).cbz'
        f.touch()
        series, _ = _series_year_for_folder(str(folder), [str(f)])
        assert series == 'X-Men Special'

    def test_falls_back_to_filename_when_folder_empty(self, tmp_path, db_connection):
        """Edge: folder name strips down to empty — use filename series."""
        from core.bulk_metadata import _series_year_for_folder
        folder = tmp_path / '(1989)'  # Strips to ''
        folder.mkdir()
        f = folder / 'Avengers 001 (1989).cbz'
        f.touch()
        series, _ = _series_year_for_folder(str(folder), [str(f)])
        assert series == 'Avengers'

    def test_prefers_folder_year_over_file_year(self, tmp_path, db_connection):
        """Folder year tracks the series start (e.g. 'v2002'), file year
        tracks the individual issue's publication date. For series matching
        the folder year is the right signal — otherwise the provider ranks
        wrong-year volumes first."""
        from core.bulk_metadata import _series_year_for_folder
        folder = tmp_path / 'v2002'
        folder.mkdir()
        f = folder / 'Captain Marvel 015 (2003).cbz'
        f.touch()
        series, year = _series_year_for_folder(str(folder), [str(f)])
        assert series == 'Captain Marvel'
        assert year == 2002

    def test_falls_back_to_file_year_when_folder_has_no_year(self, tmp_path, db_connection):
        from core.bulk_metadata import _series_year_for_folder
        folder = tmp_path / 'Batman'
        folder.mkdir()
        f = folder / 'Batman 001 (2020).cbz'
        f.touch()
        _, year = _series_year_for_folder(str(folder), [str(f)])
        assert year == 2020

    def test_falls_through_providers_until_match(self, db_connection):
        from core import bulk_metadata as bm

        empty = MagicMock()
        empty.search_series.return_value = []
        hit = MagicMock()
        hit.search_series.return_value = [
            _make_search(ProviderType.COMICVINE, 'Saga', 2012, id='9'),
        ]

        def factory(name):
            return empty if name == 'metron' else hit

        with patch.object(bm, '_instantiate_provider', side_effect=factory):
            provider, accepted, agg = bm._resolve_series_auto(
                ['metron', 'comicvine'], 'Saga', 2012
            )
        assert provider == 'comicvine'
        assert accepted is not None


class TestIssueMatching:

    def test_single_match_returns_chosen(self, db_connection):
        from core import bulk_metadata as bm

        provider = MagicMock()
        provider.get_issues.return_value = [
            _make_issue(ProviderType.METRON, '42', '001', issue_id='1'),
            _make_issue(ProviderType.METRON, '42', '002', issue_id='2'),
        ]
        with patch.object(bm, '_instantiate_provider', return_value=provider):
            chosen, all_issues = bm._resolve_issue('metron', '42', '1')
        assert chosen is not None
        assert chosen.id == '1'

    def test_leading_zeros_are_ignored(self, db_connection):
        from core import bulk_metadata as bm

        provider = MagicMock()
        provider.get_issues.return_value = [
            _make_issue(ProviderType.METRON, '42', '1', issue_id='X'),
        ]
        with patch.object(bm, '_instantiate_provider', return_value=provider):
            chosen, _ = bm._resolve_issue('metron', '42', '001')
        assert chosen is not None and chosen.id == 'X'


class TestScopeExpansion:

    def test_files_groups_by_parent(self, tmp_path):
        from core.bulk_metadata import _group_files_by_folder

        f1 = tmp_path / 'a' / 'one.cbz'
        f2 = tmp_path / 'a' / 'two.cbz'
        f3 = tmp_path / 'b' / 'three.cbz'
        for p in (f1, f2, f3):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()

        buckets = _group_files_by_folder([str(f1), str(f2), str(f3)])
        assert len(buckets) == 2
        assert str(f1.parent) in buckets
        assert sorted(buckets[str(f1.parent)]) == sorted([str(f1), str(f2)])


class TestReadExistingComicinfo:

    def _make_cbz(self, path, comicinfo=None):
        with zipfile.ZipFile(str(path), 'w') as zf:
            zf.writestr('page1.png', b'fake')
            if comicinfo is not None:
                zf.writestr('ComicInfo.xml', comicinfo)

    def test_returns_none_when_absent(self, tmp_path):
        from core.bulk_metadata import _read_existing_comicinfo_bytes
        p = tmp_path / 'no_meta.cbz'
        self._make_cbz(p)
        assert _read_existing_comicinfo_bytes(str(p)) is None

    def test_returns_bytes_when_present(self, tmp_path):
        from core.bulk_metadata import _read_existing_comicinfo_bytes
        p = tmp_path / 'with_meta.cbz'
        self._make_cbz(p, comicinfo=b'<ComicInfo><Series>X</Series></ComicInfo>')
        data = _read_existing_comicinfo_bytes(str(p))
        assert data is not None
        assert b'<Series>X</Series>' in data
