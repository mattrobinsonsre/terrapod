"""Tests for service-catalog RBAC (#535) — the dedicated catalog_permission
axis (none/read/use/admin), opt-in default, owner, and resolution order."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.services.catalog_rbac_service import (
    CATALOG_PERMISSION_HIERARCHY,
    has_catalog_permission,
    resolve_catalog_permission,
)


def _make_role(
    *,
    name="custom-role",
    catalog_permission="none",
    allow_labels=None,
    allow_names=None,
    deny_labels=None,
    deny_names=None,
):
    role = MagicMock()
    role.name = name
    role.catalog_permission = catalog_permission
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


class TestHasCatalogPermission:
    def test_admin_meets_all(self):
        for required in CATALOG_PERMISSION_HIERARCHY:
            assert has_catalog_permission("admin", required) is True

    def test_read_meets_read_only(self):
        assert has_catalog_permission("read", "read") is True
        assert has_catalog_permission("read", "use") is False
        assert has_catalog_permission("read", "admin") is False

    def test_use_meets_read_and_use(self):
        assert has_catalog_permission("use", "read") is True
        assert has_catalog_permission("use", "use") is True
        assert has_catalog_permission("use", "admin") is False

    def test_none_meets_nothing(self):
        for required in CATALOG_PERMISSION_HIERARCHY:
            assert has_catalog_permission(None, required) is False


class TestResolveCatalogPermission:
    @pytest.mark.asyncio
    async def test_platform_admin_gets_admin(self):
        db = AsyncMock()
        result = await resolve_catalog_permission(db, "admin@test.com", ["admin"], "vpc", {}, "")
        assert result == "admin"

    @pytest.mark.asyncio
    async def test_audit_gets_read(self):
        db = _mock_db_with_roles([])
        result = await resolve_catalog_permission(db, "user@test.com", ["audit"], "vpc", {}, "")
        assert result == "read"

    @pytest.mark.asyncio
    async def test_owner_gets_admin(self):
        db = _mock_db_with_roles([])
        result = await resolve_catalog_permission(
            db, "owner@test.com", [], "vpc", {}, "owner@test.com"
        )
        assert result == "admin"

    @pytest.mark.asyncio
    async def test_plain_user_gets_none(self):
        """Opt-in: no role, no owner, no audit → no catalog access."""
        db = _mock_db_with_roles([])
        result = await resolve_catalog_permission(db, "user@test.com", ["everyone"], "vpc", {}, "")
        assert result is None

    @pytest.mark.asyncio
    async def test_none_permission_role_grants_nothing(self):
        """A custom role whose catalog_permission is 'none' contributes nothing
        even when its labels match."""
        role = _make_role(catalog_permission="none", allow_labels={"team": ["platform"]})
        db = _mock_db_with_roles([role])
        result = await resolve_catalog_permission(
            db, "user@test.com", ["custom-role"], "vpc", {"team": "platform"}, ""
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_use_role_matches_labels(self):
        role = _make_role(catalog_permission="use", allow_labels={"team": ["platform"]})
        db = _mock_db_with_roles([role])
        result = await resolve_catalog_permission(
            db, "user@test.com", ["custom-role"], "vpc", {"team": "platform"}, ""
        )
        assert result == "use"

    @pytest.mark.asyncio
    async def test_use_role_no_label_match_is_none(self):
        role = _make_role(catalog_permission="use", allow_labels={"team": ["platform"]})
        db = _mock_db_with_roles([role])
        result = await resolve_catalog_permission(
            db, "user@test.com", ["custom-role"], "vpc", {"team": "data"}, ""
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_deny_label_blocks_role(self):
        role = _make_role(
            catalog_permission="admin",
            allow_labels={"team": ["platform"]},
            deny_labels={"sensitive": ["true"]},
        )
        db = _mock_db_with_roles([role])
        result = await resolve_catalog_permission(
            db,
            "user@test.com",
            ["custom-role"],
            "vpc",
            {"team": "platform", "sensitive": "true"},
            "",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_highest_of_multiple_roles_wins(self):
        read_role = _make_role(name="r1", catalog_permission="read", allow_names=["vpc"])
        use_role = _make_role(name="r2", catalog_permission="use", allow_names=["vpc"])
        db = _mock_db_with_roles([read_role, use_role])
        result = await resolve_catalog_permission(db, "user@test.com", ["r1", "r2"], "vpc", {}, "")
        assert result == "use"

    @pytest.mark.asyncio
    async def test_no_everyone_floor(self):
        """Unlike workspaces/registry, access=everyone does NOT grant catalog read."""
        db = _mock_db_with_roles([])
        result = await resolve_catalog_permission(
            db, "user@test.com", ["everyone"], "vpc", {"access": "everyone"}, ""
        )
        assert result is None
