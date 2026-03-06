"""Tests for run task CRUD, callback, and override endpoints."""

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


def _mock_workspace(ws_id=None, name="test-ws"):
    ws = MagicMock()
    ws.id = ws_id or uuid.uuid4()
    ws.name = name
    ws.auto_apply = False
    ws.execution_backend = "tofu"
    ws.labels = {}
    ws.owner_email = "test@example.com"
    return ws


def _mock_run_task(rt_id=None, ws=None, name="OPA Check", stage="post_plan"):
    rt = MagicMock()
    rt.id = rt_id or uuid.uuid4()
    ws = ws or _mock_workspace()
    rt.workspace_id = ws.id
    rt.workspace = ws
    rt.name = name
    rt.url = "https://opa.example.com/check"
    rt.hmac_key = None
    rt.enabled = True
    rt.stage = stage
    rt.enforcement_level = "mandatory"
    rt.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    rt.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return rt


def _mock_task_stage(ts_id=None, run_id=None, stage="post_plan", status="running"):
    ts = MagicMock()
    ts.id = ts_id or uuid.uuid4()
    ts.run_id = run_id or uuid.uuid4()
    ts.stage = stage
    ts.status = status
    ts.results = []
    ts.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    ts.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return ts


def _mock_task_stage_result(tsr_id=None, ts_id=None, rt_id=None, status="pending"):
    tsr = MagicMock()
    tsr.id = tsr_id or uuid.uuid4()
    tsr.task_stage_id = ts_id or uuid.uuid4()
    tsr.run_task_id = rt_id or uuid.uuid4()
    tsr.status = status
    tsr.message = ""
    tsr.callback_token = "test-token"
    tsr.started_at = None
    tsr.finished_at = None
    tsr.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    tsr.run_task = _mock_run_task(rt_id=tsr.run_task_id)
    return tsr


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


# ── Create ────────────────────────────────────────────────────────────


