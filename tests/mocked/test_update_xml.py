"""Tests for models/update_xml.py -- update ComicInfo.xml fields in CBZ files."""
import os
import zipfile
import xml.etree.ElementTree as ET

import pytest


SAMPLE_COMICINFO = """\
<?xml version='1.0' encoding='utf-8'?>
<ComicInfo>
  <Series>Batman</Series>
  <Number>1</Number>
  <Volume>2020</Volume>
</ComicInfo>"""

SAMPLE_COMICINFO_NO_VOLUME = """\
<?xml version='1.0' encoding='utf-8'?>
<ComicInfo>
  <Series>Batman</Series>
  <Number>1</Number>
</ComicInfo>"""


def make_cbz(path, comicinfo_xml=None, images=None):
    """Create a test CBZ (ZIP) file with optional ComicInfo.xml and images."""
    with zipfile.ZipFile(path, "w") as zf:
        if comicinfo_xml:
            zf.writestr("ComicInfo.xml", comicinfo_xml)
        if images:
            for name, data in images.items():
                zf.writestr(name, data)


def read_comicinfo_from_cbz(path):
    """Read and parse ComicInfo.xml from a CBZ file, returning the root Element."""
    with zipfile.ZipFile(path, "r") as zf:
        xml_data = zf.read("ComicInfo.xml")
    return ET.fromstring(xml_data)


