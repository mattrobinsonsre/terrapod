"""Tests for user management endpoints."""

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
