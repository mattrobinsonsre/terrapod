"""Integration: a listener claims a configure as a configure (#1972).

Real Postgres, because the whole thing under test is the claim query's `WHERE`.
A mocked session would assert the conditions we wrote back at us, and the defect
this exists to prevent is precisely that the conditions do not say what they
appear to: a standalone configure and a plan BOTH sit in `queued` (#1974 settled
the shape as one Run carrying a flag), and the claim loop returns the first
matching tuple -- so without the discriminator a configure is claimed as a plan
and the listener launches a Job that runs `tofu plan` against a directory of
playbooks.
"""

import uuid as _uuid

import pytest
from sqlalchemy import select

from terrapod.db.models import AgentPool, ConfigurationVersion, Run, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import pool_set, run_service

pytestmark = pytest.mark.integration


async def _seed(name: str) -> tuple[Workspace, AgentPool, ConfigurationVersion]:
    async with get_db_session() as db:
        pool = AgentPool(name=f"cfg-{name}-{_uuid.uuid4().hex[:6]}")
        db.add(pool)
        await db.flush()

        ws = Workspace(
            name=f"configure-claim-{name}-{_uuid.uuid4().hex[:6]}",
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
        return ws, pool, cv


async def _queue(ws_id, cv_id, *, is_configure: bool, path=("queued",), **kw) -> Run:
    """Seed a run and walk it along `path`.

    A path rather than a target, because the state machine refuses a jump: the
    run must travel `pending -> queued -> planning -> planned -> confirmed` and
    `transition_run` raises on anything else, which is the machine doing its job.
    """
    async with get_db_session() as db:
        ws = (await db.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
        run = await run_service.create_run(db, ws, configuration_version_id=cv_id, **kw)
        # Set directly: #1974 owns the API attribute that lets an operator ask
        # for a configure, and until it lands nothing can create one through a
        # surface. Seeding the column is what lets the EXECUTION path be proven
        # before the trigger exists.
        run.is_configure = is_configure
        await db.flush()
        for status in path:
            run = await run_service.transition_run(db, run, status)
        await db.commit()
        return run


async def _claim(pool_id):
    async with get_db_session() as db:
        return await run_service.claim_next_run(db, _uuid.uuid4(), pool_id, "listener-1")


class TestAConfigureIsNotClaimedAsAPlan:
    async def test_a_queued_configure_is_claimed_as_a_configure(self, app):
        ws, pool, cv = await _seed("standalone")
        run = await _queue(ws.id, cv.id, is_configure=True)

        claim = await _claim(pool.id)
        assert claim is not None, "a queued configure was not claimed at all"
        claimed, phase = claim
        assert claimed.id == run.id
        assert phase == "configure"

        async with get_db_session() as db:
            stored = (await db.execute(select(Run).where(Run.id == run.id))).scalar_one()
        assert stored.status == "configuring"
        # The reconciler's stale clock reads this; without it a configure has no
        # timestamp to time out against (#2131).
        assert stored.configure_started_at is not None

    async def test_an_ordinary_queued_run_is_still_claimed_as_a_plan(self, app):
        ws, pool, cv = await _seed("plan")
        run = await _queue(ws.id, cv.id, is_configure=False)

        claimed, phase = await _claim(pool.id)
        assert (claimed.id, phase) == (run.id, "plan")
        async with get_db_session() as db:
            stored = (await db.execute(select(Run).where(Run.id == run.id))).scalar_one()
        assert stored.status == "planning"

    async def test_the_plan_tuple_does_not_pick_up_a_configure(self, app):
        """The mutation this file exists for.

        A configure is the ONLY queued run here, so if the plan tuple matched it
        the claim would come back as `plan` and transition to `planning` -- which
        is a Job running `tofu plan` over playbooks.
        """
        ws, pool, cv = await _seed("only-configure")
        await _queue(ws.id, cv.id, is_configure=True)

        claimed, phase = await _claim(pool.id)
        assert phase != "plan", "a configure run was claimed as a plan"
        assert phase == "configure"


class TestACompositeRunIsStillClaimableAsAnApply:
    async def test_is_configure_does_not_block_the_apply_phase(self, app):
        """A composite run is apply-THEN-configure (#1407 point 1a).

        It therefore carries `is_configure=True` and sits in `confirmed` waiting
        for its apply. Constraining the apply tuple to `is_configure=False` --
        the symmetrical-looking change -- would strand every composite run at its
        apply for ever, so the apply tuple is deliberately unconstrained.
        """
        ws, pool, cv = await _seed("composite")
        run = await _queue(
            ws.id,
            cv.id,
            is_configure=True,
            path=("queued", "planning", "planned", "confirmed"),
        )

        claim = await _claim(pool.id)
        assert claim is not None, "a composite run was stranded at its apply"
        claimed, phase = claim
        assert (claimed.id, phase) == (run.id, "apply")
        async with get_db_session() as db:
            after = (await db.execute(select(Run).where(Run.id == run.id))).scalar_one()
        assert after.status == "applying"
