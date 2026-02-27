"""Tests for the unified auth dependency (session + API token)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from terrapod.api.dependencies import (
    AuthenticatedUser,
    get_current_user,
    require_admin,
    require_admin_or_audit,
)


class TestGetCurrentUser:
    @patch("terrapod.api.dependencies.get_session")
    @patch("terrapod.api.dependencies.validate_api_token")
    async def test_api_token_takes_priority(self, mock_validate_token, mock_get_session):
        """If token matches an API token, session is not checked."""
        mock_token = MagicMock()
        mock_token.user_email = "bot@example.com"
        mock_validate_token.return_value = mock_token

        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="test.tpod.token")
        mock_db = AsyncMock()

        user = await get_current_user(credentials=credentials, db=mock_db)

        assert user.email == "bot@example.com"
        assert user.auth_method == "api_token"
        mock_get_session.assert_not_called()

    @patch("terrapod.api.dependencies.get_session")
    @patch("terrapod.api.dependencies.validate_api_token")
    async def test_falls_back_to_session(self, mock_validate_token, mock_get_session):
        """If token is not an API token, check Redis sessions."""
        mock_validate_token.return_value = None

        mock_session = MagicMock()
        mock_session.email = "user@example.com"
        mock_session.display_name = "User"
        mock_session.roles = ["admin"]
        mock_session.provider_name = "local"
        mock_session.last_active_at = "2026-01-01T00:00:00+00:00"
        mock_get_session.return_value = mock_session

        # Mock _should_refresh_session to return False
        with patch("terrapod.api.dependencies._should_refresh_session", return_value=False):
            credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="session-token")
            mock_db = AsyncMock()

            user = await get_current_user(credentials=credentials, db=mock_db)

        assert user.email == "user@example.com"
        assert user.auth_method == "session"
        assert user.roles == ["admin"]

    @patch("terrapod.api.dependencies.get_session")
    @patch("terrapod.api.dependencies.validate_api_token")
    async def test_neither_match_raises_401(self, mock_validate_token, mock_get_session):
        """If neither API token nor session matches, raise 401."""
        mock_validate_token.return_value = None
        mock_get_session.return_value = None

        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="invalid-token")
        mock_db = AsyncMock()

        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=credentials, db=mock_db)

        assert exc_info.value.status_code == 401

    @patch("terrapod.api.dependencies.refresh_session")
    @patch("terrapod.api.dependencies._should_refresh_session", return_value=True)
    @patch("terrapod.api.dependencies.get_session")
    @patch("terrapod.api.dependencies.validate_api_token")
    async def test_session_refresh_on_stale(
        self,
        mock_validate_token,
        mock_get_session,
        mock_should_refresh,
        mock_refresh,
    ):
        """Stale sessions trigger a TTL refresh."""
        mock_validate_token.return_value = None

        mock_session = MagicMock()
        mock_session.email = "user@example.com"
        mock_session.display_name = None
        mock_session.roles = []
        mock_session.provider_name = "oidc"
        mock_get_session.return_value = mock_session

        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="stale-session")
        mock_db = AsyncMock()

        await get_current_user(credentials=credentials, db=mock_db)

        mock_refresh.assert_called_once_with("stale-session", mock_session)


class TestRequireAdmin:
    async def test_admin_passes(self):
        user = AuthenticatedUser(
            email="admin@example.com",
            display_name="Admin",
            roles=["admin"],
            provider_name="local",
            auth_method="session",
        )
        result = await require_admin(user=user)
        assert result.email == "admin@example.com"

    async def test_non_admin_raises_403(self):
        user = AuthenticatedUser(
            email="user@example.com",
            display_name="User",
            roles=["viewer"],
            provider_name="local",
            auth_method="session",
        )
        with pytest.raises(HTTPException) as exc_info:
            await require_admin(user=user)

        assert exc_info.value.status_code == 403


class TestRequireAdminOrAudit:
    async def test_admin_passes(self):
        user = AuthenticatedUser(
            email="admin@example.com",
            display_name=None,
            roles=["admin"],
            provider_name="local",
            auth_method="session",
        )
        result = await require_admin_or_audit(user=user)
        assert result is user

    async def test_audit_passes(self):
        user = AuthenticatedUser(
            email="auditor@example.com",
            display_name=None,
            roles=["audit"],
            provider_name="local",
            auth_method="session",
        )
        result = await require_admin_or_audit(user=user)
        assert result is user

    async def test_neither_raises_403(self):
        user = AuthenticatedUser(
            email="user@example.com",
            display_name=None,
            roles=["viewer", "dev"],
            provider_name="local",
            auth_method="session",
        )
        with pytest.raises(HTTPException) as exc_info:
            await require_admin_or_audit(user=user)

        assert exc_info.value.status_code == 403
