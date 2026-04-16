"""Tests for pool RBAC — permission resolution, owner, pool_permission field."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.services.pool_rbac_service import (
    POOL_PERMISSION_HIERARCHY,
    has_pool_permission,
    resolve_pool_permission,
)


def _make_role(
    *,
    name="custom-role",
    pool_permission="read",
    allow_labels=None,
    allow_names=None,
    deny_labels=None,
    deny_names=None,
):
    role = MagicMock()
    role.name = name
    role.pool_permission = pool_permission
    role.allow_labels = allow_labels or {}
    role.allow_names = allow_names or []
    role.deny_labels = deny_labels or {}
    role.deny_names = deny_names or []
    return role


def _mock_db_with_roles(roles):
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = roles
    db.execute.return_value = result
    return db


class TestHasPoolPermission:
    def test_admin_meets_all(self):
        for required in POOL_PERMISSION_HIERARCHY:
            assert has_pool_permission("admin", required) is True

    def test_read_only_meets_read(self):
        assert has_pool_permission("read", "read") is True
        assert has_pool_permission("read", "write") is False
        assert has_pool_permission("read", "admin") is False

    def test_write_meets_read_and_write(self):
        assert has_pool_permission("write", "read") is True
        assert has_pool_permission("write", "write") is True
        assert has_pool_permission("write", "admin") is False

    def test_none_meets_nothing(self):
        for required in POOL_PERMISSION_HIERARCHY:
            assert has_pool_permission(None, required) is False


class TestResolvePoolPermission:
    @pytest.mark.asyncio
    async def test_admin_gets_admin(self):
        db = AsyncMock()
        result = await resolve_pool_permission(db, "admin@test.com", ["admin"], "my-pool", {}, None)
        assert result == "admin"

    @pytest.mark.asyncio
    async def test_audit_gets_read(self):
        db = _mock_db_with_roles([])
        result = await resolve_pool_permission(db, "user@test.com", ["audit"], "my-pool", {}, None)
        assert result == "read"

    @pytest.mark.asyncio
    async def test_owner_gets_admin(self):
        db = _mock_db_with_roles([])
        result = await resolve_pool_permission(
            db, "owner@test.com", ["everyone"], "my-pool", {}, "owner@test.com"
        )
        assert result == "admin"

    @pytest.mark.asyncio
    async def test_label_based_access_uses_pool_permission(self):
        """Custom roles use pool_permission field, not workspace_permission."""
        role = _make_role(pool_permission="write", allow_labels={"env": ["prod"]})
        db = _mock_db_with_roles([role])
        result = await resolve_pool_permission(
            db, "user@test.com", ["custom-role"], "prod-pool", {"env": "prod"}, None
        )
        assert result == "write"

    @pytest.mark.asyncio
    async def test_deny_label_blocks(self):
        role = _make_role(
            pool_permission="write",
            allow_labels={"env": ["prod"]},
            deny_labels={"restricted": ["true"]},
        )
        db = _mock_db_with_roles([role])
        result = await resolve_pool_permission(
            db,
            "user@test.com",
            ["custom-role"],
            "prod-pool",
            {"env": "prod", "restricted": "true"},
            None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_deny_name_blocks(self):
        role = _make_role(
            pool_permission="write",
            allow_labels={"env": ["prod"]},
            deny_names=["secret-pool"],
        )
        db = _mock_db_with_roles([role])
        result = await resolve_pool_permission(
            db,
            "user@test.com",
            ["custom-role"],
            "secret-pool",
            {"env": "prod"},
            None,
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_everyone_access_label(self):
        db = _mock_db_with_roles([])
        result = await resolve_pool_permission(
            db,
            "user@test.com",
            ["everyone"],
            "public-pool",
            {"access": "everyone"},
            None,
        )
        assert result == "read"

    @pytest.mark.asyncio
    async def test_no_access_default(self):
        db = _mock_db_with_roles([])
        result = await resolve_pool_permission(
            db, "user@test.com", ["everyone"], "private-pool", {}, None
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_highest_permission_wins(self):
        role_reader = _make_role(
            name="reader", pool_permission="read", allow_labels={"env": ["prod"]}
        )
        role_writer = _make_role(
            name="writer", pool_permission="admin", allow_labels={"env": ["prod"]}
        )
        db = _mock_db_with_roles([role_reader, role_writer])
        result = await resolve_pool_permission(
            db,
            "user@test.com",
            ["reader", "writer"],
            "prod-pool",
            {"env": "prod"},
            None,
        )
        assert result == "admin"

    @pytest.mark.asyncio
    async def test_name_based_access(self):
        role = _make_role(pool_permission="write", allow_names=["special-pool"])
        db = _mock_db_with_roles([role])
        result = await resolve_pool_permission(
            db, "user@test.com", ["custom-role"], "special-pool", {}, None
        )
        assert result == "write"

    @pytest.mark.asyncio
    async def test_owner_empty_string_does_not_match(self):
        """Empty owner_email should not grant admin."""
        db = _mock_db_with_roles([])
        result = await resolve_pool_permission(db, "user@test.com", ["everyone"], "my-pool", {}, "")
        assert result is None

    @pytest.mark.asyncio
    async def test_owner_none_does_not_match(self):
        """None owner_email should not grant admin."""
        db = _mock_db_with_roles([])
        result = await resolve_pool_permission(
            db, "user@test.com", ["everyone"], "my-pool", {}, None
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_audit_with_custom_role_elevation(self):
        """Audit user with a custom role granting write gets write (not just read)."""
        role = _make_role(pool_permission="write", allow_labels={"env": ["prod"]})
        db = _mock_db_with_roles([role])
        result = await resolve_pool_permission(
            db,
            "auditor@test.com",
            ["audit", "custom-role"],
            "prod-pool",
            {"env": "prod"},
            None,
        )
        assert result == "write"

    @pytest.mark.asyncio
    async def test_audit_without_matching_role_gets_read(self):
        """Audit user with a custom role that doesn't match still gets read from audit."""
        role = _make_role(pool_permission="write", allow_labels={"env": ["staging"]})
        db = _mock_db_with_roles([role])
        result = await resolve_pool_permission(
            db,
            "auditor@test.com",
            ["audit", "custom-role"],
            "prod-pool",
            {"env": "prod"},
            None,
        )
        assert result == "read"

    @pytest.mark.asyncio
    async def test_preloaded_roles_skips_db_query(self):
        """When preloaded_roles is passed, no DB query should be made."""
        role = _make_role(pool_permission="admin", allow_names=["my-pool"])
        db = AsyncMock()
        result = await resolve_pool_permission(
            db,
            "user@test.com",
            ["custom-role"],
            "my-pool",
            {},
            None,
            preloaded_roles=[role],
        )
        assert result == "admin"
        db.execute.assert_not_called()
