"""Tests for workspace-specific RBAC permission resolution."""

from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.services.workspace_rbac_service import (
    PERMISSION_HIERARCHY,
    has_permission,
    resolve_workspace_permission,
)

# ── has_permission ─────────────────────────────────────────────────────


class TestHasPermission:
    def test_read_meets_read(self):
        assert has_permission("read", "read") is True

    def test_plan_meets_read(self):
        assert has_permission("plan", "read") is True

    def test_write_meets_plan(self):
        assert has_permission("write", "plan") is True

    def test_admin_meets_admin(self):
        assert has_permission("admin", "admin") is True

    def test_read_does_not_meet_plan(self):
        assert has_permission("read", "plan") is False

    def test_plan_does_not_meet_write(self):
        assert has_permission("plan", "write") is False

    def test_write_does_not_meet_admin(self):
        assert has_permission("write", "admin") is False

    def test_none_never_meets_any(self):
        for level in PERMISSION_HIERARCHY:
            assert has_permission(None, level) is False

    def test_unknown_effective_fails(self):
        assert has_permission("unknown", "read") is False

    def test_unknown_required_fails(self):
        assert has_permission("admin", "unknown") is False

    def test_full_hierarchy(self):
        """Every level meets itself and below, fails above."""
        levels = ["read", "plan", "write", "admin"]
        for i, effective in enumerate(levels):
            for j, required in enumerate(levels):
                expected = i >= j
                assert has_permission(effective, required) is expected, (
                    f"has_permission({effective!r}, {required!r}) should be {expected}"
                )


# ── resolve_workspace_permission ───────────────────────────────────────


def _mock_workspace(**kwargs):
    ws = MagicMock()
    ws.name = kwargs.get("name", "test-ws")
    ws.labels = kwargs.get("labels", {})
    ws.owner_email = kwargs.get("owner_email", "")
    return ws


def _mock_db_with_roles(roles: list | None = None):
    """Create a mock db that returns given Role objects from execute()."""
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


