"""Tests for POST /api/force-metadata-match -- tag a file with an issue by id."""
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


def _make_cbz(path, with_comicinfo=False):
    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr("page_001.png", b"fake image data")
        if with_comicinfo:
            zf.writestr("ComicInfo.xml", "<ComicInfo><Series>Test</Series></ComicInfo>")


def _no_writes(stack):
    """Stub out the archive write and the index update; return the write mock."""
    write = stack.enter_context(patch("routes.metadata.add_comicinfo_to_cbz", return_value=True))
    stack.enter_context(patch("core.database.update_file_index_from_comicinfo"))
    stack.enter_context(patch("models.comicvine.auto_move_file", return_value=None))
    return write


class TestParseForcedIssueId:

    @pytest.mark.parametrize("provider,raw,expected", [
        ("metron", "12345", 12345),
        ("metron", "  12345 ", 12345),
        ("metron", "https://metron.cloud/issue/12345/", 12345),
        ("comicvine", "12345", 12345),
        ("comicvine", "4000-12345", 12345),
        ("comicvine", "https://comicvine.gamespot.com/batman-1/4000-12345/", 12345),
        ("comicvine", "https://comicvine.gamespot.com/batman-1/4000-12345", 12345),
    ])
    def test_accepted_forms(self, provider, raw, expected):
        from routes.metadata import _parse_forced_issue_id
        assert _parse_forced_issue_id(provider, raw) == expected

    @pytest.mark.parametrize("provider,raw", [
        ("metron", ""),
        ("metron", None),
        ("metron", "batman"),
        ("metron", "https://metron.cloud/issue/batman-2016-1/"),
        ("comicvine", "4050-abc"),
    ])
    def test_rejected_forms(self, provider, raw):
        from routes.metadata import _parse_forced_issue_id
        assert _parse_forced_issue_id(provider, raw) is None


class TestForceMetadataValidation:

    def test_missing_file_path(self, client):
        resp = client.post('/api/force-metadata-match', json={'provider': 'metron', 'issue_id': '1'})
        assert resp.status_code == 400

    def test_unknown_provider(self, client, tmp_path):
        cbz = tmp_path / "Batman 001.cbz"
        _make_cbz(str(cbz))
        resp = client.post('/api/force-metadata-match', json={
            'file_path': str(cbz), 'provider': 'gcd', 'issue_id': '1'})
        assert resp.status_code == 400

    def test_missing_file(self, client, tmp_path):
        resp = client.post('/api/force-metadata-match', json={
            'file_path': str(tmp_path / "nope.cbz"), 'provider': 'metron', 'issue_id': '1'})
        assert resp.status_code == 404

    def test_cbr_is_refused(self, client, tmp_path):
        cbr = tmp_path / "Batman 001.cbr"
        cbr.write_bytes(b"Rar!\x1a\x07\x00not really")
        resp = client.post('/api/force-metadata-match', json={
            'file_path': str(cbr), 'provider': 'metron', 'issue_id': '1'})
        assert resp.status_code == 400
        assert 'CBZ' in resp.get_json()['error']

    def test_bad_issue_id(self, client, tmp_path):
        cbz = tmp_path / "Batman 001.cbz"
        _make_cbz(str(cbz))
        resp = client.post('/api/force-metadata-match', json={
            'file_path': str(cbz), 'provider': 'metron', 'issue_id': 'batman'})
        assert resp.status_code == 400

    def test_path_outside_allowed_roots(self, client, tmp_path):
        cbz = tmp_path / "Batman 001.cbz"
        _make_cbz(str(cbz))
        with patch("helpers.library.is_allowed_path", return_value=False):
            resp = client.post('/api/force-metadata-match', json={
                'file_path': str(cbz), 'provider': 'metron', 'issue_id': '1'})
        assert resp.status_code == 403


class TestForceMetadataMetron:

    def test_applies_the_named_issue(self, client, tmp_path):
        cbz = tmp_path / "Wrong Name 007.cbz"
        _make_cbz(str(cbz))
        issue = {"id": 98765, "image": "https://example.com/cover.jpg"}
        mapped = {"Series": "Batman", "Number": "1", "Notes": "Metadata from Metron"}

        with ExitStack() as stack:
            write = _no_writes(stack)
            stack.enter_context(patch("models.metron.is_metron_configured", return_value=True))
            stack.enter_context(patch("models.metron.get_flask_api", return_value=MagicMock()))
            fetch = stack.enter_context(patch("models.metron.fetch_issue_detail", return_value=issue))
            stack.enter_context(patch("models.metron.map_to_comicinfo", return_value=mapped))
            resp = client.post('/api/force-metadata-match', json={
                'file_path': str(cbz), 'file_name': cbz.name,
                'provider': 'metron', 'issue_id': 'https://metron.cloud/issue/98765/'})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['source'] == 'metron'
        assert data['metadata'] == mapped
        assert data['image_url'] == "https://example.com/cover.jpg"
        assert 'rename_config' in data
        assert fetch.call_args.args[1] == 98765
        write.assert_called_once()
        assert write.call_args.args[0] == str(cbz)

    def test_not_found_leaves_file_alone(self, client, tmp_path):
        cbz = tmp_path / "Batman 001.cbz"
        _make_cbz(str(cbz))
        with ExitStack() as stack:
            write = _no_writes(stack)
            stack.enter_context(patch("models.metron.is_metron_configured", return_value=True))
            stack.enter_context(patch("models.metron.get_flask_api", return_value=MagicMock()))
            stack.enter_context(patch("models.metron.fetch_issue_detail", return_value=None))
            resp = client.post('/api/force-metadata-match', json={
                'file_path': str(cbz), 'provider': 'metron', 'issue_id': '5'})
        assert resp.status_code == 404
        write.assert_not_called()

    def test_not_configured(self, client, tmp_path):
        cbz = tmp_path / "Batman 001.cbz"
        _make_cbz(str(cbz))
        with ExitStack() as stack:
            write = _no_writes(stack)
            stack.enter_context(patch("models.metron.is_metron_configured", return_value=False))
            resp = client.post('/api/force-metadata-match', json={
                'file_path': str(cbz), 'provider': 'metron', 'issue_id': '5'})
        assert resp.status_code == 400
        write.assert_not_called()


