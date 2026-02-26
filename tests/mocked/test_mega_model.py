"""Tests for models/mega.py -- mocked MEGA download client."""
import pytest
import base64
import json
import os
import struct
from unittest.mock import patch, MagicMock, mock_open

cryptography = pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_base64_key(length=32):
    """Return a URL-safe base64 string encoding ``length`` random-ish bytes."""
    raw = bytes(range(length)) + bytes(range(length))  # deterministic filler
    raw = raw[:length]
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _encrypt_attr(name, key_bytes):
    """Encrypt a MEGA-style attribute blob so _decrypt_attr can read it.

    MEGA attributes are ``b'MEGA' + json + NUL-padding``, encrypted with
    AES-CBC using a zero IV.
    """
    plaintext = b"MEGA" + json.dumps({"n": name}).encode()
    # Pad to 16-byte boundary
    pad_len = 16 - (len(plaintext) % 16)
    plaintext += b"\x00" * pad_len

    cipher = Cipher(algorithms.AES(key_bytes), modes.CBC(b"\x00" * 16))
    encryptor = cipher.encryptor()
    return encryptor.update(plaintext) + encryptor.finalize()


def _derive_meta_key(raw_key):
    """Derive the 16-byte AES key used for attribute decryption (big-endian)."""
    key_len = len(raw_key)
    if key_len == 32:
        k = struct.unpack(">8I", raw_key)
        return struct.pack(">4I", k[0] ^ k[4], k[1] ^ k[5], k[2] ^ k[6], k[3] ^ k[7])
    else:
        k = struct.unpack(">4I", raw_key[:16])
        return struct.pack(">4I", k[0] ^ k[1], k[1] ^ k[2], k[2] ^ k[3], k[3] ^ k[0])


# A fixed 32-byte key for tests (deterministic, URL-safe-base64-friendly)
RAW_KEY_32 = bytes(range(32))
B64_KEY_32 = base64.urlsafe_b64encode(RAW_KEY_32).rstrip(b"=").decode()

# A fixed 16-byte key for old-format tests
RAW_KEY_16 = bytes(range(16))
B64_KEY_16 = base64.urlsafe_b64encode(RAW_KEY_16).rstrip(b"=").decode()


# =========================================================================
# URL Parsing
# =========================================================================

class TestParseNewFormatUrl:

    def test_parse_new_format_url(self):
        from models.mega import MegaDownloader

        url = f"https://mega.nz/file/AbCdEfGh#{B64_KEY_32}"
        dl = MegaDownloader(url)
        assert dl.file_id == "AbCdEfGh"
        assert dl.raw_key == RAW_KEY_32


class TestParseOldFormatUrl:

    def test_parse_old_format_url(self):
        from models.mega import MegaDownloader

        url = f"https://mega.nz/#!XyZfIlEd!{B64_KEY_32}"
        dl = MegaDownloader(url)
        assert dl.file_id == "XyZfIlEd"
        assert dl.raw_key == RAW_KEY_32


class TestParseOldDomainUrl:

    def test_parse_old_domain_url(self):
        from models.mega import MegaDownloader

        url = f"https://mega.co.nz/#!OldDoMaIn!{B64_KEY_32}"
        dl = MegaDownloader(url)
        assert dl.file_id == "OldDoMaIn"
        assert dl.raw_key == RAW_KEY_32


class TestParseUrlMissingKey:

    def test_parse_url_missing_key(self):
        from models.mega import MegaDownloader

        with pytest.raises(Exception, match="Could not extract encryption key"):
            MegaDownloader("https://mega.nz/file/AbCdEfGh")

    def test_parse_url_empty_fragment(self):
        from models.mega import MegaDownloader

        with pytest.raises(Exception, match="Could not extract encryption key"):
            MegaDownloader("https://mega.nz/file/AbCdEfGh#")


class TestParseUrlInvalidBase64:

    def test_parse_url_invalid_base64(self):
        from models.mega import MegaDownloader

        # \x80 bytes are not valid base64 input; urlsafe_b64decode will raise
        with pytest.raises(Exception, match="Invalid encryption key"):
            MegaDownloader("https://mega.nz/file/AbCdEfGh#\x80\x80\x80\x80")


