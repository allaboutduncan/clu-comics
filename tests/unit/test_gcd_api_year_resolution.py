"""Tests for GCD API start year resolution logic."""
import pytest
import os
from unittest.mock import patch, MagicMock


class TestResolveGCDApiStartYear:
    """Test _resolve_gcd_api_start_year from routes/metadata.py."""

    def _resolve(self, file_path, series_name="Batman"):
        from routes.metadata import _resolve_gcd_api_start_year
        return _resolve_gcd_api_start_year(file_path, series_name)

    def test_folder_name_v_year(self, tmp_path):
        folder = tmp_path / "Batman v2025"
        folder.mkdir()
        cbz = folder / "Batman 001.cbz"
        cbz.write_text("")
        assert self._resolve(str(cbz)) == 2025

    def test_folder_name_parenthetical_year(self, tmp_path):
        folder = tmp_path / "Batman (2025)"
        folder.mkdir()
        cbz = folder / "Batman 001.cbz"
        cbz.write_text("")
        assert self._resolve(str(cbz)) == 2025

    def test_folder_name_no_year(self, tmp_path):
        folder = tmp_path / "Batman"
        folder.mkdir()
        cbz = folder / "Batman 001.cbz"
        cbz.write_text("")
        # No year in folder name and no sibling CBZs with Volume field
        assert self._resolve(str(cbz)) is None

    def test_no_file_path(self):
        assert self._resolve(None) is None

    def test_sibling_xml_volume_field(self, tmp_path):
        """When folder name has no year, check sibling CBZ Volume field."""
        folder = tmp_path / "Batman"
        folder.mkdir()
        target = folder / "Batman 003.cbz"
        target.write_text("")
        sibling = folder / "Batman 001.cbz"

        # Create a real CBZ with ComicInfo.xml containing Volume=2025
        import zipfile
        with zipfile.ZipFile(str(sibling), 'w') as zf:
            zf.writestr("ComicInfo.xml", '<?xml version="1.0"?><ComicInfo><Volume>2025</Volume></ComicInfo>')

        result = self._resolve(str(target))
        assert result == 2025

    def test_sibling_xml_no_volume(self, tmp_path):
        """Sibling CBZ without Volume field returns None."""
        folder = tmp_path / "Batman"
        folder.mkdir()
        target = folder / "Batman 003.cbz"
        target.write_text("")
        sibling = folder / "Batman 001.cbz"

        import zipfile
        with zipfile.ZipFile(str(sibling), 'w') as zf:
            zf.writestr("ComicInfo.xml", '<?xml version="1.0"?><ComicInfo><Series>Batman</Series></ComicInfo>')

        result = self._resolve(str(target))
        assert result is None

    def test_folder_v_year_case_insensitive(self, tmp_path):
        folder = tmp_path / "Batman V2025"
        folder.mkdir()
        cbz = folder / "Batman 001.cbz"
        cbz.write_text("")
        assert self._resolve(str(cbz)) == 2025

    def test_invalid_year_ignored(self, tmp_path):
        folder = tmp_path / "Batman (1800)"
        folder.mkdir()
        cbz = folder / "Batman 001.cbz"
        cbz.write_text("")
        # 1800 is outside valid range (1900-2100), no siblings either
        assert self._resolve(str(cbz)) is None
