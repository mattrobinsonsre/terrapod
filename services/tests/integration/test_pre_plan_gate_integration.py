"""Integration: the pre-plan run task gate against real Postgres (#1837).

The safety property is **a gated run is not handed to a runner**, and it is
enforced by a SQL predicate inside the dispatcher's
``SELECT … FOR UPDATE SKIP LOCKED``. A mocked test can assert the predicate is
written down; only a real engine can show it selects what it claims to.

The sharpest case here is `test_a_run_with_no_stage_yet_is_still_held`. There
is a window between a run becoming `queued` and its stage being created, and
the obvious phrasing of the exclusion — "a stage exists and is unresolved" —
finds no row to exclude during it, so the run is dispatched and the plan the
gate exists to prevent runs anyway. That test pins the exclusion against the
window by suppressing the stage-opening pre-pass entirely.
"""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from terrapod.db.models import AgentPool, ConfigurationVersion, Run, RunTask, TaskStage, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import pool_set, run_service, run_task_service

pytestmark = pytest.mark.integration


async def _seed(name: str, *, stage=None, enforcement="mandatory", enabled=True):
    """A workspace on one pool, with an uploaded CV and optionally a run task."""
    async with get_db_session() as db:
        pool = AgentPool(name=f"{name}-pool")
        db.add(pool)
        await db.flush()

        ws = Workspace(name=name, execution_mode="agent")
        pool_set.set_workspace_pools(ws, [pool.id])
        db.add(ws)
        await db.flush()

        if stage is not None:
            db.add(
                RunTask(
                    workspace_id=ws.id,
                    name=f"{name}-task",
                    url="https://example.invalid/hook",
                    stage=stage,
                    enforcement_level=enforcement,
                    enabled=enabled,
                )
            )

        cv = ConfigurationVersion(workspace_id=ws.id, status="uploaded", source="tfe-api")
        db.add(cv)
        await db.flush()
        await db.commit()
        return ws.id, pool.id, cv.id


