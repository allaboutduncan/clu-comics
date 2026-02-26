"""Tests for comicinfo.py -- ComicInfo.xml parsing, sanitization, and manipulation."""
import pytest
from unittest.mock import patch, MagicMock
import xml.etree.ElementTree as ET


# ===== clean_markdown =====

class TestCleanMarkdown:

    def test_removes_headings(self):
        from comicinfo import clean_markdown
        text = "# Heading\nSome text\n## Another heading\nMore text"
        result = clean_markdown(text)
        assert "# Heading" not in result
        assert "## Another" not in result
        assert "Some text" in result
        assert "More text" in result

    def test_removes_table_lines(self):
        from comicinfo import clean_markdown
        text = "Normal text\n| Col1 | Col2 |\n|---|---|\n| data | data |\nAfter table"
        result = clean_markdown(text)
        assert "|" not in result
        assert "Normal text" in result
        assert "After table" in result

    def test_removes_bold(self):
        from comicinfo import clean_markdown
        text = "This is **bold text** here"
        result = clean_markdown(text)
        assert "**" not in result
        assert "bold text" not in result
        assert "This is" in result

    def test_removes_underscore_bold(self):
        from comicinfo import clean_markdown
        text = "This is __bold__ text"
        result = clean_markdown(text)
        assert "__" not in result

    def test_empty_string(self):
        from comicinfo import clean_markdown
        assert clean_markdown("") == ""

    def test_plain_text_unchanged(self):
        from comicinfo import clean_markdown
        assert clean_markdown("Just plain text") == "Just plain text"


# ===== clean_markdown_list =====

class TestCleanMarkdownList:

    def test_removes_list_block(self):
        from comicinfo import clean_markdown_list
        text = "Before\n*List of items\nAfter"
        result = clean_markdown_list(text)
        assert "Before" in result
        assert "*List" not in result
        assert "After" in result

    def test_removes_list_with_following_table(self):
        from comicinfo import clean_markdown_list
        text = "Before\n*List header\n| a | b |\n| c | d |\nAfter"
        result = clean_markdown_list(text)
        assert "*List" not in result
        assert "|" not in result
        assert "After" in result

    def test_removes_list_with_blank_line_then_table(self):
        from comicinfo import clean_markdown_list
        text = "Before\n*List header\n\n| a | b |\nAfter"
        result = clean_markdown_list(text)
        assert "*List" not in result
        assert "|" not in result
        assert "After" in result

    def test_no_list_returns_unchanged(self):
        from comicinfo import clean_markdown_list
        text = "Just normal text\nWith lines"
        assert clean_markdown_list(text) == text


# ===== _sanitize_xml =====

class TestSanitizeXml:

    def test_removes_control_characters(self):
        from comicinfo import _sanitize_xml
        xml_data = b"<ComicInfo><Title>Bad\x01Char</Title></ComicInfo>"
        result = _sanitize_xml(xml_data)
        assert b"\x01" not in result

    def test_escapes_ampersands_in_text(self):
        from comicinfo import _sanitize_xml
        xml_data = b"<ComicInfo><Title>Tom &amp; Jerry</Title></ComicInfo>"
        result = _sanitize_xml(xml_data)
        # Should be parseable after sanitization
        decoded = result.decode("utf-8")
        assert "&amp;" in decoded

    def test_preserves_valid_xml(self):
        from comicinfo import _sanitize_xml
        xml_data = b"<ComicInfo><Title>Good Title</Title></ComicInfo>"
        result = _sanitize_xml(xml_data)
        decoded = result.decode("utf-8")
        assert "Good Title" in decoded

    def test_handles_bare_ampersand(self):
        from comicinfo import _sanitize_xml
        xml_data = b"<ComicInfo><Title>Rock & Roll</Title></ComicInfo>"
        result = _sanitize_xml(xml_data)
        # Result should be parseable XML
        root = ET.fromstring(result)
        assert root.find("Title").text == "Rock & Roll"


# ===== read_comicinfo_xml =====

