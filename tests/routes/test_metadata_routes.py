"""Tests for routes/metadata.py -- metadata management endpoints."""
import io
import os
import zipfile
import pytest
from unittest.mock import patch, MagicMock


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

    @patch("database.set_has_comicinfo")
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

    @patch("database.set_has_comicinfo")
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

    @patch("database.set_has_comicinfo")
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

    @patch("database.set_has_comicinfo")
    def test_single_endpoint_still_works(self, mock_set, client, tmp_path):
        cbz_path = str(tmp_path / "data" / "single.cbz")
        os.makedirs(str(tmp_path / "data"), exist_ok=True)
        _make_cbz(cbz_path)

        resp = client.post('/cbz-clear-comicinfo',
                           json={"path": cbz_path})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
