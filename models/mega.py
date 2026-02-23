import requests
import json
import base64
import struct
import os
import logging
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

logger = logging.getLogger(__name__)


class MegaDownloader:
    def __init__(self, url: str):
        logger.debug(f"MegaDownloader initializing with URL: {url}")
        self.url = url
        try:
            self.file_id, self.raw_key = self._parse_url(url)
            logger.debug(f"Parsed file_id: {self.file_id}, key_length: {len(self.raw_key)}")
        except Exception as e:
            logger.error(f"Failed to parse MEGA URL: {e}")
            raise
        self.api_url = "https://g.api.mega.co.nz/cs?id=0&ak=STAnVByJ"

    def _parse_url(self, url: str):
        logger.debug(f"Parsing MEGA URL: {url}")
        # Handle different MEGA URL formats:
        # https://mega.nz/file/FILEID#KEY
        # https://mega.nz/#!FILEID!KEY (old format)
        # https://mega.co.nz/#!FILEID!KEY (old domain)

        if '#!' in url and '!' in url.split('#!')[1]:
            # Old format: mega.nz/#!FILEID!KEY
            after_hash = url.split('#!')[1]
            parts = after_hash.split('!')
            file_id = parts[0]
            key_str = parts[1] if len(parts) > 1 else ''
            logger.debug(f"Old format detected: file_id={file_id}")
        elif '/file/' in url and '#' in url:
            # New format: mega.nz/file/FILEID#KEY
            path_part = url.split('/file/')[1]
            parts = path_part.split('#')
            file_id = parts[0]
            key_str = parts[1] if len(parts) > 1 else ''
            logger.debug(f"New format detected: file_id={file_id}")
        else:
            # Fallback to original parsing
            parts = url.split('/')[-1].split('#')
            file_id = parts[0]
            key_str = parts[1] if len(parts) > 1 else ''
            logger.debug(f"Fallback parsing: file_id={file_id}")

        if not key_str:
            raise Exception(f"Could not extract encryption key from URL: {url}")

        # Adding padding for base64 if missing
        key_str += '=' * (-len(key_str) % 4)
        try:
            raw_key = base64.urlsafe_b64decode(key_str)
            logger.debug(f"Decoded key length: {len(raw_key)} bytes")
        except Exception as e:
            logger.error(f"Failed to decode base64 key: {e}")
            raise Exception(f"Invalid encryption key in URL: {e}")

        return file_id, raw_key

    def _base64_url_decode(self, data):
        data += '=' * (-len(data) % 4)
        return base64.urlsafe_b64decode(data)

    def _decrypt_attr(self, attr, key):
        cipher = Cipher(algorithms.AES(key), modes.CBC(b'\x00' * 16))
        decryptor = cipher.decryptor()
        data = decryptor.update(attr) + decryptor.finalize()
        if data.startswith(b'MEGA'):
            # Remove padding and 'MEGA' prefix
            return json.loads(data[4:].split(b'\0')[0].decode('utf-8'))
        return None

    def get_metadata(self):
        logger.debug(f"Fetching metadata for file_id: {self.file_id}")
        payload = [{"a": "g", "g": 1, "p": self.file_id}]

        try:
            logger.debug(f"POST to {self.api_url}")
            resp = requests.post(self.api_url, json=payload, timeout=30)
            logger.debug(f"API response status: {resp.status_code}")
            response = resp.json()
            logger.debug(f"API response: {response}")
        except requests.RequestException as e:
            logger.error(f"API request failed: {e}")
            raise Exception(f"Failed to connect to MEGA API: {e}")
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse API response: {e}")
            raise Exception(f"Invalid response from MEGA API: {e}")

        if not response or isinstance(response[0], int) or 'at' not in response[0]:
            # MEGA returns error codes as integers (e.g., -2 = file not found, -9 = object not found)
            error_code = response[0] if response and isinstance(response[0], int) else "unknown"
            error_messages = {
                -1: "Internal error",
                -2: "Invalid arguments / File not found",
                -3: "Request failed, retrying may help",
                -4: "Rate limited",
                -9: "Object not found",
                -11: "Access denied",
                -14: "Resource temporarily unavailable",
                -16: "Too many connections",
                -17: "Out of range",
                -18: "Expired link"
            }
            error_msg = error_messages.get(error_code, f"Unknown error code: {error_code}")
            logger.error(f"MEGA API error: {error_code} - {error_msg}")
            raise Exception(f"MEGA error: {error_msg}")

        file_info = response[0]
        logger.debug(f"File info received: size={file_info.get('s')}, has_download_url={'g' in file_info}")

        encrypted_attr = self._base64_url_decode(file_info['at'])

        # MEGA attribute key derivation - depends on key length
        key_len = len(self.raw_key)
        logger.debug(f"Raw key length: {key_len} bytes")

        if key_len == 32:
            # New format (32-byte key): XOR first 16 bytes with last 16 bytes
            k = struct.unpack('>8I', self.raw_key)  # 8 x 4-byte integers, big-endian
            meta_key = struct.pack('>4I', k[0] ^ k[4], k[1] ^ k[5], k[2] ^ k[6], k[3] ^ k[7])
            logger.debug("Using 32-byte key derivation (new format)")
        elif key_len >= 16:
            # Old format or fallback: use first 16 bytes with internal XOR
            k = struct.unpack('>4I', self.raw_key[:16])
            meta_key = struct.pack('>4I', k[0] ^ k[1], k[1] ^ k[2], k[2] ^ k[3], k[3] ^ k[0])
            logger.debug("Using 16-byte key derivation (old format)")
        else:
            logger.error(f"Key too short: {key_len} bytes, need at least 16")
            raise Exception(f"Invalid encryption key length: {key_len}")

        attributes = self._decrypt_attr(encrypted_attr, meta_key)
        if not attributes:
            logger.error(f"Failed to decrypt file attributes. Encrypted attr length: {len(encrypted_attr)}")
            # Try alternate endianness as fallback
            logger.debug("Trying alternate key derivation (little-endian)...")
            if key_len == 32:
                k = struct.unpack('<8I', self.raw_key)
                meta_key = struct.pack('<4I', k[0] ^ k[4], k[1] ^ k[5], k[2] ^ k[6], k[3] ^ k[7])
            else:
                k = struct.unpack('<4I', self.raw_key[:16])
                meta_key = struct.pack('<4I', k[0] ^ k[1], k[1] ^ k[2], k[2] ^ k[3], k[3] ^ k[0])
            attributes = self._decrypt_attr(encrypted_attr, meta_key)

        if not attributes:
            logger.error("Failed to decrypt file attributes with both endianness options")
            raise Exception("Failed to decrypt file metadata - invalid key or corrupted data")

        logger.info(f"MEGA metadata: filename={attributes.get('n')}, size={file_info['s']}")

        return {
            "filename": attributes['n'],
            "size": file_info['s'],
            "download_url": file_info['g']
        }

    def download(self, dest_folder: str, progress_callback=None):
        logger.debug(f"Starting download to folder: {dest_folder}")

        meta = self.get_metadata()
        filename = meta['filename']
        total_size = meta['size']
        dl_url = meta['download_url']

        logger.info(f"Downloading: {filename} ({total_size / 1024 / 1024:.2f} MB)")
        logger.debug(f"Download URL: {dl_url[:80]}...")

        # Prepare Decryption (AES-CTR)
        key_len = len(self.raw_key)
        logger.debug(f"Preparing decryption with {key_len}-byte key")

        if key_len == 32:
            # New format: derive file key from 32-byte key
            # File key is XOR of first and second half
            k = struct.unpack('>8I', self.raw_key)
            file_key = struct.pack('>4I', k[0] ^ k[4], k[1] ^ k[5], k[2] ^ k[6], k[3] ^ k[7])
            # IV is derived from the key parts
            iv = struct.pack('>2I', k[4], k[5]) + b'\0' * 8
            logger.debug("Using 32-byte key derivation for decryption")
        elif key_len >= 16:
            # Old format: use key directly
            file_key = self.raw_key[:16]
            if key_len >= 24:
                iv = self.raw_key[16:24] + b'\0' * 8
            else:
                iv = b'\0' * 16
            logger.debug("Using direct key for decryption")
        else:
            logger.error(f"Key too short: {key_len} bytes")
            raise Exception(f"Invalid key length for decryption: {key_len}")

        cipher = Cipher(algorithms.AES(file_key), modes.CTR(iv))
        decryptor = cipher.decryptor()

        full_path = os.path.join(dest_folder, filename)
        temp_path = full_path + '.part'
        downloaded_bytes = 0

        logger.debug(f"Output path: {full_path}")
        logger.debug(f"Temp path: {temp_path}")

        try:
            logger.debug(f"Initiating GET request to download URL")
            res = requests.get(dl_url, stream=True, timeout=30)
            res.raise_for_status()
            logger.debug(f"Download response status: {res.status_code}")

            with open(temp_path, 'wb') as f:
                for chunk in res.iter_content(chunk_size=128 * 1024):  # 128KB chunks
                    if chunk:
                        f.write(decryptor.update(chunk))
                        downloaded_bytes += len(chunk)

                        if progress_callback:
                            percent = (downloaded_bytes / total_size) * 100
                            # Callback returns False to signal cancellation
                            if progress_callback(downloaded_bytes, total_size, percent) is False:
                                logger.info("Download cancelled by user")
                                raise Exception("Download cancelled by user")

            logger.debug(f"Download complete, renaming temp file")
            # Rename temp file to final path
            os.replace(temp_path, full_path)
            logger.info(f"MEGA download complete: {full_path}")
            return full_path

        except Exception as e:
            logger.error(f"Download failed: {e}")
            # Clean up temp file on error
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                    logger.debug(f"Cleaned up temp file: {temp_path}")
                except OSError as oe:
                    logger.warning(f"Failed to clean up temp file: {oe}")
            raise