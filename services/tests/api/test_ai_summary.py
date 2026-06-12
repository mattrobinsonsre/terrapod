"""Tests for the AI plan summary API surface (#401).

Covers:
  - GET /api/terrapod/v1/runs/{run_id}/plan-summary 404 / 200 / auth-required paths
  - PATCH workspace ai-summary-mode validation
  - PATCH workspace ai-summary-context length cap
  - Workspace GET includes the new ai-summary-* attributes
  - /plans/{plan_id} response advertises the ai-summary-url link
  - POST /api/terrapod/v1/runs/{run_id}/plan-summary/regenerate (v0.30.4)
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
    ws.drift_ignore_rules = []
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
    @patch("terrapod.api.routers.runs.resolve_workspace_permission_for")
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
            resp = await c.get(f"/api/terrapod/v1/runs/run-{run.id}/plan-summary", headers=_AUTH)

        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["kind"] == "plan_summary"
        assert attrs["status"] == "ready"
        assert attrs["risk-level"] == "high"
        assert attrs["description"] == "All good."

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission_for")
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
            resp = await c.get(f"/api/terrapod/v1/runs/run-{run.id}/plan-summary", headers=_AUTH)

        assert resp.status_code == 404

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission_for")
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

        with patch("terrapod.api.routers.runs.settings") as mock_settings:
            mock_settings.ai_summary.enabled = True
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(f"/api/v2/plans/plan-{run.id}", headers=_AUTH)

        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert "ai-summary-url" in attrs
        assert str(run.id) in attrs["ai-summary-url"]

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission_for")
    async def test_plan_response_omits_ai_summary_url_when_disabled(self, mock_resolve, *mocks):
        """AI globally disabled → plan response carries no ai-summary-url
        so the UI doesn't fetch /plan-summary and waste a round-trip
        getting back a guaranteed 404 (#463 phase 7)."""
        mock_resolve.return_value = "read"
        run = _mock_run()
        ws = _mock_workspace(ws_id=run.workspace_id)

        app, mock_db = _make_app(_user())
        mock_db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=run))
        )
        mock_db.get = AsyncMock(return_value=ws)

        with patch("terrapod.api.routers.runs.settings") as mock_settings:
            mock_settings.ai_summary.enabled = False
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(f"/api/v2/plans/plan-{run.id}", headers=_AUTH)

        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert "ai-summary-url" not in attrs


# ── Workspace PATCH ─────────────────────────────────────────────────────────


class TestWorkspaceAISummaryFields:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
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
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
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
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
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
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
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
    @patch("terrapod.api.routers.tfe_v2.resolve_workspace_permission_for")
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


# ── POST /runs/{id}/plan-summary/regenerate (v0.30.4) ───────────────────────


