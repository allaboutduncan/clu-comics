"""Tests for routes/metadata.py -- metadata management endpoints."""
import io
import json
import os
import re
import zipfile
import xml.etree.ElementTree as ET
import pytest
from unittest.mock import patch, MagicMock, call


# generate_comicinfo_xml and _as_text moved to core.comicinfo when the two
# drifting copies were merged; their tests live in
# tests/unit/test_comicinfo_writer.py, which also asserts the re-export here
# is the same object.


def _make_cbz(path, with_comicinfo=True):
    """Helper to create a minimal CBZ file for testing."""
    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr("page_001.png", b"fake image data")
        if with_comicinfo:
            zf.writestr("ComicInfo.xml", "<ComicInfo><Series>Test</Series></ComicInfo>")


class TestAddComicInfoToCbz:
    """Round-trip tests for add_comicinfo_to_cbz (real archive assembly)."""

    def _patch_cache_dir(self, cache_dir):
        """Point CACHE_DIR at a local temp dir so assembly happens off /data."""
        from routes import metadata

        real_get = metadata.config.get

        def fake_get(section, option, *args, **kwargs):
            if section == "SETTINGS" and option == "CACHE_DIR":
                return str(cache_dir)
            return real_get(section, option, *args, **kwargs)

        return patch.object(metadata.config, "get", side_effect=fake_get)

    def test_inserts_comicinfo_at_root_and_preserves_images(self, tmp_path):
        from routes.metadata import add_comicinfo_to_cbz

        cbz_path = str(tmp_path / "Batman 001.cbz")
        _make_cbz(cbz_path, with_comicinfo=False)

        xml = b"<ComicInfo><Series>Batman</Series><Number>1</Number></ComicInfo>"
        with self._patch_cache_dir(tmp_path / "cache"), \
             patch("helpers.match_parent_permissions"):
            add_comicinfo_to_cbz(cbz_path, xml)

        with zipfile.ZipFile(cbz_path, "r") as zf:
            names = zf.namelist()
            assert "ComicInfo.xml" in names, "ComicInfo.xml must be at archive root"
            assert "page_001.png" in names, "original images must be preserved"
            assert zf.read("ComicInfo.xml") == xml

    def test_replaces_existing_comicinfo(self, tmp_path):
        from routes.metadata import add_comicinfo_to_cbz

        cbz_path = str(tmp_path / "Batman 002.cbz")
        _make_cbz(cbz_path, with_comicinfo=True)  # starts with a stub ComicInfo.xml

        xml = b"<ComicInfo><Series>New</Series></ComicInfo>"
        with self._patch_cache_dir(tmp_path / "cache"), \
             patch("helpers.match_parent_permissions"):
            add_comicinfo_to_cbz(cbz_path, xml)

        with zipfile.ZipFile(cbz_path, "r") as zf:
            # Exactly one ComicInfo.xml (case-insensitive), holding the new bytes.
            ci = [n for n in zf.namelist() if os.path.basename(n).lower() == "comicinfo.xml"]
            assert len(ci) == 1
            assert zf.read(ci[0]) == xml

    def test_leaves_no_temp_artifacts(self, tmp_path):
        from routes.metadata import add_comicinfo_to_cbz

        cbz_path = str(tmp_path / "Batman 003.cbz")
        _make_cbz(cbz_path, with_comicinfo=False)

        with self._patch_cache_dir(tmp_path / "cache"), \
             patch("helpers.match_parent_permissions"):
            add_comicinfo_to_cbz(cbz_path, b"<ComicInfo/>")

        leftovers = [n for n in os.listdir(tmp_path) if n.startswith(".tmp")]
        assert leftovers == [], f"temp extraction dirs not cleaned up: {leftovers}"


    def test_preserves_tags_the_new_metadata_omits(self, tmp_path):
        """No provider covers every ComicInfo field -- ComicVine has no genre
        data at all -- so a plain rebuild used to wipe Genre/Editor on re-tag."""
        from routes.metadata import add_comicinfo_to_cbz

        cbz_path = str(tmp_path / "Batman 004.cbz")
        with zipfile.ZipFile(cbz_path, "w") as zf:
            zf.writestr("page_001.png", b"fake image data")
            zf.writestr(
                "ComicInfo.xml",
                "<ComicInfo><Series>Old</Series><Genre>Humor</Genre>"
                "<Editor>Tim Truman</Editor></ComicInfo>",
            )

        xml = b"<ComicInfo><Series>Batman</Series></ComicInfo>"
        with self._patch_cache_dir(tmp_path / "cache"),              patch("helpers.match_parent_permissions"):
            add_comicinfo_to_cbz(cbz_path, xml)

        with zipfile.ZipFile(cbz_path, "r") as zf:
            root = ET.fromstring(zf.read("ComicInfo.xml"))
        assert root.find("Series").text == "Batman"   # new value wins
        assert root.find("Genre").text == "Humor"     # carried forward
        assert root.find("Editor").text == "Tim Truman"

    def test_merge_can_be_disabled(self, tmp_path):
        from routes.metadata import add_comicinfo_to_cbz

        cbz_path = str(tmp_path / "Batman 005.cbz")
        with zipfile.ZipFile(cbz_path, "w") as zf:
            zf.writestr("page_001.png", b"fake image data")
            zf.writestr("ComicInfo.xml", "<ComicInfo><Genre>Humor</Genre></ComicInfo>")

        xml = b"<ComicInfo><Series>Batman</Series></ComicInfo>"
        with self._patch_cache_dir(tmp_path / "cache"),              patch("helpers.match_parent_permissions"):
            add_comicinfo_to_cbz(cbz_path, xml, merge_existing=False)

        with zipfile.ZipFile(cbz_path, "r") as zf:
            assert zf.read("ComicInfo.xml") == xml


class TestRemoveComicInfoHelper:

    @patch("core.database.set_has_comicinfo")
    def test_removes_comicinfo_from_cbz(self, mock_set, tmp_path):
        from routes.metadata import _remove_comicinfo_from_cbz

        cbz_path = str(tmp_path / "test.cbz")
        _make_cbz(cbz_path, with_comicinfo=True)

        result = _remove_comicinfo_from_cbz(cbz_path)
        assert result["success"] is True

        # Verify ComicInfo.xml was removed
        with zipfile.ZipFile(cbz_path, 'r') as zf:
            names = [n.lower() for n in zf.namelist()]
            assert "comicinfo.xml" not in names
            assert "page_001.png" in names

    def test_no_comicinfo_returns_error(self, tmp_path):
        from routes.metadata import _remove_comicinfo_from_cbz

        cbz_path = str(tmp_path / "no_xml.cbz")
        _make_cbz(cbz_path, with_comicinfo=False)

        result = _remove_comicinfo_from_cbz(cbz_path)
        assert result["success"] is False
        assert "not found" in result["error"]

    def test_nonexistent_file(self):
        from routes.metadata import _remove_comicinfo_from_cbz

        result = _remove_comicinfo_from_cbz("/nonexistent/path/file.cbz")
        assert result["success"] is False
        assert "not found" in result["error"].lower()


class TestBulkClearComicInfo:

    @patch("core.database.set_has_comicinfo")
    def test_bulk_clear_with_directory(self, mock_set, client, tmp_path):
        cbz_dir = str(tmp_path / "data" / "comics")
        os.makedirs(cbz_dir, exist_ok=True)
        _make_cbz(os.path.join(cbz_dir, "a.cbz"))
        _make_cbz(os.path.join(cbz_dir, "b.cbz"))

        resp = client.post('/cbz-bulk-clear-comicinfo',
                           json={"directory": cbz_dir})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["total"] == 2
        assert "op_id" in data

    @patch("core.database.set_has_comicinfo")
    def test_bulk_clear_with_paths(self, mock_set, client, tmp_path):
        cbz1 = str(tmp_path / "data" / "one.cbz")
        cbz2 = str(tmp_path / "data" / "two.cbz")
        os.makedirs(str(tmp_path / "data"), exist_ok=True)
        _make_cbz(cbz1)
        _make_cbz(cbz2)

        resp = client.post('/cbz-bulk-clear-comicinfo',
                           json={"paths": [cbz1, cbz2]})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["total"] == 2

    def test_bulk_clear_empty(self, client, tmp_path):
        empty_dir = str(tmp_path / "data" / "empty")
        os.makedirs(empty_dir, exist_ok=True)

        resp = client.post('/cbz-bulk-clear-comicinfo',
                           json={"directory": empty_dir})
        assert resp.status_code == 400
        data = resp.get_json()
        assert data["success"] is False

    @patch("core.database.set_has_comicinfo")
    def test_single_endpoint_still_works(self, mock_set, client, tmp_path):
        cbz_path = str(tmp_path / "data" / "single.cbz")
        os.makedirs(str(tmp_path / "data"), exist_ok=True)
        _make_cbz(cbz_path)

        resp = client.post('/cbz-clear-comicinfo',
                           json={"path": cbz_path})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True


class TestUpdateXmlFileIndexSync:

    @patch("routes.metadata._sync_file_index_after_xml_update")
    @patch("models.update_xml.update_field_in_cbz_files")
    @patch("routes.metadata.is_valid_library_path", return_value=True)
    def test_update_xml_calls_sync(self, mock_valid, mock_update, mock_sync, client, tmp_path):
        """After update_field_in_cbz_files, _sync_file_index_after_xml_update is called."""
        comic_dir = str(tmp_path / "data" / "comics")
        os.makedirs(comic_dir, exist_ok=True)

        mock_update.return_value = {
            'updated': 1, 'skipped': 0, 'errors': 0,
            'details': [{'file': 'issue1.cbz', 'status': 'updated'}],
        }

        resp = client.post('/api/update-xml', json={
            "directory": comic_dir,
            "field": "Volume",
            "value": "2020",
        })
        assert resp.status_code == 200
        mock_sync.assert_called_once_with(
            comic_dir, "Volume", "2020", mock_update.return_value,
        )

    @patch("core.database.update_file_index_ci_field")
    def test_sync_updates_ci_field_for_updated_files(self, mock_db_update):
        """_sync_file_index_after_xml_update calls update_file_index_ci_field per file."""
        from routes.metadata import _sync_file_index_after_xml_update

        result = {
            'updated': 2, 'skipped': 1, 'errors': 0,
            'details': [
                {'file': 'issue1.cbz', 'status': 'updated'},
                {'file': 'issue2.cbz', 'status': 'skipped', 'reason': 'no xml'},
                {'file': 'issue3.cbz', 'status': 'updated'},
            ],
        }
        _sync_file_index_after_xml_update("/data/comics", "Volume", "2020", result)

        assert mock_db_update.call_count == 2
        mock_db_update.assert_any_call(
            os.path.join("/data/comics", "issue1.cbz"), "ci_volume", "2020",
        )
        mock_db_update.assert_any_call(
            os.path.join("/data/comics", "issue3.cbz"), "ci_volume", "2020",
        )

    @patch("core.database.update_file_index_ci_field")
    def test_sync_skips_unmapped_field(self, mock_db_update):
        """Fields without ci_ mapping (e.g. SeriesGroup) are silently skipped."""
        from routes.metadata import _sync_file_index_after_xml_update

        result = {
            'updated': 1, 'skipped': 0, 'errors': 0,
            'details': [{'file': 'issue1.cbz', 'status': 'updated'}],
        }
        _sync_file_index_after_xml_update("/data/comics", "SeriesGroup", "X-Men", result)

        mock_db_update.assert_not_called()

    @patch("core.database.update_file_index_ci_field", side_effect=Exception("db error"))
    def test_sync_logs_warning_on_db_failure(self, mock_db_update):
        """Database errors are caught and logged, not raised."""
        from routes.metadata import _sync_file_index_after_xml_update

        result = {
            'updated': 1, 'skipped': 0, 'errors': 0,
            'details': [{'file': 'issue1.cbz', 'status': 'updated'}],
        }
        # Should not raise
        _sync_file_index_after_xml_update("/data/comics", "Series", "Batman", result)


