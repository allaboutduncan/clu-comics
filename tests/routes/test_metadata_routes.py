"""Tests for routes/metadata.py -- metadata management endpoints."""
import os
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
