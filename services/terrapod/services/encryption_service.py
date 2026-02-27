"""Fernet symmetric encryption for sensitive values and state files.

Uses AES-128-CBC + HMAC-SHA256 via the cryptography library's Fernet.
Master key sourced from TERRAPOD_ENCRYPTION__KEY environment variable.
"""

from cryptography.fernet import Fernet, InvalidToken

from terrapod.logging_config import get_logger

logger = get_logger(__name__)

_fernet: Fernet | None = None
_initialized: bool = False


def init_encryption() -> None:
    """Initialize encryption from config. Call during API lifespan startup."""
    global _fernet, _initialized  # noqa: PLW0603

    from terrapod.config import settings

    key = settings.encryption_key
    if not key:
        logger.warning(
            "No encryption key configured (TERRAPOD_ENCRYPTION__KEY). "
            "Sensitive variables will be rejected."
        )
        _fernet = None
        _initialized = True
        return

    try:
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
        _initialized = True
        logger.info("Encryption initialized")
    except Exception as e:
        logger.error("Invalid encryption key", error=str(e))
        _fernet = None
        _initialized = True


def is_encryption_available() -> bool:
    """Check if encryption is configured and available."""
    return _fernet is not None


def encrypt_value(plaintext: str) -> str:
    """Encrypt a plaintext string. Returns base64-encoded Fernet ciphertext."""
    if _fernet is None:
        raise RuntimeError(
            "Encryption not configured. Set TERRAPOD_ENCRYPTION__KEY to enable sensitive variables."
        )
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_value(ciphertext: str) -> str:
    """Decrypt a Fernet ciphertext string. Returns plaintext."""
    if _fernet is None:
        raise RuntimeError("Encryption not configured.")
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        raise ValueError("Failed to decrypt value — key mismatch or corrupted data") from None


# --- State file encryption (bytes ↔ bytes) ---

# Magic prefix to distinguish encrypted state from legacy plaintext state.
# Allows transparent reading of state files written before encryption was enabled.
_STATE_MAGIC = b"TPENC1:"


def encrypt_state(plaintext: bytes) -> bytes:
    """Encrypt a state file. Returns prefixed Fernet ciphertext bytes.

    If encryption is not configured, returns plaintext unchanged (no-op).
    This allows Terrapod to run without an encryption key for development,
    while ensuring state is always encrypted in production.
    """
    if _fernet is None:
        return plaintext
    return _STATE_MAGIC + _fernet.encrypt(plaintext)


def decrypt_state(data: bytes) -> bytes:
    """Decrypt a state file. Handles both encrypted and legacy plaintext state.

    If the data starts with the encryption magic prefix, it is decrypted.
    Otherwise it is returned as-is (legacy plaintext state written before
    encryption was enabled).
    """
    if not data.startswith(_STATE_MAGIC):
        # Legacy plaintext state — return as-is
        return data
    if _fernet is None:
        raise RuntimeError(
            "State is encrypted but no encryption key is configured. "
            "Set TERRAPOD_ENCRYPTION__KEY to decrypt state files."
        )
    try:
        return _fernet.decrypt(data[len(_STATE_MAGIC) :])
    except InvalidToken:
        raise ValueError("Failed to decrypt state — key mismatch or corrupted data") from None