# =========================================================================
# get_metadata
# =========================================================================

class TestGetMetadataSuccess:

    @patch("models.mega.requests.post")
    def test_get_metadata_success(self, mock_post):
        from models.mega import MegaDownloader

        meta_key = _derive_meta_key(RAW_KEY_32)
        encrypted_attr = _encrypt_attr("TestComic.cbz", meta_key)
        encoded_attr = base64.urlsafe_b64encode(encrypted_attr).rstrip(b"=").decode()

        mock_resp = MagicMock()
        mock_resp.json.return_value = [{
            "at": encoded_attr,
            "s": 1234567,
            "g": "https://dl.example.com/file",
        }]
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        url = f"https://mega.nz/file/AbCdEfGh#{B64_KEY_32}"
        dl = MegaDownloader(url)
        meta = dl.get_metadata()

        assert meta["filename"] == "TestComic.cbz"
        assert meta["size"] == 1234567
        assert meta["download_url"] == "https://dl.example.com/file"

        # Verify API was called with correct payload structure
        call_args = mock_post.call_args
        payload = call_args[1].get("json") or call_args[0][1]
        assert payload[0]["a"] == "g"
        assert payload[0]["p"] == "AbCdEfGh"


class TestGetMetadataFileNotFound:

    @patch("models.mega.requests.post")
    def test_get_metadata_file_not_found(self, mock_post):
        from models.mega import MegaDownloader

        mock_resp = MagicMock()
        mock_resp.json.return_value = [-2]
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        url = f"https://mega.nz/file/AbCdEfGh#{B64_KEY_32}"
        dl = MegaDownloader(url)

        with pytest.raises(Exception, match="File not found"):
            dl.get_metadata()


class TestGetMetadataApiError:

    @patch("models.mega.requests.post")
    def test_get_metadata_api_error(self, mock_post):
        from models.mega import MegaDownloader

        mock_resp = MagicMock()
        mock_resp.json.return_value = [-9]
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        url = f"https://mega.nz/file/AbCdEfGh#{B64_KEY_32}"
        dl = MegaDownloader(url)

        with pytest.raises(Exception, match="Object not found"):
            dl.get_metadata()


class TestGetMetadataConnectionError:

    @patch("models.mega.requests.post")
    def test_get_metadata_connection_error(self, mock_post):
        import requests as real_requests
        from models.mega import MegaDownloader

        mock_post.side_effect = real_requests.RequestException("Connection refused")

        url = f"https://mega.nz/file/AbCdEfGh#{B64_KEY_32}"
        dl = MegaDownloader(url)

        with pytest.raises(Exception, match="Failed to connect to MEGA API"):
            dl.get_metadata()


# =========================================================================
# download
# =========================================================================

class TestDownloadSuccess:

    @patch("models.mega.requests.get")
    @patch("models.mega.requests.post")
    def test_download_success(self, mock_post, mock_get, tmp_path):
        from models.mega import MegaDownloader

        # -- Prepare metadata response --------------------------------
        meta_key = _derive_meta_key(RAW_KEY_32)
        encrypted_attr = _encrypt_attr("comic.cbz", meta_key)
        encoded_attr = base64.urlsafe_b64encode(encrypted_attr).rstrip(b"=").decode()

        mock_meta_resp = MagicMock()
        mock_meta_resp.json.return_value = [{
            "at": encoded_attr,
            "s": 64,
            "g": "https://dl.example.com/file",
        }]
        mock_meta_resp.status_code = 200
        mock_post.return_value = mock_meta_resp

        # -- Prepare download response --------------------------------
        # Encrypt 64 bytes of sample data with AES-CTR using the same
        # key derivation the downloader will use.
        k = struct.unpack(">8I", RAW_KEY_32)
        file_key = struct.pack(">4I", k[0] ^ k[4], k[1] ^ k[5], k[2] ^ k[6], k[3] ^ k[7])
        iv = struct.pack(">2I", k[4], k[5]) + b"\x00" * 8
        cipher = Cipher(algorithms.AES(file_key), modes.CTR(iv))
        encryptor = cipher.encryptor()
        plaintext = b"A" * 64
        ciphertext = encryptor.update(plaintext) + encryptor.finalize()

        mock_dl_resp = MagicMock()
        mock_dl_resp.iter_content.return_value = [ciphertext]
        mock_dl_resp.status_code = 200
        mock_dl_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_dl_resp

        # -- Execute --------------------------------------------------
        url = f"https://mega.nz/file/AbCdEfGh#{B64_KEY_32}"
        dl = MegaDownloader(url)
        result = dl.download(str(tmp_path))

        expected_path = os.path.join(str(tmp_path), "comic.cbz")
        assert result == expected_path
        assert os.path.exists(expected_path)

        with open(expected_path, "rb") as fh:
            assert fh.read() == plaintext


