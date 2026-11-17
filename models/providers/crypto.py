"""
AES-256-GCM encryption for provider credentials.

This module provides encryption and decryption functions for storing
provider credentials securely in the database. Credentials are encrypted
using AES-256-GCM with a randomly generated key stored in the config directory.

Security notes:
- The encryption key is stored at CONFIG_DIR/.provider_key
- Key file permissions are set to 0600 (owner read/write only) on Unix
- If the key file is lost, stored credentials cannot be recovered
- The key file should be included in backup strategies
- The key is generated ONLY when the file does not exist. A file that exists
  but is unreadable or the wrong length raises ProviderKeyError and is never
  overwritten: one transient read error must not destroy every credential.
- Creation is atomic (temp file + fsync + hard link / O_EXCL), so concurrent
  first runs across threads and processes (gunicorn, monitor.py) agree on one key.
- The key is cached in memory after the first successful load.
"""
import os
import json
import tempfile
import threading
import time
from typing import Tuple, Dict, Any, Optional

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    AESGCM = None

from core.app_logging import app_logger

# Key file location (in config directory, persisted across restarts)
KEY_FILE = os.path.join(os.environ.get("CONFIG_DIR", "/config"), ".provider_key")
KEY_LENGTH = 32

# How long the loser of a creation race waits for the winner's write to land
# (O_EXCL fallback only; the hard-link publish is complete when it appears).
_RACE_READ_TIMEOUT = 2.0

# (key file path, key). Set only after a successful load or creation, so a
# later transient read failure cannot matter and a bad value is never pinned.
# Keyed on the path so a test that repoints KEY_FILE never sees a stale key.
_key_cache: Optional[Tuple[str, bytes]] = None
_key_lock = threading.Lock()


class ProviderKeyError(RuntimeError):
    """The key file exists but is unusable. It is never overwritten."""


def is_crypto_available() -> bool:
    """Check if cryptography library is available."""
    return CRYPTO_AVAILABLE


def _get_or_create_key() -> bytes:
    """
    Load or generate the 256-bit AES encryption key.

    Returns:
        32-byte encryption key

    Raises:
        RuntimeError: If cryptography library is not available
    """
    if not CRYPTO_AVAILABLE:
        raise RuntimeError(
            "cryptography library is required for credential encryption. "
            "Install with: pip install cryptography"
        )

    global _key_cache
    path = KEY_FILE
    cached = _key_cache
    if cached is not None and cached[0] == path:
        return cached[1]

    with _key_lock:
        cached = _key_cache
        if cached is not None and cached[0] == path:
            return cached[1]

        key = _read_existing_key(path)
        if key is None:
            key = _create_key(path)
        # Only a validated 32-byte key ever reaches the cache.
        _key_cache = (path, key)
        return key


def _reset_key_cache() -> None:
    """Forget the in-memory key (tests, and after a rotation)."""
    global _key_cache
    with _key_lock:
        _key_cache = None


def _read_existing_key(path: str) -> Optional[bytes]:
    """Read the key file.

    Returns None ONLY when the file does not exist. A file that exists but
    cannot be read, or holds anything other than exactly 32 bytes, raises
    ProviderKeyError and is left untouched: regenerating would make every
    stored credential permanently undecryptable.
    """
    try:
        with open(path, "rb") as f:
            key = f.read(KEY_LENGTH + 1)
    except FileNotFoundError:
        return None
    except OSError as e:
        app_logger.error(
            f"Provider encryption key {path} exists but could not be read: {e}. "
            "Stored provider credentials cannot be decrypted until it is readable; "
            "the file has not been modified."
        )
        raise ProviderKeyError(f"Cannot read provider key file {path}: {e}") from e

    if len(key) != KEY_LENGTH:
        app_logger.error(
            f"Provider encryption key {path} has an invalid length "
            f"({len(key)} bytes, expected {KEY_LENGTH}). It has not been modified; "
            "restore it from a backup, or delete it to start over (stored provider "
            "credentials will then have to be re-entered)."
        )
        raise ProviderKeyError(
            f"Provider key file {path} is {len(key)} bytes, expected {KEY_LENGTH}"
        )
    return key


def _read_winner_key(path: str) -> bytes:
    """Read a key another thread/process just published.

    Only reached by the loser of a creation race. With the hard-link publish
    the file is complete the instant it appears; the O_EXCL fallback can be
    caught mid-write, so a short read is retried briefly before it is
    treated as the error it would be anywhere else.
    """
    deadline = time.monotonic() + _RACE_READ_TIMEOUT
    while True:
        try:
            key = _read_existing_key(path)
            if key is not None:
                return key
        except ProviderKeyError:
            if time.monotonic() >= deadline:
                raise
        if time.monotonic() >= deadline:
            raise ProviderKeyError(f"Provider key file {path} vanished during creation")
        time.sleep(0.02)


