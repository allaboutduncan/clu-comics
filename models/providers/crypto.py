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
"""
import os
import json
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

    if os.path.exists(KEY_FILE):
        try:
            with open(KEY_FILE, "rb") as f:
                key = f.read()
                if len(key) == 32:  # Valid 256-bit key
                    return key
                app_logger.warning("Invalid key file size, regenerating")
        except Exception as e:
            app_logger.warning(f"Error reading key file: {e}, regenerating")

    # Generate new key
    key = AESGCM.generate_key(bit_length=256)

    # Ensure config directory exists
    key_dir = os.path.dirname(KEY_FILE)
    if key_dir and not os.path.exists(key_dir):
        os.makedirs(key_dir, exist_ok=True)

    # Write with restricted permissions (owner read/write only)
    try:
        # Use os.open for atomic creation with permissions on Unix
        fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key)
    except (OSError, AttributeError) as e:
        # Fallback for Windows (doesn't support mode in os.open the same way)
        app_logger.debug(f"Using fallback key write method: {e}")
        with open(KEY_FILE, "wb") as f:
            f.write(key)

    app_logger.info("Generated new encryption key for provider credentials")
    return key


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
    masked = {}
    for key, value in credentials.items():
        if isinstance(value, str):
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
    if not CRYPTO_AVAILABLE:
        app_logger.error("Cannot rotate key: cryptography library not available")
        return False

    try:
        # Delete the old key file
        if os.path.exists(KEY_FILE):
            os.remove(KEY_FILE)

        # Generate new key (will be created on next _get_or_create_key call)
        _get_or_create_key()
        app_logger.info("Encryption key rotated successfully")
        return True
    except Exception as e:
        app_logger.error(f"Failed to rotate encryption key: {e}")
        return False
