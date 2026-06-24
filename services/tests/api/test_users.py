"""Tests for user management endpoints."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from terrapod.api.dependencies import AuthenticatedUser


def _admin_user():
    return AuthenticatedUser(
        email="admin@example.com",
        display_name="Admin",
        roles=["admin"],
        provider_name="local",
        auth_method="session",
    )


def _audit_user():
    return AuthenticatedUser(
        email="auditor@example.com",
        display_name="Auditor",
        roles=["audit"],
        provider_name="local",
        auth_method="session",
    )


def _regular_user():
    return AuthenticatedUser(
        email="user@example.com",
        display_name="User",
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )


class TestRequireAdminOrAudit:
    async def test_admin_passes(self):
        from terrapod.api.dependencies import require_admin_or_audit

        result = await require_admin_or_audit(user=_admin_user())
        assert result.email == "admin@example.com"

    async def test_audit_passes(self):
        from terrapod.api.dependencies import require_admin_or_audit

        result = await require_admin_or_audit(user=_audit_user())
        assert result.email == "auditor@example.com"

    async def test_regular_user_rejected(self):
        from terrapod.api.dependencies import require_admin_or_audit

        with pytest.raises(HTTPException) as exc_info:
            await require_admin_or_audit(user=_regular_user())
        assert exc_info.value.status_code == 403


class TestRequireAdmin:
    async def test_admin_passes(self):
        from terrapod.api.dependencies import require_admin

        result = await require_admin(user=_admin_user())
        assert result.email == "admin@example.com"

    async def test_audit_rejected(self):
        from terrapod.api.dependencies import require_admin

        with pytest.raises(HTTPException) as exc_info:
            await require_admin(user=_audit_user())
        assert exc_info.value.status_code == 403

    async def test_regular_user_rejected(self):
        from terrapod.api.dependencies import require_admin

        with pytest.raises(HTTPException) as exc_info:
            await require_admin(user=_regular_user())
        assert exc_info.value.status_code == 403


class TestOffboardingRevocation:
    """Deactivating or deleting a user must revoke not just web sessions but the
    cached token-role set AND every API token bound to the identity — otherwise
    a deactivated admin keeps cached admin roles (60s TTL) on API-token requests.
    (Detached/org-level tokens, bound_to NULL, are intentionally left alone.)"""

    @pytest.mark.asyncio
    @patch("terrapod.redis.client.get_redis_client")
    @patch("terrapod.auth.sessions.revoke_all_user_sessions", new_callable=AsyncMock)
    @patch("terrapod.auth.api_tokens.revoke_all_for_user", new_callable=AsyncMock)
    async def test_revokes_sessions_token_cache_and_api_tokens(
        self, mock_tokens, mock_sessions, mock_redis
    ):
        from terrapod.api.routers import users

        redis = AsyncMock()
        mock_redis.return_value = redis
        db = AsyncMock()

        await users._revoke_all_user_access(db, "gone@example.com")

        mock_sessions.assert_awaited_once_with("gone@example.com")
        mock_tokens.assert_awaited_once_with(db, "gone@example.com")
        redis.delete.assert_awaited_once()
        assert "gone@example.com" in redis.delete.call_args.args[0]
