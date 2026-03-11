"""Tests for manga cvinfo field read/write in models/comicvine.py."""
import os
import pytest


class TestReadCvinfoMangaFields:

    def test_read_empty(self, tmp_path):
        """No manga fields in cvinfo returns all None values."""
        from models.comicvine import read_cvinfo_manga_fields
        cvinfo = tmp_path / "cvinfo"
        cvinfo.write_text("https://comicvine.gamespot.com/volume/4050-12345/\n")
        result = read_cvinfo_manga_fields(str(cvinfo))
        assert result['mangadex_id'] is None
        assert result['mangadex_title'] is None
        assert result['mangaupdates_id'] is None

    def test_read_file_not_found(self, tmp_path):
        """Missing file returns dict with all None values."""
        from models.comicvine import read_cvinfo_manga_fields
        result = read_cvinfo_manga_fields(str(tmp_path / "nonexistent"))
        assert result['mangadex_id'] is None
        assert result['mangadex_title'] is None

    def test_read_with_data(self, tmp_path):
        """Manga fields present are parsed correctly."""
        from models.comicvine import read_cvinfo_manga_fields
        cvinfo = tmp_path / "cvinfo"
        cvinfo.write_text(
            "https://comicvine.gamespot.com/volume/4050-12345/\n"
            "mangadex_id: 31be4cc4-d7c8-47d7-9d80-4f1b2db7979e\n"
            "mangadex_title: Angel Heart\n"
            "mangadex_alt_title: エンジェル・ハート\n"
            "mangaupdates_id: 99999\n"
            "mangaupdates_title: Angel Heart\n",
            encoding='utf-8',
        )
        result = read_cvinfo_manga_fields(str(cvinfo))
        assert result['mangadex_id'] == '31be4cc4-d7c8-47d7-9d80-4f1b2db7979e'
        assert result['mangadex_title'] == 'Angel Heart'
        assert result['mangadex_alt_title'] == 'エンジェル・ハート'
        assert result['mangaupdates_id'] == '99999'
        assert result['mangaupdates_title'] == 'Angel Heart'
        assert result['mangaupdates_alt_title'] is None


class TestWriteCvinfoMangaFields:

    def test_write_creates_file(self, tmp_path):
        """Writing to nonexistent path creates the file."""
        from models.comicvine import write_cvinfo_manga_fields
        cvinfo = tmp_path / "cvinfo"
        result = write_cvinfo_manga_fields(str(cvinfo), {
            'mangadex_id': 'abc-123',
            'mangadex_title': 'Test Manga',
        })
        assert result is True
        content = cvinfo.read_text(encoding='utf-8')
        assert 'mangadex_id: abc-123' in content
        assert 'mangadex_title: Test Manga' in content

    def test_write_appends(self, tmp_path):
        """Existing cvinfo content is preserved when appending manga fields."""
        from models.comicvine import write_cvinfo_manga_fields
        cvinfo = tmp_path / "cvinfo"
        cvinfo.write_text("https://comicvine.gamespot.com/volume/4050-12345/\n")
        write_cvinfo_manga_fields(str(cvinfo), {
            'mangadex_id': 'abc-123',
            'mangadex_title': 'Test Manga',
        })
        content = cvinfo.read_text(encoding='utf-8')
        assert 'https://comicvine.gamespot.com/volume/4050-12345/' in content
        assert 'mangadex_id: abc-123' in content

    def test_write_no_duplicates(self, tmp_path):
        """Writing same fields twice does not duplicate them."""
        from models.comicvine import write_cvinfo_manga_fields
        cvinfo = tmp_path / "cvinfo"
        cvinfo.write_text("")
        fields = {
            'mangadex_id': 'abc-123',
            'mangadex_title': 'Test Manga',
        }
        write_cvinfo_manga_fields(str(cvinfo), fields)
        write_cvinfo_manga_fields(str(cvinfo), fields)
        content = cvinfo.read_text(encoding='utf-8')
        assert content.count('mangadex_id:') == 1
        assert content.count('mangadex_title:') == 1
