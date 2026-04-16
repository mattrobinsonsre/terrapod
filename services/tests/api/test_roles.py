"""Tests for role and role assignment CRUD endpoints (admin-only)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import (
    AuthenticatedUser,
    get_current_user,
    require_admin,
    require_admin_or_audit,
)
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}


def _user(email="admin@example.com", roles=None):
    return AuthenticatedUser(
        email=email,
        display_name="Admin",
        roles=roles or ["admin", "everyone"],
        provider_name="local",
        auth_method="session",
    )


def _mock_role(name="dev-team", ws_perm="read"):
    role = MagicMock()
    role.name = name
    role.description = "A custom role"
    role.allow_labels = {"env": ["dev"]}
    role.allow_names = []
    role.deny_labels = {}
    role.deny_names = []
    role.workspace_permission = ws_perm
    role.pool_permission = "read"
    role.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    role.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return role


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user

    # Override admin/audit dependencies based on user's roles
    if "admin" in (user.roles or []):
        app.dependency_overrides[require_admin] = lambda: user
        app.dependency_overrides[require_admin_or_audit] = lambda: user
    elif "audit" in (user.roles or []):
        app.dependency_overrides[require_admin_or_audit] = lambda: user

    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


# ── List Roles ─────────────────────────────────────────────────────────


class TestListRoles:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_list_includes_builtins_and_custom(self, *mocks):
        user = _user(roles=["admin"])
        app, mock_db = _make_app(user)
        custom_role = _mock_role("custom-role")
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [custom_role]
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/roles", headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()["data"]
        names = [r["name"] for r in data]
        # Built-in roles
        assert "admin" in names
        assert "audit" in names
        assert "everyone" in names
        # Custom role
        assert "custom-role" in names
        # Built-ins are marked
        admin_entry = next(r for r in data if r["name"] == "admin")
        assert admin_entry["attributes"]["built-in"] is True
        custom_entry = next(r for r in data if r["name"] == "custom-role")
        assert custom_entry["attributes"]["built-in"] is False

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_audit_can_list(self, *mocks):
        user = _user(roles=["audit"])
        app, mock_db = _make_app(user)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/roles", headers=_AUTH)
        assert resp.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_non_admin_non_audit_returns_403(self, *mocks):
        user = _user(roles=["everyone"])
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/roles", headers=_AUTH)
        assert resp.status_code == 403


# ── Create Role ────────────────────────────────────────────────────────


class TestCreateRole:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_custom_role(self, *mocks):
        user = _user(roles=["admin"])
        app, mock_db = _make_app(user)
        # No existing role
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/roles",
                json={
                    "data": {
                        "name": "new-role",
                        "attributes": {
                            "description": "A new role",
                            "workspace-permission": "write",
                            "allow-labels": {"env": ["dev"]},
                        },
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 201

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_builtin_name_rejected(self, *mocks):
        user = _user(roles=["admin"])
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/roles",
                json={
                    "data": {
                        "name": "admin",
                        "attributes": {"workspace-permission": "admin"},
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 422
        assert "built-in" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_duplicate_rejected(self, *mocks):
        user = _user(roles=["admin"])
        app, mock_db = _make_app(user)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = _mock_role("existing")
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/roles",
                json={
                    "data": {
                        "name": "existing",
                        "attributes": {"workspace-permission": "read"},
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 422
        assert "already exists" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_invalid_permission_rejected(self, *mocks):
        user = _user(roles=["admin"])
        app, mock_db = _make_app(user)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/roles",
                json={
                    "data": {
                        "name": "bad-role",
                        "attributes": {"workspace-permission": "superadmin"},
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_requires_admin(self, *mocks):
        user = _user(roles=["audit"])
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/roles",
                json={
                    "data": {
                        "name": "x",
                        "attributes": {"workspace-permission": "read"},
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 403


# ── Show Role ──────────────────────────────────────────────────────────


class TestShowRole:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_show_builtin_role(self, *mocks):
        app, _ = _make_app(_user(roles=["admin"]))

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/roles/admin", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["built-in"] is True

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_show_custom_role(self, *mocks):
        role = _mock_role("my-role", ws_perm="write")
        app, mock_db = _make_app(_user(roles=["admin"]))
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = role
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/roles/my-role", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["workspace-permission"] == "write"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_show_not_found(self, *mocks):
        app, mock_db = _make_app(_user(roles=["admin"]))
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/roles/nope", headers=_AUTH)
        assert resp.status_code == 404


# ── Update Role ────────────────────────────────────────────────────────


class TestUpdateRole:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_update_builtin_rejected(self, *mocks):
        app, _ = _make_app(_user(roles=["admin"]))

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                "/api/v2/roles/admin",
                json={"data": {"attributes": {"description": "hacked"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422
        assert "built-in" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_update_custom_role(self, *mocks):
        role = _mock_role("my-role")
        app, mock_db = _make_app(_user(roles=["admin"]))
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = role
        mock_db.execute.return_value = mock_result
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                "/api/v2/roles/my-role",
                json={"data": {"attributes": {"workspace-permission": "write"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 200


# ── Delete Role ────────────────────────────────────────────────────────


class TestDeleteRole:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_delete_builtin_rejected(self, *mocks):
        app, _ = _make_app(_user(roles=["admin"]))

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete("/api/v2/roles/everyone", headers=_AUTH)
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_delete_custom_role(self, *mocks):
        role = _mock_role("temp-role")
        app, mock_db = _make_app(_user(roles=["admin"]))
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = role
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete("/api/v2/roles/temp-role", headers=_AUTH)
        assert resp.status_code == 204

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_delete_not_found(self, *mocks):
        app, mock_db = _make_app(_user(roles=["admin"]))
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete("/api/v2/roles/nope", headers=_AUTH)
        assert resp.status_code == 404


# ── Role Assignments ──────────────────────────────────────────────────


class TestRoleAssignments:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_list_assignments(self, *mocks):
        app, mock_db = _make_app(_user(roles=["admin"]))
        # Return empty for both queries (platform + custom)
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/role-assignments", headers=_AUTH)
        assert resp.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.redis.client.get_redis_client")
    async def test_set_assignments_admin(self, mock_redis_fn, *mocks):
        mock_redis = AsyncMock()
        mock_redis_fn.return_value = mock_redis

        app, mock_db = _make_app(_user(roles=["admin"]))
        # Mock: existing assignments empty, role validation pass
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = []
        mock_result.scalar_one_or_none.return_value = None  # will be called multiple times
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.put(
                "/api/v2/role-assignments",
                json={
                    "data": {
                        "attributes": {
                            "provider-name": "local",
                            "email": "user@test.com",
                            "roles": ["admin"],
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_set_assignments_non_admin_returns_403(self, *mocks):
        user = _user(roles=["everyone"])
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.put(
                "/api/v2/role-assignments",
                json={
                    "data": {
                        "attributes": {
                            "email": "user@test.com",
                            "roles": ["admin"],
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.redis.client.get_redis_client")
    async def test_delete_assignment(self, mock_redis_fn, *mocks):
        mock_redis = AsyncMock()
        mock_redis_fn.return_value = mock_redis

        app, mock_db = _make_app(_user(roles=["admin"]))
        mock_pra = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_pra
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(
                "/api/v2/role-assignments/local/user@test.com/admin",
                headers=_AUTH,
            )
        assert resp.status_code == 204

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_delete_assignment_not_found(self, *mocks):
        app, mock_db = _make_app(_user(roles=["admin"]))
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(
                "/api/v2/role-assignments/local/nobody@test.com/admin",
                headers=_AUTH,
            )
        assert resp.status_code == 404