class TestUpdateFieldInCbzFiles:

    def test_update_field_basic(self, tmp_path):
        """Update Volume from 2020 to 2021."""
        from models.update_xml import update_field_in_cbz_files

        cbz = tmp_path / "batman_001.cbz"
        make_cbz(str(cbz), comicinfo_xml=SAMPLE_COMICINFO)

        result = update_field_in_cbz_files(str(tmp_path), "Volume", "2021")

        assert result["updated"] == 1
        assert result["skipped"] == 0
        assert result["errors"] == 0

        root = read_comicinfo_from_cbz(str(cbz))
        assert root.find("Volume").text == "2021"

    def test_update_field_creates_element(self, tmp_path):
        """Update a field that doesn't exist yet in ComicInfo.xml."""
        from models.update_xml import update_field_in_cbz_files

        cbz = tmp_path / "batman_001.cbz"
        make_cbz(str(cbz), comicinfo_xml=SAMPLE_COMICINFO_NO_VOLUME)

        result = update_field_in_cbz_files(str(tmp_path), "Volume", "2023")

        assert result["updated"] == 1
        assert result["skipped"] == 0

        root = read_comicinfo_from_cbz(str(cbz))
        assert root.find("Volume").text == "2023"
        # Original fields should still be present
        assert root.find("Series").text == "Batman"
        assert root.find("Number").text == "1"

    def test_update_field_skips_when_already_set(self, tmp_path):
        """Value already matches -- should be skipped."""
        from models.update_xml import update_field_in_cbz_files

        cbz = tmp_path / "batman_001.cbz"
        make_cbz(str(cbz), comicinfo_xml=SAMPLE_COMICINFO)

        result = update_field_in_cbz_files(str(tmp_path), "Volume", "2020")

        assert result["updated"] == 0
        assert result["skipped"] == 1
        assert result["details"][0]["reason"] == "already set"

    def test_update_field_skips_no_comicinfo(self, tmp_path):
        """CBZ without ComicInfo.xml should be skipped."""
        from models.update_xml import update_field_in_cbz_files

        cbz = tmp_path / "noxml.cbz"
        make_cbz(str(cbz), images={"page01.jpg": b"fake-image-data"})

        result = update_field_in_cbz_files(str(tmp_path), "Volume", "2021")

        assert result["updated"] == 0
        assert result["skipped"] == 1
        assert result["details"][0]["reason"] == "no ComicInfo.xml"

    def test_update_field_multiple_files(self, tmp_path):
        """Folder with multiple CBZ files -- all should be processed."""
        from models.update_xml import update_field_in_cbz_files

        for i in range(1, 4):
            cbz = tmp_path / f"issue_{i:03d}.cbz"
            make_cbz(str(cbz), comicinfo_xml=SAMPLE_COMICINFO)

        result = update_field_in_cbz_files(str(tmp_path), "Volume", "2025")

        assert result["updated"] == 3
        assert result["skipped"] == 0
        assert result["errors"] == 0
        assert len(result["details"]) == 3

        # Verify each file was updated
        for i in range(1, 4):
            cbz = tmp_path / f"issue_{i:03d}.cbz"
            root = read_comicinfo_from_cbz(str(cbz))
            assert root.find("Volume").text == "2025"

    def test_update_field_non_cbz_ignored(self, tmp_path):
        """Non-CBZ files (.txt, .jpg, etc.) should be ignored entirely."""
        from models.update_xml import update_field_in_cbz_files

        cbz = tmp_path / "comic.cbz"
        make_cbz(str(cbz), comicinfo_xml=SAMPLE_COMICINFO)

        (tmp_path / "readme.txt").write_text("not a comic")
        (tmp_path / "cover.jpg").write_bytes(b"fake-jpg")
        (tmp_path / "notes.pdf").write_bytes(b"fake-pdf")

        result = update_field_in_cbz_files(str(tmp_path), "Volume", "2021")

        assert result["updated"] == 1
        assert result["skipped"] == 0
        assert result["errors"] == 0
        assert len(result["details"]) == 1

    def test_update_field_invalid_directory(self):
        """Non-existent directory should return an error dict."""
        from models.update_xml import update_field_in_cbz_files

        result = update_field_in_cbz_files("/nonexistent/path", "Volume", "2021")

        assert "error" in result
        assert "not a valid directory" in result["error"]

    def test_update_field_empty_directory(self, tmp_path):
        """Empty directory -- 0 updated, 0 skipped, 0 errors."""
        from models.update_xml import update_field_in_cbz_files

        result = update_field_in_cbz_files(str(tmp_path), "Volume", "2021")

        assert result["updated"] == 0
        assert result["skipped"] == 0
        assert result["errors"] == 0
        assert result["details"] == []

    def test_update_field_preserves_other_entries(self, tmp_path):
        """Images and other ZIP entries should still be in CBZ after update."""
        from models.update_xml import update_field_in_cbz_files

        cbz = tmp_path / "batman_001.cbz"
        images = {
            "page01.jpg": b"fake-image-1",
            "page02.jpg": b"fake-image-2",
            "page03.png": b"fake-image-3",
        }
        make_cbz(str(cbz), comicinfo_xml=SAMPLE_COMICINFO, images=images)

        result = update_field_in_cbz_files(str(tmp_path), "Volume", "2025")
        assert result["updated"] == 1

        with zipfile.ZipFile(str(cbz), "r") as zf:
            names = zf.namelist()
            assert "ComicInfo.xml" in names
            assert "page01.jpg" in names
            assert "page02.jpg" in names
            assert "page03.png" in names
            # Verify image data is preserved
            assert zf.read("page01.jpg") == b"fake-image-1"
            assert zf.read("page02.jpg") == b"fake-image-2"
            assert zf.read("page03.png") == b"fake-image-3"

    def test_update_field_error_handling(self, tmp_path):
        """Corrupted CBZ file should be counted as an error."""
        from models.update_xml import update_field_in_cbz_files

        corrupted = tmp_path / "corrupted.cbz"
        corrupted.write_bytes(b"this is not a valid zip file")

        result = update_field_in_cbz_files(str(tmp_path), "Volume", "2021")

        assert result["errors"] == 1
        assert result["updated"] == 0
        assert result["details"][0]["status"] == "error"
        assert result["details"][0]["file"] == "corrupted.cbz"

    def test_update_field_mixed_results(self, tmp_path):
        """Folder with a mix of updatable, skippable, and no-xml CBZ files."""
        from models.update_xml import update_field_in_cbz_files

        # This one should be updated (Volume is 2020, setting to 2025)
        make_cbz(str(tmp_path / "update_me.cbz"), comicinfo_xml=SAMPLE_COMICINFO)
        # This one should be skipped (already set to 2020)
        make_cbz(str(tmp_path / "skip_me.cbz"), comicinfo_xml=SAMPLE_COMICINFO)
        # This one has no ComicInfo.xml
        make_cbz(str(tmp_path / "no_xml.cbz"), images={"page.jpg": b"img"})

        # First set skip_me to 2025 so it matches
        update_field_in_cbz_files(str(tmp_path), "Volume", "2025")

        # Now update to 2025 again -- update_me and skip_me are both 2025 now,
        # no_xml still has no ComicInfo.xml
        result = update_field_in_cbz_files(str(tmp_path), "Volume", "2025")

        assert result["updated"] == 0
        assert result["skipped"] == 3  # 2 already set + 1 no ComicInfo.xml

    def test_update_field_different_fields(self, tmp_path):
        """Can update fields other than Volume (e.g., Series, Number)."""
        from models.update_xml import update_field_in_cbz_files

        cbz = tmp_path / "test.cbz"
        make_cbz(str(cbz), comicinfo_xml=SAMPLE_COMICINFO)

        result = update_field_in_cbz_files(str(tmp_path), "Series", "Superman")

        assert result["updated"] == 1

        root = read_comicinfo_from_cbz(str(cbz))
        assert root.find("Series").text == "Superman"
        # Other fields unchanged
        assert root.find("Volume").text == "2020"
        assert root.find("Number").text == "1"

    def test_update_field_case_sensitive_extension(self, tmp_path):
        """CBZ extension matching should be case-insensitive."""
        from models.update_xml import update_field_in_cbz_files

        cbz_upper = tmp_path / "upper.CBZ"
        make_cbz(str(cbz_upper), comicinfo_xml=SAMPLE_COMICINFO)

        result = update_field_in_cbz_files(str(tmp_path), "Volume", "2021")

        assert result["updated"] == 1

    def test_update_field_temp_file_cleanup_on_error(self, tmp_path):
        """Temp file should be cleaned up if an error occurs mid-processing."""
        from models.update_xml import update_field_in_cbz_files

        corrupted = tmp_path / "bad.cbz"
        corrupted.write_bytes(b"not a zip")

        update_field_in_cbz_files(str(tmp_path), "Volume", "2021")

        # Only the original corrupted file should remain -- no leftover temp files
        remaining = list(tmp_path.iterdir())
        assert len(remaining) == 1
        assert remaining[0].name == "bad.cbz"