async def _queue_run(ws_id, cv_id) -> Run:
    async with get_db_session() as db:
        ws = (await db.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
        run = await run_service.create_run(db, ws, configuration_version_id=cv_id)
        run = await run_service.transition_run(db, run, "queued")
        await db.commit()
        return run


async def _claim(pool_id):
    async with get_db_session() as db:
        claim = await run_service.claim_next_run(db, listener_id=pool_id, pool_id=pool_id)
        await db.commit()
        return claim


async def _reload(run_id) -> Run:
    async with get_db_session() as db:
        return (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()


class TestTheGateHoldsTheRun:
    async def test_a_workspace_with_no_pre_plan_task_is_unaffected(self, app):
        """The overwhelmingly common case. Nobody configuring no run tasks
        should notice this feature exists at all."""
        ws_id, pool_id, cv_id = await _seed("pre-plan-none")
        run = await _queue_run(ws_id, cv_id)

        claim = await _claim(pool_id)
        assert claim is not None, "an ungated run must dispatch exactly as before"
        claimed, phase = claim
        assert claimed.id == run.id
        assert phase == "plan"

    async def test_a_gated_run_is_not_claimed_while_the_stage_runs(self, app):
        ws_id, pool_id, cv_id = await _seed("pre-plan-gated", stage="pre_plan")
        run = await _queue_run(ws_id, cv_id)

        claim = await _claim(pool_id)
        assert claim is None, "a run whose pre-plan gate is unresolved must not dispatch"

        stored = await _reload(run.id)
        assert stored.status == "queued", "it waits, it does not fail"

        # ...and the pre-pass opened the stage, so there is something to resolve.
        async with get_db_session() as db:
            stage = (
                await db.execute(select(TaskStage).where(TaskStage.run_id == run.id))
            ).scalar_one()
        assert stage.stage == "pre_plan"
        assert stage.status == "running"

    async def test_a_run_with_no_stage_yet_is_still_held(self, app):
        """The window between `queued` and the stage being created.

        Phrasing the exclusion as "an unresolved stage row exists" would find
        nothing to exclude here and dispatch the run — running the plan the
        gate exists to hold. Suppressing the pre-pass reproduces that window
        exactly.
        """
        ws_id, pool_id, cv_id = await _seed("pre-plan-window", stage="pre_plan")
        run = await _queue_run(ws_id, cv_id)

        with patch.object(run_service, "_open_pre_plan_stages", AsyncMock(return_value=None)):
            claim = await _claim(pool_id)

        assert claim is None, (
            "a run whose workspace wants a pre-plan gate must be held even "
            "before its stage row exists"
        )
        async with get_db_session() as db:
            stages = (
                (await db.execute(select(TaskStage).where(TaskStage.run_id == run.id)))
                .scalars()
                .all()
            )
        assert stages == [], "the pre-pass really was suppressed"
        assert (await _reload(run.id)).status == "queued"

    async def test_a_passed_stage_releases_the_run(self, app):
        ws_id, pool_id, cv_id = await _seed("pre-plan-release", stage="pre_plan")
        run = await _queue_run(ws_id, cv_id)

        assert await _claim(pool_id) is None

        # The external service reports back.
        async with get_db_session() as db:
            stage = (
                await db.execute(select(TaskStage).where(TaskStage.run_id == run.id))
            ).scalar_one()
            for result in await _results_for(db, stage.id):
                result.status = "passed"
            await db.flush()
            await run_task_service.resolve_stage(db, stage.id)
            await db.commit()

        claim = await _claim(pool_id)
        assert claim is not None, "a passed gate must release the run"
        assert claim[0].id == run.id

    async def test_a_disabled_task_does_not_gate(self, app):
        """A disabled task is configuration, not a gate. Holding runs on one
        would make disabling a broken task impossible without deleting it."""
        ws_id, pool_id, cv_id = await _seed("pre-plan-disabled", stage="pre_plan", enabled=False)
        await _queue_run(ws_id, cv_id)
        assert await _claim(pool_id) is not None

    async def test_a_post_plan_task_does_not_gate_the_dispatch(self, app):
        """Boundaries must not leak into each other — a post-plan task has no
        business holding a run before it plans."""
        ws_id, pool_id, cv_id = await _seed("pre-plan-postonly", stage="post_plan")
        await _queue_run(ws_id, cv_id)
        assert await _claim(pool_id) is not None


class TestAMandatoryFailureIsFinal:
    async def test_it_errors_the_run_naming_the_task(self, app):
        ws_id, pool_id, cv_id = await _seed("pre-plan-fail", stage="pre_plan")
        run = await _queue_run(ws_id, cv_id)

        # Open the stage, then fail it the way a callback would.
        assert await _claim(pool_id) is None
        async with get_db_session() as db:
            stage = (
                await db.execute(select(TaskStage).where(TaskStage.run_id == run.id))
            ).scalar_one()
            for result in await _results_for(db, stage.id):
                result.status = "failed"
            await db.flush()
            await run_task_service.resolve_stage(db, stage.id)
            await db.commit()

        # The next poll sees the failed gate and resolves the run.
        assert await _claim(pool_id) is None
        stored = await _reload(run.id)
        assert stored.status == "errored"
        assert "pre-plan-fail-task" in stored.error_message
        assert "final" in stored.error_message

    async def test_an_advisory_failure_lets_the_run_through(self, app):
        """Advisory means advisory at every boundary."""
        ws_id, pool_id, cv_id = await _seed(
            "pre-plan-advisory", stage="pre_plan", enforcement="advisory"
        )
        run = await _queue_run(ws_id, cv_id)

        assert await _claim(pool_id) is None
        async with get_db_session() as db:
            stage = (
                await db.execute(select(TaskStage).where(TaskStage.run_id == run.id))
            ).scalar_one()
            for result in await _results_for(db, stage.id):
                result.status = "failed"
            await db.flush()
            await run_task_service.resolve_stage(db, stage.id)
            await db.commit()

        claim = await _claim(pool_id)
        assert claim is not None, "an advisory failure must not block the plan"
        assert claim[0].id == run.id
        assert (await _reload(run.id)).status == "planning"


async def _results_for(db, stage_id):
    from terrapod.db.models import TaskStageResult

    return (
        (await db.execute(select(TaskStageResult).where(TaskStageResult.task_stage_id == stage_id)))
        .scalars()
        .all()
    )
