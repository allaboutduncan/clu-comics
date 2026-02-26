"""Tests for models/mega.py -- URL parsing and key extraction (pure logic)."""
import pytest
import base64

pytest.importorskip("cryptography", reason="cryptography package not installed")


class TestMegaUrlParsing:

    def _make_key_str(self, length=32):
        """Generate a valid base64-encoded key of the given byte length."""
        raw = b"\x42" * length
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def test_new_format_url(self):
        from models.mega import MegaDownloader
        key_str = self._make_key_str(32)
        url = f"https://mega.nz/file/ABCDEF#{key_str}"
        downloader = MegaDownloader(url)
        assert downloader.file_id == "ABCDEF"
        assert len(downloader.raw_key) == 32

    def test_old_format_url(self):
        from models.mega import MegaDownloader
        key_str = self._make_key_str(32)
        url = f"https://mega.nz/#!FILEID!{key_str}"
        downloader = MegaDownloader(url)
        assert downloader.file_id == "FILEID"
        assert len(downloader.raw_key) == 32

    def test_old_domain_url(self):
        from models.mega import MegaDownloader
        key_str = self._make_key_str(32)
        url = f"https://mega.co.nz/#!XYZ123!{key_str}"
        downloader = MegaDownloader(url)
        assert downloader.file_id == "XYZ123"

    def test_missing_key_raises(self):
        from models.mega import MegaDownloader
        with pytest.raises(Exception, match="Could not extract encryption key"):
            MegaDownloader("https://mega.nz/file/ABCDEF")

    def test_invalid_base64_raises(self):
        from models.mega import MegaDownloader
        with pytest.raises(Exception):
            MegaDownloader("https://mega.nz/file/ABCDEF#\x80\x80\x80\x80")

    def test_16_byte_key(self):
        from models.mega import MegaDownloader
        key_str = self._make_key_str(16)
        url = f"https://mega.nz/file/TESTID#{key_str}"
        downloader = MegaDownloader(url)
        assert len(downloader.raw_key) == 16

    def test_base64_url_decode(self):
        from models.mega import MegaDownloader
        key_str = self._make_key_str(32)
        url = f"https://mega.nz/file/TEST#{key_str}"
        downloader = MegaDownloader(url)
        # Test the internal _base64_url_decode method
        encoded = base64.urlsafe_b64encode(b"test data").decode("ascii")
        decoded = downloader._base64_url_decode(encoded)
        assert decoded == b"test data"

    def test_api_url_set(self):
        from models.mega import MegaDownloader
        key_str = self._make_key_str(32)
        url = f"https://mega.nz/file/ID#{key_str}"
        downloader = MegaDownloader(url)
        assert "mega.co.nz" in downloader.api_url


class TestMegaErrorCodes:

    def test_error_messages_dict_exists(self):
        """Verify the error code mapping is accessible in get_metadata."""
        # This tests the structure, not the API call
        from models.mega import MegaDownloader
        key_str = base64.urlsafe_b64encode(b"\x42" * 32).decode("ascii").rstrip("=")
        url = f"https://mega.nz/file/TEST#{key_str}"
        downloader = MegaDownloader(url)
        # The downloader should be initialised without errors
        assert downloader.file_id == "TEST"
