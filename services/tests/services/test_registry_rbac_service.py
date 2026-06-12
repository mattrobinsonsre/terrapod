"""Tests for registry RBAC — permission resolution, owner, runner-token read, and the
dedicated registry_permission field (independent of workspace_permission)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.services.registry_rbac_service import (
    REGISTRY_PERMISSION_HIERARCHY,
    has_registry_permission,
    resolve_registry_permission,
)

_WS_TO_REG = {"read": "read", "plan": "read", "write": "write", "admin": "admin"}


def _make_role(
    *,
    name="custom-role",
    workspace_permission="read",
    registry_permission=None,
    allow_labels=None,
    allow_names=None,
    deny_labels=None,
    deny_names=None,
):
    role = MagicMock()
    role.name = name
    role.workspace_permission = workspace_permission
    # Default the registry level to mirror workspace_permission so tests that
    # only specify a workspace level still express their intended registry
    # access; pass registry_permission explicitly to decouple them.
    role.registry_permission = (
        registry_permission if registry_permission is not None else _WS_TO_REG[workspace_permission]
    )
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


class TestHasRegistryPermission:
    def test_admin_meets_all(self):
        for required in REGISTRY_PERMISSION_HIERARCHY:
            assert has_registry_permission("admin", required) is True

    def test_read_only_meets_read(self):
        assert has_registry_permission("read", "read") is True
        assert has_registry_permission("read", "write") is False
        assert has_registry_permission("read", "admin") is False

    def test_write_meets_read_and_write(self):
        assert has_registry_permission("write", "read") is True
        assert has_registry_permission("write", "write") is True
        assert has_registry_permission("write", "admin") is False

    def test_none_meets_nothing(self):
        for required in REGISTRY_PERMISSION_HIERARCHY:
            assert has_registry_permission(None, required) is False


class TestResolveRegistryPermission:
    @pytest.mark.asyncio
    async def test_admin_gets_admin(self):
        db = AsyncMock()
        result = await resolve_registry_permission(
            db, "admin@test.com", ["admin"], "my-module", {}, ""
        )
        assert result == "admin"

    @pytest.mark.asyncio
    async def test_audit_gets_read(self):
        db = _mock_db_with_roles([])
        result = await resolve_registry_permission(
            db, "user@test.com", ["audit"], "my-module", {}, ""
        )
        assert result == "read"

    @pytest.mark.asyncio
    async def test_owner_gets_admin(self):
        db = _mock_db_with_roles([])
        result = await resolve_registry_permission(
            db, "owner@test.com", ["everyone"], "my-module", {}, "owner@test.com"
        )
        assert result == "admin"

    @pytest.mark.asyncio
    async def test_runner_token_gets_read(self):
        db = _mock_db_with_roles([])
        result = await resolve_registry_permission(
            db,
            "runner@system",
            ["everyone"],
            "my-module",
            {},
            "",
            auth_method="runner_token",
        )
        assert result == "read"

    @pytest.mark.asyncio
    async def test_label_based_access(self):
        role = _make_role(registry_permission="write", allow_labels={"scope": ["public"]})
        db = _mock_db_with_roles([role])
        result = await resolve_registry_permission(
            db, "user@test.com", ["custom-role"], "my-module", {"scope": "public"}, ""
        )
        assert result == "write"

    @pytest.mark.asyncio
    async def test_registry_permission_independent_of_workspace(self):
        """The registry level comes from registry_permission, NOT workspace_permission.

        A role with admin workspace access but read-only registry access grants
        only registry read — the old derive-from-workspace behaviour is gone.
        """
        role = _make_role(
            workspace_permission="admin",
            registry_permission="read",
            allow_labels={"scope": ["public"]},
        )
        db = _mock_db_with_roles([role])
        result = await resolve_registry_permission(
            db, "user@test.com", ["custom-role"], "my-module", {"scope": "public"}, ""
        )
        assert result == "read"

    @pytest.mark.asyncio
    async def test_registry_admin_via_dedicated_field(self):
        role = _make_role(
            workspace_permission="read",
            registry_permission="admin",
            allow_labels={"scope": ["public"]},
        )
        db = _mock_db_with_roles([role])
        result = await resolve_registry_permission(
            db, "user@test.com", ["custom-role"], "my-module", {"scope": "public"}, ""
        )
        assert result == "admin"

    @pytest.mark.asyncio
    async def test_runner_token_read_unaffected_by_registry_permission(self):
        """Runner tokens get registry read regardless of any role's registry level.

        The runner read comes from the auth_method path, BEFORE the custom-role
        loop — switching the role loop to registry_permission must not change it.
        Runners carry the 'everyone' role only, so no custom role applies, but a
        matching write role here would still floor at read (it only raises).
        """
        role = _make_role(registry_permission="admin", allow_labels={"scope": ["public"]})
        db = _mock_db_with_roles([role])
        result = await resolve_registry_permission(
            db,
            "runner@system",
            ["everyone"],  # runners carry everyone only — the admin role won't match
            "my-module",
            {"scope": "public"},
            "",
            auth_method="runner_token",
        )
        assert result == "read"

    @pytest.mark.asyncio
    async def test_deny_label_blocks(self):
        role = _make_role(
            workspace_permission="write",
            allow_labels={"scope": ["public"]},
            deny_labels={"restricted": ["true"]},
        )
        db = _mock_db_with_roles([role])
        result = await resolve_registry_permission(
            db,
            "user@test.com",
            ["custom-role"],
            "my-module",
            {"scope": "public", "restricted": "true"},
            "",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_deny_name_blocks(self):
        role = _make_role(
            workspace_permission="write",
            allow_labels={"scope": ["public"]},
            deny_names=["secret-module"],
        )
        db = _mock_db_with_roles([role])
        result = await resolve_registry_permission(
            db,
            "user@test.com",
            ["custom-role"],
            "secret-module",
            {"scope": "public"},
            "",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_everyone_access_label(self):
        db = _mock_db_with_roles([])
        result = await resolve_registry_permission(
            db,
            "user@test.com",
            ["everyone"],
            "public-module",
            {"access": "everyone"},
            "",
        )
        assert result == "read"

    @pytest.mark.asyncio
    async def test_no_access_default(self):
        db = _mock_db_with_roles([])
        result = await resolve_registry_permission(
            db, "user@test.com", ["everyone"], "private-module", {}, ""
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_highest_permission_wins(self):
        role_reader = _make_role(
            name="reader", workspace_permission="read", allow_labels={"scope": ["public"]}
        )
        role_writer = _make_role(
            name="writer", workspace_permission="admin", allow_labels={"scope": ["public"]}
        )
        db = _mock_db_with_roles([role_reader, role_writer])
        result = await resolve_registry_permission(
            db,
            "user@test.com",
            ["reader", "writer"],
            "my-module",
            {"scope": "public"},
            "",
        )
        assert result == "admin"

    @pytest.mark.asyncio
    async def test_name_based_access(self):
        role = _make_role(workspace_permission="write", allow_names=["special-module"])
        db = _mock_db_with_roles([role])
        result = await resolve_registry_permission(
            db, "user@test.com", ["custom-role"], "special-module", {}, ""
        )
        assert result == "write"
