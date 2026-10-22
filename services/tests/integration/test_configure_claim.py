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
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from terrapod.db.models import AgentPool, ConfigurationVersion, Run, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import inventory_resolve, pool_set, run_service

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
            # Declared, so `create_run` will actually build a configure
            # (#1974). Before the trigger existed these tests set the column on
            # the row afterwards, which proved the claim query and skipped the
            # refusal in front of it.
            configure_enabled=True,
        )
        pool_set.set_workspace_pools(ws, [pool.id])
        db.add(ws)
        await db.flush()

        cv = ConfigurationVersion(workspace_id=ws.id, status="uploaded", source="tfe-api")
        db.add(cv)
        await db.flush()
        await db.commit()
        return ws, pool, cv


async def _queue(
    ws_id,
    cv_id,
    *,
    is_configure: bool,
    composite: bool = False,
    path=("queued",),
    **kw,
) -> Run:
    """Seed a run and walk it along `path`.

    A path rather than a target, because the state machine refuses a jump: the
    run must travel `pending -> queued -> planning -> planned -> confirmed` and
    `transition_run` raises on anything else, which is the machine doing its job.

    `composite` asks for an apply THEN a configure on one run. It is resolved
    from the workspace rather than passed to `create_run` (#1974) -- the CLI
    cannot send a flag, so the setting is where the answer lives -- which is why
    this flips the column on the workspace and lets the resolver decide.
    """
    async with get_db_session() as db:
        ws = (await db.execute(select(Workspace).where(Workspace.id == ws_id))).scalar_one()
        if composite:
            ws.configure_after_apply = True
            await db.flush()
        # The inventory artifact is written at run creation (#2021) and RAISES
        # when it cannot resolve, by design -- so a configure cannot be created
        # without a real archive holding a real inventory. That is #2021's
        # property and has its own tests; this file is about the claim query, so
        # the write is stubbed here rather than fixtured. Mocked at the boundary
        # BELOW what is under test: `create_run` still runs in full, including
        # the declaration guard and the shape resolver whose answer the claim
        # then keys on.
        with patch.object(inventory_resolve, "write_run_artifact", AsyncMock()):
            run = await run_service.create_run(
                db,
                ws,
                configuration_version_id=cv_id,
                is_configure=is_configure and not composite,
                **kw,
            )
        assert run.is_configure == (is_configure or composite)
        assert run.configure_after_apply == composite
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
            is_configure=False,
            composite=True,
            path=("queued", "planning", "planned", "confirmed"),
        )

        claim = await _claim(pool.id)
        assert claim is not None, "a composite run was stranded at its apply"
        claimed, phase = claim
        assert (claimed.id, phase) == (run.id, "apply")
        async with get_db_session() as db:
            after = (await db.execute(select(Run).where(Run.id == run.id))).scalar_one()
        assert after.status == "applying"