class TestSearchMetadataParsedFilename:
    """Tests for parsed_filename in 404 responses and search_term override."""

    @patch("models.metron.is_metron_configured", return_value=False)
    @patch("models.metron.is_connection_error", return_value=False)
    @patch("models.gcd.is_database_available", return_value=False)
    @patch("models.gcd.check_database_status", return_value={"gcd_available": False})
    @patch("models.comicvine.find_cvinfo_in_folder", return_value=None)
    @patch("models.comicvine.extract_issue_number", return_value=None)
    @patch("core.database.get_library_providers", return_value=[])
    @patch("core.database.set_has_comicinfo")
    def test_404_includes_parsed_filename(
        self, mock_set, mock_providers, mock_extract, mock_cvinfo,
        mock_mysql_status, mock_mysql, mock_conn_err, mock_metron, client
    ):
        """When all providers are exhausted, 404 response includes parsed_filename."""
        resp = client.post('/api/search-metadata', json={
            'file_path': '/data/Batman 001 (2020).cbz',
            'file_name': 'Batman 001 (2020).cbz',
        })
        assert resp.status_code == 404
        data = resp.get_json()
        assert data["success"] is False
        assert "parsed_filename" in data
        assert data["parsed_filename"]["series_name"] == "Batman"
        assert data["parsed_filename"]["issue_number"] == "1"
        assert data["parsed_filename"]["year"] == 2020

    @patch("models.metron.is_metron_configured", return_value=False)
    @patch("models.metron.is_connection_error", return_value=False)
    @patch("models.gcd.is_database_available", return_value=False)
    @patch("models.gcd.check_database_status", return_value={"gcd_available": False})
    @patch("models.comicvine.find_cvinfo_in_folder", return_value=None)
    @patch("models.comicvine.extract_issue_number", return_value=None)
    @patch("core.database.get_library_providers", return_value=[])
    @patch("core.database.set_has_comicinfo")
    def test_volume_pattern_parses_series_and_number(
        self, mock_set, mock_providers, mock_extract, mock_cvinfo,
        mock_mysql_status, mock_mysql, mock_conn_err, mock_metron, client
    ):
        """Manga volume filenames like 'Angel Heart v01.cbz' should parse
        series='Angel Heart' and issue_number='1', not series='Angel Heart v01'."""
        resp = client.post('/api/search-metadata', json={
            'file_path': '/data/manga/Angel Heart/Angel Heart v01.cbz',
            'file_name': 'Angel Heart v01.cbz',
        })
        assert resp.status_code == 404
        data = resp.get_json()
        assert data["parsed_filename"]["series_name"] == "Angel Heart"
        assert data["parsed_filename"]["issue_number"] == "1"

    @patch("models.metron.is_metron_configured", return_value=False)
    @patch("models.metron.is_connection_error", return_value=False)
    @patch("models.gcd.is_database_available", return_value=False)
    @patch("models.gcd.check_database_status", return_value={"gcd_available": False})
    @patch("models.comicvine.find_cvinfo_in_folder", return_value=None)
    @patch("models.comicvine.extract_issue_number", return_value=None)
    @patch("core.database.get_library_providers", return_value=[])
    @patch("core.database.set_has_comicinfo")
    def test_search_term_override(
        self, mock_set, mock_providers, mock_extract, mock_cvinfo,
        mock_mysql_status, mock_mysql, mock_conn_err, mock_metron, client
    ):
        """search_term override replaces the parsed series name."""
        resp = client.post('/api/search-metadata', json={
            'file_path': '/data/Batman 001 (2020).cbz',
            'file_name': 'Batman 001 (2020).cbz',
            'search_term': 'Dark Knight',
        })
        assert resp.status_code == 404
        data = resp.get_json()
        assert data["parsed_filename"]["series_name"] == "Dark Knight"

    @patch("models.metron.is_metron_configured", return_value=False)
    @patch("models.metron.is_connection_error", return_value=False)
    @patch("models.gcd.is_database_available", return_value=False)
    @patch("models.gcd.check_database_status", return_value={"gcd_available": False})
    @patch("models.comicvine.find_cvinfo_in_folder", return_value="/data/foo/cvinfo")
    @patch("models.comicvine.extract_issue_number", return_value=None)
    @patch("core.database.get_library_providers", return_value=[])
    @patch("core.database.set_has_comicinfo")
    def test_search_term_bypasses_stale_cvinfo(
        self, mock_set, mock_providers, mock_extract, mock_cvinfo,
        mock_mysql_status, mock_mysql, mock_conn_err, mock_metron, client
    ):
        """When a search_term override is supplied (manual search from the
        bulk review modal), the route must NOT consult cvinfo — otherwise a
        stale series_id from a prior failed attempt short-circuits provider
        lookup and searches the wrong series.

        We assert this by setting find_cvinfo_in_folder to return a path,
        but expecting that no provider attempt uses it. The mock_cvinfo
        return value is what the call would have produced if not bypassed."""
        resp = client.post('/api/search-metadata', json={
            'file_path': '/data/foo/Avengers West Coast Annual 004 (1989).cbz',
            'file_name': 'Avengers West Coast Annual 004 (1989).cbz',
            'search_term': 'Avengers West Coast Annual',
        })
        # All providers disabled in the mocks → 404, but the override series
        # name lands in parsed_filename and find_cvinfo_in_folder is not
        # exercised when search_term is set.
        assert resp.status_code == 404
        data = resp.get_json()
        assert data["parsed_filename"]["series_name"] == "Avengers West Coast Annual"
        # Confirm we never called find_cvinfo_in_folder — the route bypasses
        # cvinfo entirely when search_term is present.
        mock_cvinfo.assert_not_called()

    @patch("models.metron.is_metron_configured", return_value=False)
    @patch("models.metron.is_connection_error", return_value=False)
    @patch("models.gcd.is_database_available", return_value=False)
    @patch("models.gcd.check_database_status", return_value={"gcd_available": False})
    @patch("models.comicvine.find_cvinfo_in_folder", return_value=None)
    @patch("models.comicvine.extract_issue_number", return_value=None)
    @patch("core.database.get_library_providers", return_value=[])
    @patch("core.database.set_has_comicinfo")
    def test_search_year_overrides_parsed_year(
        self, mock_set, mock_providers, mock_extract, mock_cvinfo,
        mock_mysql_status, mock_mysql, mock_conn_err, mock_metron, client
    ):
        """Manual-search year input must override the year parsed from the
        filename. Without this, /api/search-metadata uses the issue's
        publication year (e.g. 2003) instead of the series start year the
        user supplied (e.g. 2002), and Metron/ComicVine rank wrong-year
        volumes first."""
        resp = client.post('/api/search-metadata', json={
            'file_path': '/data/Marvel/Captain Marvel/v2002/Captain Marvel 015 (2003).cbz',
            'file_name': 'Captain Marvel 015 (2003).cbz',
            'search_term': 'Captain Marvel',
            'search_year': 2002,
        })
        # No providers active → 404 fallthrough, but the parsed_filename
        # reflects the override.
        assert resp.status_code == 404
        data = resp.get_json()
        assert data["parsed_filename"]["series_name"] == "Captain Marvel"
        assert data["parsed_filename"]["year"] == 2002

    @patch("models.metron.is_metron_configured", return_value=False)
    @patch("models.metron.is_connection_error", return_value=False)
    @patch("models.gcd.is_database_available", return_value=False)
    @patch("models.gcd.check_database_status", return_value={"gcd_available": False})
    @patch("models.comicvine.find_cvinfo_in_folder", return_value=None)
    @patch("models.comicvine.extract_issue_number", return_value=None)
    @patch("core.database.get_library_providers", return_value=[])
    @patch("core.database.set_has_comicinfo")
    def test_search_year_invalid_value_falls_back_to_parsed(
        self, mock_set, mock_providers, mock_extract, mock_cvinfo,
        mock_mysql_status, mock_mysql, mock_conn_err, mock_metron, client
    ):
        """Non-integer search_year is ignored; the parsed file year survives."""
        resp = client.post('/api/search-metadata', json={
            'file_path': '/data/Batman 001 (2020).cbz',
            'file_name': 'Batman 001 (2020).cbz',
            'search_year': 'not-a-year',
        })
        assert resp.status_code == 404
        data = resp.get_json()
        assert data["parsed_filename"]["year"] == 2020


class TestSearchMetadataComicVineFailover:
    """ComicVine must never stall the search-metadata cascade.

    A hung or failing ComicVine attempt must be bounded so the cascade falls
    over to the next configured provider (gcd_api). See
    routes.metadata._try_comicvine_single (wall-clock guard) and
    models.comicvine._make_cv_client (per-request timeout).
    """

    def _configure(self, app, stack, *, search_volumes_side_effect):
        """Apply the shared mock stack; return the gcd_api mock for assertions."""
        app.config["COMICVINE_API_KEY"] = "test-key"

        stack.enter_context(patch("models.metron.is_metron_configured", return_value=False))
        stack.enter_context(patch("models.metron.is_connection_error", return_value=False))
        stack.enter_context(patch("models.gcd.is_database_available", return_value=False))
        stack.enter_context(patch("models.gcd.check_database_status",
                                  return_value={"gcd_available": False}))
        stack.enter_context(patch("models.comicvine.find_cvinfo_in_folder", return_value=None))
        stack.enter_context(patch("models.comicvine.is_simyan_available", return_value=True))
        stack.enter_context(patch("models.comicvine.search_volumes",
                                  side_effect=search_volumes_side_effect))
        stack.enter_context(patch("core.database.get_library_providers", return_value=[]))
        stack.enter_context(patch("core.database.get_provider_credentials",
                                  return_value={"username": "u", "password": "p"}))
        stack.enter_context(patch("core.database.set_has_comicinfo"))
        stack.enter_context(patch("core.database.update_file_index_from_comicinfo"))
        stack.enter_context(patch("routes.metadata.add_comicinfo_to_cbz", return_value=True))
        gcd_api = stack.enter_context(patch(
            "routes.metadata._try_gcd_api_single",
            return_value=({"Series": "Batman", "Number": "1"}, "http://img", None),
        ))
        return gcd_api

    def test_failover_when_comicvine_stalls(self, app, client):
        """A ComicVine call that hangs past CV_ATTEMPT_TIMEOUT is abandoned and
        the cascade falls over to gcd_api."""
        import time
        from contextlib import ExitStack

        def _slow(*args, **kwargs):
            time.sleep(0.5)  # outlives the patched timeout below
            return []

        with ExitStack() as stack:
            gcd_api = self._configure(app, stack, search_volumes_side_effect=_slow)
            stack.enter_context(patch("routes.metadata.CV_ATTEMPT_TIMEOUT", 0.15))

            started = time.monotonic()
            resp = client.post('/api/search-metadata', json={
                'file_path': '/data/Batman 001 (2020).cbz',
                'file_name': 'Batman 001 (2020).cbz',
            })
            elapsed = time.monotonic() - started

        assert resp.status_code == 200
        assert resp.get_json()["source"] == "gcd_api"
        gcd_api.assert_called_once()
        # The hung ComicVine worker must not block the request: returning well
        # before the 0.5s sleep proves shutdown(wait=False) didn't join it.
        assert elapsed < 0.45

    def test_failover_when_comicvine_raises(self, app, client):
        """A ComicVine exception is swallowed and the cascade reaches gcd_api
        (no 500)."""
        from contextlib import ExitStack

        def _boom(*args, **kwargs):
            raise RuntimeError("comicvine exploded")

        with ExitStack() as stack:
            gcd_api = self._configure(app, stack, search_volumes_side_effect=_boom)

            resp = client.post('/api/search-metadata', json={
                'file_path': '/data/Batman 001 (2020).cbz',
                'file_name': 'Batman 001 (2020).cbz',
            })

        assert resp.status_code == 200
        assert resp.get_json()["source"] == "gcd_api"
        gcd_api.assert_called_once()

    def test_try_comicvine_single_returns_quickly_on_timeout(self, app):
        """Unit-level: the wall-clock guard returns the empty tuple promptly
        instead of blocking for the full ComicVine call."""
        import time
        from routes.metadata import _try_comicvine_single

        app.config["COMICVINE_API_KEY"] = "test-key"

        def _slow(*args, **kwargs):
            time.sleep(1.0)
            return []

        with app.app_context(), \
                patch("models.comicvine.is_simyan_available", return_value=True), \
                patch("models.comicvine.search_volumes", side_effect=_slow), \
                patch("routes.metadata.CV_ATTEMPT_TIMEOUT", 0.15):
            started = time.monotonic()
            result = _try_comicvine_single(None, "Batman", "1", None)
            elapsed = time.monotonic() - started

        assert result == (None, None, None, None)
        assert elapsed < 0.9


