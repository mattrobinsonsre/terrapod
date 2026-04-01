"""Tests for state management endpoints (delete, rollback, upload)."""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.models import StateVersion
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


def _mock_workspace(ws_id=None, name="test-ws", owner_email=""):
    ws = MagicMock()
    ws.id = ws_id or uuid.uuid4()
    ws.name = name
    ws.owner_email = owner_email
    ws.labels = {}
    ws.vcs_connection_id = None
    ws.vcs_connection = None
    ws.vcs_repo_url = ""
    return ws


def _mock_state_version(ws_id, serial=1, sv_id=None, run_id=None, created_by=None):
    sv = MagicMock(spec=StateVersion)
    sv.id = sv_id or uuid.uuid4()
    sv.workspace_id = ws_id
    sv.serial = serial
    sv.lineage = "test-lineage"
    sv.md5 = "abc123"
    sv.state_size = 100
    sv.run_id = run_id
    sv.created_by = created_by
    sv.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    return sv


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


def _scalar_result(value):
    """Create a mock result object for select queries."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.scalar_one.return_value = value
    return result


# ── Delete State Version ─────────────────────────────────────────────


class TestDeleteStateVersion:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.redis.client.publish_workspace_event", new_callable=AsyncMock)
    @patch("terrapod.api.routers.state_management.get_storage")
    @patch("terrapod.api.routers.state_management.resolve_workspace_permission")
    async def test_delete_non_current_state_version(
        self,
        mock_resolve,
        mock_get_storage,
        mock_publish,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        ws_id = uuid.uuid4()
        ws = _mock_workspace(ws_id=ws_id, owner_email="test@example.com")
        sv = _mock_state_version(ws_id, serial=1)

        mock_resolve.return_value = "admin"

        mock_db = AsyncMock()
        # First execute: get state version
        # Second execute: get max serial
        mock_db.execute.side_effect = [
            _scalar_result(sv),
            _scalar_result(3),  # max serial is 3, sv.serial is 1 => not current
        ]
        mock_db.get.return_value = ws

        mock_storage = AsyncMock()
        mock_get_storage.return_value = mock_storage

        user = _user(email="test@example.com", roles=["admin"])
        app, _ = _make_app(user, mock_db)

        sv_id = f"sv-{sv.id}"
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.delete(f"/api/v2/state-versions/{sv_id}/manage", headers=_AUTH)

        assert resp.status_code == 204
        mock_db.delete.assert_called_once_with(sv)
        mock_db.commit.assert_called_once()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.state_management.resolve_workspace_permission")
    async def test_delete_current_state_version_rejected(
        self,
        mock_resolve,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        ws_id = uuid.uuid4()
        ws = _mock_workspace(ws_id=ws_id, owner_email="test@example.com")
        sv = _mock_state_version(ws_id, serial=3)

        mock_resolve.return_value = "admin"

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            _scalar_result(sv),
            _scalar_result(3),  # max serial matches sv.serial => current
        ]
        mock_db.get.return_value = ws

        user = _user(email="test@example.com", roles=["admin"])
        app, _ = _make_app(user, mock_db)

        sv_id = f"sv-{sv.id}"
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.delete(f"/api/v2/state-versions/{sv_id}/manage", headers=_AUTH)

        assert resp.status_code == 409
        assert "current" in resp.json()["detail"].lower()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.state_management.resolve_workspace_permission")
    async def test_delete_state_requires_admin(
        self,
        mock_resolve,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        ws_id = uuid.uuid4()
        ws = _mock_workspace(ws_id=ws_id)
        sv = _mock_state_version(ws_id, serial=1)

        mock_resolve.return_value = "write"  # not admin

        mock_db = AsyncMock()
        mock_db.execute.return_value = _scalar_result(sv)
        mock_db.get.return_value = ws

        user = _user(roles=["everyone"])
        app, _ = _make_app(user, mock_db)

        sv_id = f"sv-{sv.id}"
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.delete(f"/api/v2/state-versions/{sv_id}/manage", headers=_AUTH)

        assert resp.status_code == 403


# ── Rollback State Version ───────────────────────────────────────────


class TestRollbackStateVersion:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.redis.client.publish_workspace_event", new_callable=AsyncMock)
    @patch("terrapod.api.metrics.STATE_VERSIONS_CREATED")
    @patch("terrapod.api.routers.tfe_v2._state_version_json")
    @patch("terrapod.api.routers.state_management.get_storage")
    @patch("terrapod.api.routers.state_management.resolve_workspace_permission")
    async def test_rollback_creates_new_version(
        self,
        mock_resolve,
        mock_get_storage,
        mock_sv_json,
        mock_counter,
        mock_publish,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        ws_id = uuid.uuid4()
        ws = _mock_workspace(ws_id=ws_id, owner_email="test@example.com")
        sv = _mock_state_version(ws_id, serial=1)

        mock_resolve.return_value = "write"

        state_bytes = b'{"version": 4, "serial": 1, "lineage": "test"}'
        mock_storage = AsyncMock()
        mock_storage.get.return_value = state_bytes
        mock_get_storage.return_value = mock_storage

        mock_sv_json.return_value = {
            "data": {"id": "sv-new", "type": "state-versions", "attributes": {}}
        }

        mock_db = AsyncMock()
        mock_db.execute.side_effect = [
            _scalar_result(sv),  # get state version
            _scalar_result(3),  # get max serial
        ]
        mock_db.get.return_value = ws

        user = _user(email="test@example.com")
        app, _ = _make_app(user, mock_db)

        sv_id = f"sv-{sv.id}"
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.post(
                f"/api/v2/state-versions/{sv_id}/actions/rollback", headers=_AUTH
            )

        assert resp.status_code == 201
        mock_db.add.assert_called_once()
        mock_storage.put.assert_called_once()
        mock_counter.inc.assert_called_once()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.state_management.get_storage")
    @patch("terrapod.api.routers.state_management.resolve_workspace_permission")
    async def test_rollback_missing_storage_returns_404(
        self,
        mock_resolve,
        mock_get_storage,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        ws_id = uuid.uuid4()
        ws = _mock_workspace(ws_id=ws_id, owner_email="test@example.com")
        sv = _mock_state_version(ws_id, serial=1)

        mock_resolve.return_value = "write"

        mock_storage = AsyncMock()
        mock_storage.get.side_effect = Exception("Not found")
        mock_get_storage.return_value = mock_storage

        mock_db = AsyncMock()
        mock_db.execute.return_value = _scalar_result(sv)
        mock_db.get.return_value = ws

        user = _user(email="test@example.com")
        app, _ = _make_app(user, mock_db)

        sv_id = f"sv-{sv.id}"
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.post(
                f"/api/v2/state-versions/{sv_id}/actions/rollback", headers=_AUTH
            )

        assert resp.status_code == 404


# ── Upload State ─────────────────────────────────────────────────────


class TestUploadState:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.redis.client.publish_workspace_event", new_callable=AsyncMock)
    @patch("terrapod.api.metrics.STATE_VERSIONS_CREATED")
    @patch("terrapod.api.routers.tfe_v2._state_version_json")
    @patch("terrapod.api.routers.state_management.get_storage")
    @patch("terrapod.api.routers.state_management.resolve_workspace_permission")
    @patch("terrapod.api.routers.tfe_v2._get_workspace_by_id")
    async def test_upload_state_manual(
        self,
        mock_get_ws,
        mock_resolve,
        mock_get_storage,
        mock_sv_json,
        mock_counter,
        mock_publish,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        ws_id = uuid.uuid4()
        ws = _mock_workspace(ws_id=ws_id)
        mock_get_ws.return_value = ws
        mock_resolve.return_value = "write"

        mock_sv_json.return_value = {
            "data": {"id": "sv-new", "type": "state-versions", "attributes": {}}
        }

        mock_storage = AsyncMock()
        mock_get_storage.return_value = mock_storage

        mock_db = AsyncMock()
        mock_db.execute.return_value = _scalar_result(5)  # max serial

        user = _user(email="test@example.com")
        app, _ = _make_app(user, mock_db)

        state_json = json.dumps({"version": 4, "serial": 1, "lineage": "test-lineage"})
        ws_id_str = f"ws-{ws_id}"
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.post(
                f"/api/v2/workspaces/{ws_id_str}/state-versions/actions/upload",
                content=state_json,
                headers={**_AUTH, "Content-Type": "application/json"},
            )

        assert resp.status_code == 201
        mock_db.add.assert_called_once()
        mock_storage.put.assert_called_once()
        mock_counter.inc.assert_called_once()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.state_management.resolve_workspace_permission")
    @patch("terrapod.api.routers.tfe_v2._get_workspace_by_id")
    async def test_upload_state_requires_write(
        self,
        mock_get_ws,
        mock_resolve,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        ws = _mock_workspace()
        mock_get_ws.return_value = ws
        mock_resolve.return_value = "read"  # not write

        mock_db = AsyncMock()
        user = _user(roles=["everyone"])
        app, _ = _make_app(user, mock_db)

        ws_id_str = f"ws-{ws.id}"
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.post(
                f"/api/v2/workspaces/{ws_id_str}/state-versions/actions/upload",
                content='{"version": 4}',
                headers={**_AUTH, "Content-Type": "application/json"},
            )

        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.state_management.resolve_workspace_permission")
    @patch("terrapod.api.routers.tfe_v2._get_workspace_by_id")
    async def test_upload_invalid_json_returns_400(
        self,
        mock_get_ws,
        mock_resolve,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        ws = _mock_workspace()
        mock_get_ws.return_value = ws
        mock_resolve.return_value = "write"

        mock_db = AsyncMock()
        user = _user(email="test@example.com")
        app, _ = _make_app(user, mock_db)

        ws_id_str = f"ws-{ws.id}"
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.post(
                f"/api/v2/workspaces/{ws_id_str}/state-versions/actions/upload",
                content="not json {{{",
                headers={**_AUTH, "Content-Type": "application/json"},
            )

        assert resp.status_code == 400


# ── State Version Serializer ─────────────────────────────────────────


class TestStateVersionJson:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_state_version_includes_created_by(
        self,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        from terrapod.api.routers.tfe_v2 import _state_version_json

        sv = _mock_state_version(uuid.uuid4(), serial=1, created_by="user@example.com")
        with patch("terrapod.config.settings") as mock_settings:
            mock_settings.auth.callback_base_url = "https://test.local"
            result = _state_version_json(sv)

        assert result["data"]["attributes"]["created-by"] == "user@example.com"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_state_version_includes_run_relationship(
        self,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        from terrapod.api.routers.tfe_v2 import _state_version_json

        run_id = uuid.uuid4()
        sv = _mock_state_version(uuid.uuid4(), serial=1, run_id=run_id)
        with patch("terrapod.config.settings") as mock_settings:
            mock_settings.auth.callback_base_url = "https://test.local"
            result = _state_version_json(sv)

        assert result["data"]["relationships"]["run"]["data"]["id"] == f"run-{run_id}"
        assert result["data"]["relationships"]["run"]["data"]["type"] == "runs"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_state_version_null_run_relationship(
        self,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        from terrapod.api.routers.tfe_v2 import _state_version_json

        sv = _mock_state_version(uuid.uuid4(), serial=1, run_id=None)
        with patch("terrapod.config.settings") as mock_settings:
            mock_settings.auth.callback_base_url = "https://test.local"
            result = _state_version_json(sv)

        assert result["data"]["relationships"]["run"]["data"] is None


# ── Run Detail State Version Link ────────────────────────────────────


class TestRunDetailStateVersion:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission")
    @patch("terrapod.api.routers.runs.run_service.get_run")
    async def test_show_run_includes_state_version(
        self,
        mock_get_run,
        mock_resolve,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        from terrapod.api.routers.runs import _run_json

        run_id = uuid.uuid4()
        sv_id = uuid.uuid4()

        result = _run_json(
            _mock_run(run_id=run_id),
            workspace_has_vcs=False,
            state_version_id=f"sv-{sv_id}",
        )

        csv_rel = result["data"]["relationships"]["created-state-version"]
        assert csv_rel["data"]["id"] == f"sv-{sv_id}"
        assert csv_rel["data"]["type"] == "state-versions"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_run_json_null_state_version(
        self,
        mock_init_db,
        mock_init_redis,
        mock_init_storage,
    ):
        from terrapod.api.routers.runs import _run_json

        result = _run_json(
            _mock_run(),
            workspace_has_vcs=False,
            state_version_id=None,
        )

        csv_rel = result["data"]["relationships"]["created-state-version"]
        assert csv_rel["data"] is None


def _mock_run(run_id=None, status="pending", ws_id=None):
    """Minimal mock run for serializer tests."""

    run = MagicMock()
    run.id = run_id or uuid.uuid4()
    run.workspace_id = ws_id or uuid.uuid4()
    run.status = status
    run.message = ""
    run.is_destroy = False
    run.auto_apply = False
    run.plan_only = False
    run.source = "tfe-api"
    run.terraform_version = "1.11"
    run.error_message = ""
    run.is_drift_detection = False
    run.has_changes = None
    run.vcs_commit_sha = ""
    run.vcs_branch = ""
    run.execution_backend = "tofu"
    run.vcs_pull_request_number = None
    run.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    run.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    run.plan_started_at = None
    run.plan_finished_at = None
    run.apply_started_at = None
    run.apply_finished_at = None
    run.listener_id = None
    run.target_addrs = None
    run.replace_addrs = None
    run.refresh_only = False
    run.refresh = True
    run.allow_empty_apply = False
    run.resource_cpu = "1"
    run.resource_memory = "2Gi"
    run.configuration_version_id = None
    run.module_overrides = None
    return run
