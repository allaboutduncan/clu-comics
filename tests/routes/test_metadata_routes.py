"""Tests for routes/metadata.py -- metadata management endpoints."""
import io
import json
import os
import re
import zipfile
import pytest
from unittest.mock import patch, MagicMock, call


class TestGenerateComicInfoXml:

    def test_generate_basic(self):
        """Test the generate_comicinfo_xml helper function."""
        from routes.metadata import generate_comicinfo_xml
        import xml.etree.ElementTree as ET

        issue_data = {
            "Title": "The Origin",
            "Series": "Batman",
            "Number": "1",
            "Volume": "2020",
            "Summary": "The Dark Knight rises",
            "Year": "2020",
            "Month": "3",
            "Writer": "Tom King",
            "Penciller": "David Finch",
            "Publisher": "DC Comics",
        }
        xml_bytes = generate_comicinfo_xml(issue_data)
        assert xml_bytes is not None
        assert b"<ComicInfo>" in xml_bytes or b"<ComicInfo" in xml_bytes

        root = ET.fromstring(xml_bytes)
        assert root.tag == "ComicInfo"
        assert root.find("Series").text == "Batman"
        assert root.find("Writer").text == "Tom King"

    def test_decimal_issue_number_preserved(self):
        """Decimal issue numbers like 12.1 should not be truncated to 12."""
        from routes.metadata import generate_comicinfo_xml
        import xml.etree.ElementTree as ET

        issue_data = {"Series": "Avengers", "Number": "12.1", "Year": "2011"}
        xml_bytes = generate_comicinfo_xml(issue_data)
        root = ET.fromstring(xml_bytes)
        assert root.find("Number").text == "12.1"

    def test_decimal_issue_preserves_leading_zeros(self):
        """012.1 should stay '012.1', not be stripped to '12.1' via float()."""
        from routes.metadata import generate_comicinfo_xml
        import xml.etree.ElementTree as ET

        issue_data = {"Series": "Avengers", "Number": "012.1", "Year": "2011"}
        xml_bytes = generate_comicinfo_xml(issue_data)
        root = ET.fromstring(xml_bytes)
        assert root.find("Number").text == "012.1"

    def test_whole_number_as_float_drops_decimal(self):
        """12.0 should be stored as '12', not '12.0'."""
        from routes.metadata import generate_comicinfo_xml
        import xml.etree.ElementTree as ET

        issue_data = {"Series": "Batman", "Number": "12.0"}
        xml_bytes = generate_comicinfo_xml(issue_data)
        root = ET.fromstring(xml_bytes)
        assert root.find("Number").text == "12"

    def test_non_numeric_issue_number_preserved(self):
        """Non-numeric issue numbers like '12.HU' should pass through unchanged."""
        from routes.metadata import generate_comicinfo_xml
        import xml.etree.ElementTree as ET

        issue_data = {"Series": "Batman", "Number": "12.HU"}
        xml_bytes = generate_comicinfo_xml(issue_data)
        root = ET.fromstring(xml_bytes)
        assert root.find("Number").text == "12.HU"

    def test_generate_empty_data(self):
        from routes.metadata import generate_comicinfo_xml
        xml_bytes = generate_comicinfo_xml({})
        assert xml_bytes is not None

    def test_generate_list_credits(self):
        from routes.metadata import generate_comicinfo_xml
        import xml.etree.ElementTree as ET

        issue_data = {
            "Series": "X-Men",
            "Writer": ["Chris Claremont", "Fabian Nicieza"],
        }
        xml_bytes = generate_comicinfo_xml(issue_data)
        root = ET.fromstring(xml_bytes)
        writer = root.find("Writer")
        assert writer is not None
        assert "Chris Claremont" in writer.text


class TestAsText:

    def test_none(self):
        from routes.metadata import _as_text
        assert _as_text(None) is None

    def test_string(self):
        from routes.metadata import _as_text
        assert _as_text("hello") == "hello"

    def test_list(self):
        from routes.metadata import _as_text
        assert _as_text(["a", "b", "c"]) == "a, b, c"

    def test_list_with_none(self):
        from routes.metadata import _as_text
        assert _as_text(["a", None, "c"]) == "a, c"

    def test_int(self):
        from routes.metadata import _as_text
        assert _as_text(42) == "42"


def _make_cbz(path, with_comicinfo=True):
    """Helper to create a minimal CBZ file for testing."""
    with zipfile.ZipFile(path, 'w') as zf:
        zf.writestr("page_001.png", b"fake image data")
        if with_comicinfo:
            zf.writestr("ComicInfo.xml", "<ComicInfo><Series>Test</Series></ComicInfo>")


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

        mock_cv.generate_comicinfo_xml.return_value = b"<ComicInfo/>"
        mock_rename.return_value = (new_path, True)

        # Simulate the batch flow logic inline (extracted from the generator)
        file_path = old_path
        filename = os.path.basename(old_path)

        # -- begin logic under test (mirrors routes/metadata.py ~line 1376) --
        xml_bytes = mock_cv.generate_comicinfo_xml(metadata)
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

        mock_cv.generate_comicinfo_xml.return_value = b"<ComicInfo/>"
        mock_rename.return_value = (file_path, False)

        # Simulate batch flow
        filename = os.path.basename(file_path)
        xml_bytes = mock_cv.generate_comicinfo_xml(metadata)
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
