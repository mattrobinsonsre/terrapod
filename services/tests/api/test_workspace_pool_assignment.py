"""Tests for workspace agent pool assignment with pool RBAC gating."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
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


def _mock_workspace(ws_id=None, pool_id=None):
    ws = MagicMock()
    ws.id = ws_id or uuid.uuid4()
    ws.name = "test-ws"
    ws.execution_mode = "agent"
    ws.auto_apply = False
    ws.execution_backend = "tofu"
    ws.terraform_version = "1.11"
    ws.working_directory = ""
    ws.locked = False
    ws.lock_id = None
    ws.agent_pool_id = pool_id
    ws.agent_pool = None
    ws.resource_cpu = "1"
    ws.resource_memory = "2Gi"
    ws.labels = {}
    ws.owner_email = "test@example.com"
    ws.vcs_connection_id = None
    ws.vcs_connection = None
    ws.vcs_repo_url = ""
    ws.vcs_branch = ""
    ws.vcs_last_commit_sha = ""
    ws.vcs_last_polled_at = None
    ws.vcs_last_error = None
    ws.vcs_last_error_at = None
    ws.var_files = []
    ws.trigger_prefixes = []
    ws.drift_detection_enabled = False
    ws.drift_detection_interval_seconds = 86400
    ws.drift_last_checked_at = None
    ws.drift_status = ""
    ws.state_diverged = False
    ws.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    ws.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return ws


def _mock_pool(pool_id=None, name="test-pool", labels=None, owner_email=None):
    pool = MagicMock()
    pool.id = pool_id or uuid.uuid4()
    pool.name = name
    pool.labels = labels or {}
    pool.owner_email = owner_email
    return pool


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


class TestWorkspacePoolAssignment:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    @patch("terrapod.api.routers.tfe_v2._agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.services.workspace_rbac_service.resolve_workspace_permission",
        new_callable=AsyncMock,
    )
    async def test_assign_pool_with_write_permission(
        self, mock_ws_perm, mock_get_pool, mock_pool_perm, *mocks
    ):
        """User with write on pool can assign it to workspace."""
        user = _user(roles=["everyone", "pool-writer"])
        pool = _mock_pool(name="prod-pool")
        ws = _mock_workspace()

        mock_ws_perm.return_value = "admin"
        mock_get_pool.return_value = pool
        mock_pool_perm.return_value = "write"

        app, mock_db = _make_app(user)

        # Mock workspace lookup
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = ws_result
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={
                    "data": {
                        "attributes": {
                            "agent-pool-id": f"apool-{pool.id}",
                        }
                    }
                },
                headers=_AUTH,
            )

        assert res.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    @patch("terrapod.api.routers.tfe_v2._agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.services.workspace_rbac_service.resolve_workspace_permission",
        new_callable=AsyncMock,
    )
    async def test_assign_pool_without_write_permission_403(
        self, mock_ws_perm, mock_get_pool, mock_pool_perm, *mocks
    ):
        """User without write on pool gets 403."""
        user = _user(roles=["everyone"])
        pool = _mock_pool(name="restricted-pool")
        ws = _mock_workspace()

        mock_ws_perm.return_value = "admin"
        mock_get_pool.return_value = pool
        mock_pool_perm.return_value = "read"  # Only read, not write

        app, mock_db = _make_app(user)

        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = ws_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={
                    "data": {
                        "attributes": {
                            "agent-pool-id": f"apool-{pool.id}",
                        }
                    }
                },
                headers=_AUTH,
            )

        assert res.status_code == 403
        assert "write permission" in res.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch(
        "terrapod.services.workspace_rbac_service.resolve_workspace_permission",
        new_callable=AsyncMock,
    )
    async def test_clear_pool_no_permission_check(self, mock_ws_perm, *mocks):
        """Clearing pool (setting null) does not require pool permission."""
        user = _user(roles=["everyone"])
        ws = _mock_workspace(pool_id=uuid.uuid4())

        mock_ws_perm.return_value = "admin"

        app, mock_db = _make_app(user)

        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = ws_result
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={
                    "data": {
                        "attributes": {
                            "agent-pool-id": None,
                        }
                    }
                },
                headers=_AUTH,
            )

        assert res.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch(
        "terrapod.api.routers.tfe_v2.resolve_pool_permission",
        new_callable=AsyncMock,
    )
    @patch("terrapod.api.routers.tfe_v2._agent_pool_service.get_pool", new_callable=AsyncMock)
    @patch(
        "terrapod.services.workspace_rbac_service.resolve_workspace_permission",
        new_callable=AsyncMock,
    )
    async def test_platform_admin_bypasses_pool_check(
        self, mock_ws_perm, mock_get_pool, mock_pool_perm, *mocks
    ):
        """Platform admin can assign any pool."""
        user = _user(email="admin@example.com", roles=["admin"])
        pool = _mock_pool(name="restricted-pool")
        ws = _mock_workspace()

        mock_ws_perm.return_value = "admin"
        mock_get_pool.return_value = pool
        mock_pool_perm.return_value = "admin"  # admin resolves to admin

        app, mock_db = _make_app(user)

        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = ws_result
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            res = await client.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={
                    "data": {
                        "attributes": {
                            "agent-pool-id": f"apool-{pool.id}",
                        }
                    }
                },
                headers=_AUTH,
            )

        assert res.status_code == 200
