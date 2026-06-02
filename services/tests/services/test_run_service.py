"""Tests for run state machine and lifecycle management."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.services.run_service import (
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    _enqueue_ai_plan_summary,
    _publish_run_available,
    _publish_run_event,
    can_transition,
    cancel_run,
    claim_next_run,
    complete_apply,
    complete_plan,
    confirm_run,
    create_run,
    discard_run,
    queue_run,
    transition_run,
)

# ── can_transition ─────────────────────────────────────────────────────


class TestCanTransition:
    def test_all_valid_transitions_accepted(self):
        """Every edge in VALID_TRANSITIONS is accepted."""
        for source, targets in VALID_TRANSITIONS.items():
            for target in targets:
                assert can_transition(source, target) is True, (
                    f"Expected {source} → {target} to be valid"
                )

    def test_terminal_states_reject_all(self):
        """No transitions from terminal states."""
        for terminal in TERMINAL_STATES:
            for target in [
                "pending",
                "queued",
                "planning",
                "planned",
                "confirmed",
                "applying",
                "applied",
            ]:
                assert can_transition(terminal, target) is False, (
                    f"Expected {terminal} → {target} to be rejected"
                )

    def test_invalid_forward_transitions(self):
        assert can_transition("pending", "planning") is False
        assert can_transition("pending", "applied") is False
        assert can_transition("queued", "confirmed") is False
        assert can_transition("planning", "applying") is False

    def test_backward_transitions_rejected(self):
        assert can_transition("planned", "queued") is False
        assert can_transition("applying", "planning") is False
        assert can_transition("confirmed", "planned") is False

    def test_pending_to_queued(self):
        assert can_transition("pending", "queued") is True

    def test_queued_to_planning(self):
        assert can_transition("queued", "planning") is True

    def test_planning_to_planned(self):
        assert can_transition("planning", "planned") is True

    def test_planned_to_confirmed(self):
        assert can_transition("planned", "confirmed") is True

    def test_planned_to_discarded(self):
        assert can_transition("planned", "discarded") is True

    def test_confirmed_to_applying(self):
        assert can_transition("confirmed", "applying") is True

    def test_applying_to_applied(self):
        assert can_transition("applying", "applied") is True

    def test_any_non_terminal_to_canceled(self):
        # `applying` is intentionally excluded: cancel-while-applying
        # routes through `canceling` and the reconciler picks the
        # terminal from observable Job outcome (state-version present
        # → applied; clean kill → canceled; otherwise → errored). The
        # direct `applying → canceled` path is closed because it would
        # silently mark a run "canceled" while real infra may have
        # changed.
        for state in ["pending", "queued", "planning", "planned", "confirmed"]:
            assert can_transition(state, "canceled") is True
        assert can_transition("applying", "canceling") is True
        assert can_transition("applying", "canceled") is False

    def test_any_non_terminal_to_errored(self):
        for state in ["pending", "queued", "planning", "planned", "confirmed", "applying"]:
            assert can_transition(state, "errored") is True


# ── transition_run ─────────────────────────────────────────────────────


def _mock_run(**kwargs):
    run = MagicMock()
    run.id = kwargs.get("id", uuid.uuid4())
    run.status = kwargs.get("status", "pending")
    run.workspace_id = kwargs.get("workspace_id", uuid.uuid4())
    run.plan_started_at = kwargs.get("plan_started_at", None)
    run.plan_finished_at = kwargs.get("plan_finished_at", None)
    run.apply_started_at = kwargs.get("apply_started_at", None)
    run.apply_finished_at = kwargs.get("apply_finished_at", None)
    run.error_message = kwargs.get("error_message", "")
    run.auto_apply = kwargs.get("auto_apply", False)
    run.plan_only = kwargs.get("plan_only", False)
    run.listener_id = kwargs.get("listener_id", None)
    run.locked = kwargs.get("locked", False)
    return run


class TestTransitionRun:
    async def test_valid_transition_updates_status(self):
        db = AsyncMock(spec=AsyncSession)
        run = _mock_run(status="pending")
        result = await transition_run(db, run, "queued")
        assert result.status == "queued"
        db.flush.assert_called_once()

    async def test_invalid_transition_raises(self):
        db = AsyncMock(spec=AsyncSession)
        run = _mock_run(status="pending")
        with pytest.raises(ValueError, match="Invalid transition"):
            await transition_run(db, run, "applied")

    async def test_planning_sets_plan_started_at(self):
        db = AsyncMock(spec=AsyncSession)
        run = _mock_run(status="queued")
        result = await transition_run(db, run, "planning")
        assert result.plan_started_at is not None

    async def test_planned_sets_plan_finished_at(self):
        db = AsyncMock(spec=AsyncSession)
        run = _mock_run(status="planning", plan_started_at=datetime.now(UTC))
        result = await transition_run(db, run, "planned")
        assert result.plan_finished_at is not None

    async def test_applying_sets_apply_started_at(self):
        db = AsyncMock(spec=AsyncSession)
        run = _mock_run(status="confirmed")
        result = await transition_run(db, run, "applying")
        assert result.apply_started_at is not None

    @patch("terrapod.redis.client.delete_job_status", new_callable=AsyncMock)
    async def test_applying_clears_plan_job_state(self, mock_delete_status):
        """Transitioning to 'applying' clears stale plan-phase Job state.

        Phase-keyed Redis status (tp:job_status:{run_id}:{phase}) prevents
        the race condition, but we still clean up the plan key as hygiene.
        """
        db = AsyncMock(spec=AsyncSession)
        run = _mock_run(
            status="confirmed",
            # Simulate leftover from plan phase
        )
        run.job_name = "tprun-abc12345-plan"
        run.job_namespace = "terrapod-runners"

        result = await transition_run(db, run, "applying")

        # job_name and job_namespace must be cleared
        assert result.job_name is None
        assert result.job_namespace is None
        # Redis plan-phase job status must be deleted
        mock_delete_status.assert_called_once_with(str(run.id), "plan")

    @patch("terrapod.services.run_service.fire_run_triggers", new_callable=AsyncMock)
    async def test_applied_sets_apply_finished_at(self, mock_fire):
        db = AsyncMock(spec=AsyncSession)
        run = _mock_run(
            status="applying",
            apply_started_at=datetime.now(UTC),
        )
        result = await transition_run(db, run, "applied")
        assert result.apply_finished_at is not None

    async def test_error_message_stored(self):
        db = AsyncMock(spec=AsyncSession)
        run = _mock_run(status="planning")
        await transition_run(db, run, "errored", error_message="terraform crashed")
        assert run.error_message == "terraform crashed"

    async def test_errored_during_plan_sets_plan_finished(self):
        db = AsyncMock(spec=AsyncSession)
        run = _mock_run(status="planning", plan_started_at=datetime.now(UTC))
        await transition_run(db, run, "errored")
        assert run.plan_finished_at is not None

    async def test_errored_during_apply_sets_apply_finished(self):
        db = AsyncMock(spec=AsyncSession)
        run = _mock_run(
            status="applying",
            apply_started_at=datetime.now(UTC),
        )
        await transition_run(db, run, "errored")
        assert run.apply_finished_at is not None


# ── create_run ─────────────────────────────────────────────────────────


def _mock_workspace(**kwargs):
    ws = MagicMock()
    ws.id = kwargs.get("id", uuid.uuid4())
    ws.name = kwargs.get("name", "test-ws")
    ws.auto_apply = kwargs.get("auto_apply", False)
    ws.terraform_version = kwargs.get("terraform_version", "1.11")
    ws.resource_cpu = kwargs.get("resource_cpu", "1")
    ws.resource_memory = kwargs.get("resource_memory", "2Gi")
    ws.agent_pool_id = kwargs.get("agent_pool_id", None)
    return ws


class TestCreateRun:
    @patch("terrapod.services.run_service.Run")
    async def test_creates_run_with_defaults(self, MockRun):
        db = AsyncMock(spec=AsyncSession)

        ws = _mock_workspace()
        instance = MockRun.return_value
        instance.id = uuid.uuid4()
        instance.status = "pending"

        await create_run(db, ws, message="initial run")
        MockRun.assert_called_once()
        call_kwargs = MockRun.call_args[1]
        assert call_kwargs["status"] == "pending"
        assert call_kwargs["message"] == "initial run"
        assert call_kwargs["auto_apply"] == ws.auto_apply
        db.add.assert_called_once_with(instance)
        db.flush.assert_called_once()

    @patch("terrapod.services.run_service.Run")
    async def test_auto_apply_override(self, MockRun):
        db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute.return_value = mock_result

        ws = _mock_workspace(auto_apply=False)
        instance = MockRun.return_value
        instance.id = uuid.uuid4()
        instance.status = "pending"

        await create_run(db, ws, auto_apply=True)
        assert MockRun.call_args[1]["auto_apply"] is True

    @patch("terrapod.services.run_service.Run")
    async def test_resource_snapshot_from_workspace(self, MockRun):
        db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute.return_value = mock_result

        ws = _mock_workspace(resource_cpu="2", resource_memory="4Gi")
        instance = MockRun.return_value
        instance.id = uuid.uuid4()
        instance.status = "pending"

        await create_run(db, ws)
        assert MockRun.call_args[1]["resource_cpu"] == "2"
        assert MockRun.call_args[1]["resource_memory"] == "4Gi"

    @patch("terrapod.services.run_service.Run")
    async def test_pool_from_workspace(self, MockRun):
        db = AsyncMock(spec=AsyncSession)
        pool_id = uuid.uuid4()
        ws = _mock_workspace(agent_pool_id=pool_id)
        instance = MockRun.return_value
        instance.id = uuid.uuid4()
        instance.status = "pending"

        await create_run(db, ws)
        assert MockRun.call_args[1]["pool_id"] == pool_id

    @patch("terrapod.services.binary_cache_service.resolve_version", new_callable=AsyncMock)
    @patch("terrapod.services.run_service.Run")
    async def test_partial_version_resolved_and_pinned_on_run(self, MockRun, m_resolve):
        """A workspace's partial version (e.g. '1.11') is resolved to an
        exact x.y.z and snapshotted onto the run, so the runner's
        upstream fallback gets a version that actually exists (#338)."""
        db = AsyncMock(spec=AsyncSession)
        ws = _mock_workspace(terraform_version="1.11")
        ws.execution_backend = "tofu"
        m_resolve.return_value = "1.11.9"
        instance = MockRun.return_value
        instance.id = uuid.uuid4()
        instance.status = "pending"

        await create_run(db, ws)

        m_resolve.assert_awaited_once_with("tofu", "1.11")
        assert MockRun.call_args[1]["terraform_version"] == "1.11.9"

    @patch("terrapod.services.binary_cache_service.resolve_version", new_callable=AsyncMock)
    @patch("terrapod.services.run_service.Run")
    async def test_version_resolution_failure_falls_back_to_requested(self, MockRun, m_resolve):
        """Resolution must never block run creation — on failure the run
        is still created, pinned to the requested version as-is."""
        db = AsyncMock(spec=AsyncSession)
        ws = _mock_workspace(terraform_version="1.12")
        ws.execution_backend = "tofu"
        m_resolve.side_effect = RuntimeError("upstream index unreachable")
        instance = MockRun.return_value
        instance.id = uuid.uuid4()
        instance.status = "pending"

        await create_run(db, ws)

        MockRun.assert_called_once()
        assert MockRun.call_args[1]["terraform_version"] == "1.12"
        db.add.assert_called_once_with(instance)


# ── confirm_run / discard_run / cancel_run ─────────────────────────────


class TestConfirmRun:
    async def test_confirms_planned_run(self):
        db = AsyncMock(spec=AsyncSession)
        run = _mock_run(status="planned")
        result = await confirm_run(db, run)
        assert result.status == "confirmed"

    async def test_rejects_non_planned(self):
        db = AsyncMock(spec=AsyncSession)
        run = _mock_run(status="queued")
        with pytest.raises(ValueError, match="planned"):
            await confirm_run(db, run)


class TestDiscardRun:
    async def test_discards_planned_run(self):
        db = AsyncMock(spec=AsyncSession)
        ws = MagicMock()
        ws.locked = True
        ws.lock_id = "lock-123"
        db.get.return_value = ws
        run = _mock_run(status="planned")
        result = await discard_run(db, run)
        assert result.status == "discarded"
        # Workspace should be unlocked
        assert ws.locked is False
        assert ws.lock_id is None

    async def test_rejects_non_planned(self):
        db = AsyncMock(spec=AsyncSession)
        run = _mock_run(status="applying")
        with pytest.raises(ValueError, match="planned"):
            await discard_run(db, run)


class TestCancelRun:
    async def test_cancels_non_terminal_run(self):
        db = AsyncMock(spec=AsyncSession)
        ws = MagicMock()
        ws.locked = True
        db.get.return_value = ws
        run = _mock_run(status="planning")
        result = await cancel_run(db, run)
        assert result.status == "canceled"
        assert ws.locked is False

    async def test_rejects_terminal_state(self):
        db = AsyncMock(spec=AsyncSession)
        for state in TERMINAL_STATES:
            run = _mock_run(status=state)
            with pytest.raises(ValueError, match="terminal"):
                await cancel_run(db, run)


# ── queue_run ──────────────────────────────────────────────────────────


class TestQueueRun:
    async def test_queues_pending_run(self):
        db = AsyncMock(spec=AsyncSession)
        run = _mock_run(status="pending")
        result = await queue_run(db, run)
        assert result.status == "queued"


# ── claim_next_run ─────────────────────────────────────────────────────


class TestClaimNextRun:
    async def test_claims_queued_run_for_plan_phase(self):
        db = AsyncMock(spec=AsyncSession)
        run = _mock_run(status="queued")
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = run
        db.execute.return_value = mock_result

        listener_id = uuid.uuid4()
        pool_id = uuid.uuid4()

        result = await claim_next_run(db, listener_id, pool_id, "listener-1")
        assert result is not None
        claimed_run, phase = result
        assert phase == "plan"
        assert claimed_run.status == "planning"
        assert claimed_run.listener_id == listener_id

    async def test_claims_confirmed_run_for_apply_phase(self):
        db = AsyncMock(spec=AsyncSession)
        # First call (queued query) returns None, second (confirmed query) returns run
        confirmed_run = _mock_run(status="confirmed")
        mock_empty = MagicMock()
        mock_empty.scalar_one_or_none.return_value = None
        mock_confirmed = MagicMock()
        mock_confirmed.scalar_one_or_none.return_value = confirmed_run
        db.execute.side_effect = [mock_empty, mock_confirmed]

        listener_id = uuid.uuid4()
        pool_id = uuid.uuid4()

        result = await claim_next_run(db, listener_id, pool_id, "listener-1")
        assert result is not None
        claimed_run, phase = result
        assert phase == "apply"
        assert claimed_run.status == "applying"
        assert claimed_run.listener_id == listener_id

    async def test_returns_none_when_queue_empty(self):
        db = AsyncMock(spec=AsyncSession)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute.return_value = mock_result

        pool_id = uuid.uuid4()

        result = await claim_next_run(db, uuid.uuid4(), pool_id)
        assert result is None


# ── _publish_run_event ────────────────────────────────────────────────


class TestPublishRunEvent:
    @patch("terrapod.redis.client.publish_event", new_callable=AsyncMock)
    async def test_publishes_to_all_three_channels(self, mock_publish):
        """Run events publish to per-workspace, admin, and workspace list channels."""
        run = _mock_run(status="planning")
        workspace_id = run.workspace_id

        await _publish_run_event(run, "queued", "planning")

        assert mock_publish.call_count == 3
        channels = [call.args[0] for call in mock_publish.call_args_list]
        assert f"tp:run_events:{workspace_id}" in channels
        assert "tp:admin_events" in channels
        assert "tp:workspace_list_events" in channels


# ── _publish_run_available ────────────────────────────────────────────


class TestPublishRunAvailable:
    @patch("terrapod.redis.client.publish_listener_event", new_callable=AsyncMock)
    async def test_publishes_to_pool_channel(self, mock_publish):
        """run_available event is published to the pool's listener channel."""
        pool_id = uuid.uuid4()
        run = _mock_run(status="queued")
        run.pool_id = pool_id

        await _publish_run_available(run)

        mock_publish.assert_called_once()
        assert mock_publish.call_args.args[0] == str(pool_id)
        event = mock_publish.call_args.args[1]
        assert event["event"] == "run_available"

    @patch("terrapod.redis.client.publish_listener_event", new_callable=AsyncMock)
    async def test_handles_publish_failure_gracefully(self, mock_publish):
        """Publishing failure does not raise — the state machine must not break."""
        mock_publish.side_effect = Exception("Redis down")
        run = _mock_run(status="queued")
        run.pool_id = uuid.uuid4()

        # Should not raise
        await _publish_run_available(run)


# ── complete_plan / complete_apply ────────────────────────────────────


@pytest.fixture
def _mock_db():
    db = AsyncMock(spec=AsyncSession)
    db.flush = AsyncMock()
    db.get = AsyncMock(return_value=None)
    return db


class TestCompletePlan:
    """`complete_plan` is the shared landing point for both the runner-driven
    `/plan-result` POST and the reconciler-driven listener Job-status path.
    Critical property: it is idempotent against re-entry from either path.
    """

    @pytest.fixture(autouse=True)
    def _stub_policy_gate(self):
        """complete_plan calls the post-plan OPA policy gate (#343). These
        tests use a MagicMock db, so stub the gate to a clean pass — the
        gate has its own dedicated tests in test_policy_set_service."""
        with patch(
            "terrapod.services.policy_set_service.evaluate_post_plan",
            new=AsyncMock(return_value="passed"),
        ):
            yield

    @patch("terrapod.services.run_service._publish_run_event", new_callable=AsyncMock)
    @patch("terrapod.services.run_service._publish_run_available", new_callable=AsyncMock)
    @patch(
        "terrapod.services.run_task_service.create_task_stage",
        new_callable=AsyncMock,
        return_value=None,
    )
    async def test_no_op_when_already_past_planning(self, _stage, _avail, _evt, _mock_db):
        """If the run is already `planned`, the call is a no-op — the racing
        path won. No transition_run, no flush, run returned unchanged."""
        run = _mock_run(status="planned")
        result = await complete_plan(_mock_db, run)
        assert result.status == "planned"
        # transition_run was never called → no flush
        _mock_db.flush.assert_not_called()

    @patch("terrapod.services.run_service._publish_run_event", new_callable=AsyncMock)
    @patch("terrapod.services.run_service._publish_run_available", new_callable=AsyncMock)
    @patch(
        "terrapod.services.run_task_service.create_task_stage",
        new_callable=AsyncMock,
        return_value=None,
    )
    async def test_clean_plan_transitions_to_planned(self, _stage, _avail, _evt, _mock_db):
        run = _mock_run(status="planning", plan_started_at=datetime.now(UTC), plan_only=True)
        run.has_changes = True
        result = await complete_plan(_mock_db, run, has_changes=True)
        assert result.status == "planned"
        assert result.has_changes is True

    @patch("terrapod.services.run_service.fire_run_triggers", new_callable=AsyncMock)
    @patch("terrapod.services.run_service._publish_run_event", new_callable=AsyncMock)
    @patch("terrapod.services.run_service._publish_run_available", new_callable=AsyncMock)
    @patch(
        "terrapod.services.run_task_service.create_task_stage",
        new_callable=AsyncMock,
        return_value=None,
    )
    async def test_zero_change_non_speculative_short_circuits_to_applied(
        self, _stage, _avail, _evt, _fire, _mock_db
    ):
        """A non-plan-only run that produced no changes goes straight to
        `applied` (no apply Job needed; otherwise we d burn an empty apply
        and trip the duplicate-serial 500 on state upload)."""
        run = _mock_run(
            status="planning", plan_started_at=datetime.now(UTC), plan_only=False, auto_apply=False
        )
        result = await complete_plan(_mock_db, run, has_changes=False)
        assert result.status == "applied"

    @patch("terrapod.services.run_service._publish_run_event", new_callable=AsyncMock)
    @patch("terrapod.services.run_service._publish_run_available", new_callable=AsyncMock)
    @patch(
        "terrapod.services.run_task_service.create_task_stage",
        new_callable=AsyncMock,
        return_value=None,
    )
    async def test_auto_apply_advances_to_confirmed(self, _stage, _avail, _evt, _mock_db):
        run = _mock_run(
            status="planning", plan_started_at=datetime.now(UTC), plan_only=False, auto_apply=True
        )
        result = await complete_plan(_mock_db, run, has_changes=True)
        assert result.status == "confirmed"

    @patch("terrapod.services.run_service._publish_run_event", new_callable=AsyncMock)
    @patch("terrapod.services.run_service._publish_run_available", new_callable=AsyncMock)
    @patch("terrapod.services.run_task_service.resolve_stage", new_callable=AsyncMock)
    @patch("terrapod.services.run_task_service.create_task_stage", new_callable=AsyncMock)
    async def test_failed_post_plan_stage_errors_run(
        self, mock_create, mock_resolve, _avail, _evt, _mock_db
    ):
        """A failed post-plan task stage transitions the run to `errored`
        instead of `planned`."""
        ts = MagicMock()
        ts.id = uuid.uuid4()
        mock_create.return_value = ts
        mock_resolve.return_value = "failed"

        run = _mock_run(status="planning", plan_started_at=datetime.now(UTC), plan_only=True)
        result = await complete_plan(_mock_db, run, has_changes=True)
        assert result.status == "errored"

    @patch("terrapod.services.run_service._publish_run_event", new_callable=AsyncMock)
    @patch("terrapod.services.run_service._publish_run_available", new_callable=AsyncMock)
    @patch("terrapod.services.run_task_service.resolve_stage", new_callable=AsyncMock)
    @patch("terrapod.services.run_task_service.create_task_stage", new_callable=AsyncMock)
    async def test_pending_post_plan_stage_keeps_run_in_planning(
        self, mock_create, mock_resolve, _avail, _evt, _mock_db
    ):
        """A still-pending post-plan stage leaves the run in `planning` so
        the next reconciler tick re-checks. No transition fires."""
        ts = MagicMock()
        ts.id = uuid.uuid4()
        mock_create.return_value = ts
        mock_resolve.return_value = "running"

        run = _mock_run(status="planning", plan_started_at=datetime.now(UTC), plan_only=True)
        result = await complete_plan(_mock_db, run, has_changes=True)
        assert result.status == "planning"


class TestCompleteApply:
    @patch("terrapod.services.run_service._publish_run_event", new_callable=AsyncMock)
    @patch("terrapod.services.run_service._publish_run_available", new_callable=AsyncMock)
    @patch(
        "terrapod.services.run_service.fire_run_triggers",
        new_callable=AsyncMock,
    )
    async def test_no_op_when_not_applying(self, _fire, _avail, _evt, _mock_db):
        run = _mock_run(status="applied")
        result = await complete_apply(_mock_db, run)
        assert result.status == "applied"
        _mock_db.flush.assert_not_called()

    @patch("terrapod.services.run_service._publish_run_event", new_callable=AsyncMock)
    @patch("terrapod.services.run_service._publish_run_available", new_callable=AsyncMock)
    @patch(
        "terrapod.services.run_service.fire_run_triggers",
        new_callable=AsyncMock,
    )
    async def test_applying_transitions_to_applied(self, _fire, _avail, _evt, _mock_db):
        run = _mock_run(status="applying", apply_started_at=datetime.now(UTC))
        result = await complete_apply(_mock_db, run)
        assert result.status == "applied"


# ── AI plan-summary trigger (#401) ─────────────────────────────────────


class TestEnqueueAIPlanSummary:
    async def test_no_op_when_globally_disabled(self):
        run = _mock_run()
        with (
            patch("terrapod.config.settings") as mock_settings,
            patch(
                "terrapod.services.scheduler.enqueue_trigger",
                new_callable=AsyncMock,
            ) as mock_enq,
        ):
            mock_settings.ai_summary.enabled = False
            await _enqueue_ai_plan_summary(run, "plan_summary")
            mock_enq.assert_not_called()

    async def test_enqueues_trigger_when_enabled(self):
        run = _mock_run()
        with (
            patch("terrapod.config.settings") as mock_settings,
            patch(
                "terrapod.services.scheduler.enqueue_trigger",
                new_callable=AsyncMock,
            ) as mock_enq,
        ):
            mock_settings.ai_summary.enabled = True
            await _enqueue_ai_plan_summary(run, "plan_summary")
            mock_enq.assert_awaited_once()
            args, kwargs = mock_enq.call_args
            assert args[0] == "ai_plan_summary"
            assert args[1] == {"run_id": str(run.id), "kind": "plan_summary"}
            assert kwargs.get("dedup_key") == f"aisum:{run.id}:plan_summary"

    async def test_handles_enqueue_failure_silently(self):
        run = _mock_run()
        with (
            patch("terrapod.config.settings") as mock_settings,
            patch(
                "terrapod.services.scheduler.enqueue_trigger",
                new_callable=AsyncMock,
                side_effect=RuntimeError("redis down"),
            ),
        ):
            mock_settings.ai_summary.enabled = True
            # Must not raise — feature is best-effort, never breaks runs
            await _enqueue_ai_plan_summary(run, "plan_summary")


# ── Cancel-while-applying: canceling intermediate + reality-wins resolution ──


class TestCancelWhileApplying:
    """`applying` → `canceling` (not direct → `canceled`).

    The cancel route sends `cancel_job` to the listener so the K8s Job
    is deleted, but the run's terminal status is decided by what the
    apply actually did — never by the cancel intent alone. Otherwise
    we risk marking a run "canceled" while real infrastructure
    changed (the worst possible outcome for an IaC platform).
    """

    @pytest.mark.asyncio
    async def test_applying_cancel_transitions_to_canceling_not_canceled(self):
        from terrapod.services import run_service

        run = MagicMock()
        run.id = uuid.uuid4()
        run.workspace_id = uuid.uuid4()
        run.pool_id = uuid.uuid4()
        run.job_name = "tpjob-abc"
        run.status = "applying"

        db = AsyncMock()
        ws = MagicMock()
        ws.locked = True
        db.get.return_value = ws

        async def _transition(_db, _run, target, **_):
            _run.status = target
            return _run

        with (
            patch.object(run_service, "transition_run", new=AsyncMock(side_effect=_transition)),
            patch.object(run_service, "_publish_cancel_job", new=AsyncMock()) as mock_publish,
        ):
            result = await run_service.cancel_run(db, run)

        # Reality check: status went to canceling, NOT canceled. Workspace
        # stays locked (the apply Job is still alive until reconciler
        # confirms otherwise).
        assert result.status == "canceling"
        assert ws.locked is True
        # The cancel_job event MUST have been published — without it the
        # listener doesn't know to delete the Job and we have a true
        # zombie apply.
        mock_publish.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_confirmed_cancel_goes_direct_to_canceled(self):
        """confirmed has no Job yet (claim is atomic with confirmed→applying)
        so the canceling intermediate isn't needed — straight to canceled
        and unlock the workspace."""
        from terrapod.services import run_service

        run = MagicMock()
        run.id = uuid.uuid4()
        run.workspace_id = uuid.uuid4()
        run.pool_id = None
        run.job_name = None
        run.status = "confirmed"

        db = AsyncMock()
        ws = MagicMock()
        ws.locked = True
        ws.lock_id = "lock-1"
        db.get.return_value = ws

        async def _transition(_db, _run, target, **_):
            _run.status = target
            return _run

        with (
            patch.object(run_service, "transition_run", new=AsyncMock(side_effect=_transition)),
            patch.object(run_service, "_publish_cancel_job", new=AsyncMock()),
        ):
            result = await run_service.cancel_run(db, run)

        assert result.status == "canceled"
        assert ws.locked is False
        assert ws.lock_id is None


class TestResolveCancelingRun:
    """The reconciler resolves a `canceling` run to a terminal status
    based on observable Job outcome and whether a state-version was
    actually uploaded. State-version presence is the ground truth: if
    state landed, real infra changed, and the run is `applied`
    regardless of the cancel intent.
    """

    @pytest.mark.asyncio
    async def test_state_uploaded_resolves_to_applied(self):
        """Apply landed before the kill (or completed naturally) — state
        is recorded, so the terminal MUST be applied. We never claim
        "canceled" while a state-version exists for this run."""
        from terrapod.services import run_service

        run = MagicMock()
        run.id = uuid.uuid4()
        run.workspace_id = uuid.uuid4()
        run.status = "canceling"

        db = AsyncMock()
        # One StateVersion row → state was uploaded.
        sv_result = MagicMock()
        sv_result.scalar_one_or_none = MagicMock(return_value=MagicMock())
        db.execute.return_value = sv_result

        ws = MagicMock()
        ws.locked = True
        ws.state_diverged = False
        db.get.return_value = ws

        async def _transition(_db, _run, target, **_):
            _run.status = target
            return _run

        with patch.object(run_service, "transition_run", new=AsyncMock(side_effect=_transition)):
            result = await run_service.resolve_canceling_run(db, run, job_status="deleted")

        assert result.status == "applied"
        # Workspace lock released; state_diverged NOT set (state is fine).
        assert ws.locked is False
        assert ws.state_diverged is False

    @pytest.mark.asyncio
    async def test_clean_kill_no_state_resolves_to_canceled_with_drift_flag(self):
        """Job was killed before state was uploaded — best case the cancel
        arrived before any mutation, worst case it killed mid-mutation.
        Without a runner-side "nothing applied" report we can't tell,
        so we set state_diverged as the operator-visible drift signal.
        """
        from terrapod.services import run_service

        run = MagicMock()
        run.id = uuid.uuid4()
        run.workspace_id = uuid.uuid4()
        run.status = "canceling"

        db = AsyncMock()
        sv_result = MagicMock()
        sv_result.scalar_one_or_none = MagicMock(return_value=None)
        db.execute.return_value = sv_result

        ws = MagicMock()
        ws.locked = True
        ws.state_diverged = False
        db.get.return_value = ws

        async def _transition(_db, _run, target, **_):
            _run.status = target
            return _run

        with patch.object(run_service, "transition_run", new=AsyncMock(side_effect=_transition)):
            result = await run_service.resolve_canceling_run(db, run, job_status="deleted")

        assert result.status == "canceled"
        assert ws.locked is False
        # The whole point: signal the operator that real infra may have
        # moved without a state record. Without this flag the
        # "canceled" outcome would silently hide possible drift.
        assert ws.state_diverged is True

    @pytest.mark.asyncio
    async def test_failed_no_state_resolves_to_errored_with_drift_flag(self):
        from terrapod.services import run_service

        run = MagicMock()
        run.id = uuid.uuid4()
        run.workspace_id = uuid.uuid4()
        run.status = "canceling"

        db = AsyncMock()
        sv_result = MagicMock()
        sv_result.scalar_one_or_none = MagicMock(return_value=None)
        db.execute.return_value = sv_result

        ws = MagicMock()
        ws.locked = True
        ws.state_diverged = False
        db.get.return_value = ws

        async def _transition(_db, _run, target, error_message=""):
            _run.status = target
            _run.error_message = error_message
            return _run

        with patch.object(run_service, "transition_run", new=AsyncMock(side_effect=_transition)):
            result = await run_service.resolve_canceling_run(db, run, job_status="failed")

        assert result.status == "errored"
        assert "no state-version" in result.error_message
        assert ws.state_diverged is True
