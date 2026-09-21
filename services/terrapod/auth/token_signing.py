"""Shared signing-key derivation for stateless HMAC tokens.

Runner tokens (`runner_tokens.py`) and run-task callback tokens
(`run_task_service.py`) are both stateless HMAC-SHA256 tokens verified
purely from their signature. They share one signing key.

That key was derived solely from the database URL, which couples
database credentials to token-forgery resistance: anyone who learns the
DB URL can mint valid runner/callback tokens (GHSA-hc47-q72v-4vcm). Set
a dedicated secret via `TERRAPOD_TOKEN_SIGNING_KEY` (Helm:
`api.tokenSigningKey`).

**The fallback still works, and still derives the same key.** Changing
the derivation would invalidate every token already in flight, which on
a patch means killing running plans and applies — so the weak path is
reported rather than removed: a warning by default, fatal under
`require_strong_secrets`. Removing the fallback belongs to a MAJOR.

Note that capability URLs deliberately do NOT use this key — they derive
from the CA private key instead, so that fixing one advisory did not
depend on fixing this one. See `auth/capability_urls.py`.
"""

import hashlib

from terrapod.logging_config import get_logger

logger = get_logger(__name__)

_signing_key: bytes | None = None

#: Set once the fallback has been reported, so a warning that is true for the
#: life of the process is not repeated on every token mint.
_reported: bool = False


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
    report_key_strength(configured, strict=bool(settings.require_strong_secrets))
    material = configured if configured else str(settings.database_url)
    _signing_key = hashlib.sha256(material.encode()).digest()
    return _signing_key


def report_key_strength(configured: str, *, strict: bool) -> str | None:
    """Name the problem with the signing key, log it, and raise under `strict`.

    Returns the problem description, or None when the key is a properly
    generated secret. Called from the derivation so it cannot be skipped, and
    again at startup so an operator sees it before the first token is minted.

    **The fallback is reported on identity, never on score** — the shipped
    default DSN measures as strong (see terrapod/secret_strength.py); it is weak
    because the database URL is shared with everything that talks to the
    database, not because it is guessable.
    """
    global _reported  # noqa: PLW0603
    from terrapod.secret_strength import describe_weakness

    if not configured:
        problem = (
            "token_signing_key is not set, so runner tokens, run-task callback "
            "tokens, download tickets and Slack link tokens are all signed with a "
            "key derived from the database URL. Anyone who learns that URL can mint "
            "them. Set token_signing_key (Helm: api.tokenSigningKey) to a generated "
            "secret: openssl rand -base64 32"
        )
    else:
        problem = describe_weakness(configured, name="token_signing_key")

    if problem is None:
        return None
    if strict:
        raise ValueError(problem)
    if not _reported:
        _reported = True
        logger.warning("weak token signing key", problem=problem)
    return problem


def _reset_cache_for_tests() -> None:
    """Clear the cached key (tests that mutate config only)."""
    global _signing_key, _reported  # noqa: PLW0603
    _signing_key = None
    _reported = False
