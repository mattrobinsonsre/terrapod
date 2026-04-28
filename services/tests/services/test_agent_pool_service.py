"""Tests for agent pool service — join token generation and validation."""

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.services.agent_pool_service import (
    create_pool_token,
    generate_join_token,
    validate_join_token,
)

# ── generate_join_token ──────────────────────────────────────────────


class TestGenerateJoinToken:
    def test_returns_url_safe_token(self):
        """Raw token is URL-safe base64 (no +, /, or = characters)."""
        raw, _ = generate_join_token()
        # URL-safe base64 uses only alphanumerics, hyphens, and underscores
        assert all(c.isalnum() or c in "-_" for c in raw)

    def test_hash_matches_raw(self):
        """SHA-256 hash matches the raw token value."""
        raw, token_hash = generate_join_token()
        expected = hashlib.sha256(raw.encode()).hexdigest()
        assert token_hash == expected

    def test_uniqueness(self):
        """Each call produces a different token."""
        tokens = {generate_join_token()[0] for _ in range(100)}
        assert len(tokens) == 100


# ── validate_join_token ──────────────────────────────────────────────


def _mock_token(**overrides):
    """Create a mock AgentPoolToken."""
    token = MagicMock()
    token.is_revoked = overrides.get("is_revoked", False)
    token.expires_at = overrides.get("expires_at", None)
    token.max_uses = overrides.get("max_uses", None)
    token.use_count = overrides.get("use_count", 0)
    return token


class TestValidateJoinToken:
    @pytest.mark.asyncio
    async def test_valid_token(self):
        """A valid, non-expired, non-revoked token returns the record."""
        raw, token_hash = generate_join_token()
        mock_record = _mock_token()

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = mock_record
        db.execute.return_value = result_mock

        result = await validate_join_token(db, raw)
        assert result is mock_record

    @pytest.mark.asyncio
    async def test_unknown_token_returns_none(self):
        """A token not in the database returns None."""
        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        db.execute.return_value = result_mock

        result = await validate_join_token(db, "nonexistent-token")
        assert result is None

    @pytest.mark.asyncio
    async def test_revoked_token_returns_none(self):
        """A revoked token returns None."""
        raw, _ = generate_join_token()
        mock_record = _mock_token(is_revoked=True)

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = mock_record
        db.execute.return_value = result_mock

        result = await validate_join_token(db, raw)
        assert result is None

    @pytest.mark.asyncio
    async def test_expired_token_returns_none(self):
        """An expired token returns None."""
        raw, _ = generate_join_token()
        mock_record = _mock_token(expires_at=datetime.now(UTC) - timedelta(hours=1))

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = mock_record
        db.execute.return_value = result_mock

        result = await validate_join_token(db, raw)
        assert result is None

    @pytest.mark.asyncio
    async def test_max_uses_exceeded_returns_none(self):
        """A token that has reached max_uses returns None."""
        raw, _ = generate_join_token()
        mock_record = _mock_token(max_uses=5, use_count=5)

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = mock_record
        db.execute.return_value = result_mock

        result = await validate_join_token(db, raw)
        assert result is None

    @pytest.mark.asyncio
    async def test_none_expiry_is_valid(self):
        """A token with no expiry (None) is valid."""
        raw, _ = generate_join_token()
        mock_record = _mock_token(expires_at=None)

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = mock_record
        db.execute.return_value = result_mock

        result = await validate_join_token(db, raw)
        assert result is mock_record

    @pytest.mark.asyncio
    async def test_none_max_uses_is_valid(self):
        """A token with no max_uses (None) is valid regardless of use_count."""
        raw, _ = generate_join_token()
        mock_record = _mock_token(max_uses=None, use_count=999)

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = mock_record
        db.execute.return_value = result_mock

        result = await validate_join_token(db, raw)
        assert result is mock_record

    @pytest.mark.asyncio
    async def test_under_max_uses_is_valid(self):
        """A token with use_count < max_uses is valid."""
        raw, _ = generate_join_token()
        mock_record = _mock_token(max_uses=10, use_count=3)

        db = AsyncMock()
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = mock_record
        db.execute.return_value = result_mock

        result = await validate_join_token(db, raw)
        assert result is mock_record


# ── create_pool_token defaults ──────────────────────────────────────


def _capture_pool_token_call() -> AsyncMock:
    """Build an AsyncSession mock that records the AgentPoolToken passed to add()."""
    db = AsyncMock()
    captured = {}

    def add(token):
        captured["token"] = token

    db.add = MagicMock(side_effect=add)
    db.flush = AsyncMock()
    db._captured = captured
    return db


class TestCreatePoolToken:
    @pytest.mark.asyncio
    async def test_defaults_max_uses_to_two(self):
        """Default max_uses comes from settings.agent_pools (2 in the bundled config)."""
        db = _capture_pool_token_call()

        await create_pool_token(
            db,
            pool_id=uuid.uuid4(),
            description="test",
            created_by="alice@example.com",
        )

        assert db._captured["token"].max_uses == 2

    @pytest.mark.asyncio
    async def test_defaults_expiry_to_one_hour_from_now(self):
        """Default expires_at is now + default_join_token_ttl_seconds (3600)."""
        db = _capture_pool_token_call()

        before = datetime.now(UTC)
        await create_pool_token(
            db,
            pool_id=uuid.uuid4(),
            description="test",
            created_by="alice@example.com",
        )
        after = datetime.now(UTC)

        expires_at = db._captured["token"].expires_at
        # Should land within (now+1h - small skew, now+1h + small skew)
        assert before + timedelta(seconds=3590) <= expires_at <= after + timedelta(seconds=3610)

    @pytest.mark.asyncio
    async def test_explicit_max_uses_none_means_unlimited(self):
        """Caller passing max_uses=None (vs default sentinel) opts out of the cap."""
        db = _capture_pool_token_call()

        await create_pool_token(
            db,
            pool_id=uuid.uuid4(),
            description="bootstrap",
            created_by="op@example.com",
            max_uses=None,
        )

        assert db._captured["token"].max_uses is None

    @pytest.mark.asyncio
    async def test_explicit_expires_at_none_means_no_expiry(self):
        """Caller passing expires_at=None opts out of the default TTL."""
        db = _capture_pool_token_call()

        await create_pool_token(
            db,
            pool_id=uuid.uuid4(),
            description="bootstrap",
            created_by="op@example.com",
            expires_at=None,
        )

        assert db._captured["token"].expires_at is None

    @pytest.mark.asyncio
    async def test_explicit_values_override_defaults(self):
        """Caller-supplied max_uses + expires_at win over the config defaults."""
        db = _capture_pool_token_call()
        custom_expiry = datetime.now(UTC) + timedelta(days=7)

        await create_pool_token(
            db,
            pool_id=uuid.uuid4(),
            description="weekly-rotated",
            created_by="op@example.com",
            max_uses=10,
            expires_at=custom_expiry,
        )

        assert db._captured["token"].max_uses == 10
        assert db._captured["token"].expires_at == custom_expiry
