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
        with patch.object(
            inventory_resolve, "write_run_artifact", AsyncMock(return_value=("key", {}))
        ):
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
            # Stubbed for the same reason `_queue` stubs it, and now at a second
            # call site: the hand-over RE-RESOLVES the inventory (#2230), because
            # a composite resolved at creation would configure the state from
            # before its own apply. `_seed`'s configuration version is a row with
            # no archive behind it, so a real resolution here fails and the
            # refusal errors the run -- which is correct behaviour meeting an
            # unrepresentative fixture. This file is about the claim query.
            with patch.object(
                inventory_resolve, "write_run_artifact", AsyncMock(return_value=("key", {}))
            ):
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
            # Stubbed for the same reason `_queue` stubs it, and now at a second
            # call site: the hand-over RE-RESOLVES the inventory (#2230), because
            # a composite resolved at creation would configure the state from
            # before its own apply. `_seed`'s configuration version is a row with
            # no archive behind it, so a real resolution here fails and the
            # refusal errors the run -- which is correct behaviour meeting an
            # unrepresentative fixture. This file is about the claim query.
            with patch.object(
                inventory_resolve, "write_run_artifact", AsyncMock(return_value=("key", {}))
            ):
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


class TestAConfigureAndAnApplyDoNotRunTogether:
    """#1975: the workspace's single mutating slot covers a configure too.

    **Both directions are seeded, because neither alone establishes the
    property.** A fixture that only checks "a configure waits for an apply"
    passes whether or not `configuring` is in the in-flight set -- that
    direction already held, since a standalone configure is `queued` with
    `plan_only` refused on it, so it already met the gate. The direction that
    was silently broken is the converse, and it is only visible when a run is
    actually sitting in `configuring`.

    Each blocked case carries a positive control. Without one the test would
    pass if nothing were ever claimable, which is the failure mode that makes a
    green contention test worthless.
    """

    async def test_an_apply_capable_run_waits_for_an_in_flight_configure(self, app):
        """The half #1975 fixed. A configure mutates real hosts for as long as
        the playbook runs; an apply must not start underneath one."""
        ws, pool, cv = await _seed("slot-apply-waits")
        configure = await _queue(ws.id, cv.id, is_configure=True, path=("queued", "configuring"))
        assert configure.status == "configuring"

        await _queue(ws.id, cv.id, is_configure=False, plan_only=False)

        claimed = await _claim(pool.id)
        assert claimed is None, (
            f"claimed {claimed} while a configure was in flight on the "
            "same workspace — an apply would start against hosts a playbook is "
            "part-way through changing."
        )

    async def test_the_control_that_run_IS_claimable_once_the_configure_ends(self, app):
        """The positive control for the case above.

        Without it, that test passes if the run were unclaimable for any
        unrelated reason -- a wrong pool, a missing configuration version, a
        gate nobody noticed.
        """
        ws, pool, cv = await _seed("slot-apply-control")
        configure = await _queue(ws.id, cv.id, is_configure=True, path=("queued", "configuring"))
        plan = await _queue(ws.id, cv.id, is_configure=False, plan_only=False)

        async with get_db_session() as db:
            row = (await db.execute(select(Run).where(Run.id == configure.id))).scalar_one()
            await run_service.transition_run(db, row, "applied")
            await db.commit()

        claim = await _claim(pool.id)
        assert claim is not None, (
            "with the configure finished the apply-capable run must be claimable; if it "
            "is not, the blocked assertion above proves nothing."
        )
        claimed, _phase = claim
        assert claimed.id == plan.id

    async def test_a_configure_waits_for_an_in_flight_apply(self, app):
        """The direction that already held, seeded so the pair is a contrast.

        Kept rather than trimmed: it is what makes the set a RELATIONSHIP test.
        Dropping it would leave a suite that cannot tell "both serialise" from
        "configures never run".
        """
        ws, pool, cv = await _seed("slot-configure-waits")
        applying = await _queue(
            ws.id,
            cv.id,
            is_configure=False,
            plan_only=False,
            path=("queued", "planning", "planned", "confirmed", "applying"),
        )
        assert applying.status == "applying"

        await _queue(ws.id, cv.id, is_configure=True)

        claimed = await _claim(pool.id)
        assert claimed is None, (
            f"claimed {claimed} while an apply was in flight — a "
            "configure would target half-applied infrastructure, and a half-written "
            "inventory with it."
        )

    async def test_the_control_that_a_configure_IS_claimable_on_a_free_workspace(self, app):
        ws, pool, cv = await _seed("slot-configure-control")
        configure = await _queue(ws.id, cv.id, is_configure=True)

        claim = await _claim(pool.id)
        assert claim is not None, (
            "a configure on an idle workspace must be claimable; if it is not, the "
            "blocked assertion above proves nothing."
        )
        claimed, phase = claim
        assert (claimed.id, phase) == (configure.id, "configure")