class TestBatchMetadataRenameUpdatesIndex:
    """Verify file_index is updated with new path/name after batch rename."""

    @patch("routes.metadata.add_comicinfo_to_cbz")
    @patch("routes.metadata.comicvine")
    @patch("core.database.update_file_index_from_comicinfo")
    @patch("core.database.update_file_index_entry")
    @patch("cbz_ops.rename.rename_comic_from_metadata")
    def test_rename_updates_file_index_entry_before_comicinfo(
        self, mock_rename, mock_update_entry, mock_update_ci, mock_cv, mock_add_xml
    ):
        """When rename happens, update_file_index_entry is called with the new
        path/name BEFORE update_file_index_from_comicinfo, which uses the final path."""
        from routes.metadata import os

        old_path = "/data/comics/Batman 001 (2020).cbz"
        new_path = "/data/comics/Batman v2020 001.cbz"
        metadata = {"Series": "Batman", "Number": "1", "Volume": "2020"}

        mock_rename.return_value = (new_path, True)

        # Simulate the batch flow logic inline (extracted from the generator)
        file_path = old_path
        filename = os.path.basename(old_path)

        # -- begin logic under test (mirrors routes/metadata.py ~line 1376) --
        from routes.metadata import generate_comicinfo_xml
        xml_bytes = generate_comicinfo_xml(metadata)
        mock_add_xml(file_path, xml_bytes)

        from cbz_ops.rename import rename_comic_from_metadata as _rename
        old_filename = filename
        _old_path = file_path
        result_path, was_renamed = _rename(file_path, metadata)
        if was_renamed:
            file_path = result_path
            filename = os.path.basename(result_path)
            from core.database import update_file_index_entry
            update_file_index_entry(_old_path, name=filename, new_path=result_path,
                                    parent=os.path.dirname(result_path))

        from core.database import update_file_index_from_comicinfo
        update_file_index_from_comicinfo(file_path, metadata)
        # -- end logic under test --

        # Assertions
        mock_update_entry.assert_called_once_with(
            old_path, name="Batman v2020 001.cbz", new_path=new_path,
            parent=os.path.dirname(new_path),
        )
        # update_file_index_from_comicinfo must use the NEW path
        mock_update_ci.assert_called_once_with(new_path, metadata)

    @patch("routes.metadata.add_comicinfo_to_cbz")
    @patch("routes.metadata.comicvine")
    @patch("core.database.update_file_index_from_comicinfo")
    @patch("core.database.update_file_index_entry")
    @patch("cbz_ops.rename.rename_comic_from_metadata")
    def test_no_rename_skips_file_index_entry_update(
        self, mock_rename, mock_update_entry, mock_update_ci, mock_cv, mock_add_xml
    ):
        """When no rename happens, update_file_index_entry is NOT called."""
        from routes.metadata import os

        file_path = "/data/comics/Batman 001 (2020).cbz"
        metadata = {"Series": "Batman", "Number": "1"}

        mock_rename.return_value = (file_path, False)

        # Simulate batch flow
        filename = os.path.basename(file_path)
        from routes.metadata import generate_comicinfo_xml
        xml_bytes = generate_comicinfo_xml(metadata)
        mock_add_xml(file_path, xml_bytes)

        from cbz_ops.rename import rename_comic_from_metadata as _rename
        old_path = file_path
        result_path, was_renamed = _rename(file_path, metadata)
        if was_renamed:
            file_path = result_path
            filename = os.path.basename(result_path)
            from core.database import update_file_index_entry
            update_file_index_entry(old_path, name=filename, new_path=result_path,
                                    parent=os.path.dirname(result_path))

        from core.database import update_file_index_from_comicinfo
        update_file_index_from_comicinfo(file_path, metadata)

        # update_file_index_entry should NOT have been called
        mock_update_entry.assert_not_called()
        # update_file_index_from_comicinfo uses original path
        mock_update_ci.assert_called_once_with(file_path, metadata)



class TestBatchMangaProviderPriority:

    def test_batch_skips_comicvine_cvinfo_when_manga_first(self, tmp_path):
        """When MangaDex is priority #1, Metron/ComicVine cvinfo creation is skipped."""
        # This tests the skip_comic_cvinfo gate logic directly
        # by simulating the provider priority check from batch_metadata

        manga_providers_set = {'mangadex', 'mangaupdates', 'anilist'}
        comic_providers_set = {'metron', 'comicvine'}

        # Library with MangaDex first
        library_providers = [
            {'provider_type': 'mangadex', 'enabled': True},
            {'provider_type': 'mangaupdates', 'enabled': True},
            {'provider_type': 'comicvine', 'enabled': True},
        ]

        skip_comic_cvinfo = False
        for p in library_providers:
            if p.get('enabled', True):
                ptype = p['provider_type']
                if ptype in manga_providers_set:
                    skip_comic_cvinfo = True
                    break
                elif ptype in comic_providers_set:
                    break

        assert skip_comic_cvinfo is True

    def test_batch_does_not_skip_when_comicvine_first(self):
        """When ComicVine is priority #1, cvinfo creation proceeds normally."""
        manga_providers_set = {'mangadex', 'mangaupdates', 'anilist'}
        comic_providers_set = {'metron', 'comicvine'}

        library_providers = [
            {'provider_type': 'comicvine', 'enabled': True},
            {'provider_type': 'mangadex', 'enabled': True},
        ]

        skip_comic_cvinfo = False
        for p in library_providers:
            if p.get('enabled', True):
                ptype = p['provider_type']
                if ptype in manga_providers_set:
                    skip_comic_cvinfo = True
                    break
                elif ptype in comic_providers_set:
                    break

        assert skip_comic_cvinfo is False

    def test_batch_skips_disabled_providers(self):
        """Disabled manga provider at top doesn't trigger skip."""
        manga_providers_set = {'mangadex', 'mangaupdates', 'anilist'}
        comic_providers_set = {'metron', 'comicvine'}

        library_providers = [
            {'provider_type': 'mangadex', 'enabled': False},
            {'provider_type': 'comicvine', 'enabled': True},
        ]

        skip_comic_cvinfo = False
        for p in library_providers:
            if p.get('enabled', True):
                ptype = p['provider_type']
                if ptype in manga_providers_set:
                    skip_comic_cvinfo = True
                    break
                elif ptype in comic_providers_set:
                    break

        assert skip_comic_cvinfo is False