class TestTheCompositeHandsOverToItsConfigure:
    """The second claim, which is what makes a composite one run (#1974).

    Real Postgres for the same reason as the rest of this file: the hand-over is
    expressed in the claim query's `WHERE`, and the property is that exactly one
    listener can take it.
    """

    async def test_a_composite_is_claimed_as_a_configure_once_its_apply_has_finished(self, app):
        ws, pool, cv = await _seed("handover")
        run = await _queue(
            ws.id,
            cv.id,
            is_configure=False,
            composite=True,
            path=("queued", "planning", "planned", "confirmed", "applying"),
        )

        # The apply lands. The run deliberately does NOT go terminal: a
        # composite that reached `applied` here would close the CLI's log
        # stream before the configure it is attached to had run.
        async with get_db_session() as db:
            stored = (await db.execute(select(Run).where(Run.id == run.id))).scalar_one()
            await run_service.complete_apply(db, stored)
            await db.commit()

        async with get_db_session() as db:
            mid = (await db.execute(select(Run).where(Run.id == run.id))).scalar_one()
        assert mid.status == "applying", "a composite went terminal at the end of its apply"
        assert mid.apply_finished_at is not None, "nothing marks the hand-over"
        assert mid.job_name is None, "the finished apply's Job is still attached"

        claim = await _claim(pool.id)
        assert claim is not None, "a composite was stranded between its two phases"
        claimed, phase = claim
        assert (claimed.id, phase) == (run.id, "configure")

        async with get_db_session() as db:
            after = (await db.execute(select(Run).where(Run.id == run.id))).scalar_one()
        assert after.status == "configuring"
        assert after.configure_started_at is not None

    async def test_an_applying_run_whose_apply_is_still_going_is_not_claimed(self, app):
        """The discriminator is `apply_finished_at`, not the status.

        Claiming on `applying` alone would hand a listener a run whose apply Job
        is still running, and it would launch a configure against hosts the
        apply has not finished building.
        """
        ws, pool, cv = await _seed("inflight")
        await _queue(
            ws.id,
            cv.id,
            is_configure=False,
            composite=True,
            path=("queued", "planning", "planned", "confirmed", "applying"),
        )

        assert await _claim(pool.id) is None, "a composite mid-apply was claimed for its configure"

    async def test_only_one_listener_can_take_the_hand_over(self, app):
        """The transition to `configuring` is what closes the race.

        A self-transition would leave the row matching its own claim condition,
        so a second listener polling a moment later would launch a second
        configure Job against the same hosts.
        """
        ws, pool, cv = await _seed("race")
        run = await _queue(
            ws.id,
            cv.id,
            is_configure=False,
            composite=True,
            path=("queued", "planning", "planned", "confirmed", "applying"),
        )
        async with get_db_session() as db:
            stored = (await db.execute(select(Run).where(Run.id == run.id))).scalar_one()
            await run_service.complete_apply(db, stored)
            await db.commit()

        first = await _claim(pool.id)
        assert first is not None and first[1] == "configure"
        assert await _claim(pool.id) is None, "the hand-over was claimable twice"

    async def test_a_standalone_configure_is_not_claimed_out_of_applying(self, app):
        """It never reaches `applying` at all -- `queued -> configuring` is its
        entry -- so this pins that the hand-over tuple cannot pick up the other
        shape by matching on `is_configure` alone."""
        ws, pool, cv = await _seed("standalone-not-applying")
        run = await _queue(ws.id, cv.id, is_configure=True)

        claimed, phase = await _claim(pool.id)
        assert (claimed.id, phase) == (run.id, "configure")
        async with get_db_session() as db:
            after = (await db.execute(select(Run).where(Run.id == run.id))).scalar_one()
        # From `queued`, not from `applying`: a standalone configure has no
        # apply half, so `apply_started_at` stays NULL.
        assert after.status == "configuring"
        assert after.apply_started_at is None


class TestTheConfigureCompletesTheRun:
    async def test_complete_configure_drives_a_standalone_to_applied(self, app):
        """`applied` is the terminal for a configure too, which is what makes it
        mean applied AND configured (#1988).

        Its absence was not a style question: both engines' `resolve_terminal`
        returned "nothing to do" for a `configuring` run, so a playbook that
        finished cleanly drove nothing and the stale check eventually errored
        it.
        """
        ws, pool, cv = await _seed("complete-standalone")
        run = await _queue(ws.id, cv.id, is_configure=True, path=("queued", "configuring"))

        async with get_db_session() as db:
            stored = (await db.execute(select(Run).where(Run.id == run.id))).scalar_one()
            await run_service.complete_configure(db, stored)
            await db.commit()

        async with get_db_session() as db:
            after = (await db.execute(select(Run).where(Run.id == run.id))).scalar_one()
        assert after.status == "applied"
        assert after.configure_finished_at is not None

    async def test_both_engines_ask_for_it(self, app):
        """A configure runs beside whichever engine the workspace uses, so both
        strategies have to answer for one -- and they answer identically,
        because a configure is not an engine."""
        from terrapod.engines.pulumi import PulumiStrategy
        from terrapod.engines.terraform import TerraformStrategy

        for strategy in (TerraformStrategy(), PulumiStrategy()):
            outcome = strategy.resolve_terminal(
                run_status="configuring", run_source="tfe-api", job_status="succeeded"
            )
            assert outcome.action == "complete_configure", type(strategy).__name__
            assert outcome.phase == "configure", type(strategy).__name__
