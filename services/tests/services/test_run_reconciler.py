"""Tests for run reconciler — periodic task that drives run state transitions."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services.run_reconciler import (
    DEFAULT_STALE_TIMEOUT_SECONDS,
    _check_stale,
    _handle_failed,
    _handle_succeeded,
    _reconcile_one,
)


def _mock_run(**kwargs):
    run = MagicMock()
    run.id = kwargs.get("id", uuid.uuid4())
    run.status = kwargs.get("status", "planning")
    run.workspace_id = kwargs.get("workspace_id", uuid.uuid4())
    run.pool_id = kwargs.get("pool_id", uuid.uuid4())
    run.job_name = kwargs.get("job_name", "tprun-abc123-plan")
    run.job_namespace = kwargs.get("job_namespace", "terrapod-runners")
    run.plan_started_at = kwargs.get("plan_started_at", None)
    run.plan_finished_at = kwargs.get("plan_finished_at", None)
    run.apply_started_at = kwargs.get("apply_started_at", None)
    run.apply_finished_at = kwargs.get("apply_finished_at", None)
    run.auto_apply = kwargs.get("auto_apply", False)
    run.plan_only = kwargs.get("plan_only", False)
    run.error_message = kwargs.get("error_message", "")
    run.vcs_pull_request_number = kwargs.get("vcs_pull_request_number", None)
    run.vcs_commit_sha = kwargs.get("vcs_commit_sha", None)
    run.is_drift_detection = kwargs.get("is_drift_detection", False)
    # Default `has_changes=True` so existing tests keep matching the
    # "real plan with changes" path — only the no-op short-circuit cares.
    run.has_changes = kwargs.get("has_changes", True)
    return run


# ── _reconcile_one ────────────────────────────────────────────────────


class TestReconcileOne:
    @patch("terrapod.redis.client.get_job_status_from_redis", new_callable=AsyncMock)
    @patch("terrapod.redis.client.publish_listener_event", new_callable=AsyncMock)
    async def test_publishes_check_job_status_and_stream_logs(self, mock_publish, mock_get_status):
        """Reconciler publishes check_job_status and stream_logs events with phase."""
        db = AsyncMock()
        run = _mock_run()
        mock_get_status.return_value = "running"

        await _reconcile_one(db, run)

        assert mock_publish.call_count == 2
        events = [call.args[1]["event"] for call in mock_publish.call_args_list]
        assert "check_job_status" in events
        assert "stream_logs" in events
        # Verify phase is included in events
        for call in mock_publish.call_args_list:
            assert "phase" in call.args[1]
            assert call.args[1]["phase"] == "plan"

    @patch("terrapod.redis.client.get_job_status_from_redis", new_callable=AsyncMock)
    @patch("terrapod.redis.client.publish_listener_event", new_callable=AsyncMock)
    async def test_publishes_apply_phase_for_applying_run(self, mock_publish, mock_get_status):
        """Reconciler passes phase=apply for runs in applying status."""
        db = AsyncMock()
        run = _mock_run(status="applying")
        mock_get_status.return_value = "running"

        await _reconcile_one(db, run)

        for call in mock_publish.call_args_list:
            assert call.args[1]["phase"] == "apply"
        # Verify get_job_status_from_redis called with phase
        mock_get_status.assert_called_once_with(str(run.id), "apply")

    @patch("terrapod.services.run_reconciler._handle_succeeded", new_callable=AsyncMock)
    @patch("terrapod.redis.client.get_job_status_from_redis", new_callable=AsyncMock)
    @patch("terrapod.redis.client.publish_listener_event", new_callable=AsyncMock)
    async def test_handles_succeeded_status(self, mock_publish, mock_get_status, mock_handle):
        db = AsyncMock()
        run = _mock_run()
        mock_get_status.return_value = "succeeded"

        await _reconcile_one(db, run)

        mock_handle.assert_called_once_with(db, run)

    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    @patch("terrapod.redis.client.get_job_status_from_redis", new_callable=AsyncMock)
    @patch("terrapod.redis.client.publish_listener_event", new_callable=AsyncMock)
    async def test_handles_failed_status(self, mock_publish, mock_get_status, mock_handle):
        db = AsyncMock()
        run = _mock_run()
        mock_get_status.return_value = "failed"

        await _reconcile_one(db, run)

        mock_handle.assert_called_once_with(db, run, "Job failed")

    @patch("terrapod.services.run_reconciler._check_stale", new_callable=AsyncMock)
    @patch("terrapod.redis.client.get_job_status_from_redis", new_callable=AsyncMock)
    @patch("terrapod.redis.client.publish_listener_event", new_callable=AsyncMock)
    async def test_checks_stale_when_no_status(self, mock_publish, mock_get_status, mock_stale):
        db = AsyncMock()
        run = _mock_run()
        mock_get_status.return_value = None

        await _reconcile_one(db, run)

        mock_stale.assert_called_once_with(db, run)

    @patch("terrapod.redis.client.get_job_status_from_redis", new_callable=AsyncMock)
    @patch("terrapod.redis.client.publish_listener_event", new_callable=AsyncMock)
    async def test_running_status_is_noop(self, mock_publish, mock_get_status):
        """Running status means Job is still in progress — no transition."""
        db = AsyncMock()
        run = _mock_run()
        mock_get_status.return_value = "running"

        await _reconcile_one(db, run)

        # No transition calls — just publish events and return
        assert mock_publish.call_count == 2


# ── _handle_succeeded ─────────────────────────────────────────────────


_PERSIST_PATCH = "terrapod.services.run_reconciler._persist_live_log_if_missing"


class TestHandleSucceeded:
    @pytest.fixture(autouse=True)
    def _stub_policy_gate(self):
        """complete_plan now runs a post-plan OPA policy gate (#343).
        These tests use a mock db, so stub the gate to a clean pass —
        the gate has its own dedicated tests in test_policy_set_service."""
        with patch(
            "terrapod.services.policy_set_service.evaluate_post_plan",
            new=AsyncMock(return_value="passed"),
        ):
            yield

    @patch(_PERSIST_PATCH, new_callable=AsyncMock)
    @patch("terrapod.services.run_task_service.create_task_stage", new_callable=AsyncMock)
    @patch("terrapod.services.run_service.transition_run", new_callable=AsyncMock)
    async def test_plan_succeeded_transitions_to_planned(
        self, mock_transition, mock_stage, mock_persist
    ):
        db = AsyncMock()
        run = _mock_run(status="planning")
        mock_stage.return_value = None
        mock_transition.return_value = run

        await _handle_succeeded(db, run)

        mock_transition.assert_called_once_with(db, run, "planned")
        mock_persist.assert_called_once_with(run, "plan")

    @patch(_PERSIST_PATCH, new_callable=AsyncMock)
    @patch("terrapod.services.run_task_service.create_task_stage", new_callable=AsyncMock)
    @patch("terrapod.services.run_service.transition_run", new_callable=AsyncMock)
    async def test_plan_with_auto_apply_transitions_to_confirmed(
        self, mock_transition, mock_stage, mock_persist
    ):
        db = AsyncMock()
        # Auto-apply respects a manual lock — return an unlocked workspace so
        # the auto-confirm proceeds.
        db.get.return_value = MagicMock(locked=False)
        run = _mock_run(status="planning", auto_apply=True)
        mock_stage.return_value = None
        # First call returns planned run, second returns confirmed
        planned_run = _mock_run(status="planned", auto_apply=True, plan_only=False)
        confirmed_run = _mock_run(status="confirmed", auto_apply=True)
        mock_transition.side_effect = [planned_run, confirmed_run]

        await _handle_succeeded(db, run)

        assert mock_transition.call_count == 2
        assert mock_transition.call_args_list[0].args[2] == "planned"
        assert mock_transition.call_args_list[1].args[2] == "confirmed"

    @patch(_PERSIST_PATCH, new_callable=AsyncMock)
    @patch("terrapod.services.run_task_service.create_task_stage", new_callable=AsyncMock)
    @patch("terrapod.services.run_service.transition_run", new_callable=AsyncMock)
    async def test_auto_apply_blocked_by_manual_lock_stays_planned(
        self, mock_transition, mock_stage, mock_persist
    ):
        """A manually locked workspace must not auto-apply: the run settles in
        `planned` (one transition) and is NOT auto-confirmed."""
        db = AsyncMock()
        db.get.return_value = MagicMock(locked=True)  # workspace is locked
        db.scalar.return_value = None  # no newer run (isolate the lock behaviour)
        run = _mock_run(status="planning", auto_apply=True)
        mock_stage.return_value = None
        planned_run = _mock_run(status="planned", auto_apply=True, plan_only=False)
        mock_transition.side_effect = [planned_run]

        await _handle_succeeded(db, run)

        # Only the planned transition fired — no auto-confirm past the lock.
        assert mock_transition.call_count == 1
        assert mock_transition.call_args_list[0].args[2] == "planned"

    @patch(_PERSIST_PATCH, new_callable=AsyncMock)
    @patch("terrapod.services.run_task_service.create_task_stage", new_callable=AsyncMock)
    @patch("terrapod.services.run_service.transition_run", new_callable=AsyncMock)
    async def test_plan_with_no_changes_skips_apply(
        self, mock_transition, mock_stage, mock_persist
    ):
        """has_changes=False short-circuits planned → applied without an apply Job.

        Regression for the state-upload 500 cycle: tofu apply of a no-changes
        plan doesn't bump the state serial, so the runner's PUT
        /artifacts/state hits the unique constraint on (workspace_id, serial)
        and 500s. Skipping the apply Job entirely avoids the round-trip.
        """
        db = AsyncMock()
        run = _mock_run(status="planning", auto_apply=False, plan_only=False, has_changes=False)
        mock_stage.return_value = None
        planned_run = _mock_run(
            status="planned", auto_apply=False, plan_only=False, has_changes=False
        )
        applied_run = _mock_run(
            status="applied", auto_apply=False, plan_only=False, has_changes=False
        )
        mock_transition.side_effect = [planned_run, applied_run]
        ws = MagicMock()
        ws.locked = True
        db.get.return_value = ws

        await _handle_succeeded(db, run)

        # Two transitions: planning→planned, then planned→applied.
        # Critically, NO transition to "confirmed" — the apply Job is never queued.
        assert mock_transition.call_count == 2
        assert mock_transition.call_args_list[0].args[2] == "planned"
        assert mock_transition.call_args_list[1].args[2] == "applied"
        assert ws.locked is False

    @patch(_PERSIST_PATCH, new_callable=AsyncMock)
    @patch("terrapod.services.run_task_service.create_task_stage", new_callable=AsyncMock)
    @patch("terrapod.services.run_service.transition_run", new_callable=AsyncMock)
    async def test_plan_with_no_changes_overrides_auto_apply(
        self, mock_transition, mock_stage, mock_persist
    ):
        """auto_apply doesn't matter when has_changes=False — still skip to applied.

        Without this, an auto-apply workspace with a no-op plan would queue a
        confirm → applying transition and re-trigger the same 500 loop.
        """
        db = AsyncMock()
        run = _mock_run(status="planning", auto_apply=True, plan_only=False, has_changes=False)
        mock_stage.return_value = None
        planned_run = _mock_run(
            status="planned", auto_apply=True, plan_only=False, has_changes=False
        )
        applied_run = _mock_run(
            status="applied", auto_apply=True, plan_only=False, has_changes=False
        )
        mock_transition.side_effect = [planned_run, applied_run]
        db.get.return_value = MagicMock(locked=False)

        await _handle_succeeded(db, run)

        # Must be planned → applied, not planned → confirmed.
        assert mock_transition.call_args_list[1].args[2] == "applied"
        # No third transition — auto_apply path is bypassed.
        assert mock_transition.call_count == 2

    @patch(_PERSIST_PATCH, new_callable=AsyncMock)
    @patch("terrapod.services.run_task_service.create_task_stage", new_callable=AsyncMock)
    @patch("terrapod.services.run_service.transition_run", new_callable=AsyncMock)
    async def test_plan_only_unlocks_workspace(self, mock_transition, mock_stage, mock_persist):
        db = AsyncMock()
        run = _mock_run(status="planning", plan_only=True)
        mock_stage.return_value = None
        planned_run = _mock_run(status="planned", plan_only=True, auto_apply=False)
        mock_transition.return_value = planned_run
        ws = MagicMock()
        ws.locked = True
        ws.lock_id = "lock-123"
        db.get.return_value = ws

        await _handle_succeeded(db, run)

        assert ws.locked is False
        assert ws.lock_id is None

    @patch(_PERSIST_PATCH, new_callable=AsyncMock)
    @patch("terrapod.services.run_service.transition_run", new_callable=AsyncMock)
    async def test_apply_succeeded_transitions_to_applied(self, mock_transition, mock_persist):
        db = AsyncMock()
        run = _mock_run(status="applying")
        mock_transition.return_value = run
        ws = MagicMock()
        ws.locked = True
        db.get.return_value = ws

        await _handle_succeeded(db, run)

        mock_transition.assert_called_once_with(db, run, "applied")
        assert ws.locked is False
        mock_persist.assert_called_once_with(run, "apply")

    @patch(_PERSIST_PATCH, new_callable=AsyncMock)
    @patch("terrapod.services.run_task_service.resolve_stage", new_callable=AsyncMock)
    @patch("terrapod.services.run_task_service.create_task_stage", new_callable=AsyncMock)
    @patch("terrapod.services.run_service.transition_run", new_callable=AsyncMock)
    async def test_post_plan_task_stage_failed_errors_run(
        self, mock_transition, mock_create_stage, mock_resolve, mock_persist
    ):
        db = AsyncMock()
        run = _mock_run(status="planning")
        stage = MagicMock()
        stage.id = uuid.uuid4()
        mock_create_stage.return_value = stage
        mock_resolve.return_value = "failed"
        mock_transition.return_value = run

        await _handle_succeeded(db, run)

        mock_transition.assert_called_once_with(
            db, run, "errored", error_message="Post-plan task stage failed"
        )


# ── _handle_failed ────────────────────────────────────────────────────


class TestHandleFailed:
    @patch(_PERSIST_PATCH, new_callable=AsyncMock)
    @patch("terrapod.services.run_service.transition_run", new_callable=AsyncMock)
    async def test_transitions_to_errored(self, mock_transition, mock_persist):
        db = AsyncMock()
        run = _mock_run(status="planning")
        mock_transition.return_value = run
        ws = MagicMock()
        ws.locked = True
        db.get.return_value = ws

        await _handle_failed(db, run, "Job failed")

        mock_transition.assert_called_once_with(db, run, "errored", error_message="Job failed")
        assert ws.locked is False
        mock_persist.assert_called_once_with(run, "plan")


# ── _build_failure_message (#430) ─────────────────────────────────────


class TestBuildFailureMessage:
    """The reconciler renders a typed error message from runner_exit_status.

    The signal flows: listener observes K8s container-terminated reason →
    report_job_status sets runner_exit_status to a stable bucket
    ("oom" / "killed" / "error" / "clean") → reconciler reads it here and
    renders the operator-facing message. This test pins the four buckets
    so a refactor of either side keeps the contract intact.

    For OOM the message MUST name the actionable knob (resource_memory)
    because #430's whole motivation is "OOMs were invisible / surfaced
    as generic 'Job failed'".
    """

    def test_oom_with_peak_names_resource_memory_and_peak(self):
        from terrapod.services.run_reconciler import _build_failure_message

        run = _mock_run()
        run.runner_exit_status = "oom"
        run.peak_memory_bytes = 2 * (1 << 30)  # 2 GiB
        run.resource_memory = "1Gi"

        msg = _build_failure_message(run, "failed")
        assert "OOM" in msg
        assert "2.00 Gi" in msg  # peak rendered binary
        assert "1Gi" in msg  # workspace's request, verbatim
        assert "Increase resource_memory" in msg

    def test_oom_without_peak_still_actionable(self):
        from terrapod.services.run_reconciler import _build_failure_message

        run = _mock_run()
        run.runner_exit_status = "oom"
        run.peak_memory_bytes = None
        run.resource_memory = "4Gi"

        msg = _build_failure_message(run, "failed")
        assert "OOM" in msg
        assert "4Gi" in msg
        assert "Increase resource_memory" in msg

    def test_killed_points_at_likely_oom(self):
        """exit 137 with no explicit K8s reason — pod was probably GCed
        before we could read terminated.reason. Still surface OOM as the
        most likely cause + the alternative (node eviction)."""
        from terrapod.services.run_reconciler import _build_failure_message

        run = _mock_run()
        run.runner_exit_status = "killed"
        run.runner_exit_code = 137
        run.resource_memory = "2Gi"

        msg = _build_failure_message(run, "failed")
        assert "137" in msg
        assert "OOM" in msg
        assert "eviction" in msg.lower()

    def test_error_includes_exit_code(self):
        from terrapod.services.run_reconciler import _build_failure_message

        run = _mock_run()
        run.runner_exit_status = "error"
        run.runner_exit_code = 2

        msg = _build_failure_message(run, "failed")
        assert "2" in msg
        assert "OOM" not in msg

    def test_unset_status_falls_back_to_generic(self):
        """Backwards-compat: runs that pre-date #430 have empty
        runner_exit_status and must still get the historical message
        shape (no NameError, no 'OOM' false alarm)."""
        from terrapod.services.run_reconciler import _build_failure_message

        run = _mock_run()
        run.runner_exit_status = ""

        msg = _build_failure_message(run, "failed")
        assert msg == "Job failed"


# ── _check_stale ──────────────────────────────────────────────────────


class TestCheckStale:
    @patch("terrapod.services.run_reconciler.load_runner_config")
    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    async def test_stale_plan_gets_errored(self, mock_handle, mock_config):
        mock_config.return_value = MagicMock(
            stale_timeout_seconds=DEFAULT_STALE_TIMEOUT_SECONDS, launch_timeout_seconds=300
        )
        db = AsyncMock()
        run = _mock_run(
            status="planning",
            plan_started_at=datetime.now(UTC)
            - timedelta(seconds=DEFAULT_STALE_TIMEOUT_SECONDS)
            - timedelta(minutes=5),
        )

        await _check_stale(db, run)

        mock_handle.assert_called_once()
        assert "stale" in mock_handle.call_args.args[2].lower()

    @patch("terrapod.services.run_reconciler.load_runner_config")
    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    async def test_stale_apply_gets_errored(self, mock_handle, mock_config):
        mock_config.return_value = MagicMock(
            stale_timeout_seconds=DEFAULT_STALE_TIMEOUT_SECONDS, launch_timeout_seconds=300
        )
        db = AsyncMock()
        run = _mock_run(
            status="applying",
            apply_started_at=datetime.now(UTC)
            - timedelta(seconds=DEFAULT_STALE_TIMEOUT_SECONDS)
            - timedelta(minutes=5),
        )

        await _check_stale(db, run)

        mock_handle.assert_called_once()

    @patch("terrapod.services.run_reconciler.load_runner_config")
    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    async def test_not_stale_yet(self, mock_handle, mock_config):
        mock_config.return_value = MagicMock(
            stale_timeout_seconds=DEFAULT_STALE_TIMEOUT_SECONDS, launch_timeout_seconds=300
        )
        db = AsyncMock()
        run = _mock_run(
            status="planning",
            plan_started_at=datetime.now(UTC) - timedelta(minutes=30),
        )

        await _check_stale(db, run)

        mock_handle.assert_not_called()

    @patch("terrapod.services.run_reconciler.load_runner_config")
    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    async def test_no_phase_start_skips_check(self, mock_handle, mock_config):
        mock_config.return_value = MagicMock(
            stale_timeout_seconds=DEFAULT_STALE_TIMEOUT_SECONDS, launch_timeout_seconds=300
        )
        db = AsyncMock()
        run = _mock_run(status="planning", plan_started_at=None)

        await _check_stale(db, run)

        mock_handle.assert_not_called()

    @patch("terrapod.services.run_reconciler.load_runner_config")
    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    async def test_custom_timeout_from_config(self, mock_handle, mock_config):
        """Stale timeout is read from RunnerConfig, not hardcoded."""
        custom_timeout = 300  # 5 minutes
        mock_config.return_value = MagicMock(
            stale_timeout_seconds=custom_timeout, launch_timeout_seconds=300
        )
        db = AsyncMock()
        # 10 minutes ago — stale with 5m timeout, NOT stale with default 1h
        run = _mock_run(
            status="planning",
            plan_started_at=datetime.now(UTC) - timedelta(minutes=10),
        )

        await _check_stale(db, run)

        mock_handle.assert_called_once()
        assert "stale" in mock_handle.call_args.args[2].lower()


# ── drift-detection max-duration cap ─────────────────────────────────


class TestDriftMaxDuration:
    """Independent cap for drift runs.

    Distinct from the generic stale_timeout (which only fires when a
    listener stops reporting): this cap fires on a drift run that's
    legitimately running too long for a background plan-only check.
    The motivating incident: a github-provider state refresh ran past
    50 min on terrapod-config and silently blocked drift on that
    workspace; the listener was still reporting and the runner pod was
    healthy, so neither stale_timeout nor launch_timeout caught it.
    """

    @patch("terrapod.services.run_reconciler.load_runner_config")
    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    async def test_drift_planning_over_cap_gets_errored(self, mock_handle, mock_config):
        mock_config.return_value = MagicMock(
            stale_timeout_seconds=DEFAULT_STALE_TIMEOUT_SECONDS,
            launch_timeout_seconds=300,
            drift_max_duration_seconds=1800,
        )
        db = AsyncMock()
        # 35 min in — over the 30-min default drift cap
        run = _mock_run(
            status="planning",
            is_drift_detection=True,
            plan_started_at=datetime.now(UTC) - timedelta(minutes=35),
        )

        await _check_stale(db, run)

        mock_handle.assert_called_once()
        assert "drift" in mock_handle.call_args.args[2].lower()
        assert "max duration" in mock_handle.call_args.args[2].lower()

    @patch("terrapod.services.run_reconciler.load_runner_config")
    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    async def test_drift_under_cap_is_not_errored(self, mock_handle, mock_config):
        mock_config.return_value = MagicMock(
            stale_timeout_seconds=DEFAULT_STALE_TIMEOUT_SECONDS,
            launch_timeout_seconds=300,
            drift_max_duration_seconds=1800,
        )
        db = AsyncMock()
        run = _mock_run(
            status="planning",
            is_drift_detection=True,
            plan_started_at=datetime.now(UTC) - timedelta(minutes=10),
        )

        await _check_stale(db, run)

        mock_handle.assert_not_called()

    @patch("terrapod.services.run_reconciler.load_runner_config")
    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    async def test_drift_cap_disabled_falls_through_to_stale(self, mock_handle, mock_config):
        """drift_max_duration_seconds=0 disables the cap.

        The run can still be errored by the generic stale_timeout, but
        not by the drift-specific cap. With a 1h stale timeout and a
        35-min-old run, neither fires.
        """
        mock_config.return_value = MagicMock(
            stale_timeout_seconds=DEFAULT_STALE_TIMEOUT_SECONDS,
            launch_timeout_seconds=300,
            drift_max_duration_seconds=0,
        )
        db = AsyncMock()
        run = _mock_run(
            status="planning",
            is_drift_detection=True,
            plan_started_at=datetime.now(UTC) - timedelta(minutes=35),
        )

        await _check_stale(db, run)

        mock_handle.assert_not_called()

    @patch("terrapod.services.run_reconciler.load_runner_config")
    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    async def test_non_drift_run_ignores_cap(self, mock_handle, mock_config):
        """Regular (non-drift) runs MUST NOT be subject to the drift cap.

        A real interactive plan/apply can legitimately exceed 30 min;
        only the drift cohort is constrained by it.
        """
        mock_config.return_value = MagicMock(
            stale_timeout_seconds=DEFAULT_STALE_TIMEOUT_SECONDS,
            launch_timeout_seconds=300,
            drift_max_duration_seconds=1800,
        )
        db = AsyncMock()
        run = _mock_run(
            status="planning",
            is_drift_detection=False,
            plan_started_at=datetime.now(UTC) - timedelta(minutes=35),
        )

        await _check_stale(db, run)

        # Below the 1h stale timeout, drift cap doesn't apply → no error
        mock_handle.assert_not_called()


# ── launch_timeout (no job_name) cohort ──────────────────────────────


class TestCheckStaleLaunchTimeout:
    """Runs that were claimed but never had a Job launched.

    The listener can claim a run (transitioning it to planning/applying with
    listener_id set) but then fail to actually create the K8s Job — auth
    failure on /runner-token, K8s outage at create_job time, listener crash
    mid-launch. Without picking these up they sit indefinitely. The shorter
    launch_timeout (default 5 min) catches them quickly.
    """

    @patch("terrapod.services.run_reconciler.load_runner_config")
    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    async def test_pre_launch_run_errors_after_launch_timeout(self, mock_handle, mock_config):
        mock_config.return_value = MagicMock(stale_timeout_seconds=3600, launch_timeout_seconds=300)
        db = AsyncMock()
        run = _mock_run(
            status="planning",
            job_name=None,  # never launched
            plan_started_at=datetime.now(UTC) - timedelta(minutes=10),  # > 5 min
        )

        await _check_stale(db, run)

        mock_handle.assert_called_once()
        assert "pre-launch" in mock_handle.call_args.args[2].lower()

    @patch("terrapod.services.run_reconciler.load_runner_config")
    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    async def test_pre_launch_timeout_increments_metric(self, mock_handle, mock_config):
        """Backstop counter for silent listener failures must fire on pre-launch timeout."""
        from terrapod.api.metrics import LISTENER_PRELAUNCH_TIMEOUTS

        mock_config.return_value = MagicMock(stale_timeout_seconds=3600, launch_timeout_seconds=300)
        db = AsyncMock()
        run = _mock_run(
            status="planning",
            job_name=None,
            plan_started_at=datetime.now(UTC) - timedelta(minutes=10),
        )

        before = LISTENER_PRELAUNCH_TIMEOUTS._value.get()
        await _check_stale(db, run)
        after = LISTENER_PRELAUNCH_TIMEOUTS._value.get()

        assert after == before + 1

    @patch("terrapod.services.run_reconciler.load_runner_config")
    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    async def test_pre_launch_run_within_window_not_errored(self, mock_handle, mock_config):
        """Same condition as above but only 2 min in — under the 5m launch_timeout."""
        mock_config.return_value = MagicMock(stale_timeout_seconds=3600, launch_timeout_seconds=300)
        db = AsyncMock()
        run = _mock_run(
            status="planning",
            job_name=None,
            plan_started_at=datetime.now(UTC) - timedelta(minutes=2),
        )

        await _check_stale(db, run)

        mock_handle.assert_not_called()

    @patch("terrapod.services.run_reconciler.load_runner_config")
    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    async def test_running_job_uses_stale_timeout_not_launch_timeout(
        self, mock_handle, mock_config
    ):
        """A run with job_name set must use stale_timeout (1h), not launch_timeout (5m).

        Otherwise legitimate long plans would get killed at 5 min just because
        the Job hasn't reported intermediate status.
        """
        mock_config.return_value = MagicMock(stale_timeout_seconds=3600, launch_timeout_seconds=300)
        db = AsyncMock()
        run = _mock_run(
            status="planning",
            job_name="tprun-abc123-plan",  # Job exists
            plan_started_at=datetime.now(UTC) - timedelta(minutes=20),  # >5m, <1h
        )

        await _check_stale(db, run)

        mock_handle.assert_not_called()


class TestReconcileOneWithoutJobName:
    """Pre-launch runs (no job_name) skip the SSE round-trip and go straight to stale check."""

    @patch("terrapod.redis.client.get_job_status_from_redis", new_callable=AsyncMock)
    @patch("terrapod.redis.client.publish_listener_event", new_callable=AsyncMock)
    @patch("terrapod.services.run_reconciler._check_stale", new_callable=AsyncMock)
    async def test_no_job_name_skips_publish(self, mock_check_stale, mock_publish, mock_status):
        """No SSE check_job_status published — there's no Job for listeners to query."""
        db = AsyncMock()
        run = _mock_run(job_name=None)

        await _reconcile_one(db, run)

        mock_publish.assert_not_called()
        mock_status.assert_not_called()
        mock_check_stale.assert_called_once()
