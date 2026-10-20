"""Integration: the reconciler sees a `configuring` run at all (#1972).

Real Postgres, because what is under test is a `WHERE` clause and a column
mapping, and both are invisible to a mocked session. `reconcile_runs` selects
in-flight runs by status; before this phase existed that list was
planning/applying/canceling, so a `configuring` run matched nothing and the
reconciler was not merely slow to notice it -- it could never notice it. The
run sat in `configuring` for ever: no stale check, no unschedulable check, no
Job-status round trip, and nothing to move it even once its Job had finished.

The property is asserted through the pre-launch timeout rather than directly,
because that is the one path a test can drive without a live listener and a
real Job, and it needs BOTH halves of the change to fire:

  * the status must be in the query, or the run is never visited;
  * `_phase_start_of` must map `configuring` to `configure_started_at`, or the
    elapsed time is computed from a NULL and `_check_stale` returns early.

Remove either and this test fails, which is what makes it a gate on the pair
rather than on whichever half was written second.
"""

import uuid as _uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from terrapod.db.models import AgentPool, ConfigurationVersion, Run, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import pool_set, run_service
from terrapod.services.run_reconciler import reconcile_runs

pytestmark = pytest.mark.integration


async def _seed() -> tuple[Workspace, ConfigurationVersion]:
    async with get_db_session() as db:
        pool = AgentPool(name=f"cfgrec-{_uuid.uuid4().hex[:6]}")
        db.add(pool)
        await db.flush()

        ws = Workspace(
            name=f"configure-reconcile-{_uuid.uuid4().hex[:6]}",
            execution_mode="agent",
            config_directory="ansible",
            default_playbook="site.yml",
        )
        pool_set.set_workspace_pools(ws, [pool.id])
        db.add(ws)
        await db.flush()

        cv = ConfigurationVersion(workspace_id=ws.id, status="uploaded", source="tfe-api")
        db.add(cv)
        await db.flush()
        await db.commit()
        return ws, cv


async def _configuring_run(ws_id, cv_id, *, started_minutes_ago: int) -> Run:
    """A run parked in `configuring` with no Job, as a failed launch leaves it.

    `job_name` stays NULL deliberately: that is the cohort whose only rescue is
    the pre-launch timeout, and it is the cohort a listener that died between
    claiming and creating the Job produces.

    The status is set directly rather than walked. `transition_run` refuses
    `queued -> configuring` today because #1974 owns the transition that puts a
    run there, and the execution path has to be provable before its trigger
    exists -- the same reason the claim test seeds `is_configure`.
    """
    async with get_db_session() as db:
        ws = (await db.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
        run = await run_service.create_run(db, ws, configuration_version_id=cv_id)
        run.is_configure = True
        run.status = "configuring"
        run.job_name = None
        run.configure_started_at = datetime.now(UTC) - timedelta(minutes=started_minutes_ago)
        await db.flush()
        await db.commit()
        return run


async def _status_of(run_id) -> tuple[str, str | None]:
    async with get_db_session() as db:
        run = (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()
        return run.status, run.error_message


class TestTheReconcilerVisitsAConfiguringRun:
    async def test_a_configure_stuck_pre_launch_is_failed(self):
        """Past the launch grace with no Job, the run is errored, not abandoned."""
        ws, cv = await _seed()
        # Well past the five-minute default, so the grace cannot be the reason
        # this passes or fails.
        run = await _configuring_run(ws.id, cv.id, started_minutes_ago=90)

        await reconcile_runs()

        status, message = await _status_of(run.id)
        assert status == "errored", (
            f"a configuring run with no Job was left in {status!r}. The reconciler "
            "either never selected it or could not tell how long it had been there."
        )
        assert message, "errored with no message tells an operator nothing"

    async def test_a_configure_inside_the_grace_is_left_alone(self):
        """The contrast, without which the test above passes on a blanket rule.

        A reconciler that errored every Job-less run would satisfy the first
        assertion perfectly while breaking every configure that is merely a few
        seconds old -- i.e. every one, at the moment it starts.
        """
        ws, cv = await _seed()
        run = await _configuring_run(ws.id, cv.id, started_minutes_ago=0)

        await reconcile_runs()

        status, _ = await _status_of(run.id)
        assert status == "configuring", (
            f"a configure that had only just started was moved to {status!r}"
        )
