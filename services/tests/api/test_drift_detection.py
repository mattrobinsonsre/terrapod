"""Tests for drift detection workspace attributes and health dashboard endpoint."""

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


def _mock_workspace(ws_id=None, name="test-ws", **overrides):
    ws = MagicMock()
    ws.id = ws_id or uuid.uuid4()
    ws.name = name
    ws.auto_apply = False
    ws.execution_mode = "remote"
    ws.terraform_version = "1.11"
    ws.working_directory = ""
    ws.locked = False
    ws.lock_id = None
    ws.resource_cpu = "1"
    ws.resource_memory = "2Gi"
    ws.vcs_repo_url = ""
    ws.vcs_branch = ""
    ws.vcs_connection_id = None
    ws.vcs_connection = None
    ws.labels = {}
    ws.owner_email = "test@example.com"
    ws.drift_detection_enabled = overrides.get("drift_detection_enabled", False)
    ws.drift_detection_interval_seconds = overrides.get("drift_detection_interval_seconds", 86400)
    ws.drift_last_checked_at = overrides.get("drift_last_checked_at", None)
    ws.drift_status = overrides.get("drift_status", "")
    ws.state_diverged = overrides.get("state_diverged", False)
    ws.execution_backend = overrides.get("execution_backend", "tofu")
    ws.agent_pool = None
    ws.agent_pool_id = overrides.get("agent_pool_id", None)
    ws.var_files = []
    ws.trigger_prefixes = []
    ws.vcs_last_polled_at = None
    ws.vcs_last_error = None
    ws.vcs_last_error_at = None
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


# ── Workspace Drift Attributes ────────────────────────────────────────


class TestWorkspaceDriftAttributes:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_workspace_includes_drift_attributes(self, mock_resolve, *mocks):
        """GET workspace includes drift fields in response."""
        mock_resolve.return_value = "read"
        ws = _mock_workspace(drift_detection_enabled=True, drift_status="no_drift")

        app, mock_db = _make_app(_user())
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        no_run_result = MagicMock()
        no_run_result.scalar_one_or_none.return_value = None
        mock_db.execute.side_effect = [ws_result, no_run_result]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/workspaces/ws-{ws.id}", headers=_AUTH)
        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["drift-detection-enabled"] is True
        assert attrs["drift-detection-interval-seconds"] == 86400
        assert attrs["drift-status"] == "no_drift"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_update_drift_settings(self, mock_resolve, *mocks):
        """PATCH workspace drift-detection-enabled updates model."""
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={
                    "data": {
                        "attributes": {
                            "drift-detection-enabled": True,
                            "drift-detection-interval-seconds": 7200,
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert ws.drift_detection_enabled is True
        assert ws.drift_detection_interval_seconds == 7200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_drift_interval_minimum_enforced(self, mock_resolve, *mocks):
        """Interval below minimum is clamped to configured floor."""
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={
                    "data": {
                        "attributes": {
                            "drift-detection-interval-seconds": 60,
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 200
        # Should be clamped to min (default 3600)
        assert ws.drift_detection_interval_seconds >= 3600


# ── Run Drift Attributes ─────────────────────────────────────────────


class TestRunDriftAttributes:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission")
    async def test_run_includes_drift_fields(self, mock_resolve, *mocks):
        """GET run includes is-drift-detection and has-changes."""
        mock_resolve.return_value = "read"
        run_id = uuid.uuid4()
        ws_id = uuid.uuid4()
        run = MagicMock()
        run.id = run_id
        run.workspace_id = ws_id
        run.configuration_version_id = None
        run.status = "planned"
        run.message = "Drift detection check"
        run.is_destroy = False
        run.auto_apply = False
        run.plan_only = True
        run.source = "drift-detection"
        run.terraform_version = "1.11"
        run.error_message = ""
        run.is_drift_detection = True
        run.has_changes = True
        run.vcs_commit_sha = ""
        run.vcs_branch = ""
        run.vcs_pull_request_number = None
        run.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        run.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
        run.plan_started_at = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
        run.plan_finished_at = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
        run.apply_started_at = None
        run.apply_finished_at = None
        run.execution_backend = "tofu"
        run.listener_id = None
        run.target_addrs = None
        run.replace_addrs = None
        run.refresh_only = False
        run.refresh = True
        run.allow_empty_apply = False
        run.resource_cpu = "1"
        run.resource_memory = "2Gi"
        run.module_overrides = None

        ws = _mock_workspace(ws_id=ws_id)

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = run
        mock_db.execute.return_value = mock_result
        mock_db.get.return_value = ws

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/runs/run-{run_id}", headers=_AUTH)
        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["is-drift-detection"] is True
        assert attrs["has-changes"] is True
