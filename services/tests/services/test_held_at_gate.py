"""A run held at a post-plan gate is a finished plan awaiting a decision (#1725).

`complete_plan` stamps `plan_finished_at`, then evaluates the post-plan run task,
policy and security-scan gates. A gate that blocks returns early and leaves the
run in `planning`. Before #1725 that run was treated as a plan still running:

- once Kubernetes cleaned up the finished Job (`ttlSecondsAfterFinished`), the
  listener reported `deleted` and the reconciler **errored the run** -- so an
  override was impossible ten minutes after the plan finished;
- its plan phase was reported `running`, so `tofu apply` **hung**;
- it could not be discarded, and a newer run did not supersede it.

Reproduced end to end on a live stack before the fix.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

from terrapod.services import run_service
from terrapod.services.run_reconciler import _reconcile_one

FINISHED = datetime(2026, 9, 17, 12, 30, tzinfo=UTC)


def _run(**kw):
    run = MagicMock()
    run.id = kw.get("id", uuid.uuid4())
    run.workspace_id = kw.get("workspace_id", uuid.uuid4())
    run.status = kw.get("status", "planning")
    run.plan_finished_at = kw.get("plan_finished_at", FINISHED)
    run.plan_started_at = kw.get("plan_started_at", FINISHED)
    run.plan_only = kw.get("plan_only", False)
    run.job_name = kw.get("job_name", "tprun-abc-plan")
    run.job_namespace = "terrapod-runners"
    run.pool_id = uuid.uuid4()
    run.discard_reason = None
    run.message = kw.get("message", "original message")
    run.is_drift_detection = False
    run.has_changes = True
    return run


class TestWhatCountsAsHeld:
    def test_a_planning_run_with_a_finished_plan_is_held(self):
        assert run_service.is_held_at_gate(_run())

    def test_a_planning_run_whose_plan_is_still_running_is_not(self):
        assert not run_service.is_held_at_gate(_run(plan_finished_at=None))

    @pytest.mark.parametrize("status", ["planned", "applying", "queued", "errored"])
    def test_other_statuses_are_not_held(self, status):
        assert not run_service.is_held_at_gate(_run(status=status))

    def test_only_an_apply_capable_hold_is_discardable(self):
        assert run_service.is_discardable_hold(_run())
        assert not run_service.is_discardable_hold(_run(plan_only=True))


class TestTheReconcilerNoLongerErrorsAHeldRun:
    """The bug that made an override impossible ten minutes after the plan."""

    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    @patch("terrapod.services.run_reconciler._check_stale", new_callable=AsyncMock)
    @patch("terrapod.services.run_service.complete_plan", new_callable=AsyncMock)
    @patch("terrapod.redis.client.get_job_status_from_redis", new_callable=AsyncMock)
    @patch("terrapod.redis.client.publish_listener_event", new_callable=AsyncMock)
    @pytest.mark.parametrize("job_status", ["deleted", None])
    async def test_a_cleaned_up_or_unreported_job_re_drives_the_gates(
        self, _publish, get_status, complete_plan, check_stale, handle_failed, job_status
    ):
        get_status.return_value = job_status
        db, run = AsyncMock(), _run()

        await _reconcile_one(db, run)

        # Re-driving is how a passing run task, or an override, releases it.
        complete_plan.assert_awaited_once_with(db, run)
        handle_failed.assert_not_awaited()
        check_stale.assert_not_awaited()

    @patch("terrapod.services.run_reconciler._handle_failed", new_callable=AsyncMock)
    @patch("terrapod.redis.client.get_job_status_from_redis", new_callable=AsyncMock)
    @patch("terrapod.redis.client.publish_listener_event", new_callable=AsyncMock)
    async def test_a_deleted_job_still_errors_a_plan_that_never_finished(
        self, _publish, get_status, handle_failed
    ):
        # The fix must not hide a Job that vanished mid-plan.
        get_status.return_value = "deleted"
        run = _run(plan_finished_at=None)

        await _reconcile_one(AsyncMock(), run)

        handle_failed.assert_awaited_once()

    @patch("terrapod.services.run_reconciler._check_stale", new_callable=AsyncMock)
    @patch("terrapod.redis.client.get_job_status_from_redis", new_callable=AsyncMock)
    @patch("terrapod.redis.client.publish_listener_event", new_callable=AsyncMock)
    async def test_a_running_plan_with_no_status_still_ages_toward_stale(
        self, _publish, get_status, check_stale
    ):
        get_status.return_value = None
        run = _run(plan_finished_at=None)

        await _reconcile_one(AsyncMock(), run)

        check_stale.assert_awaited_once()


class TestAHeldRunCanBeDiscarded:
    async def test_discard_accepts_a_held_apply_run(self):
        run = _run()
        with (
            patch("terrapod.services.ha_role.ensure_leader", new_callable=AsyncMock),
            patch.object(
                run_service, "transition_run", new=AsyncMock(side_effect=lambda db, r, s, **_: r)
            ) as transition,
        ):
            await run_service.discard_run(AsyncMock(), run, reason="no thanks")
        transition.assert_awaited_once()
        assert transition.await_args.args[2] == "discarded"
        assert run.discard_reason == "no thanks"

    @pytest.mark.parametrize(
        "run_kw",
        [{"plan_finished_at": None}, {"plan_only": True}],
        ids=["still planning", "plan-only"],
    )
    async def test_discard_still_refuses_what_it_refused_before(self, run_kw):
        with (
            patch("terrapod.services.ha_role.ensure_leader", new_callable=AsyncMock),
            pytest.raises(ValueError),
        ):
            await run_service.discard_run(AsyncMock(), _run(**run_kw))

    def test_the_transition_is_legal(self):
        assert "discarded" in run_service.VALID_TRANSITIONS["planning"]


class TestANewerRunSupersedesAHeldOne:
    def _db_returning(self, runs):
        db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = runs
        db.execute.return_value = result
        return db

    async def test_the_held_run_is_discarded_and_the_running_one_left_alone(self):
        newer = _run(status="queued", plan_finished_at=None)
        newer.created_at = datetime(2026, 9, 17, 13, 0, tzinfo=UTC)
        newer.vcs_pull_request_number = None
        held = _run()
        running = _run(plan_finished_at=None)

        with (
            patch.object(run_service, "_is_supersedeable_kind", return_value=True),
            patch.object(run_service, "discard_run", new_callable=AsyncMock) as discard,
            patch.object(run_service, "cancel_run", new_callable=AsyncMock) as cancel,
        ):
            n = await run_service.supersede_stale_runs(self._db_returning([held, running]), newer)

        assert n == 1
        discard.assert_awaited_once()
        assert discard.await_args.args[1] is held
        cancel.assert_not_awaited()
        # A run whose plan is still running is not superseded -- and is not
        # relabelled as if it were.
        assert running.message == "original message"


class TestBlockedBy:
    async def test_not_held_is_none(self):
        assert await run_service.blocked_by(AsyncMock(), _run(status="planned")) is None

    @pytest.mark.parametrize(
        ("stage_status", "policy", "scan", "expected"),
        [
            ("pending", True, True, "run-task"),
            ("passed", True, True, "policy"),
            ("overridden", False, True, "security-scan"),
            (None, False, True, "security-scan"),
            ("passed", False, False, None),
        ],
    )
    async def test_names_the_gate_in_the_order_complete_plan_checks_them(
        self, stage_status, policy, scan, expected
    ):
        stage = None if stage_status is None else MagicMock(status=stage_status)
        with (
            patch(
                "terrapod.services.run_task_service._existing_stage",
                AsyncMock(return_value=stage),
            ),
            patch(
                "terrapod.services.policy_set_service.run_is_policy_blocked",
                AsyncMock(return_value=policy),
            ),
            patch(
                "terrapod.services.security_scan_service.run_is_scan_blocked",
                AsyncMock(return_value=scan),
            ),
        ):
            assert await run_service.blocked_by(AsyncMock(), _run()) == expected


class TestWhatTheApiReports:
    def test_a_held_run_reports_its_plan_finished(self):
        from terrapod.api.routers.runs import _plan_status

        # `running` kept go-tfe's plan log reader polling: `tofu apply` hung.
        assert _plan_status(_run()) == "finished"
        assert _plan_status(_run(plan_finished_at=None)) == "running"

    def test_a_held_run_is_discardable_and_says_what_holds_it(self):
        from terrapod.api.routers.runs import _run_json

        held = _run()
        for attr in (
            "vcs_commit_sha",
            "vcs_branch",
            "vcs_pull_request_number",
            "configuration_version_id",
            "created_by",
        ):
            setattr(held, attr, None)
        with patch.object(run_service, "resolve_auto_apply_mode", return_value="never"):
            attrs = _run_json(held, blocked_by="security-scan")["data"]["attributes"]
        assert attrs["blocked-by"] == "security-scan"
        assert attrs["actions"]["is-discardable"] is True
        assert attrs["actions"]["is-confirmable"] is False


class TestWhatAHeldRunCostsAndWhatStillFreesIt:
    """Found by the v1.7.2 pre-release review."""

    def _held(self, **kw):
        run = _run(**kw)
        run.vcs_pull_request_number = kw.get("pr")
        run.is_drift_detection = kw.get("drift", False)
        return run

    @patch("terrapod.services.run_service.complete_plan", new_callable=AsyncMock)
    @patch("terrapod.redis.client.get_job_status_from_redis", new_callable=AsyncMock)
    @patch("terrapod.redis.client.publish_listener_event", new_callable=AsyncMock)
    async def test_listeners_are_not_asked_about_a_held_runs_job(
        self, publish, get_status, complete_plan
    ):
        # Every held run used to send its pool a status query and a log-stream
        # request for a long-deleted Job every tick, for as long as it waited.
        with patch.object(run_service, "_has_newer_live_run", AsyncMock(return_value=False)):
            await _reconcile_one(AsyncMock(), self._held())
        publish.assert_not_awaited()
        get_status.assert_not_awaited()
        complete_plan.assert_awaited_once()

    @patch("terrapod.services.run_reconciler._check_stale", new_callable=AsyncMock)
    @patch("terrapod.services.run_service.complete_plan", new_callable=AsyncMock)
    async def test_a_newer_run_queued_behind_a_held_one_supersedes_it(
        self, complete_plan, check_stale
    ):
        # The queue-time supersede skipped this run while it was still
        # planning; once held, it would block the newer run indefinitely.
        run = self._held()
        with (
            patch.object(run_service, "_has_newer_live_run", AsyncMock(return_value=True)),
            patch.object(run_service, "discard_run", new_callable=AsyncMock) as discard,
        ):
            await _reconcile_one(AsyncMock(), run)
        discard.assert_awaited_once()
        assert discard.await_args.args[1] is run
        complete_plan.assert_not_awaited()

    @patch("terrapod.services.run_reconciler._check_stale", new_callable=AsyncMock)
    @patch("terrapod.services.run_service.complete_plan", new_callable=AsyncMock)
    async def test_an_apply_run_waits_for_a_decision_without_a_timeout(
        self, complete_plan, check_stale
    ):
        with patch.object(run_service, "_has_newer_live_run", AsyncMock(return_value=False)):
            await _reconcile_one(AsyncMock(), self._held())
        check_stale.assert_not_awaited()

    @pytest.mark.parametrize(
        "kw",
        [{"drift": True}, {"pr": 12}, {}],
        ids=["drift check", "speculative PR plan", "CLI plan"],
    )
    @patch("terrapod.services.run_reconciler._check_stale", new_callable=AsyncMock)
    @patch("terrapod.services.run_service.complete_plan", new_callable=AsyncMock)
    async def test_a_plan_only_run_keeps_its_timeouts(self, complete_plan, check_stale, kw):
        # Nothing to decide for a run that cannot apply: a run task that never
        # calls back must not keep it, or a drift check, forever.
        run = self._held(plan_only=True, **kw)
        with patch.object(run_service, "_has_newer_live_run", AsyncMock()) as newer:
            await _reconcile_one(AsyncMock(), run)
        newer.assert_not_awaited()
        complete_plan.assert_awaited_once()
        check_stale.assert_awaited_once_with(ANY, run)

    @patch("terrapod.services.run_reconciler._check_stale", new_callable=AsyncMock)
    async def test_a_plan_only_run_released_by_its_gate_is_not_checked(self, check_stale):
        run = self._held(plan_only=True)

        async def release(db, r):
            r.status = "planned"
            return r

        with patch.object(run_service, "complete_plan", AsyncMock(side_effect=release)):
            await _reconcile_one(AsyncMock(), run)
        check_stale.assert_not_awaited()
