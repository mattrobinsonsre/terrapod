"""Tests for variable and variable set CRUD endpoints with RBAC."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user, require_admin
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}


def _user(email="test@example.com", roles=None):
    return AuthenticatedUser(
        email=email,
        display_name="Test",
        roles=roles or ["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _mock_workspace(ws_id=None):
    ws = MagicMock()
    ws.id = ws_id or uuid.uuid4()
    ws.name = "test-ws"
    ws.execution_backend = "tofu"
    return ws


def _mock_var(key="region", value="us-east-1", sensitive=False, ws_id=None, var_id=None):
    var = MagicMock()
    var.id = var_id or uuid.uuid4()
    var.workspace_id = ws_id or uuid.uuid4()
    var.key = key
    var.value = value
    var.sensitive = sensitive
    var.category = "terraform"
    var.hcl = False
    var.description = ""
    var.version_id = "abc123"
    var.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    var.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return var


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    # Also override require_admin for varset endpoints
    if "admin" in (user.roles or []):
        app.dependency_overrides[require_admin] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


# ── List Variables ─────────────────────────────────────────────────────


class TestListVariables:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.list_variables")
    @patch("terrapod.api.routers.variables.resolve_workspace_permission")
    async def test_list_with_read_perm(self, mock_resolve, mock_list, *mocks):
        mock_resolve.return_value = "read"
        ws = _mock_workspace()
        var = _mock_var(ws_id=ws.id)
        mock_list.return_value = [var]

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/workspaces/ws-{ws.id}/vars", headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 1
        assert data[0]["attributes"]["key"] == "region"
        assert data[0]["attributes"]["value"] == "us-east-1"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.list_variables")
    @patch("terrapod.api.routers.variables.resolve_workspace_permission")
    async def test_sensitive_values_masked(self, mock_resolve, mock_list, *mocks):
        mock_resolve.return_value = "read"
        ws = _mock_workspace()
        var = _mock_var(key="secret", sensitive=True, ws_id=ws.id)
        mock_list.return_value = [var]

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/workspaces/ws-{ws.id}/vars", headers=_AUTH)
        data = resp.json()["data"]
        assert data[0]["attributes"]["value"] is None
        assert data[0]["attributes"]["sensitive"] is True

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.resolve_workspace_permission")
    async def test_list_no_permission_returns_403(self, mock_resolve, *mocks):
        mock_resolve.return_value = None
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/workspaces/ws-{ws.id}/vars", headers=_AUTH)
        assert resp.status_code == 403


# ── Create Variable ────────────────────────────────────────────────────


class TestCreateVariable:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.create_variable")
    @patch("terrapod.api.routers.variables.resolve_workspace_permission")
    async def test_create_with_write_perm(self, mock_resolve, mock_create, *mocks):
        mock_resolve.return_value = "write"
        ws = _mock_workspace()
        var = _mock_var(ws_id=ws.id)
        mock_create.return_value = var

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/vars",
                json={
                    "data": {
                        "attributes": {
                            "key": "region",
                            "value": "us-east-1",
                            "category": "terraform",
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 201

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.resolve_workspace_permission")
    async def test_create_missing_key_returns_422(self, mock_resolve, *mocks):
        mock_resolve.return_value = "write"
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/vars",
                json={"data": {"attributes": {"value": "val"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.resolve_workspace_permission")
    async def test_create_read_only_returns_403(self, mock_resolve, *mocks):
        mock_resolve.return_value = "read"
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/vars",
                json={"data": {"attributes": {"key": "k", "value": "v"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.create_variable")
    @patch("terrapod.api.routers.variables.resolve_workspace_permission")
    async def test_create_encryption_error_returns_422(self, mock_resolve, mock_create, *mocks):
        mock_resolve.return_value = "write"
        mock_create.side_effect = ValueError("encryption not configured")
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/vars",
                json={"data": {"attributes": {"key": "s", "value": "x", "sensitive": True}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422


# ── Update Variable ────────────────────────────────────────────────────


class TestUpdateVariable:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.update_variable")
    @patch("terrapod.api.routers.variables.variable_service.get_variable")
    @patch("terrapod.api.routers.variables.resolve_workspace_permission")
    async def test_update_with_write_perm(self, mock_resolve, mock_get, mock_update, *mocks):
        mock_resolve.return_value = "write"
        ws = _mock_workspace()
        var = _mock_var(ws_id=ws.id)
        mock_get.return_value = var
        mock_update.return_value = var

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}/vars/var-{var.id}",
                json={"data": {"attributes": {"value": "new-val"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.get_variable")
    @patch("terrapod.api.routers.variables.resolve_workspace_permission")
    async def test_update_not_found_returns_404(self, mock_resolve, mock_get, *mocks):
        mock_resolve.return_value = "write"
        mock_get.return_value = None
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}/vars/var-{uuid.uuid4()}",
                json={"data": {"attributes": {"value": "x"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 404


# ── Delete Variable ────────────────────────────────────────────────────


class TestDeleteVariable:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.variables.variable_service.delete_variable")
    @patch("terrapod.api.routers.variables.variable_service.get_variable")
    @patch("terrapod.api.routers.variables.resolve_workspace_permission")
    async def test_delete_with_write_perm(self, mock_resolve, mock_get, mock_delete, *mocks):
        mock_resolve.return_value = "write"
        ws = _mock_workspace()
        var = _mock_var(ws_id=ws.id)
        mock_get.return_value = var

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(
                f"/api/v2/workspaces/ws-{ws.id}/vars/var-{var.id}",
                headers=_AUTH,
            )
        assert resp.status_code == 204


# ── Variable Sets ─────────────────────────────────────────────────────


def _mock_varset(name="my-varset", vs_id=None):
    vs = MagicMock()
    vs.id = vs_id or uuid.uuid4()
    vs.name = name
    vs.description = ""
    vs.global_set = False
    vs.priority = False
    vs.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    vs.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return vs


class TestVariableSetCRUD:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_list_varsets(self, *mocks):
        user = _user()
        app, mock_db = _make_app(user)
        vs = _mock_varset()
        mock_result = MagicMock()
        mock_result.scalars.return_value.all.return_value = [vs]
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/v2/organizations/default/varsets", headers=_AUTH)
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 1

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_varset_requires_admin(self, *mocks):
        """Non-admin cannot create variable sets."""
        user = _user(roles=["everyone"])  # not admin
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/organizations/default/varsets",
                json={"data": {"attributes": {"name": "test"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_varset_admin_returns_201(self, *mocks):
        user = _user(roles=["admin"])
        app, mock_db = _make_app(user)
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/organizations/default/varsets",
                json={"data": {"attributes": {"name": "test-set"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 201

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_delete_varset_requires_admin(self, *mocks):
        user = _user(roles=["everyone"])
        app, _ = _make_app(user)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(
                f"/api/v2/varsets/varset-{uuid.uuid4()}",
                headers=_AUTH,
            )
        assert resp.status_code == 403
