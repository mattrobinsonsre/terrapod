"""Tests for run CRUD and lifecycle endpoints with RBAC."""

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


def _mock_run(
    run_id=None,
    status="pending",
    ws_id=None,
    auto_apply=False,
    plan_only=False,
    message="",
):
    run = MagicMock()
    run.id = run_id or uuid.uuid4()
    run.workspace_id = ws_id or uuid.uuid4()
    run.status = status
    run.message = message
    run.is_destroy = False
    run.auto_apply = auto_apply
    run.plan_only = plan_only
    run.source = "tfe-api"
    run.terraform_version = "1.9.0"
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
    return run


def _mock_workspace(ws_id=None, name="test-ws"):
    ws = MagicMock()
    ws.id = ws_id or uuid.uuid4()
    ws.name = name
    return ws


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


# ── Create Run ─────────────────────────────────────────────────────────


class TestCreateRun:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.run_service.queue_run")
    @patch("terrapod.api.routers.runs.run_service.create_run")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission")
    async def test_create_plan_only_with_plan_perm(
        self, mock_resolve, mock_create_run, mock_queue, *mocks
    ):
        mock_resolve.return_value = "plan"
        ws = _mock_workspace()
        run = _mock_run(ws_id=ws.id, plan_only=True, status="queued")
        mock_create_run.return_value = run
        mock_queue.return_value = run

        user = _user()
        app, mock_db = _make_app(user)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/runs",
                json={
                    "data": {
                        "attributes": {"plan-only": True},
                        "relationships": {
                            "workspace": {"data": {"id": f"ws-{ws.id}", "type": "workspaces"}}
                        },
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 201
        assert resp.json()["data"]["type"] == "runs"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission")
    async def test_create_apply_needs_write_perm(self, mock_resolve, *mocks):
        """Plan-only=false (default) requires write permission."""
        mock_resolve.return_value = "plan"  # only plan, not write
        ws = _mock_workspace()
        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/runs",
                json={
                    "data": {
                        "attributes": {},
                        "relationships": {"workspace": {"data": {"id": f"ws-{ws.id}"}}},
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_missing_workspace_returns_422(self, *mocks):
        app, _ = _make_app(_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v2/runs",
                json={"data": {"attributes": {}, "relationships": {}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422


# ── Show Run ───────────────────────────────────────────────────────────


class TestShowRun:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission")
    @patch("terrapod.api.routers.runs.run_service.get_run")
    async def test_show_with_read(self, mock_get_run, mock_resolve, *mocks):
        mock_resolve.return_value = "read"
        run = _mock_run()
        mock_get_run.return_value = run

        ws = _mock_workspace(ws_id=run.workspace_id)
        app, mock_db = _make_app(_user())
        mock_db.get.return_value = ws

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/runs/run-{run.id}", headers=_AUTH)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["attributes"]["status"] == "pending"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.run_service.get_run")
    async def test_show_not_found(self, mock_get_run, *mocks):
        mock_get_run.return_value = None
        app, _ = _make_app(_user())

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/runs/run-{uuid.uuid4()}", headers=_AUTH)
        assert resp.status_code == 404


# ── Confirm Run ────────────────────────────────────────────────────────


class TestConfirmRun:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.run_service.confirm_run")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission")
    @patch("terrapod.api.routers.runs.run_service.get_run")
    async def test_confirm_with_write_perm(self, mock_get_run, mock_resolve, mock_confirm, *mocks):
        mock_resolve.return_value = "write"
        run = _mock_run(status="planned")
        mock_get_run.return_value = run
        confirmed = _mock_run(status="confirmed")
        mock_confirm.return_value = confirmed

        ws = _mock_workspace(ws_id=run.workspace_id)
        app, mock_db = _make_app(_user())
        mock_db.get.return_value = ws

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/runs/run-{run.id}/actions/confirm",
                headers=_AUTH,
            )
        assert resp.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.run_service.confirm_run")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission")
    @patch("terrapod.api.routers.runs.run_service.get_run")
    async def test_confirm_wrong_state_returns_409(
        self, mock_get_run, mock_resolve, mock_confirm, *mocks
    ):
        mock_resolve.return_value = "write"
        run = _mock_run(status="queued")
        mock_get_run.return_value = run
        mock_confirm.side_effect = ValueError("Can only confirm planned")

        ws = _mock_workspace(ws_id=run.workspace_id)
        app, mock_db = _make_app(_user())
        mock_db.get.return_value = ws

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/runs/run-{run.id}/actions/confirm",
                headers=_AUTH,
            )
        assert resp.status_code == 409


# ── Discard Run ────────────────────────────────────────────────────────


class TestDiscardRun:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.run_service.discard_run")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission")
    @patch("terrapod.api.routers.runs.run_service.get_run")
    async def test_discard_with_plan_perm(self, mock_get_run, mock_resolve, mock_discard, *mocks):
        mock_resolve.return_value = "plan"
        run = _mock_run(status="planned")
        mock_get_run.return_value = run
        mock_discard.return_value = _mock_run(status="discarded")

        ws = _mock_workspace(ws_id=run.workspace_id)
        app, mock_db = _make_app(_user())
        mock_db.get.return_value = ws

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/runs/run-{run.id}/actions/discard",
                headers=_AUTH,
            )
        assert resp.status_code == 200


