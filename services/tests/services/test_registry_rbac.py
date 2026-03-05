"""Tests for registry-specific RBAC permission resolution."""

from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.services.registry_rbac_service import (
    _WS_PERM_TO_REGISTRY,
    REGISTRY_PERMISSION_HIERARCHY,
    has_registry_permission,
    resolve_registry_permission,
)

# ── has_registry_permission ────────────────────────────────────────────


class TestHasRegistryPermission:
    def test_read_meets_read(self):
        assert has_registry_permission("read", "read") is True

    def test_write_meets_read(self):
        assert has_registry_permission("write", "read") is True

    def test_admin_meets_admin(self):
        assert has_registry_permission("admin", "admin") is True

    def test_read_does_not_meet_write(self):
        assert has_registry_permission("read", "write") is False

    def test_write_does_not_meet_admin(self):
        assert has_registry_permission("write", "admin") is False

    def test_none_never_meets_any(self):
        for level in REGISTRY_PERMISSION_HIERARCHY:
            assert has_registry_permission(None, level) is False

    def test_unknown_effective_fails(self):
        assert has_registry_permission("unknown", "read") is False

    def test_full_hierarchy(self):
        levels = ["read", "write", "admin"]
        for i, effective in enumerate(levels):
            for j, required in enumerate(levels):
                expected = i >= j
                assert has_registry_permission(effective, required) is expected


# ── Permission mapping ─────────────────────────────────────────────────


class TestPermissionMapping:
    def test_read_maps_to_read(self):
        assert _WS_PERM_TO_REGISTRY["read"] == "read"

    def test_plan_maps_to_read(self):
        assert _WS_PERM_TO_REGISTRY["plan"] == "read"

    def test_write_maps_to_write(self):
        assert _WS_PERM_TO_REGISTRY["write"] == "write"

    def test_admin_maps_to_admin(self):
        assert _WS_PERM_TO_REGISTRY["admin"] == "admin"


# ── resolve_registry_permission ────────────────────────────────────────


def _mock_db_with_roles(roles=None):
    db = AsyncMock(spec=AsyncSession)
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = roles or []
    db.execute.return_value = mock_result
    return db


def _mock_role(
    name,
    workspace_permission="read",
    allow_labels=None,
    allow_names=None,
    deny_labels=None,
    deny_names=None,
):
    role = MagicMock()
    role.name = name
    role.workspace_permission = workspace_permission
    role.allow_labels = allow_labels or {}
    role.allow_names = allow_names or []
    role.deny_labels = deny_labels or {}
    role.deny_names = deny_names or []
    return role


class TestResolveRegistryPermission:
    async def test_platform_admin_returns_admin(self):
        db = _mock_db_with_roles()
        result = await resolve_registry_permission(
            db, "user@test.com", ["admin"], "my-module", {}, ""
        )
        assert result == "admin"
        db.execute.assert_not_called()

    async def test_platform_audit_returns_read(self):
        db = _mock_db_with_roles()
        result = await resolve_registry_permission(
            db, "user@test.com", ["audit"], "my-module", {}, ""
        )
        assert result == "read"

    async def test_owner_returns_admin(self):
        db = _mock_db_with_roles()
        result = await resolve_registry_permission(
            db, "owner@test.com", ["everyone"], "my-module", {}, "owner@test.com"
        )
        assert result == "admin"

    async def test_custom_role_label_match(self):
        role = _mock_role(
            "mod-team",
            workspace_permission="write",
            allow_labels={"team": ["platform"]},
        )
        db = _mock_db_with_roles([role])
        result = await resolve_registry_permission(
            db, "user@test.com", ["mod-team", "everyone"], "my-module", {"team": "platform"}, ""
        )
        assert result == "write"

    async def test_plan_permission_maps_to_read(self):
        """A role with workspace_permission='plan' grants registry 'read'."""
        role = _mock_role(
            "plan-role",
            workspace_permission="plan",
            allow_names=["my-module"],
        )
        db = _mock_db_with_roles([role])
        result = await resolve_registry_permission(
            db, "user@test.com", ["plan-role", "everyone"], "my-module", {}, ""
        )
        assert result == "read"

    async def test_deny_blocks_role(self):
        role = _mock_role(
            "team-role",
            workspace_permission="admin",
            allow_labels={"team": ["platform"]},
            deny_names=["secret-module"],
        )
        db = _mock_db_with_roles([role])
        result = await resolve_registry_permission(
            db,
            "user@test.com",
            ["team-role", "everyone"],
            "secret-module",
            {"team": "platform"},
            "",
        )
        assert result is None

    async def test_everyone_label_grants_read(self):
        db = _mock_db_with_roles()
        result = await resolve_registry_permission(
            db, "user@test.com", ["everyone"], "public-module", {"access": "everyone"}, ""
        )
        assert result == "read"

    async def test_no_match_returns_none(self):
        db = _mock_db_with_roles()
        result = await resolve_registry_permission(
            db, "user@test.com", ["everyone"], "private-module", {"env": "prod"}, ""
        )
        assert result is None

    async def test_highest_role_wins(self):
        reader = _mock_role("reader", workspace_permission="read", allow_labels={"env": ["dev"]})
        admin_role = _mock_role(
            "admin-role", workspace_permission="admin", allow_labels={"env": ["dev"]}
        )
        db = _mock_db_with_roles([reader, admin_role])
        result = await resolve_registry_permission(
            db,
            "user@test.com",
            ["reader", "admin-role", "everyone"],
            "my-module",
            {"env": "dev"},
            "",
        )
        assert result == "admin"

    async def test_deny_label_blocks(self):
        role = _mock_role(
            "partial",
            workspace_permission="write",
            allow_labels={"env": ["dev", "prod"]},
            deny_labels={"env": ["prod"]},
        )
        db = _mock_db_with_roles([role])
        result = await resolve_registry_permission(
            db, "user@test.com", ["partial", "everyone"], "module", {"env": "prod"}, ""
        )
        assert result is None

    async def test_only_builtin_roles_skip_db_query(self):
        db = _mock_db_with_roles()
        await resolve_registry_permission(db, "user@test.com", ["everyone"], "mod", {}, "")
        db.execute.assert_not_called()
