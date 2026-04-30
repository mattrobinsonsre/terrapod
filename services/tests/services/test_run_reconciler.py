"""Tests for run reconciler — periodic task that drives run state transitions."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

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
    run.vcs_commit_sha = kwargs.get("vcs_commit_sha", None)
    run.is_drift_detection = kwargs.get("is_drift_detection", False)
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
