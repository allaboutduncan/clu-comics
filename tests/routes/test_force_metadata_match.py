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
        ("gcd", "554991", 554991),
        ("gcd", "https://www.comics.org/issue/554991/", 554991),
        ("gcd", "https://www.comics.org/issue/554991", 554991),
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


class TestForceMetadataGCD:

    def _local_only(self, stack, db):
        stack.enter_context(patch("models.gcd._get_saved_credentials",
                                  return_value={"database_path": db}))
        stack.enter_context(patch("routes.metadata._gcd_api_configured", return_value=False))

    def test_local_db_by_issue_id(self, client, tmp_path):
        from tests.mocked.conftest import build_gcd_sqlite
        db = build_gcd_sqlite(tmp_path / "gcd.db")
        cbz = tmp_path / "Batman 001.cbz"
        _make_cbz(str(cbz))

        with ExitStack() as stack:
            write = _no_writes(stack)
            self._local_only(stack, db)
            resp = client.post('/api/force-metadata-match', json={
                'file_path': str(cbz), 'provider': 'gcd',
                'issue_id': 'https://www.comics.org/issue/500/'})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['source'] == 'gcd'
        meta = data['metadata']
        assert meta['Series'] == 'Batman'
        assert meta['Number'] == '1'
        assert meta['Publisher'] == 'DC Comics'
        assert meta['Writer'] == 'Bob Kane'
        write.assert_called_once()

    def test_id_wins_over_the_filename(self, client, tmp_path):
        """Issue 520 is Diabolik #1; the file is named for Batman #1."""
        from tests.mocked.conftest import build_gcd_sqlite
        db = build_gcd_sqlite(tmp_path / "gcd.db")
        cbz = tmp_path / "Batman 001.cbz"
        _make_cbz(str(cbz))

        with ExitStack() as stack:
            _no_writes(stack)
            self._local_only(stack, db)
            resp = client.post('/api/force-metadata-match', json={
                'file_path': str(cbz), 'provider': 'gcd', 'issue_id': '520'})

        assert resp.status_code == 200
        meta = resp.get_json()['metadata']
        assert meta['Series'] == 'Diabolik'
        assert meta['Title'] == 'Il re del terrore'

    def test_unknown_id_without_api_is_not_found(self, client, tmp_path):
        from tests.mocked.conftest import build_gcd_sqlite
        db = build_gcd_sqlite(tmp_path / "gcd.db")
        cbz = tmp_path / "Batman 001.cbz"
        _make_cbz(str(cbz))
        with ExitStack() as stack:
            write = _no_writes(stack)
            self._local_only(stack, db)
            resp = client.post('/api/force-metadata-match', json={
                'file_path': str(cbz), 'provider': 'gcd', 'issue_id': '999999'})
        assert resp.status_code == 404
        write.assert_not_called()

    def test_api_fallback(self, client, tmp_path):
        cbz = tmp_path / "Batman 001.cbz"
        _make_cbz(str(cbz))
        api_meta = {"Series": "Batman", "Number": "1", "Notes": "Metadata from GCD REST API.",
                    "_cover_url": "https://files1.comics.org/cover.jpg"}
        with ExitStack() as stack:
            write = _no_writes(stack)
            stack.enter_context(patch("models.gcd.check_database_status",
                                      return_value={"gcd_available": False}))
            stack.enter_context(patch("routes.metadata._gcd_api_configured", return_value=True))
            fetch = stack.enter_context(patch(
                "models.providers.gcd_api_provider.GCDApiProvider.get_issue_metadata_by_id",
                return_value=dict(api_meta)))
            resp = client.post('/api/force-metadata-match', json={
                'file_path': str(cbz), 'provider': 'gcd',
                'issue_id': 'https://www.comics.org/issue/554991/'})

        assert resp.status_code == 200
        data = resp.get_json()
        assert fetch.call_args.args == (554991,)
        assert data['image_url'] == "https://files1.comics.org/cover.jpg"
        assert '_cover_url' not in data['metadata']
        write.assert_called_once()

    def test_not_configured(self, client, tmp_path):
        cbz = tmp_path / "Batman 001.cbz"
        _make_cbz(str(cbz))
        with ExitStack() as stack:
            write = _no_writes(stack)
            stack.enter_context(patch("models.gcd.check_database_status",
                                      return_value={"gcd_available": False}))
            stack.enter_context(patch("routes.metadata._gcd_api_configured", return_value=False))
            resp = client.post('/api/force-metadata-match', json={
                'file_path': str(cbz), 'provider': 'gcd', 'issue_id': '500'})
        assert resp.status_code == 400
        write.assert_not_called()


class TestGCDApiGetIssueMetadataById:

    def test_builds_from_issue_and_its_series(self):
        from models.providers.gcd_api_provider import GCDApiProvider
        client = MagicMock()
        client.get_issue.return_value = {
            "id": 554991, "descriptor": "1", "key_date": "2016-06-00",
            "series": "https://www.comics.org/api/series/88888/",
            "cover": "https://files1.comics.org/cover.jpg", "story_set": [],
        }
        client.get_series.return_value = {"name": "Batman", "year_began": 2016}
        prov = GCDApiProvider()
        prov._client_instance = client

        meta = prov.get_issue_metadata_by_id(554991)

        client.get_issue.assert_called_once_with(554991)
        client.get_series.assert_called_once_with(88888)
        assert meta['Series'] == 'Batman'
        assert meta['Number'] == '1'
        assert meta['Web'] == 'https://www.comics.org/issue/554991/'
        assert meta['_cover_url'] == 'https://files1.comics.org/cover.jpg'

    def test_missing_issue(self):
        from models.providers.gcd_api_provider import GCDApiProvider
        client = MagicMock()
        client.get_issue.return_value = None
        prov = GCDApiProvider()
        prov._client_instance = client
        assert prov.get_issue_metadata_by_id(1) is None


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