def _fsync_dir(directory: str) -> None:
    """Best-effort fsync of a directory entry (no-op where unsupported)."""
    if os.name == "nt":
        return
    try:
        fd = os.open(directory or ".", os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _create_key(path: str) -> bytes:
    """Generate a key and publish it at *path* without clobbering an existing one.

    The key is written to a temp file in the same directory and fsynced, then
    hard-linked onto the final name (atomic, and fails if the name exists).
    Where hard links are unsupported (some CIFS/FUSE mounts) the final name is
    created with O_CREAT|O_EXCL instead. Either way the loser of a race reads
    and returns the winner's key.
    """
    key_dir = os.path.dirname(path)
    if key_dir:
        os.makedirs(key_dir, exist_ok=True)

    key = AESGCM.generate_key(bit_length=256)

    # mkstemp creates the file 0600 on POSIX.
    fd, tmp_path = tempfile.mkstemp(
        prefix=".provider_key.", suffix=".tmp", dir=key_dir or None
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())

        try:
            os.link(tmp_path, path)
            published = True
        except FileExistsError:
            published = False
        except (OSError, NotImplementedError, AttributeError) as e:
            app_logger.debug(f"Hard link unavailable for provider key ({e}); using O_EXCL")
            published = _publish_exclusive(path, key)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    if not published:
        app_logger.info("Provider encryption key was created concurrently; using it")
        return _read_winner_key(path)

    _fsync_dir(key_dir)
    app_logger.info("Generated new encryption key for provider credentials")
    return key


def _publish_exclusive(path: str, key: bytes) -> bool:
    """Create *path* with O_EXCL and write *key*. False if it already exists."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(key)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        # Never leave a partial key behind for the next reader to reject.
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return True


def encrypt_credentials(credentials: Dict[str, Any]) -> Tuple[bytes, bytes]:
    """
    Encrypt a credentials dictionary using AES-256-GCM.

    Args:
        credentials: Dictionary of credential data (api_key, username, password, etc.)

    Returns:
        Tuple of (ciphertext, nonce) as bytes

    Raises:
        RuntimeError: If cryptography library is not available
    """
    if not CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography library is required for encryption")

    key = _get_or_create_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)  # 96-bit nonce for GCM
    plaintext = json.dumps(credentials).encode("utf-8")
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    return ciphertext, nonce


def decrypt_credentials(ciphertext: bytes, nonce: bytes) -> Dict[str, Any]:
    """
    Decrypt credentials from AES-256-GCM encrypted data.

    Args:
        ciphertext: Encrypted credential data
        nonce: Nonce used during encryption

    Returns:
        Decrypted credentials dictionary

    Raises:
        RuntimeError: If cryptography library is not available
        cryptography.exceptions.InvalidTag: If decryption fails (wrong key or tampered data)
    """
    if not CRYPTO_AVAILABLE:
        raise RuntimeError("cryptography library is required for decryption")

    key = _get_or_create_key()
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return json.loads(plaintext.decode("utf-8"))


def mask_credential(value: Optional[str], show_chars: int = 4) -> str:
    """
    Mask a credential value for safe display (e.g., "abcd...wxyz").

    Args:
        value: The credential value to mask
        show_chars: Number of characters to show at start and end

    Returns:
        Masked string safe for display
    """
    if not value:
        return ""
    if len(value) <= show_chars * 2:
        return "***"
    return f"{value[:show_chars]}...{value[-show_chars:]}"


def mask_credentials_dict(credentials: Dict[str, Any]) -> Dict[str, Any]:
    """
    Mask all string values in a credentials dictionary.

    Args:
        credentials: Dictionary of credential data

    Returns:
        Dictionary with masked values
    """
    # Fields that are not secrets and should be shown in full so the user can
    # see/confirm what they configured (e.g. a local database file path).
    non_secret_keys = {"database_path"}

    masked = {}
    for key, value in credentials.items():
        if key in non_secret_keys:
            masked[key] = value
        elif isinstance(value, str):
            masked[key] = mask_credential(value)
        elif value is None:
            masked[key] = None
        else:
            masked[key] = value
    return masked


def rotate_key() -> bool:
    """
    Rotate the encryption key (for security purposes).

    Note: This requires re-encrypting all stored credentials.
    Call this function only when you have access to all decrypted credentials.

    Returns:
        True if key was rotated successfully
    """
    global _key_cache
    if not CRYPTO_AVAILABLE:
        app_logger.error("Cannot rotate key: cryptography library not available")
        return False

    try:
        # Delete the old key file
        # Deliberate, explicit replacement: the only place a key is discarded.
        with _key_lock:
            if os.path.exists(KEY_FILE):
                os.remove(KEY_FILE)
            _key_cache = None

        # Generate new key (will be created on next _get_or_create_key call)
        _get_or_create_key()
        app_logger.info("Encryption key rotated successfully")
        return True
    except Exception as e:
        app_logger.error(f"Failed to rotate encryption key: {e}")
        return False
