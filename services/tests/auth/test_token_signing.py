"""Tests for the shared token signing-key derivation.

Verifies the dedicated-secret override and the backward-compatible
database-URL fallback (so upgrading without configuring the secret does
not invalidate in-flight runner / callback tokens).
"""

import hashlib
from unittest.mock import patch

import pytest

from terrapod.auth import token_signing


@pytest.fixture(autouse=True)
def _reset():
    token_signing._reset_cache_for_tests()
    yield
    token_signing._reset_cache_for_tests()


def test_empty_key_falls_back_to_database_url():
    """No dedicated secret → key == sha256(database_url) (unchanged behaviour)."""
    db_url = "postgresql+asyncpg://u:p@h/db"
    with patch("terrapod.config.settings") as s:
        s.token_signing_key = ""
        s.database_url = db_url
        key = token_signing.get_token_signing_key()
    assert key == hashlib.sha256(db_url.encode()).digest()


def test_dedicated_secret_overrides_database_url():
    """A configured secret is used instead of the DB URL."""
    with patch("terrapod.config.settings") as s:
        s.token_signing_key = "super-secret-signing-key"
        s.database_url = "postgresql+asyncpg://u:p@h/db"
        key = token_signing.get_token_signing_key()
    assert key == hashlib.sha256(b"super-secret-signing-key").digest()
    # And it is distinct from the DB-URL-derived key.
    assert key != hashlib.sha256(b"postgresql+asyncpg://u:p@h/db").digest()


def test_whitespace_only_key_falls_back():
    """A whitespace-only secret is treated as unset."""
    db_url = "postgresql+asyncpg://u:p@h/db"
    with patch("terrapod.config.settings") as s:
        s.token_signing_key = "   "
        s.database_url = db_url
        key = token_signing.get_token_signing_key()
    assert key == hashlib.sha256(db_url.encode()).digest()


def test_key_is_cached():
    """Derivation is cached after first call."""
    with patch("terrapod.config.settings") as s:
        s.token_signing_key = "k1"
        s.database_url = "postgresql+asyncpg://u:p@h/db"
        first = token_signing.get_token_signing_key()
    # Second call must not re-read settings (patch removed) and returns cached.
    second = token_signing.get_token_signing_key()
    assert first == second