class TestDownloadCancelled:

    @patch("models.mega.requests.get")
    @patch("models.mega.requests.post")
    def test_download_cancelled(self, mock_post, mock_get, tmp_path):
        from models.mega import MegaDownloader

        # -- Metadata response ----------------------------------------
        meta_key = _derive_meta_key(RAW_KEY_32)
        encrypted_attr = _encrypt_attr("comic.cbz", meta_key)
        encoded_attr = base64.urlsafe_b64encode(encrypted_attr).rstrip(b"=").decode()

        mock_meta_resp = MagicMock()
        mock_meta_resp.json.return_value = [{
            "at": encoded_attr,
            "s": 64,
            "g": "https://dl.example.com/file",
        }]
        mock_meta_resp.status_code = 200
        mock_post.return_value = mock_meta_resp

        # -- Download response (small chunk) --------------------------
        k = struct.unpack(">8I", RAW_KEY_32)
        file_key = struct.pack(">4I", k[0] ^ k[4], k[1] ^ k[5], k[2] ^ k[6], k[3] ^ k[7])
        iv = struct.pack(">2I", k[4], k[5]) + b"\x00" * 8
        cipher = Cipher(algorithms.AES(file_key), modes.CTR(iv))
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(b"X" * 64) + encryptor.finalize()

        mock_dl_resp = MagicMock()
        mock_dl_resp.iter_content.return_value = [ciphertext]
        mock_dl_resp.status_code = 200
        mock_dl_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_dl_resp

        # -- Callback that cancels ------------------------------------
        def cancel_callback(downloaded, total, percent):
            return False

        url = f"https://mega.nz/file/AbCdEfGh#{B64_KEY_32}"
        dl = MegaDownloader(url)

        with pytest.raises(Exception, match="Download cancelled"):
            dl.download(str(tmp_path), progress_callback=cancel_callback)

        # Temp .part file should have been cleaned up
        part_file = os.path.join(str(tmp_path), "comic.cbz.part")
        assert not os.path.exists(part_file)


class TestDownloadCleanupOnError:

    @patch("models.mega.requests.get")
    @patch("models.mega.requests.post")
    def test_download_cleanup_on_error(self, mock_post, mock_get, tmp_path):
        from models.mega import MegaDownloader

        # -- Metadata response ----------------------------------------
        meta_key = _derive_meta_key(RAW_KEY_32)
        encrypted_attr = _encrypt_attr("comic.cbz", meta_key)
        encoded_attr = base64.urlsafe_b64encode(encrypted_attr).rstrip(b"=").decode()

        mock_meta_resp = MagicMock()
        mock_meta_resp.json.return_value = [{
            "at": encoded_attr,
            "s": 1024,
            "g": "https://dl.example.com/file",
        }]
        mock_meta_resp.status_code = 200
        mock_post.return_value = mock_meta_resp

        # -- Download response that raises mid-stream -----------------
        mock_dl_resp = MagicMock()
        mock_dl_resp.raise_for_status.side_effect = Exception("HTTP 500")
        mock_get.return_value = mock_dl_resp

        url = f"https://mega.nz/file/AbCdEfGh#{B64_KEY_32}"
        dl = MegaDownloader(url)

        with pytest.raises(Exception, match="HTTP 500"):
            dl.download(str(tmp_path))

        # Neither final nor temp files should remain
        assert not os.path.exists(os.path.join(str(tmp_path), "comic.cbz"))
        assert not os.path.exists(os.path.join(str(tmp_path), "comic.cbz.part"))
