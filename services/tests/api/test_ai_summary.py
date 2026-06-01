"""Tests for the AI plan summary API surface (#401).

Covers:
  - GET /api/v2/plans/{plan_id}/summary 404 / 200 / auth-required paths
  - PATCH workspace ai-summary-mode validation
  - PATCH workspace ai-summary-context length cap
  - Workspace GET includes the new ai-summary-* attributes
  - /plans/{plan_id} response advertises the ai-summary-url link
"""

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


def _mock_workspace(ws_id=None, **overrides):
    ws = MagicMock()
    ws.id = ws_id or uuid.uuid4()
    ws.name = overrides.get("name", "test-ws")
    ws.auto_apply = False
    ws.execution_mode = "agent"
    ws.execution_backend = "tofu"
    ws.terraform_version = "1.12"
    ws.working_directory = ""
    ws.locked = False
    ws.lock_id = None
    ws.resource_cpu = "1"
    ws.resource_memory = "2Gi"
    ws.vcs_repo_url = ""
    ws.vcs_branch = ""
    ws.vcs_connection_id = None
    ws.vcs_connection = None
    ws.agent_pool_id = None
    ws.agent_pool = None
    ws.labels = {}
    ws.owner_email = "test@example.com"
    ws.var_files = []
    ws.trigger_prefixes = []
    ws.vcs_last_polled_at = None
    ws.vcs_last_error = None
    ws.vcs_last_error_at = None
    ws.vcs_workflow = "merge_then_apply"
    ws.auto_merge = False
    ws.auto_merge_strategy = "merge"
    ws.lifecycle_state = "active"
    ws.lifecycle_reason = ""
    ws.autodiscovery_pr_number = None
    ws.drift_detection_enabled = False
    ws.drift_detection_interval_seconds = 86400
    ws.drift_last_checked_at = None
    ws.drift_status = ""
    ws.state_diverged = False
    ws.ai_summary_mode = overrides.get("ai_summary_mode", "default")
    ws.ai_summary_context = overrides.get("ai_summary_context", "")
    ws.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    ws.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return ws


def _mock_run(ws_id=None, run_id=None, status="planned"):
    run = MagicMock()
    run.id = run_id or uuid.uuid4()
    run.workspace_id = ws_id or uuid.uuid4()
    run.status = status
    run.has_json_output = False
    run.resource_additions = None
    run.resource_changes = None
    run.resource_destructions = None
    run.resource_imports = None
    run.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    run.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return run


def _mock_summary(run_id, **overrides):
    s = MagicMock()
    s.id = uuid.uuid4()
    s.run_id = run_id
    s.kind = overrides.get("kind", "plan_summary")
    s.status = overrides.get("status", "ready")
    s.description = overrides.get("description", "All good.")
    s.risk_level = overrides.get("risk_level", "low")
    s.risk_factors = overrides.get("risk_factors", [])
    s.model = overrides.get("model", "test-model")
    s.input_tokens = overrides.get("input_tokens", 100)
    s.output_tokens = overrides.get("output_tokens", 50)
    s.error_message = overrides.get("error_message", "")
    s.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    s.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return s


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


# ── GET /plans/{id}/summary ─────────────────────────────────────────────────


class TestGetPlanSummary:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission")
    async def test_returns_summary_when_present(self, mock_resolve, *mocks):
        mock_resolve.return_value = "read"
        run = _mock_run()
        ws = _mock_workspace(ws_id=run.workspace_id)
        summary = _mock_summary(run.id, risk_level="high")

        app, mock_db = _make_app(_user())
        # _get_run → execute(select Run) → run
        # _require_run_ws_permission → db.get(Workspace, …) → ws
        # summary lookup → execute(select PlanSummary) → summary
        mock_db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=run)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=summary)),
            ]
        )
        mock_db.get = AsyncMock(return_value=ws)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/plans/plan-{run.id}/summary", headers=_AUTH)

        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["kind"] == "plan_summary"
        assert attrs["status"] == "ready"
        assert attrs["risk-level"] == "high"
        assert attrs["description"] == "All good."

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission")
    async def test_returns_404_when_no_summary(self, mock_resolve, *mocks):
        mock_resolve.return_value = "read"
        run = _mock_run()
        ws = _mock_workspace(ws_id=run.workspace_id)

        app, mock_db = _make_app(_user())
        mock_db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=run)),
                MagicMock(scalar_one_or_none=MagicMock(return_value=None)),
            ]
        )
        mock_db.get = AsyncMock(return_value=ws)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/plans/plan-{run.id}/summary", headers=_AUTH)

        assert resp.status_code == 404

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission")
    async def test_plan_response_includes_ai_summary_url(self, mock_resolve, *mocks):
        """GET /plans/{id} advertises the ai-summary-url even when no row exists yet."""
        mock_resolve.return_value = "read"
        run = _mock_run()
        ws = _mock_workspace(ws_id=run.workspace_id)

        app, mock_db = _make_app(_user())
        mock_db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run))
        )
        mock_db.get = AsyncMock(return_value=ws)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/plans/plan-{run.id}", headers=_AUTH)

        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert "ai-summary-url" in attrs
        assert str(run.id) in attrs["ai-summary-url"]


# ── Workspace PATCH ─────────────────────────────────────────────────────────


class TestWorkspaceAISummaryFields:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_get_includes_ai_summary_fields(self, mock_resolve, *mocks):
        mock_resolve.return_value = "read"
        ws = _mock_workspace(ai_summary_mode="enabled", ai_summary_context="vault prod")

        app, mock_db = _make_app(_user())
        ws_result = MagicMock()
        ws_result.scalar_one_or_none.return_value = ws
        no_run = MagicMock()
        no_run.scalar_one_or_none.return_value = None
        mock_db.execute.side_effect = [ws_result, no_run]

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v2/workspaces/ws-{ws.id}", headers=_AUTH)

        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["ai-summary-mode"] == "enabled"
        assert attrs["ai-summary-context"] == "vault prod"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_patch_accepts_valid_mode(self, mock_resolve, *mocks):
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
                            "ai-summary-mode": "disabled",
                            "ai-summary-context": "hands off",
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert ws.ai_summary_mode == "disabled"
        assert ws.ai_summary_context == "hands off"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_patch_rejects_invalid_mode(self, mock_resolve, *mocks):
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={"data": {"attributes": {"ai-summary-mode": "yes_please"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_patch_rejects_oversize_context(self, mock_resolve, *mocks):
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={"data": {"attributes": {"ai-summary-context": "x" * 5000}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission")
    async def test_patch_rejects_non_string_context(self, mock_resolve, *mocks):
        mock_resolve.return_value = "admin"
        ws = _mock_workspace()

        app, mock_db = _make_app(_user())
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = ws
        mock_db.execute.return_value = mock_result

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v2/workspaces/ws-{ws.id}",
                json={"data": {"attributes": {"ai-summary-context": 42}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422