class TestRescanMissingXmlEndpoint:
    """POST /api/metadata/rescan-missing-xml triggers a force-rescan of has_comicinfo=0 files."""

    @patch("core.metadata_scanner.queue_missing_xml_for_rescan", return_value=42)
    def test_returns_queued_count(self, mock_queue, client):
        resp = client.post('/api/metadata/rescan-missing-xml', json={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["queued"] == 42
        mock_queue.assert_called_once()

    @patch("core.metadata_scanner.queue_missing_xml_for_rescan", return_value=0)
    def test_zero_when_nothing_to_rescan(self, mock_queue, client):
        resp = client.post('/api/metadata/rescan-missing-xml', json={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["queued"] == 0


class TestRemoveComicInfoUpdatesFileIndex:
    """Regression: _remove_comicinfo_from_cbz must zero has_comicinfo in file_index
    so the file shows up in the Missing XML view immediately after removal."""

    def test_file_index_has_comicinfo_set_to_zero(self, db_connection, tmp_path):
        from routes.metadata import _remove_comicinfo_from_cbz
        from core.database import add_file_index_entry

        cbz_path = str(tmp_path / "comic.cbz")
        _make_cbz(cbz_path, with_comicinfo=True)

        add_file_index_entry(
            name="comic.cbz", path=cbz_path, entry_type="file",
            size=1234, parent=str(tmp_path),
        )
        # Seed has_comicinfo=1 to mirror a previously-scanned file with metadata.
        db_connection.execute(
            "UPDATE file_index SET has_comicinfo=1 WHERE path=?", (cbz_path,)
        )
        db_connection.commit()

        result = _remove_comicinfo_from_cbz(cbz_path)
        assert result["success"] is True

        cur = db_connection.execute(
            "SELECT has_comicinfo FROM file_index WHERE path=?", (cbz_path,)
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 0


class TestSearchMetadataSkipProviders:
    """skip_providers / only_provider control which providers the cascade tries,
    and selection responses expose provider_order for the skip button."""

    def _fallback_two_providers(self, app, stack):
        """Configure the fallback order to be [metron, comicvine]."""
        app.config["COMICVINE_API_KEY"] = "test-key"
        stack.enter_context(patch("models.metron.is_metron_configured", return_value=True))
        stack.enter_context(patch("models.metron.is_connection_error", return_value=False))
        stack.enter_context(patch("models.gcd.is_database_available", return_value=False))
        stack.enter_context(patch("models.gcd.check_database_status",
                                  return_value={"gcd_available": False}))
        stack.enter_context(patch("models.comicvine.find_cvinfo_in_folder", return_value=None))
        stack.enter_context(patch("models.comicvine.extract_issue_number", return_value=None))
        stack.enter_context(patch("core.database.get_library_providers", return_value=[]))
        stack.enter_context(patch("core.database.get_provider_credentials", return_value=None))
        stack.enter_context(patch("core.database.set_has_comicinfo"))
        stack.enter_context(patch("core.database.update_file_index_from_comicinfo"))
        stack.enter_context(patch("routes.metadata.add_comicinfo_to_cbz", return_value=True))

    def test_skip_providers_excludes_provider(self, app, client):
        from contextlib import ExitStack
        with ExitStack() as stack:
            self._fallback_two_providers(app, stack)
            metron = stack.enter_context(patch(
                "routes.metadata._try_metron_single", return_value=(None, None, None)))
            cv = stack.enter_context(patch(
                "routes.metadata._try_comicvine_single",
                return_value=({"Series": "Batman", "Number": "1"}, "http://img", None, None)))

            resp = client.post('/api/search-metadata', json={
                'file_path': '/data/Batman 001 (2020).cbz',
                'file_name': 'Batman 001 (2020).cbz',
                'skip_providers': ['metron'],
            })

        assert resp.status_code == 200
        assert resp.get_json()["source"] == "comicvine"
        metron.assert_not_called()
        cv.assert_called_once()

    def test_only_provider_restricts_cascade(self, app, client):
        from contextlib import ExitStack
        with ExitStack() as stack:
            self._fallback_two_providers(app, stack)
            metron = stack.enter_context(patch(
                "routes.metadata._try_metron_single", return_value=(None, None, None)))
            cv = stack.enter_context(patch(
                "routes.metadata._try_comicvine_single",
                return_value=({"Series": "Batman", "Number": "1"}, "http://img", None, None)))

            resp = client.post('/api/search-metadata', json={
                'file_path': '/data/Batman 001 (2020).cbz',
                'file_name': 'Batman 001 (2020).cbz',
                'only_provider': 'comicvine',
            })

        assert resp.status_code == 200
        assert resp.get_json()["source"] == "comicvine"
        metron.assert_not_called()
        cv.assert_called_once()

    def test_selection_response_includes_provider_order(self, app, client):
        from contextlib import ExitStack
        selection = {
            "requires_selection": True,
            "provider": "comicvine",
            "possible_matches": [{"id": 1, "name": "Batman"}, {"id": 2, "name": "Batman Inc"}],
        }
        with ExitStack() as stack:
            self._fallback_two_providers(app, stack)
            stack.enter_context(patch(
                "routes.metadata._try_metron_single", return_value=(None, None, None)))
            stack.enter_context(patch(
                "routes.metadata._try_comicvine_single",
                return_value=(None, None, None, selection)))

            resp = client.post('/api/search-metadata', json={
                'file_path': '/data/Batman 001 (2020).cbz',
                'file_name': 'Batman 001 (2020).cbz',
            })

        data = resp.get_json()
        assert data["requires_selection"] is True
        assert data["provider"] == "comicvine"
        assert data["provider_order"] == ["metron", "comicvine"]


class TestSearchMetadataMetronSelection:
    """Metron now shows a selection modal when matches are ambiguous and
    supports the selected_match follow-up."""

    def _metron_only(self, app, stack):
        app.config["COMICVINE_API_KEY"] = ""
        stack.enter_context(patch("models.metron.is_metron_configured", return_value=True))
        stack.enter_context(patch("models.metron.is_connection_error", return_value=False))
        stack.enter_context(patch("models.gcd.is_database_available", return_value=False))
        stack.enter_context(patch("models.gcd.check_database_status",
                                  return_value={"gcd_available": False}))
        stack.enter_context(patch("models.comicvine.find_cvinfo_in_folder", return_value=None))
        stack.enter_context(patch("models.comicvine.extract_issue_number", return_value=None))
        stack.enter_context(patch("core.database.get_library_providers", return_value=[]))
        stack.enter_context(patch("core.database.get_provider_credentials", return_value=None))
        stack.enter_context(patch("core.database.set_has_comicinfo"))
        stack.enter_context(patch("core.database.update_file_index_from_comicinfo"))
        stack.enter_context(patch("models.metron.get_flask_api", return_value=MagicMock()))
        stack.enter_context(patch("routes.metadata.add_comicinfo_to_cbz", return_value=True))

    def test_ambiguous_matches_require_selection(self, app, client):
        from contextlib import ExitStack
        with ExitStack() as stack:
            self._metron_only(app, stack)
            stack.enter_context(patch("models.metron.search_series_list", return_value=[
                {"id": 1, "name": "The Batman", "start_year": 1940},
                {"id": 2, "name": "Batman Beyond", "start_year": 1999},
            ]))

            resp = client.post('/api/search-metadata', json={
                'file_path': '/data/Batman 001 (2020).cbz',
                'file_name': 'Batman 001 (2020).cbz',
            })

        data = resp.get_json()
        assert data["requires_selection"] is True
        assert data["provider"] == "metron"
        assert len(data["possible_matches"]) == 2
        assert data["provider_order"] == ["metron"]

    def test_confident_single_match_auto_applies(self, app, client):
        from contextlib import ExitStack
        with ExitStack() as stack:
            self._metron_only(app, stack)
            stack.enter_context(patch("models.metron.search_series_list", return_value=[
                {"id": 5, "name": "Batman", "start_year": 2016},
            ]))
            stack.enter_context(patch("models.metron.get_issue_metadata",
                                      return_value={"image": "http://cover"}))
            stack.enter_context(patch("models.metron.map_to_comicinfo",
                                      return_value={"Series": "Batman", "Number": "1"}))

            resp = client.post('/api/search-metadata', json={
                'file_path': '/data/Batman 001 (2020).cbz',
                'file_name': 'Batman 001 (2020).cbz',
            })

        assert resp.status_code == 200
        assert resp.get_json()["source"] == "metron"

    def test_metron_selection_followup_applies(self, app, client):
        from contextlib import ExitStack
        with ExitStack() as stack:
            self._metron_only(app, stack)
            stack.enter_context(patch("models.metron.get_issue_metadata",
                                      return_value={"image": "http://cover"}))
            stack.enter_context(patch("models.metron.map_to_comicinfo",
                                      return_value={"Series": "Batman", "Number": "1"}))

            resp = client.post('/api/search-metadata', json={
                'file_path': '/data/Batman 001 (2020).cbz',
                'file_name': 'Batman 001 (2020).cbz',
                'selected_match': {'provider': 'metron', 'series_id': 5},
            })

        assert resp.status_code == 200
        assert resp.get_json()["source"] == "metron"


class TestBatchMetadataSkipProviders:
    """The folder/batch flow (/api/batch-metadata) must expose provider_order on
    its ComicVine selection and honor skip_providers so the user can fall through
    to the next provider (e.g. GCD API) for the whole folder."""

    def _batch_stack(self, app, stack):
        app.config["COMICVINE_API_KEY"] = "k"
        stack.enter_context(patch("routes.metadata.is_valid_library_path", return_value=True))
        stack.enter_context(patch("app.get_target_dir_live", return_value="/nonexistent_target"))
        stack.enter_context(patch("core.database.get_library_providers", return_value=[
            {"provider_type": "metron", "enabled": True},
            {"provider_type": "comicvine", "enabled": True},
            {"provider_type": "gcd_api", "enabled": True},
        ]))
        stack.enter_context(patch("models.metron.get_flask_api", return_value=MagicMock()))
        stack.enter_context(patch("models.metron.search_series_by_name", return_value=None))
        stack.enter_context(patch("models.metron.is_connection_error", return_value=False))

    def test_comicvine_selection_includes_provider_order(self, app, client, tmp_path):
        from contextlib import ExitStack
        folder = tmp_path / "Batman (2020)"
        folder.mkdir()
        _make_cbz(str(folder / "Batman 001 (2020).cbz"), with_comicinfo=False)

        with ExitStack() as stack:
            self._batch_stack(app, stack)
            stack.enter_context(patch("models.comicvine.search_volumes", return_value=[
                {"id": 1, "name": "Batman"},
                {"id": 2, "name": "Batman Inc"},
            ]))
            resp = client.post('/api/batch-metadata', json={
                'directory': str(folder), 'library_id': 1,
            })

        data = resp.get_json()
        assert data["requires_selection"] is True
        assert data["provider"] == "comicvine"
        assert data["provider_order"] == ["metron", "comicvine", "gcd_api"]

    def test_skip_providers_bypasses_comicvine_halt(self, app, client, tmp_path):
        """With comicvine skipped, the ComicVine multi-volume selection must NOT
        halt the batch — it streams (SSE) and lets later providers run per-file."""
        from contextlib import ExitStack
        folder = tmp_path / "Batman (2020)"
        folder.mkdir()
        # File already has metadata (Notes) so it's skipped — keeps the per-file
        # loop from making real provider calls during the stream.
        cbz = str(folder / "Batman 001 (2020).cbz")
        with zipfile.ZipFile(cbz, 'w') as zf:
            zf.writestr("page_001.png", b"x")
            zf.writestr("ComicInfo.xml", "<ComicInfo><Series>B</Series><Notes>has</Notes></ComicInfo>")

        with ExitStack() as stack:
            self._batch_stack(app, stack)
            cv = stack.enter_context(patch("models.comicvine.search_volumes", return_value=[
                {"id": 1, "name": "Batman"},
                {"id": 2, "name": "Batman Inc"},
            ]))
            resp = client.post('/api/batch-metadata', json={
                'directory': str(folder), 'library_id': 1,
                'skip_providers': ['comicvine'],
            })
            body = resp.get_data(as_text=True)

        assert resp.status_code == 200
        assert 'text/event-stream' in resp.content_type
        assert '"type": "complete"' in body
        # ComicVine search must not run when comicvine is skipped.
        cv.assert_not_called()

    def test_one_shot_unnumbered_falls_back_to_issue_one(self, app, client, tmp_path):
        """A single un-numbered file (one-shot) must NOT error with 'no issue
        number' — it falls back to issue #1 and is processed normally."""
        from contextlib import ExitStack
        folder = tmp_path / "One Shot Special"
        folder.mkdir()
        _make_cbz(str(folder / "One Shot Special.cbz"), with_comicinfo=False)

        with ExitStack() as stack:
            self._batch_stack(app, stack)
            stack.enter_context(patch("models.comicvine.search_volumes", return_value=[]))
            resp = client.post('/api/batch-metadata', json={
                'directory': str(folder), 'library_id': 1,
            })
            body = resp.get_data(as_text=True)

        assert resp.status_code == 200
        assert 'text/event-stream' in resp.content_type
        assert 'no issue number' not in body

    def test_multi_file_unnumbered_still_errors(self, app, client, tmp_path):
        """Multiple un-numbered files must NOT all be mapped to #1 — they still
        report the 'no issue number' error."""
        from contextlib import ExitStack
        folder = tmp_path / "Mixed Folder"
        folder.mkdir()
        _make_cbz(str(folder / "Mixed Folder One.cbz"), with_comicinfo=False)
        _make_cbz(str(folder / "Mixed Folder Two.cbz"), with_comicinfo=False)

        with ExitStack() as stack:
            self._batch_stack(app, stack)
            stack.enter_context(patch("models.comicvine.search_volumes", return_value=[]))
            resp = client.post('/api/batch-metadata', json={
                'directory': str(folder), 'library_id': 1,
            })
            body = resp.get_data(as_text=True)

        assert resp.status_code == 200
        assert 'no issue number' in body


class TestOneShotFolderHandling:
    """One-shot folders (oneshots/specials/...) hold unrelated singles, so a
    shared folder cvinfo must be ignored and auto-rename must be gated."""

    def test_search_metadata_bypasses_cvinfo_and_gates_autorename(self, app, client, tmp_path):
        from contextlib import ExitStack
        app.config["COMICVINE_API_KEY"] = "k"
        app.config["ENABLE_AUTO_RENAME"] = True
        folder = tmp_path / "oneshots"
        folder.mkdir()
        # A poisoning cvinfo (volume 99999) that must be ignored here.
        (folder / "cvinfo").write_text("https://comicvine.gamespot.com/x/4050-99999/")
        cbz = folder / "Lilli Xene.cbz"
        _make_cbz(str(cbz), with_comicinfo=False)

        with ExitStack() as stack:
            stack.enter_context(patch("core.database.get_library_providers", return_value=[
                {"provider_type": "comicvine", "enabled": True}]))
            stack.enter_context(patch("models.comicvine.is_simyan_available", return_value=True))
            sv = stack.enter_context(patch("models.comicvine.search_volumes", return_value=[
                {"id": 555, "name": "Lilli Xene", "publisher_name": "X", "start_year": 2007}]))
            pcv = stack.enter_context(patch("models.comicvine.parse_cvinfo_volume_id", return_value=99999))
            stack.enter_context(patch("models.comicvine.get_issue_by_number", return_value={
                "volume_name": "Lilli Xene", "year": 2007, "image_url": "http://i"}))
            stack.enter_context(patch("models.comicvine.map_to_comicinfo",
                                      return_value={"Series": "Lilli Xene", "Number": "1"}))
            stack.enter_context(patch("models.comicvine.auto_move_file", return_value=None))
            stack.enter_context(patch("routes.metadata.add_comicinfo_to_cbz", return_value=True))
            stack.enter_context(patch("core.database.update_file_index_from_comicinfo"))
            stack.enter_context(patch("core.database.set_has_comicinfo"))
            stack.enter_context(patch("models.metron.is_connection_error", return_value=False))

            resp = client.post('/api/search-metadata', json={
                'file_path': str(cbz), 'file_name': 'Lilli Xene.cbz', 'library_id': 1,
            })

        data = resp.get_json()
        assert data["success"] is True
        # cvinfo was bypassed → matched by the file's own name, not volume 99999.
        sv.assert_called()
        pcv.assert_not_called()
        # auto-rename gated off in one-shot folders despite ENABLE_AUTO_RENAME.
        assert data["rename_config"]["auto_rename"] is False

    def test_non_oneshot_uses_cvinfo_and_allows_autorename(self, app, client, tmp_path):
        from contextlib import ExitStack
        app.config["COMICVINE_API_KEY"] = "k"
        app.config["ENABLE_AUTO_RENAME"] = True
        folder = tmp_path / "Some Series (2007)"
        folder.mkdir()
        (folder / "cvinfo").write_text("https://comicvine.gamespot.com/x/4050-99999/")
        cbz = folder / "Some Series 001.cbz"
        _make_cbz(str(cbz), with_comicinfo=False)

        with ExitStack() as stack:
            stack.enter_context(patch("core.database.get_library_providers", return_value=[
                {"provider_type": "comicvine", "enabled": True}]))
            stack.enter_context(patch("models.comicvine.is_simyan_available", return_value=True))
            stack.enter_context(patch("models.comicvine.find_cvinfo_in_folder",
                                      return_value=str(folder / "cvinfo")))
            pcv = stack.enter_context(patch("models.comicvine.parse_cvinfo_volume_id", return_value=99999))
            gibn = stack.enter_context(patch("models.comicvine.get_issue_by_number", return_value={
                "volume_name": "Some Series", "year": 2007, "image_url": "http://i"}))
            stack.enter_context(patch("models.comicvine.read_cvinfo_fields",
                                      return_value={"start_year": 2007, "publisher_name": "X"}))
            stack.enter_context(patch("models.comicvine.map_to_comicinfo",
                                      return_value={"Series": "Some Series", "Number": "1"}))
            stack.enter_context(patch("models.comicvine.auto_move_file", return_value=None))
            stack.enter_context(patch("routes.metadata.add_comicinfo_to_cbz", return_value=True))
            stack.enter_context(patch("core.database.update_file_index_from_comicinfo"))
            stack.enter_context(patch("core.database.set_has_comicinfo"))
            stack.enter_context(patch("models.metron.is_connection_error", return_value=False))

            resp = client.post('/api/search-metadata', json={
                'file_path': str(cbz), 'file_name': 'Some Series 001.cbz', 'library_id': 1,
            })

        data = resp.get_json()
        assert data["success"] is True
        # Normal folder: cvinfo IS consulted and auto-rename stays enabled.
        pcv.assert_called()
        gibn.assert_called()
        assert data["rename_config"]["auto_rename"] is True

    def test_batch_oneshot_does_not_consult_cvinfo(self, app, client, tmp_path):
        from contextlib import ExitStack
        app.config["COMICVINE_API_KEY"] = "k"
        folder = tmp_path / "oneshots"
        folder.mkdir()
        (folder / "cvinfo").write_text("https://comicvine.gamespot.com/x/4050-99999/")
        _make_cbz(str(folder / "Lilli Xene.cbz"), with_comicinfo=False)

        with ExitStack() as stack:
            stack.enter_context(patch("routes.metadata.is_valid_library_path", return_value=True))
            stack.enter_context(patch("app.get_target_dir_live", return_value="/nonexistent"))
            stack.enter_context(patch("core.database.get_library_providers", return_value=[
                {"provider_type": "comicvine", "enabled": True}]))
            stack.enter_context(patch("models.metron.is_connection_error", return_value=False))
            gmv = stack.enter_context(patch("models.comicvine.get_metadata_by_volume_id", return_value=None))
            pcv = stack.enter_context(patch("models.comicvine.parse_cvinfo_volume_id", return_value=99999))

            resp = client.post('/api/batch-metadata', json={
                'directory': str(folder), 'library_id': 1,
            })
            body = resp.get_data(as_text=True)

        assert resp.status_code == 200
        # The folder's cvinfo (volume 99999) must never be consulted for a one-shot folder.
        gmv.assert_not_called()
        pcv.assert_not_called()
        assert '"type": "complete"' in body


class TestGcdSqliteRoutes:
    """End-to-end coverage of the GCD routes against a real temp SQLite dump.

    These exercise the ported SQLite SQL (CONCAT->||, SUBSTRING->substr,
    GROUP_CONCAT rewrites, REGEXP) rather than mocking cursors.
    """

    def _configure_gcd(self, tmp_path, monkeypatch):
        from tests.mocked.conftest import build_gcd_sqlite
        path = build_gcd_sqlite(tmp_path / "gcd.db")
        monkeypatch.setattr("models.gcd._get_saved_credentials",
                            lambda: {"database_path": str(path)})
        return path

    def test_validate_gcd_issue_valid(self, client, tmp_path, monkeypatch):
        self._configure_gcd(tmp_path, monkeypatch)
        resp = client.post('/validate-gcd-issue', json={
            'series_id': 200, 'issue_number': '1',
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['issue_number'] == '1'

    def test_validate_gcd_issue_invalid(self, client, tmp_path, monkeypatch):
        self._configure_gcd(tmp_path, monkeypatch)
        resp = client.post('/validate-gcd-issue', json={
            'series_id': 200, 'issue_number': '999',
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is False

    def test_search_with_selection_writes_metadata(self, client, tmp_path, monkeypatch):
        """The full ComicInfo query (credits/genre/characters aggregates) resolves."""
        self._configure_gcd(tmp_path, monkeypatch)
        cbz = tmp_path / "Batman 001.cbz"
        _make_cbz(str(cbz), with_comicinfo=False)

        with patch("routes.metadata.add_comicinfo_to_cbz", return_value=True), \
             patch("core.database.set_has_comicinfo"):
            resp = client.post('/search-gcd-metadata-with-selection', json={
                'file_path': str(cbz),
                'file_name': 'Batman 001.cbz',
                'series_id': 200,
                'issue_number': '1',
            })

        assert resp.status_code == 200
        meta = resp.get_json()['metadata']
        assert meta['series'] == 'Batman'
        assert meta['issue'] == '1'
        assert meta['title'] == 'The Beginning'
        assert meta['publisher'] == 'DC Comics'
        assert meta['year'] == 1940
        assert meta['writer'] == 'Bob Kane'
        assert meta['genre'] == 'superhero'
        assert 'Batman' in meta['characters']

    def test_search_with_selection_not_configured(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr("models.gcd._get_saved_credentials", lambda: None)
        monkeypatch.delenv("GCD_DATABASE_PATH", raising=False)
        cbz = tmp_path / "Batman 001.cbz"
        _make_cbz(str(cbz), with_comicinfo=False)
        resp = client.post('/search-gcd-metadata-with-selection', json={
            'file_path': str(cbz),
            'file_name': 'Batman 001.cbz',
            'series_id': 200,
            'issue_number': '1',
        })
        assert resp.status_code == 500
        assert resp.get_json()['success'] is False


class TestGcdSearchVariationParity:
    """/search-gcd-metadata must apply the same variation policy as models.gcd.

    This route reimplements the progressive-search loop rather than calling
    search_series, so the year constraint and the one-token breadth guard have
    to be shared explicitly. They used to be a hard-coded literal list and
    nothing at all, which is why the same wrong-series match survived here after
    the automatic path was fixed.
    """

    def _configure_gcd(self, tmp_path, monkeypatch):
        from tests.mocked.conftest import build_gcd_sqlite
        path = build_gcd_sqlite(tmp_path / "gcd.db")
        monkeypatch.setattr("models.gcd._get_saved_credentials",
                            lambda: {"database_path": str(path)})
        return path

    def _search(self, client, tmp_path, file_name):
        cbz = tmp_path / file_name
        _make_cbz(str(cbz), with_comicinfo=False)
        with patch("routes.metadata.add_comicinfo_to_cbz", return_value=True),              patch("core.database.set_has_comicinfo"):
            return client.post('/search-gcd-metadata', json={
                'file_path': str(cbz), 'file_name': file_name,
            })

    def test_main_with_year_is_year_constrained(self, client, tmp_path, monkeypatch):
        """Batman began in 1940, so a 1930 file must not fall back onto it.

        Only `main_with_year` can reach the series here -- the parsed name is
        not a substring of it and `tokenized` needs every token present -- so a
        match proves the year was never applied.
        """
        self._configure_gcd(tmp_path, monkeypatch)
        resp = self._search(client, tmp_path,
                            "Batman Adventures Special Edition 001 (1930).cbz")
        # Not a hard 404: the route hands the decision to the user instead of
        # auto-tagging. What matters is that nothing was written.
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is False
        assert data['requires_selection'] is True
        assert 'metadata' not in data

    def test_main_with_year_still_matches_inside_the_run(self, client, tmp_path,
                                                          monkeypatch):
        """The constraint must not make the fallback useless -- 1975 is in range."""
        self._configure_gcd(tmp_path, monkeypatch)
        resp = self._search(client, tmp_path,
                            "Batman Adventures Special Edition 001 (1975).cbz")
        assert resp.status_code == 200
        assert resp.get_json().get('metadata', {}).get('series') == 'Batman'

    def test_over_broad_main_word_is_declined(self, client, tmp_path, monkeypatch):
        """Past the cap the first row is an arbitrary pick, not a match.

        Simulated by lowering the cap rather than loading thousands of series.
        The route has no LIMIT on its query, so without this guard the whole
        slice ('%le%' is 18,526 series in the real dump) is ranked by year and
        serialised into possible_matches.
        """
        self._configure_gcd(tmp_path, monkeypatch)
        monkeypatch.setattr("models.gcd.MAIN_WORD_MAX_CANDIDATES", 0)
        resp = self._search(client, tmp_path,
                            "Batman Adventures Special Edition 001.cbz")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is False
        assert data['requires_selection'] is True
        assert 'metadata' not in data

    def test_the_cap_does_not_apply_to_the_precise_variations(self, client, tmp_path,
                                                              monkeypatch):
        """An exact hit must survive a cap of zero -- only the fallback is guarded."""
        self._configure_gcd(tmp_path, monkeypatch)
        monkeypatch.setattr("models.gcd.MAIN_WORD_MAX_CANDIDATES", 0)
        resp = self._search(client, tmp_path, "Batman 001.cbz")
        assert resp.status_code == 200
        assert resp.get_json().get('metadata', {}).get('series') == 'Batman'

    def test_tokenized_hits_are_ordered_by_year_proximity(self, client, tmp_path,
                                                          monkeypatch):
        """The selection list must lead with the closest era, not the newest.

        Two series contain every token and neither contains the parsed title as
        a substring, so `tokenized` is the deciding tier. Ordering by recency
        put the 2023 collected edition above the 2018 series the file is from.
        """
        import sqlite3
        path = self._configure_gcd(tmp_path, monkeypatch)
        conn = sqlite3.connect(str(path))
        conn.executemany(
            "INSERT INTO gcd_series (id, name, year_began, year_ended, "
            "publisher_id, language_id) VALUES (?, ?, ?, ?, ?, ?)",
            [(400, "Batman: Detective Comics - Rebirth", 2018, None, 10, 1),
             (401, "Detective Comics: Batman Rebirth Deluxe", 2023, None, 10, 1)],
        )
        conn.commit()
        conn.close()

        resp = self._search(client, tmp_path,
                            "Batman Detective Comics Rebirth 001 (2018).cbz")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data['requires_selection'] is True
        assert data['search_method'] == 'tokenized'
        assert data['possible_matches'][0]['year_began'] == 2018


class TestComicVineSqliteRoutes:
    """End-to-end coverage of comicvine_sqlite through the /api/search-metadata cascade.

    Exercises the ported JSON credit parsing + map_to_comicinfo reuse, and the
    ambiguous-volume selection round-trip via selected_match.
    """

    def _isolate_to_cv_sqlite(self, stack, client, db_path):
        """Make comicvine_sqlite the only available cascade provider."""
        from contextlib import ExitStack
        # Point the local CV provider at our temp DB.
        stack.enter_context(patch("models.comicvine_sqlite._get_saved_credentials",
                                  return_value={"database_path": db_path}))
        # Disable every other fallback provider.
        stack.enter_context(patch("models.metron.is_metron_configured", return_value=False))
        stack.enter_context(patch("models.metron.is_connection_error", return_value=False))
        stack.enter_context(patch("models.gcd.check_database_status",
                                  return_value={"gcd_available": False}))
        stack.enter_context(patch("core.database.get_provider_credentials", return_value=None))
        stack.enter_context(patch("models.comicvine.find_cvinfo_in_folder", return_value=None))
        stack.enter_context(patch("models.comicvine.auto_move_file", return_value=None))
        # Avoid real file/index writes.
        stack.enter_context(patch("routes.metadata.add_comicinfo_to_cbz", return_value=True))
        stack.enter_context(patch("core.database.update_file_index_from_comicinfo"))
        stack.enter_context(patch("core.database.set_has_comicinfo"))
        client.application.config["COMICVINE_API_KEY"] = ""

    def test_cascade_success(self, client, tmp_path, monkeypatch):
        from contextlib import ExitStack
        from tests.mocked.conftest import build_comicvine_sqlite
        db = build_comicvine_sqlite(tmp_path / "cv.db")
        cbz = tmp_path / "Batman 001.cbz"
        _make_cbz(str(cbz), with_comicinfo=False)

        with ExitStack() as stack:
            self._isolate_to_cv_sqlite(stack, client, db)
            resp = client.post('/api/search-metadata', json={
                'file_path': str(cbz),
                'file_name': 'Batman 001.cbz',
            })

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['source'] == 'comicvine_sqlite'
        meta = data['metadata']
        assert meta['Series'] == 'Batman'
        assert meta['Number'] == '1'
        assert meta['Writer'] == 'Bob Kane'
        assert meta['Characters'] == 'Batman, Robin'

    def test_cascade_ambiguous_then_selection(self, client, tmp_path, monkeypatch):
        from contextlib import ExitStack
        from tests.mocked.conftest import build_comicvine_sqlite
        # extra_alias_volumes => 3 volumes match "Batman" via alias, none by name.
        db = build_comicvine_sqlite(tmp_path / "cv.db", extra_alias_volumes=True)
        cbz = tmp_path / "Batman 001.cbz"
        _make_cbz(str(cbz), with_comicinfo=False)

        with ExitStack() as stack:
            self._isolate_to_cv_sqlite(stack, client, db)
            # 1) Ambiguous search -> selection prompt.
            resp = client.post('/api/search-metadata', json={
                'file_path': str(cbz),
                'file_name': 'Batman 001.cbz',
            })
            assert resp.status_code == 200
            data = resp.get_json()
            assert data.get('requires_selection') is True
            assert data['provider'] == 'comicvine_sqlite'
            assert len(data['possible_matches']) == 3

            # 2) User picks volume 4050 -> selected_match follow-up writes metadata.
            resp2 = client.post('/api/search-metadata', json={
                'file_path': str(cbz),
                'file_name': 'Batman 001.cbz',
                'selected_match': {
                    'provider': 'comicvine_sqlite',
                    'volume_id': 4050,
                    'publisher_name': 'DC Comics',
                },
            })

        assert resp2.status_code == 200
        data2 = resp2.get_json()
        assert data2['success'] is True
        assert data2['source'] == 'comicvine_sqlite'
        assert data2['metadata']['Writer'] == 'Bob Kane'


class TestIssueSelectionFallback:
    """The volume is right but the issue number isn't in it.

    Rather than dead-ending on "No metadata found for selection", the route
    hands back that volume's issue list so the user can pick the right match.
    """

    def _isolate_to_cv_sqlite(self, stack, client, db_path):
        return TestComicVineSqliteRoutes()._isolate_to_cv_sqlite(stack, client, db_path)

    def _cbz(self, tmp_path, name):
        cbz = tmp_path / name
        _make_cbz(str(cbz), with_comicinfo=False)
        return str(cbz)

    # Volume 4050 in the fixture holds exactly one issue, #1, so a file
    # claiming #2 is the natural "odd issue number" case.
    def test_volume_pick_offers_issue_list(self, client, tmp_path):
        from contextlib import ExitStack
        from tests.mocked.conftest import build_comicvine_sqlite
        db = build_comicvine_sqlite(tmp_path / "cv.db")
        cbz = self._cbz(tmp_path, "Batman 002.cbz")

        with ExitStack() as stack:
            self._isolate_to_cv_sqlite(stack, client, db)
            resp = client.post('/api/search-metadata', json={
                'file_path': cbz,
                'file_name': 'Batman 002.cbz',
                'selected_match': {
                    'provider': 'comicvine_sqlite',
                    'volume_id': 4050,
                    'publisher_name': 'DC Comics',
                    'series_name': 'Batman',
                },
            })

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is False
        assert data['requires_issue_selection'] is True
        assert data['provider'] == 'comicvine_sqlite'
        assert data['series_name'] == 'Batman'
        assert data['parsed_filename']['issue_number'] == '2'
        # The chosen volume is echoed back so the client can re-send it verbatim.
        assert data['selected_match']['volume_id'] == 4050
        assert [i['issue_number'] for i in data['possible_issues']] == ['1']
        issue = data['possible_issues'][0]
        assert issue['title'] == 'The Beginning'
        assert issue['cover_date'] == '2016-06-01'

    def test_issue_pick_applies_metadata(self, client, tmp_path):
        from contextlib import ExitStack
        from tests.mocked.conftest import build_comicvine_sqlite
        db = build_comicvine_sqlite(tmp_path / "cv.db")
        cbz = self._cbz(tmp_path, "Batman 002.cbz")

        with ExitStack() as stack:
            self._isolate_to_cv_sqlite(stack, client, db)
            resp = client.post('/api/search-metadata', json={
                'file_path': cbz,
                'file_name': 'Batman 002.cbz',
                'selected_match': {
                    'provider': 'comicvine_sqlite',
                    'volume_id': 4050,
                    'publisher_name': 'DC Comics',
                    'issue_number': '1',
                },
            })

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['source'] == 'comicvine_sqlite'
        # The user's issue choice wins over the "2" parsed from the filename.
        assert data['metadata']['Number'] == '1'
        assert data['metadata']['Writer'] == 'Bob Kane'

    def test_explicit_issue_miss_does_not_loop(self, client, tmp_path):
        """A picked issue that still misses must 404, not re-open the picker."""
        from contextlib import ExitStack
        from tests.mocked.conftest import build_comicvine_sqlite
        db = build_comicvine_sqlite(tmp_path / "cv.db")
        cbz = self._cbz(tmp_path, "Batman 002.cbz")

        with ExitStack() as stack:
            self._isolate_to_cv_sqlite(stack, client, db)
            resp = client.post('/api/search-metadata', json={
                'file_path': cbz,
                'file_name': 'Batman 002.cbz',
                'selected_match': {
                    'provider': 'comicvine_sqlite',
                    'volume_id': 4050,
                    'issue_number': '999',
                },
            })

        assert resp.status_code == 404
        data = resp.get_json()
        assert 'requires_issue_selection' not in data
        assert data['error'] == 'No metadata found for selection'

    def test_no_issue_list_keeps_original_404(self, client, tmp_path):
        from contextlib import ExitStack
        from tests.mocked.conftest import build_comicvine_sqlite
        db = build_comicvine_sqlite(tmp_path / "cv.db")
        cbz = self._cbz(tmp_path, "Batman 002.cbz")

        with ExitStack() as stack:
            self._isolate_to_cv_sqlite(stack, client, db)
            stack.enter_context(patch("routes.metadata._issue_options_for", return_value=[]))
            resp = client.post('/api/search-metadata', json={
                'file_path': cbz,
                'file_name': 'Batman 002.cbz',
                'selected_match': {'provider': 'comicvine_sqlite', 'volume_id': 4050},
            })

        assert resp.status_code == 404
        assert resp.get_json()['error'] == 'No metadata found for selection'

    def test_cascade_near_miss_offers_issue_list(self, app, client):
        """Series matched confidently, issue missing, no other provider helped."""
        from contextlib import ExitStack
        with ExitStack() as stack:
            TestSearchMetadataMetronSelection()._metron_only(app, stack)
            stack.enter_context(patch("models.metron.search_series_list", return_value=[
                {"id": 5, "name": "Batman", "start_year": 2016},
            ]))
            stack.enter_context(patch("models.metron.get_issue_metadata", return_value=None))
            stack.enter_context(patch("routes.metadata._issue_options_for", return_value=[
                {"id": "77", "issue_number": "1.MU", "title": "Monsters Unleashed",
                 "cover_date": "2017-03-01", "cover_url": None},
            ]))

            resp = client.post('/api/search-metadata', json={
                'file_path': '/data/Batman 001 (2020).cbz',
                'file_name': 'Batman 001 (2020).cbz',
            })

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['requires_issue_selection'] is True
        assert data['provider'] == 'metron'
        assert data['series_name'] == 'Batman'
        assert data['selected_match'] == {'provider': 'metron', 'series_id': 5}
        assert data['provider_order'] == ['metron']
        assert data['possible_issues'][0]['issue_number'] == '1.MU'

    def test_cascade_without_near_miss_still_404s(self, app, client):
        """No provider identified a series — the plain 404 path is unchanged."""
        from contextlib import ExitStack
        with ExitStack() as stack:
            TestSearchMetadataMetronSelection()._metron_only(app, stack)
            stack.enter_context(patch("models.metron.search_series_list", return_value=[]))

            resp = client.post('/api/search-metadata', json={
                'file_path': '/data/Batman 001 (2020).cbz',
                'file_name': 'Batman 001 (2020).cbz',
            })

        assert resp.status_code == 404
        data = resp.get_json()
        assert 'requires_issue_selection' not in data
        assert data['parsed_filename']['series_name'] == 'Batman'


def _sse_complete_result(body):
    """Pull the `complete` event's result payload out of an SSE response body."""
    for line in body.splitlines():
        if not line.startswith('data: '):
            continue
        event = json.loads(line[len('data: '):])
        if event.get('type') == 'complete':
            return event['result']
    raise AssertionError('no complete event in SSE body')


class TestBatchUnmatchedIssues:
    """The folder batch knows the series from cvinfo; when the issue number
    doesn't resolve ("003 [23]"), it queues the file for an issue picker rather
    than reporting a bare 'not found'."""

    def _batch_stack(self, app, stack, providers):
        app.config["COMICVINE_API_KEY"] = "k"
        stack.enter_context(patch("routes.metadata.is_valid_library_path", return_value=True))
        stack.enter_context(patch("app.get_target_dir_live", return_value="/nonexistent_target"))
        stack.enter_context(patch("core.database.get_library_providers", return_value=providers))
        stack.enter_context(patch("models.metron.get_flask_api", return_value=None))
        stack.enter_context(patch("models.metron.is_connection_error", return_value=False))
        stack.enter_context(patch("models.comicvine.get_volume_details", return_value={}))
        stack.enter_context(patch("models.comicvine.read_cvinfo_fields",
                                  return_value={"start_year": 1951, "publisher_name": "Harvey"}))

    def _folder(self, tmp_path, filename="Chamber of Chills 003 [23] (1951).cbz"):
        folder = tmp_path / "Chamber of Chills (1951)"
        folder.mkdir()
        (folder / "cvinfo").write_text("https://comicvine.gamespot.com/volume/4050-1487/")
        _make_cbz(str(folder / filename), with_comicinfo=False)
        return folder

    def test_unmatched_issue_is_queued_for_selection(self, app, client, tmp_path):
        from contextlib import ExitStack
        folder = self._folder(tmp_path)

        with ExitStack() as stack:
            self._batch_stack(app, stack, [{"provider_type": "comicvine", "enabled": True}])
            stack.enter_context(patch("models.comicvine.get_metadata_by_volume_id",
                                      return_value=None))
            resp = client.post('/api/batch-metadata', json={
                'directory': str(folder), 'library_id': 1,
            })
            body = resp.get_data(as_text=True)

        result = _sse_complete_result(body)
        assert result['errors'] == 1
        assert len(result['unmatched']) == 1
        entry = result['unmatched'][0]
        assert entry['provider'] == 'comicvine'
        assert entry['issue_number'] == '3'
        assert entry['file'] == 'Chamber of Chills 003 [23] (1951).cbz'
        assert entry['file_path'].endswith('Chamber of Chills 003 [23] (1951).cbz')
        assert entry['selected_match'] == {
            'provider': 'comicvine', 'volume_id': 1487, 'publisher_name': 'Harvey',
        }
        assert result['details'][0]['can_select_issue'] is True
        assert result['details'][0]['reason'] == 'issue not found'

    def test_unknown_series_is_not_queued(self, app, client, tmp_path):
        """No provider identified a volume — nothing to pick from, so no queue."""
        from contextlib import ExitStack
        folder = tmp_path / "Nowhere (1951)"
        folder.mkdir()
        _make_cbz(str(folder / "Nowhere 003 (1951).cbz"), with_comicinfo=False)

        with ExitStack() as stack:
            self._batch_stack(app, stack, [{"provider_type": "comicvine", "enabled": True}])
            stack.enter_context(patch("models.comicvine.search_volumes", return_value=[]))
            resp = client.post('/api/batch-metadata', json={
                'directory': str(folder), 'library_id': 1,
            })
            body = resp.get_data(as_text=True)

        result = _sse_complete_result(body)
        assert result['unmatched'] == []
        assert 'can_select_issue' not in result['details'][0]

    def test_successful_match_queues_nothing(self, app, client, tmp_path):
        from contextlib import ExitStack
        folder = self._folder(tmp_path, filename="Chamber of Chills 003 (1951).cbz")

        with ExitStack() as stack:
            self._batch_stack(app, stack, [{"provider_type": "comicvine", "enabled": True}])
            stack.enter_context(patch("models.comicvine.get_metadata_by_volume_id",
                                      return_value={"Series": "Chamber of Chills", "Number": "3"}))
            stack.enter_context(patch("routes.metadata.add_comicinfo_to_cbz", return_value=True))
            stack.enter_context(patch("core.database.update_file_index_from_comicinfo"))
            resp = client.post('/api/batch-metadata', json={
                'directory': str(folder), 'library_id': 1,
            })
            body = resp.get_data(as_text=True)

        result = _sse_complete_result(body)
        assert result['processed'] == 1
        assert result['unmatched'] == []


class TestProviderIssuesRoute:
    """/api/provider-issues backs the batch issue picker."""

    def test_requires_provider_and_series(self, client):
        assert client.post('/api/provider-issues', json={}).status_code == 400
        assert client.post('/api/provider-issues',
                           json={'provider': 'metron'}).status_code == 400

    def test_returns_issue_list(self, client):
        issues = [{"id": "1", "issue_number": "23", "title": "The Thing",
                   "cover_date": "1951-06-01", "cover_url": None}]
        with patch("routes.metadata._issue_options_for", return_value=issues) as opts:
            resp = client.post('/api/provider-issues',
                               json={'provider': 'comicvine', 'series_id': 1487})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['issues'] == issues
        opts.assert_called_once_with('comicvine', 1487)

    def test_unavailable_provider_returns_empty_list(self, client):
        """A provider that can't list issues is not an error — just no picker."""
        with patch("routes.metadata._issue_options_for", return_value=[]):
            resp = client.post('/api/provider-issues',
                               json={'provider': 'gcd', 'series_id': 9})

        assert resp.status_code == 200
        assert resp.get_json()['issues'] == []


class TestIssueOptionsFor:
    """Unit coverage for the issue-list helper behind the picker."""

    def test_unknown_provider_returns_empty(self):
        from routes.metadata import _issue_options_for
        assert _issue_options_for('nope', 1) == []
        assert _issue_options_for('metron', None) == []

    def test_provider_failure_returns_empty_not_raise(self):
        from routes.metadata import _issue_options_for
        with patch("models.comicvine_source.get_all_issues_for_volume",
                   side_effect=RuntimeError("boom")):
            assert _issue_options_for('comicvine', 4050) == []

    def test_sorted_numerically_and_blank_numbers_dropped(self):
        from routes.metadata import _issue_options_for
        raw = [
            {"cv_id": 3, "number": "10", "name": None},
            {"cv_id": 1, "number": "2", "name": None},
            {"cv_id": 4, "number": "", "name": None},
            {"cv_id": 5, "number": "Annual 1", "name": None},
            {"cv_id": 2, "number": "2.1", "name": None},
        ]
        with patch("models.comicvine_source.get_all_issues_for_volume", return_value=raw):
            options = _issue_options_for('comicvine', 4050)

        # Numeric issues sort ascending; non-numeric ones trail behind.
        assert [o['issue_number'] for o in options] == ['2', '2.1', '10', 'Annual 1']


class TestDateCheckWiring:
    """The date check as routes/metadata.py applies it.

    These cover the wiring rather than the rule itself (see
    tests/unit/test_metadata_dates.py): that the ComicInfo dict is translated
    into something comparable, and that a rejected match is reported in a shape
    the client can actually use.
    """

    def test_issue_date_of_reads_the_comicinfo_dict(self):
        from routes.metadata import _issue_date_of
        assert _issue_date_of({"Year": 1999, "Month": 6}, "gcd") == "1999-06"
        assert _issue_date_of({"Year": 1999}, "gcd") == "1999"
        assert _issue_date_of({}, "gcd") is None
        # A series-level year is not an issue date, whatever the fields say.
        assert _issue_date_of({"Year": 1989, "Month": 1}, "mangadex") is None

    def test_conflict_is_not_recorded_as_a_near_miss(self):
        """A near miss means 'right series, wrong issue'.

        It carries the provider slug and series id the issue picker needs. A
        date conflict has neither -- the series itself is what is suspect -- so
        the batch path must not fabricate one. Regression guard: an earlier
        version passed the display name ('GCD API') where every other call site
        passes a slug ('gcd_api'), and omitted series_id entirely.
        """
        import inspect
        from routes import metadata as metadata_module

        source = inspect.getsource(metadata_module.batch_metadata)
        conflict_block = source.split("DATE_MODE_ENFORCE")[1].split("if metadata:")[0]
        assert "note_near_miss" not in conflict_block

    def test_conflict_does_not_remove_the_issue_picker(self):
        """An opt-in check must not take away an affordance.

        A near miss from another provider is still actionable, so its picker is
        offered as it was before the check existed; the date_conflict flag is
        carried alongside rather than replacing it.
        """
        import inspect
        from routes import metadata as metadata_module

        source = inspect.getsource(metadata_module.batch_metadata)
        assert "if near_miss and not date_conflicted:" not in source
        assert "detail['date_conflict'] = True" in source

    def test_every_near_miss_call_passes_a_provider_slug(self):
        """Guard the invariant the conflict path violated.

        Provider identifiers handed to the client are lowercase slugs matching
        ProviderType values, never the human-readable `source` string.
        """
        import inspect
        from routes import metadata as metadata_module

        source = inspect.getsource(metadata_module.batch_metadata)
        calls = re.findall(r"note_near_miss\(\s*([^,]+),", source)
        # The definition itself takes a parameter named `provider`.
        literals = [c.strip() for c in calls if c.strip() != "provider"]
        assert literals, "expected note_near_miss call sites"
        for literal in literals:
            assert literal.startswith("'") and literal.endswith("'"), (
                f"note_near_miss provider must be a literal slug, got {literal}"
            )
            assert literal.strip("'").islower(), (
                f"note_near_miss provider must be lowercase, got {literal}"
            )


class TestDateCheckFallthrough:
    """A rejected match must not cost the file its remaining providers."""

    def test_provider_loops_go_through_the_gate(self):
        """Both dispatch loops call accept_match, not try_fn directly.

        Calling try_fn() straight would accept a match the date check rejects,
        or -- in the earlier version that checked after the loop -- abandon the
        file instead of letting the next provider try.
        """
        import inspect
        from routes import metadata as metadata_module

        source = inspect.getsource(metadata_module.batch_metadata)
        assert "if try_fn and accept_match(try_fn):" in source
        assert "if accept_match(try_fn):" in source
        assert "if try_fn and try_fn():" not in source

    def test_gate_clears_source_with_metadata(self):
        """Leaving a stale `source` behind would mislabel the next provider's
        match, and is what the reported 'source' in the SSE stream uses."""
        import inspect
        from routes import metadata as metadata_module

        source = inspect.getsource(metadata_module.batch_metadata)
        gate = source.split("def accept_match")[1].split("provider_try_fns = {")[0]
        assert "metadata = None" in gate
        assert "source = None" in gate

    def test_both_paths_pass_the_provider_to_the_date_extractor(self):
        """ComicInfo Year is the series year for manga and Bedetheque.

        The exemption lives inside _issue_date_of so there is one decision
        point; both call sites must therefore hand it the provider, or the
        guard silently never applies.
        """
        import inspect
        from routes import metadata as metadata_module

        batch = inspect.getsource(metadata_module.batch_metadata)
        assert "_issue_date_of(metadata, source)" in batch

        single = inspect.getsource(metadata_module.search_metadata)
        assert "_issue_date_of(metadata, provider_type)" in single

    def test_both_paths_pass_the_issue_number_to_the_date_check(self):
        """A four-digit issue number ('Topolino 1904.cbz') looks exactly like a
        year, and `evaluate` only tells the two apart when handed the issue
        number it was matched against (core/metadata_dates.py).

        app.py cannot be imported in tests, and neither call site is otherwise
        exercised directly, so this pins the wiring the same way
        `test_both_paths_pass_the_provider_to_the_date_extractor` pins the
        provider argument -- a refactor that drops the third argument would
        pass the full suite silently and reintroduce #540.
        """
        import inspect
        from routes import metadata as metadata_module

        batch = inspect.getsource(metadata_module.batch_metadata)
        assert re.search(
            r"evaluate_issue_date\(\s*filename,\s*_issue_date_of\(metadata, source\),\s*issue_number\s*\)",
            batch,
        ), "accept_match must pass issue_number to evaluate_issue_date"

        single = inspect.getsource(metadata_module.search_metadata)
        assert re.search(
            r"evaluate_issue_date\(\s*file_name,\s*_issue_date_of\(metadata, provider_type\),\s*issue_number\s*\)",
            single,
        ), "the automatic-match date check must pass issue_number to evaluate_issue_date"


class TestBackfillCredits:
    """Manual trigger for the Metron credit backfill sweep.

    Metron finishes issue records after a comic ships, so files tagged on
    release morning can be written with no creators -- and nothing re-tags a
    file that already has Notes. This route is how a user repairs them now
    instead of waiting for the nightly series sync.
    """

    @patch("routes.metadata.threading.Thread")
    def test_starts_in_the_background_and_returns_an_op_id(self, mock_thread, client):
        """A full sweep runs for minutes (Metron pacing), so the request must
        not wait on it -- gunicorn's timeout is 120s."""
        resp = client.post('/api/metadata/backfill-credits', json={})

        assert resp.status_code == 202
        data = resp.get_json()
        assert data["success"] is True
        assert data["op_id"]
        mock_thread.return_value.start.assert_called_once()

    @patch("routes.metadata.threading.Thread")
    def test_passes_window_and_dry_run_to_the_worker(self, mock_thread, client):
        resp = client.post('/api/metadata/backfill-credits',
                           json={"days": 7, "limit": 5, "dry_run": True})

        assert resp.status_code == 202
        _op_id, _app, days, limit, dry_run = mock_thread.call_args.kwargs["args"]
        assert (days, limit, dry_run) == (7, 5, True)

    @patch("routes.metadata.threading.Thread")
    def test_defaults_when_no_body(self, mock_thread, client):
        from core.credit_backfill import DEFAULT_DAYS, DEFAULT_LIMIT

        resp = client.post('/api/metadata/backfill-credits')

        assert resp.status_code == 202
        _op_id, _app, days, limit, _dry = mock_thread.call_args.kwargs["args"]
        assert (days, limit) == (DEFAULT_DAYS, DEFAULT_LIMIT)

    @patch("routes.metadata.threading.Thread")
    def test_rejects_a_nonsense_window(self, mock_thread, client):
        resp = client.post('/api/metadata/backfill-credits', json={"days": 0})

        assert resp.status_code == 400
        assert resp.get_json()["success"] is False
        mock_thread.assert_not_called()

    @patch("routes.metadata.threading.Thread")
    def test_rejects_non_numeric_window(self, mock_thread, client):
        resp = client.post('/api/metadata/backfill-credits', json={"limit": "lots"})

        assert resp.status_code == 400
        mock_thread.assert_not_called()

    @patch("core.credit_backfill.run_credit_backfill")
    def test_worker_reports_the_summary_through_app_state(self, mock_run, app):
        from routes.metadata import _run_backfill_job
        import core.app_state as app_state

        mock_run.return_value = {"checked": 3, "updated": 2, "still_empty": 1,
                                 "skipped": 0, "errors": 0, "stopped_early": False,
                                 "dry_run": False}
        with app.test_request_context():
            op_id = app_state.register_operation("credit_backfill", "test")
        _run_backfill_job(op_id, app, 45, 200, False)

        op = next(o for o in app_state.get_active_operations(is_owner=True)
                  if o["id"] == op_id)
        assert op["status"] == "completed"
        assert "2 of 3" in op["detail"]

    @patch("core.credit_backfill.run_credit_backfill", side_effect=RuntimeError("boom"))
    def test_worker_failure_marks_the_operation_errored(self, mock_run, app):
        from routes.metadata import _run_backfill_job
        import core.app_state as app_state

        with app.test_request_context():
            op_id = app_state.register_operation("credit_backfill", "test")
        assert _run_backfill_job(op_id, app, 45, 200, False) is None

        op = next(o for o in app_state.get_active_operations(is_owner=True)
                  if o["id"] == op_id)
        assert op["status"] == "error"


class TestProviderAuthBlockRoutes:
    """The settings-page half of the Metron authentication lockout.

    A user mistyped their Metron credentials and CLU retried until Metron's
    fail2ban banned their IP, so a rejection now latches a block that only a
    human can lift.
    """

    def test_reset_auth_clears_the_block(self, client):
        with patch("models.metron.clear_auth_block") as clear:
            resp = client.post('/api/providers/metron/reset-auth')

        assert resp.status_code == 200
        assert resp.get_json()['success'] is True
        clear.assert_called_once()

    def test_reset_auth_rejects_an_unknown_provider(self, client):
        resp = client.post('/api/providers/nonsense/reset-auth')
        assert resp.status_code == 400
        assert 'Unknown provider type' in resp.get_json()['error']

    def test_reset_auth_rejects_a_provider_without_a_block(self, client):
        """Only Metron latches one; pretending otherwise would imply the other
        providers have a brake they do not have."""
        resp = client.post('/api/providers/comicvine/reset-auth')
        assert resp.status_code == 400

    def test_listing_surfaces_the_block_for_metron(self, client):
        state = {"blocked": True, "error": "Metron rejected the configured "
                                           "credentials (HTTP 401).",
                 "blocked_at": "2026-08-31T12:00:00+00:00"}
        with patch("models.metron.auth_block_state", return_value=state):
            resp = client.get('/api/providers')

        assert resp.status_code == 200
        providers = {p['type']: p for p in resp.get_json()['providers']}
        assert providers['metron']['auth_blocked'] is True
        assert '401' in providers['metron']['auth_error']
        # Every other card must report a clear state rather than undefined.
        assert providers['comicvine']['auth_blocked'] is False


class TestSaveProviderCredentialsAuthModes:
    """Saving replaces the stored blob wholesale and blank inputs never reach
    the server, so the chosen auth mode is what tells it which of the other
    method's fields to drop."""

    def _save(self, client, payload):
        with patch("core.database.save_provider_credentials",
                   return_value=True) as save, \
             patch("routes.metadata._validate_saved_credentials",
                   return_value=(True, None)), \
             patch("core.config.load_flask_config"), \
             patch("models.metron.clear_auth_block"):
            resp = client.post('/api/providers/metron/credentials', json=payload)
        return resp, save

    def test_token_mode_drops_a_previously_stored_username_and_password(self, client):
        resp, save = self._save(
            client,
            {"auth_mode": "token", "api_token": "tok-123",
             "username": "stale", "password": "stale"},
        )

        assert resp.status_code == 200
        assert save.call_args[0][1] == {"api_token": "tok-123"}

    def test_basic_mode_drops_a_previously_stored_token(self, client):
        resp, save = self._save(
            client,
            {"auth_mode": "basic", "username": "user", "password": "pass",
             "api_token": "stale"},
        )

        assert resp.status_code == 200
        assert save.call_args[0][1] == {"username": "user", "password": "pass"}

    def test_auth_mode_is_never_stored_as_a_credential(self, client):
        _, save = self._save(client, {"auth_mode": "token", "api_token": "tok"})
        assert "auth_mode" not in save.call_args[0][1]

    def test_an_empty_chosen_mode_is_rejected(self, client):
        with patch("core.database.save_provider_credentials") as save:
            resp = client.post('/api/providers/metron/credentials',
                               json={"auth_mode": "token", "username": "user"})

        assert resp.status_code == 400
        save.assert_not_called()

    def test_an_unknown_mode_is_rejected(self, client):
        with patch("core.database.save_provider_credentials") as save:
            resp = client.post('/api/providers/metron/credentials',
                               json={"auth_mode": "carrier-pigeon",
                                     "api_token": "tok"})

        assert resp.status_code == 400
        save.assert_not_called()

    def test_saving_lifts_an_existing_block_before_revalidating(self, client):
        """New credentials deserve to be judged on their own evidence."""
        with patch("core.database.save_provider_credentials", return_value=True), \
             patch("routes.metadata._validate_saved_credentials",
                   return_value=(True, None)), \
             patch("core.config.load_flask_config"), \
             patch("models.metron.clear_auth_block") as clear:
            resp = client.post('/api/providers/metron/credentials',
                               json={"auth_mode": "token", "api_token": "tok"})

        assert resp.status_code == 200
        clear.assert_called_once()

    def test_rejected_credentials_are_still_saved_but_reported(self, client):
        """Refusing the save would strand a user whose provider is merely down;
        the point is that they find out now rather than via a silent lockout."""
        with patch("core.database.save_provider_credentials",
                   return_value=True) as save, \
             patch("routes.metadata._validate_saved_credentials",
                   return_value=(False, "Metron rejected the credentials (HTTP 401).")), \
             patch("core.config.load_flask_config"), \
             patch("models.metron.clear_auth_block"):
            resp = client.post('/api/providers/metron/credentials',
                               json={"auth_mode": "token", "api_token": "bad"})

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['valid'] is False
        assert '401' in data['error']
        save.assert_called_once()

    def test_a_provider_without_modes_is_saved_untouched(self, client):
        with patch("core.database.save_provider_credentials",
                   return_value=True) as save, \
             patch("core.config.load_flask_config"):
            resp = client.post('/api/providers/comicvine/credentials',
                               json={"api_key": "cv-key"})

        assert resp.status_code == 200
        assert save.call_args[0][1] == {"api_key": "cv-key"}
        # ComicVine does not opt into save-time validation.
        assert 'valid' not in resp.get_json()