class TestForceMetadataAutoRename:
    """With Auto-Rename on, a forced match renames the file server-side, for
    every provider, whether or not custom patterns are enabled."""

    MAPPED = {"Series": "Batman", "Number": "1", "Year": 2016, "Volume": 2016,
              "Notes": "Metadata from Metron"}

    def _post(self, client, cbz, stack, *, auto_rename, custom=(False, ""), oneshot=False):
        _no_writes(stack)
        stack.enter_context(patch("models.metron.is_metron_configured", return_value=True))
        stack.enter_context(patch("models.metron.get_flask_api", return_value=MagicMock()))
        stack.enter_context(patch("models.metron.fetch_issue_detail", return_value={"id": 1}))
        stack.enter_context(patch("models.metron.map_to_comicinfo", return_value=dict(self.MAPPED)))
        stack.enter_context(patch("cbz_ops.rename.load_custom_rename_config", return_value=custom))
        stack.enter_context(patch("cbz_ops.rename.load_issue_pad_width", return_value=3))
        stack.enter_context(patch("routes.metadata._is_oneshot_folder_safe", return_value=oneshot))
        move = stack.enter_context(patch("app.update_index_on_move"))
        client.application.config["ENABLE_AUTO_RENAME"] = auto_rename
        try:
            resp = client.post('/api/force-metadata-match', json={
                'file_path': str(cbz), 'file_name': cbz.name,
                'provider': 'metron', 'issue_id': '1'})
        finally:
            client.application.config["ENABLE_AUTO_RENAME"] = False
        return resp, move

    def test_renames_to_default_name_without_custom_pattern(self, client, tmp_path):
        cbz = tmp_path / "wrong name.cbz"
        _make_cbz(str(cbz))
        with ExitStack() as stack:
            resp, move = self._post(client, cbz, stack, auto_rename=True)

        data = resp.get_json()
        expected = tmp_path / "Batman 001.cbz"
        assert resp.status_code == 200
        assert data['renamed'] is True
        assert data['new_file_path'] == str(expected)
        assert expected.exists() and not cbz.exists()
        move.assert_called_once_with(str(cbz), str(expected))
        # The page must not rename a second time.
        assert data['rename_config']['auto_rename'] is False

    def test_uses_custom_pattern_when_enabled(self, client, tmp_path):
        cbz = tmp_path / "wrong name.cbz"
        _make_cbz(str(cbz))
        with ExitStack() as stack:
            resp, _ = self._post(client, cbz, stack, auto_rename=True,
                                 custom=(True, "{series_name} #{issue_number} ({volume_year})"))
        assert resp.get_json()['new_file_path'] == str(tmp_path / "Batman #001 (2016).cbz")

    def test_renames_in_a_oneshot_folder(self, client, tmp_path):
        """The one-shot guard exists for guessed matches; a forced one is not a guess."""
        cbz = tmp_path / "wrong name.cbz"
        _make_cbz(str(cbz))
        with ExitStack() as stack:
            resp, _ = self._post(client, cbz, stack, auto_rename=True, oneshot=True)
        assert resp.get_json()['renamed'] is True
        assert (tmp_path / "Batman 001.cbz").exists()

    def test_no_rename_when_auto_rename_is_off(self, client, tmp_path):
        cbz = tmp_path / "wrong name.cbz"
        _make_cbz(str(cbz))
        with ExitStack() as stack:
            resp, move = self._post(client, cbz, stack, auto_rename=False)
        data = resp.get_json()
        assert 'renamed' not in data
        assert cbz.exists()
        move.assert_not_called()

    def test_already_correct_name_is_left_alone(self, client, tmp_path):
        cbz = tmp_path / "Batman 001.cbz"
        _make_cbz(str(cbz))
        with ExitStack() as stack:
            resp, move = self._post(client, cbz, stack, auto_rename=True)
        data = resp.get_json()
        assert 'renamed' not in data
        assert data['rename_config']['auto_rename'] is False
        move.assert_not_called()

    def test_target_collision_is_not_overwritten(self, client, tmp_path):
        cbz = tmp_path / "wrong name.cbz"
        _make_cbz(str(cbz))
        existing = tmp_path / "Batman 001.cbz"
        _make_cbz(str(existing), with_comicinfo=True)
        with ExitStack() as stack:
            resp, move = self._post(client, cbz, stack, auto_rename=True)
        assert 'renamed' not in resp.get_json()
        assert cbz.exists()
        move.assert_not_called()


class TestRenameFallbackPattern:

    def test_fallback_only_used_when_custom_is_off(self, tmp_path):
        from cbz_ops.rename import rename_comic_from_metadata
        f = tmp_path / "x.cbz"
        f.write_bytes(b"")
        meta = {"Series": "Batman", "Number": "7"}
        with patch("cbz_ops.rename.load_custom_rename_config", return_value=(False, "")), \
             patch("cbz_ops.rename.load_issue_pad_width", return_value=3):
            # Historical behaviour is unchanged without a fallback.
            assert rename_comic_from_metadata(str(f), meta) == (str(f), False)
            new_path, renamed = rename_comic_from_metadata(
                str(f), meta, fallback_pattern="{series_name} {issue_number}")
        assert renamed is True
        assert new_path == str(tmp_path / "Batman 007.cbz")
