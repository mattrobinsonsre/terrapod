"""Tests for API token system — create, validate, revoke, config-driven expiry, hash storage."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.api_tokens import (
    _generate_raw_token,
    _generate_token_id,
    create_api_token,
    hash_token,
    list_user_tokens,
    revoke_token,
    validate_api_token,
)


class TestTokenGeneration:
    def test_token_id_format(self):
        token_id = _generate_token_id()
        assert token_id.startswith("at-")
        assert len(token_id) > 5

    def test_raw_token_format(self):
        raw = _generate_raw_token()
        assert ".tpod." in raw
        parts = raw.split(".tpod.")
        assert len(parts) == 2
        assert len(parts[0]) > 5  # random_id
        assert len(parts[1]) > 20  # random_secret

    def test_raw_token_is_unique(self):
        t1 = _generate_raw_token()
        t2 = _generate_raw_token()
        assert t1 != t2


class TestHashToken:
    def test_hash_is_deterministic(self):
        raw = "test-token-value.tpod.secret123"
        h1 = hash_token(raw)
        h2 = hash_token(raw)
        assert h1 == h2

    def test_hash_is_hex_sha256(self):
        h = hash_token("test")
        assert len(h) == 64  # SHA-256 produces 64 hex chars
        assert all(c in "0123456789abcdef" for c in h)

    def test_different_tokens_different_hashes(self):
        h1 = hash_token("token-a")
        h2 = hash_token("token-b")
        assert h1 != h2


class TestCreateAPIToken:
    @pytest.fixture
    def mock_db(self):
        db = AsyncMock(spec=AsyncSession)
        return db

    async def test_create_returns_model_and_raw_token(self, mock_db):
        api_token, raw_token = await create_api_token(
            db=mock_db,
            user_email="test@example.com",
            description="test token",
        )

        assert api_token.user_email == "test@example.com"
        assert api_token.description == "test token"
        assert api_token.token_type == "user"
        assert api_token.id.startswith("at-")
        assert ".tpod." in raw_token
        # Verify hash matches
        assert api_token.token_hash == hash_token(raw_token)
        mock_db.add.assert_called_once()
        mock_db.flush.assert_called_once()

    async def test_create_org_token(self, mock_db):
        api_token, _ = await create_api_token(
            db=mock_db,
            user_email="test@example.com",
            token_type="organization",
            org_name="default",
        )
        assert api_token.token_type == "organization"
        assert api_token.org_name == "default"


class TestValidateAPIToken:
    @pytest.fixture
    def mock_db(self):
        return AsyncMock(spec=AsyncSession)

    @patch("terrapod.auth.api_tokens.settings")
    async def test_validate_valid_token(self, mock_settings, mock_db):
        mock_settings.auth.api_token_max_ttl_hours = 0
        raw_token = "abc123.tpod.secret456"
        mock_token = MagicMock()
        mock_token.last_used_at = None
        mock_token.id = "at-test"
        mock_token.created_at = datetime.now(UTC)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_token
        mock_db.execute.return_value = mock_result

        result = await validate_api_token(mock_db, raw_token)

        assert result is mock_token
        # Should update last_used_at since it was None
        assert mock_db.execute.call_count == 2  # select + update

    async def test_validate_nonexistent_token(self, mock_db):
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await validate_api_token(mock_db, "nonexistent.tpod.token")
        assert result is None

    @patch("terrapod.auth.api_tokens.settings")
    async def test_validate_rejects_token_past_max_ttl(self, mock_settings, mock_db):
        """Token created 2 hours ago is rejected when max TTL is 1 hour."""
        mock_settings.auth.api_token_max_ttl_hours = 1
        mock_token = MagicMock()
        mock_token.created_at = datetime.now(UTC) - timedelta(hours=2)
        mock_token.id = "at-expired"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_token
        mock_db.execute.return_value = mock_result

        result = await validate_api_token(mock_db, "old.tpod.token")
        assert result is None

    @patch("terrapod.auth.api_tokens.settings")
    async def test_validate_accepts_token_within_max_ttl(self, mock_settings, mock_db):
        """Token created 1 hour ago is accepted when max TTL is 24 hours."""
        mock_settings.auth.api_token_max_ttl_hours = 24
        mock_token = MagicMock()
        mock_token.created_at = datetime.now(UTC) - timedelta(hours=1)
        mock_token.last_used_at = None
        mock_token.id = "at-valid"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_token
        mock_db.execute.return_value = mock_result

        result = await validate_api_token(mock_db, "valid.tpod.token")
        assert result is mock_token

    @patch("terrapod.auth.api_tokens.settings")
    async def test_validate_no_max_ttl_never_expires(self, mock_settings, mock_db):
        """With max TTL=0, tokens never expire regardless of age."""
        mock_settings.auth.api_token_max_ttl_hours = 0
        mock_token = MagicMock()
        mock_token.created_at = datetime(2020, 1, 1, tzinfo=UTC)
        mock_token.last_used_at = None
        mock_token.id = "at-ancient"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_token
        mock_db.execute.return_value = mock_result

        result = await validate_api_token(mock_db, "ancient.tpod.token")
        assert result is mock_token

    @patch("terrapod.auth.api_tokens.settings")
    async def test_validate_skips_last_used_update_when_recent(self, mock_settings, mock_db):
        """last_used_at is not updated if it was updated less than 60s ago."""
        mock_settings.auth.api_token_max_ttl_hours = 0
        now = datetime.now(UTC)
        mock_token = MagicMock()
        mock_token.created_at = now
        mock_token.last_used_at = now - timedelta(seconds=30)
        mock_token.id = "at-test"

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_token
        mock_db.execute.return_value = mock_result

        await validate_api_token(mock_db, "recent.tpod.token")
        # Only the select query, no update
        assert mock_db.execute.call_count == 1


class TestListUserTokens:
    async def test_list_returns_tokens(self):
        mock_db = AsyncMock(spec=AsyncSession)
        mock_tokens = [MagicMock(id="at-1"), MagicMock(id="at-2")]

        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = mock_tokens
        mock_db.execute.return_value = mock_result

        tokens = await list_user_tokens(mock_db, "test@example.com")
        assert len(tokens) == 2


class TestRevokeToken:
    async def test_revoke_existing_token(self):
        mock_db = AsyncMock(spec=AsyncSession)
        mock_token = MagicMock(id="at-123")

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_token
        mock_db.execute.return_value = mock_result

        result = await revoke_token(mock_db, "at-123")
        assert result is True
        mock_db.delete.assert_called_once_with(mock_token)
        mock_db.flush.assert_called_once()

    async def test_revoke_nonexistent_token(self):
        mock_db = AsyncMock(spec=AsyncSession)

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        result = await revoke_token(mock_db, "at-nonexistent")
        assert result is False
        mock_db.delete.assert_not_called()
