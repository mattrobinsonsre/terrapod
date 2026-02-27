"""Tests for role resolution during login (sso_service.process_login)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.sso import AuthenticatedIdentity
from terrapod.services.sso_service import LoginResult, process_login


class TestProcessLogin:
    @pytest.fixture
    def mock_db(self):
        db = AsyncMock(spec=AsyncSession)
        return db

    @pytest.fixture
    def sso_identity(self):
        return AuthenticatedIdentity(
            provider_name="oidc",
            subject="user-123",
            email="test@example.com",
            display_name="Test User",
            groups=["dev", "viewer"],
            raw_claims={"groups": ["terrapod:dev", "terrapod:viewer"]},
        )

    @patch("terrapod.services.sso_service.record_recent_user")
    async def test_sso_login_merges_groups_and_internal_roles(
        self, mock_record, mock_db, sso_identity
    ):
        """Groups from IDP + internal assignments are merged and deduplicated."""
        mock_record.return_value = None

        # Mock internal assignments returning ["admin"]
        role_result = MagicMock()
        role_result.scalars.return_value.all.return_value = ["admin"]
        platform_result = MagicMock()
        platform_result.scalars.return_value.all.return_value = []
        mock_db.execute.side_effect = [role_result, platform_result]

        result = await process_login(
            db=mock_db,
            identity=sso_identity,
            claims_rules=[],
        )

        assert isinstance(result, LoginResult)
        assert result.email == "test@example.com"
        assert result.display_name == "Test User"
        assert result.provider_name == "oidc"
        # Groups from IDP (dev, viewer) + internal (admin), sorted
        assert "admin" in result.roles
        assert "dev" in result.roles
        assert "viewer" in result.roles

    @patch("terrapod.services.sso_service.record_recent_user")
    async def test_sso_login_with_claims_mapping(self, mock_record, mock_db, sso_identity):
        """Claims-to-roles mapping adds additional roles."""
        from terrapod.config import ClaimsToRolesMapping

        mock_record.return_value = None

        # No internal assignments
        role_result = MagicMock()
        role_result.scalars.return_value.all.return_value = []
        platform_result = MagicMock()
        platform_result.scalars.return_value.all.return_value = []
        mock_db.execute.side_effect = [role_result, platform_result]

        rules = [
            ClaimsToRolesMapping(
                claim="groups",
                value="terrapod:dev",
                roles=["audit"],
            ),
        ]

        result = await process_login(
            db=mock_db,
            identity=sso_identity,
            claims_rules=rules,
        )

        assert "audit" in result.roles

    @patch("terrapod.services.sso_service.record_recent_user")
    async def test_local_login_requires_active_user(self, mock_record, mock_db):
        """Local login checks the users table for an active user."""
        identity = AuthenticatedIdentity(
            provider_name="local",
            subject="user@example.com",
            email="user@example.com",
            display_name=None,
        )

        # Mock: user not found
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = user_result

        with pytest.raises(ValueError, match="User not found"):
            await process_login(db=mock_db, identity=identity, claims_rules=[])

    @patch("terrapod.services.sso_service.record_recent_user")
    async def test_local_login_rejects_disabled_user(self, mock_record, mock_db):
        """Local login rejects disabled users."""
        identity = AuthenticatedIdentity(
            provider_name="local",
            subject="disabled@example.com",
            email="disabled@example.com",
            display_name=None,
        )

        mock_user = MagicMock()
        mock_user.is_active = False
        user_result = MagicMock()
        user_result.scalar_one_or_none.return_value = mock_user
        mock_db.execute.return_value = user_result

        with pytest.raises(ValueError, match="disabled"):
            await process_login(db=mock_db, identity=identity, claims_rules=[])

    @patch("terrapod.services.sso_service.record_recent_user")
    async def test_record_recent_user_failure_does_not_break_login(
        self, mock_record, mock_db, sso_identity
    ):
        """record_recent_user failure is swallowed — login still succeeds."""
        mock_record.side_effect = Exception("Redis down")

        role_result = MagicMock()
        role_result.scalars.return_value.all.return_value = []
        platform_result = MagicMock()
        platform_result.scalars.return_value.all.return_value = []
        mock_db.execute.side_effect = [role_result, platform_result]

        result = await process_login(
            db=mock_db,
            identity=sso_identity,
            claims_rules=[],
        )

        assert result.email == "test@example.com"