# ── Cancel Run ─────────────────────────────────────────────────────────


class TestCancelRun:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.run_service.cancel_run")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission")
    @patch("terrapod.api.routers.runs.run_service.get_run")
    async def test_cancel_with_plan_perm(self, mock_get_run, mock_resolve, mock_cancel, *mocks):
        mock_resolve.return_value = "plan"
        run = _mock_run(status="planning")
        mock_get_run.return_value = run
        mock_cancel.return_value = _mock_run(status="canceled")

        ws = _mock_workspace(ws_id=run.workspace_id)
        app, mock_db = _make_app(_user())
        mock_db.get.return_value = ws

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/runs/run-{run.id}/actions/cancel",
                headers=_AUTH,
            )
        assert resp.status_code == 200

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.run_service.cancel_run")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission")
    @patch("terrapod.api.routers.runs.run_service.get_run")
    async def test_cancel_terminal_returns_409(
        self, mock_get_run, mock_resolve, mock_cancel, *mocks
    ):
        mock_resolve.return_value = "plan"
        run = _mock_run(status="applied")
        mock_get_run.return_value = run
        mock_cancel.side_effect = ValueError("terminal")

        ws = _mock_workspace(ws_id=run.workspace_id)
        app, mock_db = _make_app(_user())
        mock_db.get.return_value = ws

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/v2/runs/run-{run.id}/actions/cancel",
                headers=_AUTH,
            )
        assert resp.status_code == 409


# ── JSON Serialization ────────────────────────────────────────────────


class TestRunJsonSerialization:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission")
    @patch("terrapod.api.routers.runs.run_service.get_run")
    async def test_actions_block(self, mock_get_run, mock_resolve, *mocks):
        """Verify actions reflect run state."""
        mock_resolve.return_value = "read"
        run = _mock_run(status="planned", auto_apply=False)
        mock_get_run.return_value = run

        ws = _mock_workspace(ws_id=run.workspace_id)
        app, mock_db = _make_app(_user())
        mock_db.get.return_value = ws

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/runs/run-{run.id}", headers=_AUTH)

        actions = resp.json()["data"]["attributes"]["actions"]
        assert actions["is-confirmable"] is True
        assert actions["is-discardable"] is True
        assert actions["is-cancelable"] is True

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission")
    @patch("terrapod.api.routers.runs.run_service.get_run")
    async def test_timestamps_rfc3339(self, mock_get_run, mock_resolve, *mocks):
        mock_resolve.return_value = "read"
        run = _mock_run()
        run.created_at = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
        mock_get_run.return_value = run

        ws = _mock_workspace(ws_id=run.workspace_id)
        app, mock_db = _make_app(_user())
        mock_db.get.return_value = ws

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/runs/run-{run.id}", headers=_AUTH)

        ts = resp.json()["data"]["attributes"]["created-at"]
        assert ts == "2026-03-01T12:00:00Z"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission")
    @patch("terrapod.api.routers.runs.run_service.get_run")
    async def test_auto_apply_not_confirmable(self, mock_get_run, mock_resolve, *mocks):
        """Auto-apply runs in planned state are NOT confirmable."""
        mock_resolve.return_value = "read"
        run = _mock_run(status="planned", auto_apply=True)
        mock_get_run.return_value = run

        ws = _mock_workspace(ws_id=run.workspace_id)
        app, mock_db = _make_app(_user())
        mock_db.get.return_value = ws

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/runs/run-{run.id}", headers=_AUTH)

        assert resp.json()["data"]["attributes"]["actions"]["is-confirmable"] is False