class TestCreateRunTask:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_tasks.resolve_workspace_permission")
    async def test_create(self, mock_resolve, *mocks):
        """Create run task → 201."""
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/run-tasks",
                json={
                    "data": {
                        "type": "run-tasks",
                        "attributes": {
                            "name": "OPA Check",
                            "url": "https://opa.example.com/check",
                            "stage": "post_plan",
                            "enforcement-level": "mandatory",
                        },
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 201
        assert resp.json()["data"]["type"] == "run-tasks"
        assert resp.json()["data"]["attributes"]["stage"] == "post_plan"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_tasks.resolve_workspace_permission")
    async def test_create_invalid_stage(self, mock_resolve, *mocks):
        """Invalid stage → 422."""
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/run-tasks",
                json={
                    "data": {
                        "type": "run-tasks",
                        "attributes": {
                            "name": "Bad",
                            "url": "https://example.com",
                            "stage": "post_apply",
                        },
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 422
        assert "stage" in resp.json()["detail"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_tasks.resolve_workspace_permission")
    async def test_create_requires_admin(self, mock_resolve, *mocks):
        """Write permission → 403."""
        mock_resolve.return_value = "write"
        ws = _mock_workspace()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/run-tasks",
                json={
                    "data": {
                        "type": "run-tasks",
                        "attributes": {
                            "name": "Test",
                            "url": "https://example.com",
                            "stage": "pre_plan",
                        },
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_tasks.resolve_workspace_permission")
    async def test_create_missing_url(self, mock_resolve, *mocks):
        """Missing url → 422."""
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/workspaces/ws-{ws.id}/run-tasks",
                json={
                    "data": {
                        "type": "run-tasks",
                        "attributes": {
                            "name": "No URL",
                            "stage": "pre_plan",
                        },
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 422
        assert "url" in resp.json()["detail"]


# ── List ──────────────────────────────────────────────────────────────


class TestListRunTasks:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_tasks.resolve_workspace_permission")
    async def test_list(self, mock_resolve, *mocks):
        """List returns tasks. Read permission sufficient."""
        mock_resolve.return_value = "read"
        ws = _mock_workspace()
        rt = _mock_run_task(ws=ws)

        app, mock_db = _make_app(_user())
        mock_result_ws = MagicMock()
        mock_result_ws.scalar_one_or_none.return_value = ws
        mock_result_rts = MagicMock()
        mock_result_rts.scalars.return_value.all.return_value = [rt]

        mock_db.execute.side_effect = [mock_result_ws, mock_result_rts]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/v2/workspaces/ws-{ws.id}/run-tasks",
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 1
        assert resp.json()["data"][0]["type"] == "run-tasks"


# ── Show ──────────────────────────────────────────────────────────────


class TestShowRunTask:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_tasks.resolve_workspace_permission")
    async def test_show(self, mock_resolve, *mocks):
        mock_resolve.return_value = "read"
        rt = _mock_run_task()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = rt
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/v2/run-tasks/task-{rt.id}",
                headers=_AUTH,
            )
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["attributes"]["name"] == "OPA Check"
        assert "hmac-key" not in data["attributes"]
        assert "has-hmac-key" in data["attributes"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_show_not_found(self, *mocks):
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/v2/run-tasks/task-{uuid.uuid4()}",
                headers=_AUTH,
            )
        assert resp.status_code == 404


# ── Update ────────────────────────────────────────────────────────────


class TestUpdateRunTask:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_tasks.resolve_workspace_permission")
    async def test_update_name(self, mock_resolve, *mocks):
        """PATCH updates name → 200."""
        mock_resolve.return_value = "admin"
        rt = _mock_run_task()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = rt
        mock_db.execute.return_value = mock_result
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/run-tasks/task-{rt.id}",
                json={
                    "data": {
                        "type": "run-tasks",
                        "attributes": {"name": "Updated Name"},
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_tasks.resolve_workspace_permission")
    async def test_update_requires_admin(self, mock_resolve, *mocks):
        """Write permission → 403."""
        mock_resolve.return_value = "write"
        rt = _mock_run_task()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = rt
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/run-tasks/task-{rt.id}",
                json={"data": {"type": "run-tasks", "attributes": {"name": "X"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403


# ── Delete ────────────────────────────────────────────────────────────


class TestDeleteRunTask:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_tasks.resolve_workspace_permission")
    async def test_delete(self, mock_resolve, *mocks):
        """Admin can delete → 204."""
        mock_resolve.return_value = "admin"
        rt = _mock_run_task()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = rt
        mock_db.execute.return_value = mock_result
        mock_db.delete = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(
                f"/api/v2/run-tasks/task-{rt.id}",
                headers=_AUTH,
            )
        assert resp.status_code == 204


# ── Callback ──────────────────────────────────────────────────────────


class TestCallback:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_tasks.verify_callback_token")
    @patch("terrapod.api.routers.run_tasks.get_task_stage_result")
    @patch("terrapod.api.routers.run_tasks.resolve_stage")
    async def test_callback_passed(self, mock_resolve_stage, mock_get_tsr, mock_verify, *mocks):
        """Valid callback with passed → 200."""
        tsr = _mock_task_stage_result(status="running")
        mock_verify.return_value = tsr.id
        mock_get_tsr.return_value = tsr
        mock_resolve_stage.return_value = "passed"

        app, mock_db = _make_app(_user())
        # No auth override for callback (it's unauthenticated)
        del app.dependency_overrides[get_current_user]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/task-stage-results/tsr-{tsr.id}/callback",
                json={
                    "access_token": "valid-token",
                    "status": "passed",
                    "message": "All policies passed",
                },
            )
        assert resp.status_code == 200
        assert resp.json()["data"]["status"] == "passed"
        assert resp.json()["data"]["stage-status"] == "passed"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_tasks.verify_callback_token")
    async def test_callback_invalid_token(self, mock_verify, *mocks):
        """Invalid token → 401."""
        mock_verify.return_value = None

        app, mock_db = _make_app(_user())
        del app.dependency_overrides[get_current_user]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/task-stage-results/tsr-{uuid.uuid4()}/callback",
                json={
                    "access_token": "bad-token",
                    "status": "passed",
                },
            )
        assert resp.status_code == 401

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.run_tasks.verify_callback_token")
    @patch("terrapod.api.routers.run_tasks.get_task_stage_result")
    async def test_callback_invalid_status(self, mock_get_tsr, mock_verify, *mocks):
        """Invalid result status → 422."""
        tsr = _mock_task_stage_result(status="running")
        mock_verify.return_value = tsr.id
        mock_get_tsr.return_value = tsr

        app, mock_db = _make_app(_user())
        del app.dependency_overrides[get_current_user]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/task-stage-results/tsr-{tsr.id}/callback",
                json={
                    "access_token": "valid-token",
                    "status": "maybe",
                },
            )
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_callback_missing_token(self, *mocks):
        """Missing access_token → 401."""
        app, mock_db = _make_app(_user())
        del app.dependency_overrides[get_current_user]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/task-stage-results/tsr-{uuid.uuid4()}/callback",
                json={"status": "passed"},
            )
        assert resp.status_code == 401


# ── Callback Token ────────────────────────────────────────────────────


class TestCallbackToken:
    def test_generate_and_verify(self):
        """Token round-trip works."""
        from terrapod.services.run_task_service import (
            generate_callback_token,
            verify_callback_token,
        )

        result_id = uuid.uuid4()
        token = generate_callback_token(result_id)
        verified = verify_callback_token(token)
        assert verified == result_id

    def test_verify_invalid(self):
        """Invalid token returns None."""
        from terrapod.services.run_task_service import verify_callback_token

        assert verify_callback_token("garbage") is None
        assert verify_callback_token("") is None
        assert verify_callback_token("a:b:c") is None

    def test_verify_tampered(self):
        """Tampered token returns None."""
        from terrapod.services.run_task_service import (
            generate_callback_token,
            verify_callback_token,
        )

        result_id = uuid.uuid4()
        token = generate_callback_token(result_id)
        # Tamper with signature
        parts = token.split(":")
        parts[2] = "0" * 64
        assert verify_callback_token(":".join(parts)) is None
