"""Tests for workspace RBAC — permission resolution hierarchy, label matching, owner logic."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from terrapod.services.workspace_rbac_service import (
    PERMISSION_HIERARCHY,
    has_permission,
    resolve_workspace_permission,
)


def _make_workspace(*, name="ws-1", labels=None, owner_email=None):
    ws = MagicMock()
    ws.name = name
    ws.labels = labels or {}
    ws.owner_email = owner_email
    return ws


def _make_role(
    *,
    name="custom-role",
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


def _mock_db_with_roles(roles):
    db = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = roles
    db.execute.return_value = result
    return db


class TestHasPermission:
    def test_admin_meets_all(self):
        for required in PERMISSION_HIERARCHY:
            assert has_permission("admin", required) is True

    def test_read_only_meets_read(self):
        assert has_permission("read", "read") is True
        assert has_permission("read", "plan") is False
        assert has_permission("read", "write") is False
        assert has_permission("read", "admin") is False

    def test_plan_meets_read_and_plan(self):
        assert has_permission("plan", "read") is True
        assert has_permission("plan", "plan") is True
        assert has_permission("plan", "write") is False

    def test_write_meets_read_plan_write(self):
        assert has_permission("write", "read") is True
        assert has_permission("write", "plan") is True
        assert has_permission("write", "write") is True
        assert has_permission("write", "admin") is False

    def test_none_meets_nothing(self):
        for required in PERMISSION_HIERARCHY:
            assert has_permission(None, required) is False


class TestResolveWorkspacePermission:
    @pytest.mark.asyncio
    async def test_admin_gets_admin(self):
        ws = _make_workspace()
        db = AsyncMock()
        result = await resolve_workspace_permission(db, "user@test.com", ["admin"], ws)
        assert result == "admin"

    @pytest.mark.asyncio
    async def test_audit_gets_read(self):
        ws = _make_workspace()
        db = _mock_db_with_roles([])
        result = await resolve_workspace_permission(db, "user@test.com", ["audit"], ws)
        assert result == "read"

    @pytest.mark.asyncio
    async def test_owner_gets_admin(self):
        ws = _make_workspace(owner_email="owner@test.com")
        db = _mock_db_with_roles([])
        result = await resolve_workspace_permission(db, "owner@test.com", ["everyone"], ws)
        assert result == "admin"

    @pytest.mark.asyncio
    async def test_non_owner_no_special_access(self):
        ws = _make_workspace(owner_email="owner@test.com")
        db = _mock_db_with_roles([])
        result = await resolve_workspace_permission(db, "other@test.com", ["everyone"], ws)
        assert result is None

    @pytest.mark.asyncio
    async def test_label_based_access(self):
        role = _make_role(workspace_permission="write", allow_labels={"env": ["prod"]})
        ws = _make_workspace(labels={"env": "prod"})
        db = _mock_db_with_roles([role])
        result = await resolve_workspace_permission(db, "user@test.com", ["custom-role"], ws)
        assert result == "write"

    @pytest.mark.asyncio
    async def test_name_based_access(self):
        role = _make_role(workspace_permission="plan", allow_names=["ws-special"])
        ws = _make_workspace(name="ws-special")
        db = _mock_db_with_roles([role])
        result = await resolve_workspace_permission(db, "user@test.com", ["custom-role"], ws)
        assert result == "plan"

    @pytest.mark.asyncio
    async def test_deny_label_blocks(self):
        role = _make_role(
            workspace_permission="write",
            allow_labels={"env": ["prod"]},
            deny_labels={"sensitive": ["true"]},
        )
        ws = _make_workspace(labels={"env": "prod", "sensitive": "true"})
        db = _mock_db_with_roles([role])
        result = await resolve_workspace_permission(db, "user@test.com", ["custom-role"], ws)
        assert result is None

    @pytest.mark.asyncio
    async def test_deny_name_blocks(self):
        role = _make_role(
            workspace_permission="write",
            allow_labels={"env": ["prod"]},
            deny_names=["ws-blocked"],
        )
        ws = _make_workspace(name="ws-blocked", labels={"env": "prod"})
        db = _mock_db_with_roles([role])
        result = await resolve_workspace_permission(db, "user@test.com", ["custom-role"], ws)
        assert result is None

    @pytest.mark.asyncio
    async def test_highest_permission_wins(self):
        """Multiple roles: the highest workspace_permission is returned."""
        role_reader = _make_role(
            name="reader", workspace_permission="read", allow_labels={"env": ["prod"]}
        )
        role_writer = _make_role(
            name="writer", workspace_permission="write", allow_labels={"env": ["prod"]}
        )
        ws = _make_workspace(labels={"env": "prod"})
        db = _mock_db_with_roles([role_reader, role_writer])
        result = await resolve_workspace_permission(db, "user@test.com", ["reader", "writer"], ws)
        assert result == "write"

    @pytest.mark.asyncio
    async def test_everyone_access_label(self):
        ws = _make_workspace(labels={"access": "everyone"})
        db = _mock_db_with_roles([])
        result = await resolve_workspace_permission(db, "user@test.com", ["everyone"], ws)
        assert result == "read"

    @pytest.mark.asyncio
    async def test_everyone_no_access_label(self):
        ws = _make_workspace(labels={"team": "sre"})
        db = _mock_db_with_roles([])
        result = await resolve_workspace_permission(db, "user@test.com", ["everyone"], ws)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_roles_no_access(self):
        ws = _make_workspace()
        db = _mock_db_with_roles([])
        result = await resolve_workspace_permission(db, "user@test.com", [], ws)
        assert result is None

    @pytest.mark.asyncio
    async def test_admin_bypasses_deny(self):
        """Admin role ignores deny rules entirely."""
        ws = _make_workspace(labels={"sensitive": "true"})
        db = AsyncMock()
        result = await resolve_workspace_permission(db, "admin@test.com", ["admin"], ws)
        assert result == "admin"

    @pytest.mark.asyncio
    async def test_label_role_upgrades_audit_read(self):
        """A custom role with write permission should upgrade audit's read."""
        role = _make_role(workspace_permission="write", allow_labels={"env": ["prod"]})
        ws = _make_workspace(labels={"env": "prod"})
        db = _mock_db_with_roles([role])
        result = await resolve_workspace_permission(
            db, "user@test.com", ["audit", "custom-role"], ws
        )
        assert result == "write"
