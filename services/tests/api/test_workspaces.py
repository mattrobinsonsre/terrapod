"""Tests for workspace CRUD and lock/unlock endpoints with RBAC."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}


def _user(email="test@example.com", roles=None, auth_method="session"):
    return AuthenticatedUser(
        email=email,
        display_name="Test",
        roles=roles or ["everyone"],
        provider_name="local",
        auth_method=auth_method,
    )


def _mock_workspace(
    name="my-ws",
    ws_id=None,
    owner_email="",
    labels=None,
    locked=False,
    lock_id=None,
    auto_apply=False,
    execution_mode="local",
    terraform_version="1.9.0",
    resource_cpu="1",
    resource_memory="2Gi",
    org_name="default",
):
    ws = MagicMock()
    ws.id = ws_id or uuid.uuid4()
    ws.name = name
    ws.org_name = org_name
    ws.auto_apply = auto_apply
    ws.execution_mode = execution_mode
    ws.terraform_version = terraform_version
    ws.working_directory = ""
    ws.locked = locked
    ws.lock_id = lock_id
    ws.resource_cpu = resource_cpu
    ws.resource_memory = resource_memory
    ws.labels = labels or {}
    ws.owner_email = owner_email
    ws.vcs_connection_id = None
    ws.vcs_repo_url = ""
    ws.vcs_branch = ""
    ws.vcs_working_directory = ""
    ws.drift_detection_enabled = False
    ws.drift_detection_interval_seconds = 86400
    ws.drift_last_checked_at = None
    ws.drift_status = ""
    ws.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    ws.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return ws


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user

    if mock_db is None:
        mock_db = AsyncMock()

    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


# ── Create Workspace ────────────────────────────────────────────────────


class TestCreateWorkspace:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_returns_201(self, *mocks):
        user = _user(roles=["admin"])
        app, mock_db = _make_app(user)
        # No existing workspace
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/organizations/default/workspaces",
                json={"data": {"type": "workspaces", "attributes": {"name": "new-ws"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 201
        data = resp.json()["data"]
        assert data["type"] == "workspaces"
        assert data["attributes"]["name"] == "new-ws"
        assert data["attributes"]["owner-email"] == user.email

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_duplicate_returns_422(self, *mocks):
        user = _user()
        app, mock_db = _make_app(user)
        # Existing workspace found
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = MagicMock()
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/organizations/default/workspaces",
                json={"data": {"attributes": {"name": "existing"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_missing_name_returns_422(self, *mocks):
        app, _ = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/organizations/default/workspaces",
                json={"data": {"attributes": {}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_wrong_org_returns_404(self, *mocks):
        app, _ = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/organizations/other-org/workspaces",
                json={"data": {"attributes": {"name": "ws"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 404


# ── Show Workspace ─────────────────────────────────────────────────────


class TestShowWorkspace:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_show_by_name(self, mock_resolve, *mocks):
        mock_resolve.return_value = "read"
        ws = _mock_workspace(name="test-ws")
        user = _user()
        app, mock_db = _make_app(user)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                "/api/v2/organizations/default/workspaces/test-ws",
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["name"] == "test-ws"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_show_no_permission_returns_404(self, mock_resolve, *mocks):
        """TFE behavior: workspace invisible (404) when no permission, not 403."""
        mock_resolve.return_value = None
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                "/api/v2/organizations/default/workspaces/my-ws",
                headers=_AUTH,
            )
        assert resp.status_code == 404

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_show_not_found_returns_404(self, *mocks):
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                "/api/v2/organizations/default/workspaces/nope",
                headers=_AUTH,
            )
        assert resp.status_code == 404


# ── Show Workspace by ID ──────────────────────────────────────────────


class TestShowWorkspaceById:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_show_by_id_with_read(self, mock_resolve, *mocks):
        mock_resolve.return_value = "read"
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/v2/workspaces/ws-{ws.id}",
                headers=_AUTH,
            )
        assert resp.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_show_by_id_no_permission_returns_403(self, mock_resolve, *mocks):
        mock_resolve.return_value = None
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/workspaces/ws-{ws.id}", headers=_AUTH)
        assert resp.status_code == 403


# ── Update Workspace ──────────────────────────────────────────────────


class TestUpdateWorkspace:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_update_requires_admin(self, mock_resolve, *mocks):
        mock_resolve.return_value = "write"  # not admin
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={"data": {"attributes": {"auto-apply": True}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_update_owner_requires_platform_admin(self, mock_resolve, *mocks):
        """owner-email change requires platform admin, not just workspace admin."""
        mock_resolve.return_value = "admin"
        ws = _mock_workspace(owner_email="old@test.com")
        # User is workspace admin via ownership but NOT platform admin
        user = _user(email="old@test.com", roles=["everyone"])
        app, mock_db = _make_app(user)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={"data": {"attributes": {"owner-email": "new@test.com"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403


# ── Delete Workspace ──────────────────────────────────────────────────


class TestDeleteWorkspace:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_delete_with_admin_returns_204(self, mock_resolve, *mocks):
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()
        app, mock_db = _make_app(_user(roles=["admin"]))
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(f"/api/v2/workspaces/ws-{ws.id}", headers=_AUTH)
        assert resp.status_code == 204

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_delete_without_admin_returns_403(self, mock_resolve, *mocks):
        mock_resolve.return_value = "write"
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(f"/api/v2/workspaces/ws-{ws.id}", headers=_AUTH)
        assert resp.status_code == 403


# ── Lock / Unlock ─────────────────────────────────────────────────────


class TestLockWorkspace:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_lock_with_plan_permission(self, mock_resolve, *mocks):
        mock_resolve.return_value = "plan"
        ws = _mock_workspace(locked=False)
        user = _user()
        app, mock_db = _make_app(user)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/actions/lock",
                headers=_AUTH,
            )
        assert resp.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_lock_already_locked_returns_409(self, mock_resolve, *mocks):
        mock_resolve.return_value = "plan"
        ws = _mock_workspace(locked=True, lock_id="lock-other@test.com")
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/actions/lock",
                headers=_AUTH,
            )
        assert resp.status_code == 409

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_lock_read_only_returns_403(self, mock_resolve, *mocks):
        mock_resolve.return_value = "read"
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/actions/lock",
                headers=_AUTH,
            )
        assert resp.status_code == 403


class TestUnlockWorkspace:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_unlock_own_lock(self, mock_resolve, *mocks):
        mock_resolve.return_value = "plan"
        ws = _mock_workspace(locked=True, lock_id="lock-test@example.com")
        user = _user(email="test@example.com")
        app, mock_db = _make_app(user)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/actions/unlock",
                headers=_AUTH,
            )
        assert resp.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_force_unlock_requires_admin(self, mock_resolve, *mocks):
        """Non-admin with plan perm can't force-unlock another user's lock."""
        mock_resolve.return_value = "plan"
        ws = _mock_workspace(locked=True, lock_id="lock-other@test.com")
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/actions/unlock",
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_admin_can_force_unlock(self, mock_resolve, *mocks):
        mock_resolve.return_value = "admin"
        ws = _mock_workspace(locked=True, lock_id="lock-other@test.com")
        app, mock_db = _make_app(_user(roles=["admin"]))
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/actions/unlock",
                headers=_AUTH,
            )
        assert resp.status_code == 200


# ── Permissions block ─────────────────────────────────────────────────


class TestPermissionsBlock:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_read_user_permissions(self, mock_resolve, *mocks):
        """Read permission: can read, but not update/destroy/queue."""
        mock_resolve.return_value = "read"
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/workspaces/ws-{ws.id}", headers=_AUTH)

        perms = resp.json()["data"]["attributes"]["permissions"]
        assert perms["can-read-state-versions"] is True
        assert perms["can-read-variable"] is True
        assert perms["can-update"] is False
        assert perms["can-destroy"] is False
        assert perms["can-queue-run"] is False
        assert perms["can-lock"] is False

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_admin_user_permissions(self, mock_resolve, *mocks):
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()
        app, mock_db = _make_app(_user(roles=["admin"]))
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/workspaces/ws-{ws.id}", headers=_AUTH)

        perms = resp.json()["data"]["attributes"]["permissions"]
        assert perms["can-update"] is True
        assert perms["can-destroy"] is True
        assert perms["can-queue-run"] is True
        assert perms["can-lock"] is True
        assert perms["can-force-unlock"] is True