class TestReadComicinfoXml:

    def test_parses_valid_xml(self):
        from comicinfo import read_comicinfo_xml
        xml = b'<?xml version="1.0"?><ComicInfo><Title>Issue One</Title><Series>Batman</Series></ComicInfo>'
        result = read_comicinfo_xml(xml)
        assert result["Title"] == "Issue One"
        assert result["Series"] == "Batman"

    def test_empty_tags_return_empty_string(self):
        from comicinfo import read_comicinfo_xml
        xml = b"<ComicInfo><Title></Title></ComicInfo>"
        result = read_comicinfo_xml(xml)
        assert result["Title"] == ""

    def test_invalid_xml_returns_empty_dict(self):
        from comicinfo import read_comicinfo_xml
        result = read_comicinfo_xml(b"not xml at all")
        assert result == {}

    def test_xml_with_namespace(self):
        from comicinfo import read_comicinfo_xml
        xml = b'<ComicInfo xmlns="http://example.com"><Title>Test</Title></ComicInfo>'
        result = read_comicinfo_xml(xml)
        assert result.get("Title") == "Test"

    def test_sanitises_and_retries_on_parse_error(self):
        from comicinfo import read_comicinfo_xml
        # Bare ampersand would cause parse error; sanitization should fix it
        xml = b"<ComicInfo><Title>Rock & Roll</Title></ComicInfo>"
        result = read_comicinfo_xml(xml)
        assert "Rock" in result.get("Title", "")


# ===== update_comicinfo_xml =====

class TestUpdateComicinfoXml:

    def test_updates_existing_tag(self):
        from comicinfo import update_comicinfo_xml
        xml = b"<ComicInfo><Title>Old</Title></ComicInfo>"
        result = update_comicinfo_xml(xml, {"Title": "New"})
        root = ET.fromstring(result)
        assert root.find("Title").text == "New"

    def test_adds_new_tag(self):
        from comicinfo import update_comicinfo_xml
        xml = b"<ComicInfo><Title>Test</Title></ComicInfo>"
        result = update_comicinfo_xml(xml, {"Writer": "Alan Moore"})
        root = ET.fromstring(result)
        assert root.find("Writer").text == "Alan Moore"
        assert root.find("Title").text == "Test"

    def test_multiple_updates(self):
        from comicinfo import update_comicinfo_xml
        xml = b"<ComicInfo><Title>Old</Title></ComicInfo>"
        result = update_comicinfo_xml(xml, {"Title": "New", "Volume": "2020"})
        root = ET.fromstring(result)
        assert root.find("Title").text == "New"
        assert root.find("Volume").text == "2020"

    def test_returns_bytes(self):
        from comicinfo import update_comicinfo_xml
        xml = b"<ComicInfo><Title>Test</Title></ComicInfo>"
        result = update_comicinfo_xml(xml, {"Title": "Updated"})
        assert isinstance(result, bytes)


# ===== read_comicinfo_from_zip =====

class TestReadComicinfoFromZip:

    def test_reads_from_cbz(self, create_cbz):
        from comicinfo import read_comicinfo_from_zip
        xml = '<ComicInfo><Title>Test Issue</Title><Series>Batman</Series></ComicInfo>'
        path = create_cbz("test.cbz", num_images=1, comicinfo_xml=xml)
        result = read_comicinfo_from_zip(path)
        assert result["Title"] == "Test Issue"
        assert result["Series"] == "Batman"

    def test_returns_empty_dict_when_no_comicinfo(self, create_cbz):
        from comicinfo import read_comicinfo_from_zip
        path = create_cbz("test.cbz", num_images=1, comicinfo_xml=None)
        result = read_comicinfo_from_zip(path)
        assert result == {}

    def test_rejects_non_zip_extension(self, tmp_path):
        from comicinfo import read_comicinfo_from_zip
        rar_file = tmp_path / "test.rar"
        rar_file.write_bytes(b"fake data")
        with pytest.raises(ValueError, match="Only .zip or .cbz"):
            read_comicinfo_from_zip(str(rar_file))