class TestRegeneratePlanSummary:
    """Manual regenerate endpoint. Anyone with workspace read can trigger,
    cost is gated by ai_summary.daily_token_budget on the handler side,
    dedup is bypassed so a click always goes through.
    """

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission_for")
    @patch("terrapod.services.scheduler.enqueue_trigger", new_callable=AsyncMock)
    async def test_regenerate_planned_run_returns_202_and_enqueues(
        self, mock_enq, mock_resolve, *_mocks
    ):
        mock_resolve.return_value = "read"
        run = _mock_run(status="planned")
        run.plan_started_at = datetime(2026, 1, 1, tzinfo=UTC)
        run.apply_started_at = None
        ws = _mock_workspace(ws_id=run.workspace_id)
        summary = _mock_summary(run.id, status="pending")

        app, mock_db = _make_app(_user())
        # _get_run select → run; _require_run_ws_permission → ws (db.get);
        # the upsert SQL execute returns nothing useful (None mock fine);
        # the re-read select PlanSummary → pending summary.
        mock_db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=run)),
                MagicMock(),  # upsert
                MagicMock(scalar_one_or_none=MagicMock(return_value=summary)),
            ]
        )
        mock_db.get = AsyncMock(return_value=ws)
        mock_db.commit = AsyncMock()

        with patch("terrapod.config.settings") as mock_settings:
            mock_settings.ai_summary.enabled = True
            mock_settings.ai_summary.model = "bedrock/test"
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(
                    f"/api/terrapod/v1/runs/run-{run.id}/plan-summary/regenerate",
                    headers=_AUTH,
                )

        assert resp.status_code == 202
        body = resp.json()
        assert body["data"]["attributes"]["status"] == "pending"

        # Trigger enqueued with the right kind, and WITHOUT a dedup_key
        # (manual clicks must bypass the 5-min auto-dedup).
        mock_enq.assert_awaited_once()
        args, kwargs = mock_enq.call_args
        assert args[0] == "ai_plan_summary"
        assert args[1] == {"run_id": str(run.id), "kind": "plan_summary"}
        assert "dedup_key" not in kwargs

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission_for")
    @patch("terrapod.services.scheduler.enqueue_trigger", new_callable=AsyncMock)
    async def test_regenerate_plan_phase_errored_picks_failure_analysis(
        self, mock_enq, mock_resolve, *_mocks
    ):
        mock_resolve.return_value = "read"
        run = _mock_run(status="errored")
        run.plan_started_at = datetime(2026, 1, 1, tzinfo=UTC)
        run.apply_started_at = None  # errored during plan, not apply
        ws = _mock_workspace(ws_id=run.workspace_id)
        summary = _mock_summary(run.id, status="pending", kind="failure_analysis")

        app, mock_db = _make_app(_user())
        mock_db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=run)),
                MagicMock(),
                MagicMock(scalar_one_or_none=MagicMock(return_value=summary)),
            ]
        )
        mock_db.get = AsyncMock(return_value=ws)
        mock_db.commit = AsyncMock()

        with patch("terrapod.config.settings") as mock_settings:
            mock_settings.ai_summary.enabled = True
            mock_settings.ai_summary.model = "bedrock/test"
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(
                    f"/api/terrapod/v1/runs/run-{run.id}/plan-summary/regenerate",
                    headers=_AUTH,
                )

        assert resp.status_code == 202
        mock_enq.assert_awaited_once()
        assert mock_enq.call_args.args[1]["kind"] == "failure_analysis"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission_for")
    @patch("terrapod.services.scheduler.enqueue_trigger", new_callable=AsyncMock)
    async def test_regenerate_apply_phase_errored_picks_failure_analysis(
        self, mock_enq, mock_resolve, *_mocks
    ):
        """#419: apply-phase errored runs were previously 409 on
        regenerate; now they're in scope and the regenerate flow
        produces a failure_analysis summary against the apply log.
        """
        mock_resolve.return_value = "read"
        run = _mock_run(status="errored")
        run.plan_started_at = datetime(2026, 1, 1, tzinfo=UTC)
        run.apply_started_at = datetime(2026, 1, 1, 0, 5, tzinfo=UTC)  # apply BEGAN
        ws = _mock_workspace(ws_id=run.workspace_id)
        summary = _mock_summary(run.id, status="pending", kind="failure_analysis")

        app, mock_db = _make_app(_user())
        mock_db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=run)),
                MagicMock(),
                MagicMock(scalar_one_or_none=MagicMock(return_value=summary)),
            ]
        )
        mock_db.get = AsyncMock(return_value=ws)
        mock_db.commit = AsyncMock()

        with patch("terrapod.config.settings") as mock_settings:
            mock_settings.ai_summary.enabled = True
            mock_settings.ai_summary.model = "bedrock/test"
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(
                    f"/api/terrapod/v1/runs/run-{run.id}/plan-summary/regenerate",
                    headers=_AUTH,
                )

        assert resp.status_code == 202
        mock_enq.assert_awaited_once()
        assert mock_enq.call_args.args[1]["kind"] == "failure_analysis"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission_for")
    @patch("terrapod.services.scheduler.enqueue_trigger", new_callable=AsyncMock)
    async def test_regenerate_409_when_no_summary_kind_applies(
        self, mock_enq, mock_resolve, *_mocks
    ):
        """A run still in `pending` / `queued` / `planning` has nothing
        to summarise — no plan output yet, no failure log yet. Apply-
        phase errored runs ARE in scope post-#419 and tested
        separately below.
        """
        mock_resolve.return_value = "read"
        run = _mock_run(status="queued")
        run.plan_started_at = None
        run.apply_started_at = None
        ws = _mock_workspace(ws_id=run.workspace_id)

        app, mock_db = _make_app(_user())
        mock_db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=run)),
            ]
        )
        mock_db.get = AsyncMock(return_value=ws)
        mock_db.commit = AsyncMock()

        with patch("terrapod.config.settings") as mock_settings:
            mock_settings.ai_summary.enabled = True
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(
                    f"/api/terrapod/v1/runs/run-{run.id}/plan-summary/regenerate",
                    headers=_AUTH,
                )
        assert resp.status_code == 409
        mock_enq.assert_not_called()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.scheduler.enqueue_trigger", new_callable=AsyncMock)
    async def test_regenerate_503_when_ai_summary_globally_disabled(self, mock_enq, *_mocks):
        run = _mock_run(status="planned")

        app, mock_db = _make_app(_user())
        mock_db.execute = AsyncMock()  # not reached past the gate
        mock_db.commit = AsyncMock()

        with patch("terrapod.config.settings") as mock_settings:
            mock_settings.ai_summary.enabled = False
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(
                    f"/api/terrapod/v1/runs/run-{run.id}/plan-summary/regenerate",
                    headers=_AUTH,
                )
        assert resp.status_code == 503
        mock_enq.assert_not_called()

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission_for")
    @patch("terrapod.services.scheduler.enqueue_trigger", new_callable=AsyncMock)
    async def test_regenerate_403_when_no_workspace_read(self, mock_enq, mock_resolve, *_mocks):
        """No workspace read → 403 (the permission helper raises)."""
        mock_resolve.return_value = None  # no permission
        run = _mock_run(status="planned")
        ws = _mock_workspace(ws_id=run.workspace_id)

        app, mock_db = _make_app(_user())
        mock_db.execute = AsyncMock(
            side_effect=[
                MagicMock(scalar_one_or_none=MagicMock(return_value=run)),
            ]
        )
        mock_db.get = AsyncMock(return_value=ws)

        with patch("terrapod.config.settings") as mock_settings:
            mock_settings.ai_summary.enabled = True
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(
                    f"/api/terrapod/v1/runs/run-{run.id}/plan-summary/regenerate",
                    headers=_AUTH,
                )
        assert resp.status_code in (401, 403)
        mock_enq.assert_not_called()