class TestUpdateVolumeInCbz:

    def test_update_volume_legacy(self, tmp_path, capsys):
        """Legacy function delegates to update_field_in_cbz_files with 'Volume'."""
        from models.update_xml import update_volume_in_cbz

        cbz = tmp_path / "batman_001.cbz"
        make_cbz(str(cbz), comicinfo_xml=SAMPLE_COMICINFO)

        update_volume_in_cbz(str(tmp_path), "2025")

        root = read_comicinfo_from_cbz(str(cbz))
        assert root.find("Volume").text == "2025"

        captured = capsys.readouterr()
        assert "Updated" in captured.out
        assert "Volume set" in captured.out

    def test_update_volume_legacy_invalid_dir(self, capsys):
        """Legacy function prints error for invalid directory."""
        from models.update_xml import update_volume_in_cbz

        update_volume_in_cbz("/nonexistent/path", "2021")

        captured = capsys.readouterr()
        assert "Error" in captured.out

    def test_update_volume_legacy_already_set(self, tmp_path, capsys):
        """Legacy function prints skip message when volume already matches."""
        from models.update_xml import update_volume_in_cbz

        cbz = tmp_path / "batman_001.cbz"
        make_cbz(str(cbz), comicinfo_xml=SAMPLE_COMICINFO)

        update_volume_in_cbz(str(tmp_path), "2020")

        captured = capsys.readouterr()
        assert "Skipped" in captured.out
        assert "already 2020" in captured.out
