"""Shared signing-key derivation for stateless HMAC tokens.

Runner tokens (`runner_tokens.py`) and run-task callback tokens
(`run_task_service.py`) are both stateless HMAC-SHA256 tokens verified
purely from their signature. They share one signing key.

Historically that key was derived solely from the database URL, which
couples database credentials to token-forgery resistance: anyone who
learns the DB URL can mint valid runner/callback tokens. To decouple
them, set a dedicated secret via `TERRAPOD_TOKEN_SIGNING_KEY` (Helm:
`api.tokenSigningKey`). When it is empty (the default), the key falls
back to `sha256(database_url)` exactly as before — so upgrading without
configuring the secret does NOT invalidate any in-flight token.
"""

import hashlib

_signing_key: bytes | None = None


def get_token_signing_key() -> bytes:
    """Return the process-wide 32-byte HMAC signing key.

    Uses the dedicated `token_signing_key` secret when configured,
    otherwise falls back to `sha256(database_url)` for backward
    compatibility. Cached after first derivation.
    """
    global _signing_key  # noqa: PLW0603
    if _signing_key is not None:
        return _signing_key
    from terrapod.config import settings

    configured = (settings.token_signing_key or "").strip()
    material = configured if configured else str(settings.database_url)
    _signing_key = hashlib.sha256(material.encode()).digest()
    return _signing_key


def _reset_cache_for_tests() -> None:
    """Clear the cached key (tests that mutate config only)."""
    global _signing_key  # noqa: PLW0603
    _signing_key = None