class TestResolveWorkspacePermission:
    async def test_platform_admin_returns_admin(self):
        db = _mock_db_with_roles()
        ws = _mock_workspace()
        result = await resolve_workspace_permission(db, "user@test.com", ["admin"], ws)
        assert result == "admin"
        # Admin bypasses all — should not query DB for custom roles
        db.execute.assert_not_called()

    async def test_platform_audit_returns_read(self):
        db = _mock_db_with_roles()
        ws = _mock_workspace()
        result = await resolve_workspace_permission(db, "user@test.com", ["audit"], ws)
        assert result == "read"

    async def test_workspace_owner_returns_admin(self):
        db = _mock_db_with_roles()
        ws = _mock_workspace(owner_email="owner@test.com")
        result = await resolve_workspace_permission(db, "owner@test.com", ["everyone"], ws)
        assert result == "admin"

    async def test_owner_takes_priority_over_audit(self):
        db = _mock_db_with_roles()
        ws = _mock_workspace(owner_email="user@test.com")
        result = await resolve_workspace_permission(db, "user@test.com", ["audit", "everyone"], ws)
        assert result == "admin"

    async def test_custom_role_label_match_grants_permission(self):
        role = _mock_role(
            "dev-team",
            workspace_permission="write",
            allow_labels={"env": ["dev"]},
        )
        db = _mock_db_with_roles([role])
        ws = _mock_workspace(labels={"env": "dev"})
        result = await resolve_workspace_permission(
            db, "user@test.com", ["dev-team", "everyone"], ws
        )
        assert result == "write"

    async def test_custom_role_name_match_grants_permission(self):
        role = _mock_role(
            "ws-reader",
            workspace_permission="read",
            allow_names=["my-workspace"],
        )
        db = _mock_db_with_roles([role])
        ws = _mock_workspace(name="my-workspace")
        result = await resolve_workspace_permission(
            db, "user@test.com", ["ws-reader", "everyone"], ws
        )
        assert result == "read"

    async def test_deny_label_blocks_role(self):
        role = _mock_role(
            "almost-admin",
            workspace_permission="admin",
            allow_labels={"env": ["dev", "prod"]},
            deny_labels={"env": ["prod"]},
        )
        db = _mock_db_with_roles([role])
        ws = _mock_workspace(labels={"env": "prod"})
        result = await resolve_workspace_permission(
            db, "user@test.com", ["almost-admin", "everyone"], ws
        )
        assert result is None

    async def test_deny_name_blocks_role(self):
        role = _mock_role(
            "team-role",
            workspace_permission="write",
            allow_labels={"team": ["backend"]},
            deny_names=["secret-ws"],
        )
        db = _mock_db_with_roles([role])
        ws = _mock_workspace(name="secret-ws", labels={"team": "backend"})
        result = await resolve_workspace_permission(
            db, "user@test.com", ["team-role", "everyone"], ws
        )
        assert result is None

    async def test_highest_role_wins(self):
        reader = _mock_role("reader", workspace_permission="read", allow_labels={"env": ["dev"]})
        writer = _mock_role("writer", workspace_permission="write", allow_labels={"env": ["dev"]})
        db = _mock_db_with_roles([reader, writer])
        ws = _mock_workspace(labels={"env": "dev"})
        result = await resolve_workspace_permission(
            db, "user@test.com", ["reader", "writer", "everyone"], ws
        )
        assert result == "write"

    async def test_everyone_label_grants_read(self):
        db = _mock_db_with_roles()
        ws = _mock_workspace(labels={"access": "everyone"})
        result = await resolve_workspace_permission(db, "user@test.com", ["everyone"], ws)
        assert result == "read"

    async def test_no_match_returns_none(self):
        db = _mock_db_with_roles()
        ws = _mock_workspace(labels={"env": "prod"})
        result = await resolve_workspace_permission(db, "user@test.com", ["everyone"], ws)
        assert result is None

    async def test_custom_role_overrides_everyone_read(self):
        """A custom role granting write on a workspace that also has access=everyone."""
        role = _mock_role(
            "team-write",
            workspace_permission="write",
            allow_labels={"access": ["everyone"]},
        )
        db = _mock_db_with_roles([role])
        ws = _mock_workspace(labels={"access": "everyone"})
        result = await resolve_workspace_permission(
            db, "user@test.com", ["team-write", "everyone"], ws
        )
        assert result == "write"

    async def test_audit_plus_custom_role_takes_highest(self):
        """Audit gives read, custom role gives write — write wins."""
        role = _mock_role(
            "team-write",
            workspace_permission="write",
            allow_labels={"env": ["dev"]},
        )
        db = _mock_db_with_roles([role])
        ws = _mock_workspace(labels={"env": "dev"})
        result = await resolve_workspace_permission(
            db, "user@test.com", ["audit", "team-write", "everyone"], ws
        )
        assert result == "write"

    async def test_audit_baseline_when_no_custom_match(self):
        """Audit gives read even when custom roles don't match workspace."""
        role = _mock_role(
            "unrelated",
            workspace_permission="admin",
            allow_labels={"env": ["staging"]},
        )
        db = _mock_db_with_roles([role])
        ws = _mock_workspace(labels={"env": "prod"})
        result = await resolve_workspace_permission(
            db, "user@test.com", ["audit", "unrelated", "everyone"], ws
        )
        assert result == "read"

    async def test_empty_roles_no_labels_returns_none(self):
        db = _mock_db_with_roles()
        ws = _mock_workspace()
        result = await resolve_workspace_permission(db, "user@test.com", [], ws)
        assert result is None

    async def test_only_builtin_roles_skip_db_query(self):
        """When user only has builtin roles, don't query for custom roles."""
        db = _mock_db_with_roles()
        ws = _mock_workspace()
        await resolve_workspace_permission(db, "user@test.com", ["everyone"], ws)
        db.execute.assert_not_called()