class TestForceMetadataComicVine:

    def test_local_db_by_issue_id(self, client, tmp_path):
        from tests.mocked.conftest import build_comicvine_sqlite
        db = build_comicvine_sqlite(tmp_path / "cv.db")
        cbz = tmp_path / "Something Else 042.cbz"
        _make_cbz(str(cbz))
        client.application.config["COMICVINE_API_KEY"] = ""

        with ExitStack() as stack:
            write = _no_writes(stack)
            stack.enter_context(patch("models.comicvine_sqlite._get_saved_credentials",
                                      return_value={"database_path": db}))
            api = stack.enter_context(patch("models.comicvine.get_issue_by_id"))
            resp = client.post('/api/force-metadata-match', json={
                'file_path': str(cbz), 'provider': 'comicvine',
                'issue_id': 'https://comicvine.gamespot.com/batman-1/4000-500/'})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['source'] == 'comicvine'
        meta = data['metadata']
        assert meta['Series'] == 'Batman'
        assert meta['Number'] == '1'
        assert meta['Publisher'] == 'DC Comics'
        assert meta['Writer'] == 'Bob Kane'
        assert '4000-500' in meta['Notes']
        write.assert_called_once()
        api.assert_not_called()

    def test_api_fallback_when_local_db_lacks_issue(self, client, tmp_path):
        cbz = tmp_path / "Batman 001.cbz"
        _make_cbz(str(cbz))
        client.application.config["COMICVINE_API_KEY"] = "key"
        issue_data = {
            "id": 777, "name": "Pilot", "issue_number": "1", "volume_name": "Batman",
            "volume_id": 4050, "publisher": None, "year": 2016, "month": 6, "day": 1,
            "image_url": "https://example.com/777.jpg",
        }
        try:
            with ExitStack() as stack:
                write = _no_writes(stack)
                stack.enter_context(patch("models.comicvine_sqlite.check_database_status",
                                          return_value={"cv_sqlite_available": False}))
                api = stack.enter_context(patch("models.comicvine.get_issue_by_id",
                                                return_value=issue_data))
                stack.enter_context(patch("models.comicvine_source.get_volume_details",
                                          return_value={"name": "Batman", "start_year": 2016,
                                                        "publisher_name": "DC Comics"}))
                resp = client.post('/api/force-metadata-match', json={
                    'file_path': str(cbz), 'provider': 'comicvine', 'issue_id': '4000-777'})
        finally:
            client.application.config["COMICVINE_API_KEY"] = ""

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['metadata']['Publisher'] == 'DC Comics'
        assert data['metadata']['Volume'] == 2016
        assert data['image_url'] == "https://example.com/777.jpg"
        assert api.call_args.args == ("key", 777)
        write.assert_called_once()

    def test_not_configured(self, client, tmp_path):
        cbz = tmp_path / "Batman 001.cbz"
        _make_cbz(str(cbz))
        client.application.config["COMICVINE_API_KEY"] = ""
        with ExitStack() as stack:
            write = _no_writes(stack)
            stack.enter_context(patch("models.comicvine_sqlite.check_database_status",
                                      return_value={"cv_sqlite_available": False}))
            resp = client.post('/api/force-metadata-match', json={
                'file_path': str(cbz), 'provider': 'comicvine', 'issue_id': '500'})
        assert resp.status_code == 400
        write.assert_not_called()


class TestComicVineSqliteGetIssueById:

    def test_found_and_missing(self, tmp_path):
        from tests.mocked.conftest import build_comicvine_sqlite
        from models import comicvine_sqlite
        db = build_comicvine_sqlite(tmp_path / "cv.db")
        with patch("models.comicvine_sqlite._get_saved_credentials",
                   return_value={"database_path": db}):
            found = comicvine_sqlite.get_issue_by_id(500)
            missing = comicvine_sqlite.get_issue_by_id(999999)
        assert found['id'] == 500
        assert found['volume_name'] == 'Batman'
        assert found['publisher'] == 'DC Comics'
        assert missing is None


class TestMenuWiring:
    """The menu items and the modal are wired on both pages."""

    ROOT = Path(__file__).resolve().parents[2]

    def _read(self, rel):
        return (self.ROOT / rel).read_text(encoding='utf-8')

    def test_both_pages_include_the_modal(self):
        for tpl in ('templates/files.html', 'templates/collection.html'):
            assert "partials/modal_force_metadata.html" in self._read(tpl), tpl

    def test_collection_menu_item_is_bound(self):
        assert 'action-force-metadata' in self._read('templates/collection.html')
        js = self._read('static/js/collection.js')
        assert "'.action-force-metadata': () => forceMetadataCollection(" in js

    def test_files_menu_item_is_bound(self):
        js = self._read('static/js/files.js')
        assert 'Force Metadata Match' in js
        assert 'forceMetadataMatchForFile(fullPath' in js

    def test_client_posts_to_the_route(self):
        js = self._read('static/js/clu-metadata.js')
        assert 'CLU.forceMetadataMatch = function' in js
        assert "'/api/force-metadata-match'" in js
