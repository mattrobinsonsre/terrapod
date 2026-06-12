"""Tests for the API token system (#495).

Covers token generation/hashing, kind-aware + basis-aware expiry
(token_expires_at), create/validate with idle-login rejection, rotate, and
revoke-all-for-user.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.api_tokens import (
    _generate_raw_token,
    _generate_token_id,
    create_api_token,
    hash_token,
    list_user_tokens,
    revoke_all_for_user,
    revoke_token,
    rotate_token,
    token_expires_at,
    validate_api_token,
)


def _tok(**kw):
    """A lightweight token stub for the pure expiry helper."""
    base = {
        "kind": "interactive",
        "lifespan_hours": None,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "rotated_at": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


class TestTokenGeneration:
    def test_token_id_format(self):
        assert _generate_token_id().startswith("at-")

    def test_raw_token_format(self):
        raw = _generate_raw_token()
        assert ".tpod." in raw
        a, b = raw.split(".tpod.")
        assert len(a) > 5 and len(b) > 20

    def test_raw_token_is_unique(self):
        assert _generate_raw_token() != _generate_raw_token()


class TestHashToken:
    def test_hash_is_deterministic(self):
        assert hash_token("x.tpod.y") == hash_token("x.tpod.y")

    def test_hash_is_hex_sha256(self):
        h = hash_token("test")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_different_tokens_different_hashes(self):
        assert hash_token("a") != hash_token("b")


class TestTokenExpiry:
    """Pure token_expires_at(): kind-aware cap + rotated_at-or-created_at basis."""

    @patch("terrapod.auth.api_tokens.settings")
    def test_interactive_zero_global_is_unlimited(self, ms):
        ms.auth.api_token_max_ttl_hours = 0
        assert token_expires_at(_tok(kind="interactive")) is None

    @patch("terrapod.auth.api_tokens.settings")
    def test_interactive_uses_global_cap(self, ms):
        ms.auth.api_token_max_ttl_hours = 24
        created = datetime(2026, 1, 1, tzinfo=UTC)
        assert token_expires_at(
            _tok(kind="interactive", created_at=created)
        ) == created + timedelta(hours=24)

    @patch("terrapod.auth.api_tokens.settings")
    def test_per_token_lifespan_takes_precedence(self, ms):
        ms.auth.api_token_max_ttl_hours = 0  # global unlimited
        created = datetime(2026, 1, 1, tzinfo=UTC)
        # an explicit lifespan still expires even when the global is unlimited
        assert token_expires_at(
            _tok(kind="interactive", lifespan_hours=5, created_at=created)
        ) == created + timedelta(hours=5)

    @patch("terrapod.auth.api_tokens.settings")
    def test_service_always_expires_even_when_global_zero(self, ms):
        # service cap misconfigured to 0 -> falls back, never unbounded
        ms.auth.service_token_max_ttl_hours = 0
        exp = token_expires_at(_tok(kind="service_detached"))
        assert exp is not None

    @patch("terrapod.auth.api_tokens.settings")
    def test_service_uses_service_cap_not_interactive(self, ms):
        ms.auth.service_token_max_ttl_hours = 10
        ms.auth.api_token_max_ttl_hours = 999
        created = datetime(2026, 1, 1, tzinfo=UTC)
        assert token_expires_at(
            _tok(kind="service_bound", created_at=created)
        ) == created + timedelta(hours=10)

    @patch("terrapod.auth.api_tokens.settings")
    def test_rotated_at_is_the_basis(self, ms):
        ms.auth.api_token_max_ttl_hours = 24
        created = datetime(2026, 1, 1, tzinfo=UTC)
        rotated = datetime(2026, 6, 1, tzinfo=UTC)
        # expiry measured from rotated_at, not created_at
        assert token_expires_at(
            _tok(kind="interactive", created_at=created, rotated_at=rotated)
        ) == rotated + timedelta(hours=24)


class TestCreateAPIToken:
    @pytest.fixture
    def mock_db(self):
        return AsyncMock(spec=AsyncSession)

    @patch("terrapod.auth.api_tokens.settings")
    async def test_create_interactive(self, ms, mock_db):
        ms.auth.api_token_max_ttl_hours = 8760
        token, raw = await create_api_token(
            db=mock_db,
            bound_to="test@example.com",
            created_by="test@example.com",
            kind="interactive",
            description="t",
        )
        assert token.bound_to == "test@example.com"
        assert token.created_by == "test@example.com"
        assert token.kind == "interactive"
        assert token.id.startswith("at-")
        assert token.token_hash == hash_token(raw)
        mock_db.add.assert_called_once()
        mock_db.flush.assert_called_once()

    @patch("terrapod.auth.api_tokens.settings")
    async def test_create_service_bound_with_pinned_roles(self, ms, mock_db):
        ms.auth.service_token_max_ttl_hours = 8760
        token, _ = await create_api_token(
            db=mock_db,
            bound_to="dev@example.com",
            created_by="dev@example.com",
            kind="service_bound",
            pinned_roles=["plan-only"],
        )
        assert token.kind == "service_bound"
        assert token.pinned_roles == ["plan-only"]

    @patch("terrapod.auth.api_tokens.settings")
    async def test_lifespan_clamped_to_cap(self, ms, mock_db):
        ms.auth.api_token_max_ttl_hours = 100
        token, _ = await create_api_token(
            db=mock_db,
            bound_to="a@b.com",
            created_by="a@b.com",
            lifespan_hours=9999,
        )
        assert token.lifespan_hours == 100


class TestValidateAPIToken:
    @pytest.fixture
    def mock_db(self):
        return AsyncMock(spec=AsyncSession)

    def _result_for(self, token):
        r = MagicMock()
        r.scalar_one_or_none.return_value = token
        return r

    @patch("terrapod.auth.api_tokens.settings")
    async def test_valid_detached_token_returned(self, ms, mock_db):
        # detached is exempt from idle — isolates the happy path
        ms.auth.service_token_max_ttl_hours = 8760
        token = MagicMock(
            kind="service_detached",
            created_at=datetime.now(UTC),
            rotated_at=None,
            last_used_at=None,
            lifespan_hours=None,
            id="at-d",
        )
        mock_db.execute.return_value = self._result_for(token)
        assert await validate_api_token(mock_db, "x.tpod.y") is token
        assert mock_db.execute.call_count == 2  # select + last_used update

    async def test_nonexistent_token(self, mock_db):
        mock_db.execute.return_value = self._result_for(None)
        assert await validate_api_token(mock_db, "no.tpod.token") is None

    @patch("terrapod.auth.api_tokens.settings")
    async def test_expired_token_rejected(self, ms, mock_db):
        ms.auth.service_token_max_ttl_hours = 1
        token = MagicMock(
            kind="service_detached",
            created_at=datetime.now(UTC) - timedelta(hours=2),
            rotated_at=None,
            lifespan_hours=None,
            id="at-old",
        )
        mock_db.execute.return_value = self._result_for(token)
        assert await validate_api_token(mock_db, "old.tpod.token") is None

    @patch("terrapod.auth.api_tokens._bound_token_owner_active", new_callable=AsyncMock)
    @patch("terrapod.auth.api_tokens.settings")
    async def test_bound_token_rejected_when_owner_idle(self, ms, owner_active, mock_db):
        ms.auth.service_token_max_ttl_hours = 8760
        owner_active.return_value = False
        token = MagicMock(
            kind="service_bound",
            bound_to="gone@example.com",
            created_at=datetime.now(UTC),
            rotated_at=None,
            lifespan_hours=None,
            id="at-idle",
        )
        mock_db.execute.return_value = self._result_for(token)
        assert await validate_api_token(mock_db, "idle.tpod.token") is None

    @patch("terrapod.auth.api_tokens._bound_token_owner_active", new_callable=AsyncMock)
    @patch("terrapod.auth.api_tokens.settings")
    async def test_bound_token_accepted_when_owner_active(self, ms, owner_active, mock_db):
        ms.auth.api_token_max_ttl_hours = 0
        owner_active.return_value = True
        token = MagicMock(
            kind="interactive",
            bound_to="here@example.com",
            created_at=datetime.now(UTC),
            rotated_at=None,
            last_used_at=None,
            lifespan_hours=None,
            id="at-ok",
        )
        mock_db.execute.return_value = self._result_for(token)
        assert await validate_api_token(mock_db, "ok.tpod.token") is token

    @patch("terrapod.auth.api_tokens.settings")
    async def test_skips_last_used_update_when_recent(self, ms, mock_db):
        ms.auth.service_token_max_ttl_hours = 8760
        now = datetime.now(UTC)
        token = MagicMock(
            kind="service_detached",
            created_at=now,
            rotated_at=None,
            last_used_at=now - timedelta(seconds=30),
            lifespan_hours=None,
            id="at-r",
        )
        mock_db.execute.return_value = self._result_for(token)
        await validate_api_token(mock_db, "recent.tpod.token")
        assert mock_db.execute.call_count == 1  # select only, no update


class TestRotateToken:
    async def test_rotate_sets_new_hash_and_rotated_at(self):
        mock_db = AsyncMock(spec=AsyncSession)
        token = MagicMock(id="at-1", token_hash="oldhash", rotated_at=None, kind="service_bound")
        r = MagicMock()
        r.scalar_one_or_none.return_value = token
        mock_db.execute.return_value = r

        result = await rotate_token(mock_db, "at-1")
        assert result is not None
        returned, raw = result
        assert returned is token
        assert token.token_hash == hash_token(raw)
        assert token.token_hash != "oldhash"
        assert token.rotated_at is not None

    async def test_rotate_missing_token(self):
        mock_db = AsyncMock(spec=AsyncSession)
        r = MagicMock()
        r.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = r
        assert await rotate_token(mock_db, "at-x") is None


class TestRevokeAllForUser:
    async def test_revoke_all_returns_count(self):
        mock_db = AsyncMock(spec=AsyncSession)
        r = MagicMock()
        r.rowcount = 3
        mock_db.execute.return_value = r
        count = await revoke_all_for_user(mock_db, "leaver@example.com")
        assert count == 3
        mock_db.flush.assert_awaited_once()


class TestListUserTokens:
    async def test_list_returns_tokens(self):
        mock_db = AsyncMock(spec=AsyncSession)
        r = MagicMock()
        r.scalars.return_value.all.return_value = [MagicMock(id="at-1"), MagicMock(id="at-2")]
        mock_db.execute.return_value = r
        assert len(await list_user_tokens(mock_db, "test@example.com")) == 2


class TestRevokeToken:
    async def test_revoke_existing(self):
        mock_db = AsyncMock(spec=AsyncSession)
        token = MagicMock(id="at-123")
        r = MagicMock()
        r.scalar_one_or_none.return_value = token
        mock_db.execute.return_value = r
        assert await revoke_token(mock_db, "at-123") is True
        mock_db.delete.assert_called_once_with(token)

    async def test_revoke_nonexistent(self):
        mock_db = AsyncMock(spec=AsyncSession)
        r = MagicMock()
        r.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = r
        assert await revoke_token(mock_db, "at-x") is False
        mock_db.delete.assert_not_called()
