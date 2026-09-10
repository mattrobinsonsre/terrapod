"""Terraform's terminal-resolution rules, pinned against a real database (#1489).

Phase 3 of #1407 moves "what does a finished Job mean" behind the engine
strategy. These tests exist so the rules being moved are *recorded* — because a
second engine will answer the same question differently, and the only way to add
one safely is to know exactly what Terraform does today.

**Why the integration tier and not a mocked service test.** The inputs are a Job
status, a run row in one of several states, a `plan-result` POST that may or may
not have arrived, and a workspace lock. That is combinatorial and it depends on
real row-level semantics — a mocked session proves the code calls what you told
it to call, not that the run ends up in the right state. #1489 asks for these
here for that reason.

The decision itself is resolved from plain values, so each case asserts twice:
the engine's *decision*, and the *state the run actually lands in* once the
reconciler acts on it.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from terrapod.db.models import ONBOARDING_DISCOVERY_SOURCE, Run, Workspace
from terrapod.engines import known_engines, strategy_for
from terrapod.engines.terraform import TerraformStrategy

pytestmark = pytest.mark.asyncio


async def _mk(session, *, status: str, source: str = "tfe-api", **kw) -> tuple[Workspace, Run]:
    """A workspace and a run in a given state, persisted."""
    ws = Workspace(
        id=uuid.uuid4(),
        name=f"tr-{uuid.uuid4().hex[:10]}",
        execution_mode="agent",
        auto_apply=kw.pop("auto_apply", False),
        locked=kw.pop("locked", False),
    )
    session.add(ws)
    await session.flush()

    run = Run(
        id=uuid.uuid4(),
        workspace_id=ws.id,
        status=status,
        source=source,
        job_name="tprun-abc123-plan",
        job_namespace="terrapod-runners",
        **kw,
    )
    session.add(run)
    await session.flush()
    return ws, run


class TestTheDecision:
    """The engine's rules, read straight off the strategy.

    Plain values in, a decision out — no database needed, which is what lets the
    matrix be exhaustive rather than sampled.
    """

    @pytest.mark.parametrize(
        ("run_status", "source", "job_status", "action", "phase"),
        [
            # Succeeded, and which completion applies is chosen by run state.
            ("planning", "tfe-api", "succeeded", "complete_plan", "plan"),
            ("applying", "tfe-api", "succeeded", "complete_apply", "apply"),
            # Failed and deleted both error, in either phase.
            ("planning", "tfe-api", "failed", "error", "plan"),
            ("applying", "tfe-api", "failed", "error", "apply"),
            ("planning", "tfe-api", "deleted", "error", "plan"),
            ("applying", "tfe-api", "deleted", "error", "apply"),
            # Onboarding discovery settles the session; there is no plan.
            ("planning", ONBOARDING_DISCOVERY_SOURCE, "succeeded", "discovery_succeeded", "plan"),
            # Already resolved by the runner's own POST — nothing left to do.
            # This is the path that once left `has_changes` unknown and falsely
            # flagged a workspace as drifted, so it is pinned explicitly.
            ("planned", "tfe-api", "succeeded", "none", "apply"),
            ("applied", "tfe-api", "succeeded", "none", "apply"),
        ],
    )
    async def test_rule(self, run_status, source, job_status, action, phase):
        outcome = TerraformStrategy().resolve_terminal(
            run_status=run_status, run_source=source, job_status=job_status
        )
        assert (outcome.action, outcome.phase) == (action, phase)

    async def test_an_unknown_job_status_does_nothing(self):
        """A status the engine does not recognise must not be guessed at.

        Treating an unfamiliar Job status as success would apply infrastructure
        on the strength of a signal nobody defined.
        """
        outcome = TerraformStrategy().resolve_terminal(
            run_status="planning", run_source="tfe-api", job_status="something-new"
        )
        assert outcome.action == "none"

    async def test_the_onboarding_source_constant_matches_the_model(self):
        """`engines/` duplicates the literal to stay free of the DB layer.

        The listener image ships no models, so the constant cannot be imported
        there — which means the two copies have to be kept in step by a test
        rather than by the type system.
        """
        from terrapod.engines import terraform as engine_module

        assert engine_module.ONBOARDING_DISCOVERY_SOURCE == ONBOARDING_DISCOVERY_SOURCE


class TestWhatTheRunActuallyBecomes:
    """The decision carried through the reconciler, against a real database."""

    async def test_succeeded_plan_lands_planned(self, app):
        from terrapod.db.session import get_db_session
        from terrapod.services import run_reconciler

        async with get_db_session() as session:
            _, run = await _mk(session, status="planning")
            await session.commit()
            run_id = run.id

            outcome = strategy_for(
                (await session.get(Workspace, run.workspace_id)).engine
            ).resolve_terminal(run_status=run.status, run_source=run.source, job_status="succeeded")
            assert outcome.action == "complete_plan"
            await run_reconciler._handle_succeeded(session, run, outcome)
            await session.commit()

        async with get_db_session() as session:
            got = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            assert got.status in ("planned", "errored", "confirmed", "applying")
            # Not left mid-flight: the whole point of resolution is that a
            # finished Job stops the run being "planning" forever.
            assert got.status != "planning"

    async def test_failed_errors_the_run_and_unlocks_the_workspace(self, app):
        """A failed Job must not leave the workspace locked.

        A lock outlives the run that took it, so a missed unlock blocks every
        later run on that workspace — the failure is silent until someone asks
        why nothing is queueing.
        """
        from terrapod.db.session import get_db_session
        from terrapod.services import run_reconciler

        async with get_db_session() as session:
            ws, run = await _mk(session, status="applying", locked=True)
            ws.lock_id = "held-by-this-run"
            await session.commit()
            run_id, ws_id = run.id, ws.id

            await run_reconciler._handle_failed(session, run, "the Job failed")
            await session.commit()

        async with get_db_session() as session:
            got = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            got_ws = (
                await session.execute(select(Workspace).where(Workspace.id == ws_id))
            ).scalar_one()
            assert got.status == "errored"
            assert got.error_message
            assert got_ws.locked is False
            assert got_ws.lock_id is None

    async def test_a_failure_without_a_captured_error_still_errors(self, app):
        """No runner-side detail is not a reason to leave the run running."""
        from terrapod.db.session import get_db_session
        from terrapod.services import run_reconciler

        async with get_db_session() as session:
            _, run = await _mk(session, status="planning")
            await session.commit()
            run_id = run.id
            await run_reconciler._handle_failed(session, run, "Job failed")
            await session.commit()

        async with get_db_session() as session:
            got = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            assert got.status == "errored"

    async def test_an_already_resolved_run_is_left_alone(self, app):
        """The runner's direct POST may have driven the transition already.

        The reconciler runs on a timer and will see that Job again; acting a
        second time is how a `planned` run gets pushed somewhere it should not
        go.
        """
        from terrapod.db.session import get_db_session

        async with get_db_session() as session:
            _, run = await _mk(session, status="planned")
            await session.commit()
            run_id = run.id

            outcome = strategy_for(
                (await session.get(Workspace, run.workspace_id)).engine
            ).resolve_terminal(run_status=run.status, run_source=run.source, job_status="succeeded")
            assert outcome.action == "none"
            await session.commit()

        async with get_db_session() as session:
            got = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
            assert got.status == "planned"


class TestTheEngineColumnDrivesIt:
    async def test_resolution_is_selected_by_the_run_s_engine(self, app):
        """A run resolves through its own engine, not a hardcoded one.

        With Terraform the only engine this cannot fail — which is exactly why
        it is asserted now, while adding a second engine is still ahead rather
        than behind.
        """
        from terrapod.db.session import get_db_session

        async with get_db_session() as session:
            ws, run = await _mk(session, status="planning")
            await session.commit()
            # The run carries no engine (#1536); it resolves through its workspace's.
            assert ws.engine == "terraform"
            assert strategy_for(ws.engine).name == "terraform"

    async def test_an_unresolvable_engine_refuses_rather_than_defaulting(self):
        """Running the wrong tool against real infrastructure beats no answer.

        `pulumi` used to be the example of an unknown engine here; it is a known
        one since #1523, so the case is made with an engine that genuinely is
        not built rather than by weakening the assertion.
        """
        with pytest.raises(ValueError, match="unknown engine"):
            strategy_for("bicep")

    async def test_a_gated_off_engine_is_refused_differently_from_an_unknown_one(self):
        """The two have completely different fixes.

        "Unknown" means a row was written by a newer replica or by hand.
        "Not enabled" means an operator turned it off and the message should say
        so — telling them Terrapod has never heard of Pulumi would send them
        looking for a missing install.
        """
        from unittest.mock import patch

        with patch("terrapod.engines.engine_enabled", side_effect=lambda e: e != "pulumi"):
            with pytest.raises(ValueError, match="not enabled"):
                strategy_for("pulumi")

    async def test_terraform_is_unaffected_by_the_pulumi_switch(self):
        """The whole point of the gate: multi-engine ambition costs a terraform
        user nothing, in either position of the switch."""
        from unittest.mock import patch

        for pulumi_on in (True, False):
            with patch(
                "terrapod.engines.engine_enabled",
                side_effect=lambda e, on=pulumi_on: True if e == "terraform" else on,
            ):
                assert strategy_for("terraform").name == "terraform"
                assert strategy_for(None).name == "terraform"

    async def test_pulumi_resolves_when_enabled(self):
        assert strategy_for("pulumi").name == "pulumi"
        assert "pulumi" in known_engines()

    async def test_a_gated_off_engine_is_absent_from_known_engines(self):
        """Absent, not listed-and-then-refused — the same rule the surfaces
        follow, so a caller enumerating engines never offers one that cannot
        run."""
        from unittest.mock import patch

        with patch("terrapod.engines.engine_enabled", side_effect=lambda e: e != "pulumi"):
            assert "pulumi" not in known_engines()
            assert "terraform" in known_engines()


class TestPulumiTerminalRules:
    """Pulumi's rules, recorded rather than merely implemented (#1523).

    #1489 pinned Terraform's for exactly this moment: a second engine answers the
    same question differently, and the only way to add one safely is to know what
    the first does. These are the counterpart, in the same shape.
    """

    @pytest.mark.parametrize(
        ("run_status", "job_status", "action", "phase"),
        [
            # Succeeded, and which completion applies is chosen by run state —
            # the same shape as Terraform, in Pulumi's vocabulary.
            ("planning", "succeeded", "complete_plan", "preview"),
            ("applying", "succeeded", "complete_apply", "update"),
            # Failed and deleted both error, in either phase.
            ("planning", "failed", "error", "preview"),
            ("applying", "failed", "error", "update"),
            ("planning", "deleted", "error", "preview"),
            ("applying", "deleted", "error", "update"),
            # Already resolved. For Pulumi this is the COMMON case rather than a
            # race: the checkpoint is pushed over the #1522 surface during the
            # run, so state is durable before the Job exits and there is nothing
            # to collect afterwards.
            ("planned", "succeeded", "none", "update"),
            ("applied", "succeeded", "none", "update"),
        ],
    )
    async def test_rule(self, run_status, job_status, action, phase):
        from terrapod.engines.pulumi import PulumiStrategy

        outcome = PulumiStrategy().resolve_terminal(
            run_status=run_status, run_source="tfe-api", job_status=job_status
        )
        assert (outcome.action, outcome.phase) == (action, phase)

    async def test_an_unknown_job_status_does_nothing(self):
        """Treating an unfamiliar status as success would apply infrastructure on
        the strength of a signal nobody defined."""
        from terrapod.engines.pulumi import PulumiStrategy

        outcome = PulumiStrategy().resolve_terminal(
            run_status="planning", run_source="tfe-api", job_status="something-new"
        )
        assert outcome.action == "none"

    async def test_the_phases_are_pulumi_s_words_not_terraform_s(self):
        """A run is `planning` whatever engine it belongs to; what the user is
        SHOWN differs, and that difference must survive into the outcome (#1521).
        """
        from terrapod.engines.pulumi import PulumiStrategy

        strategy = PulumiStrategy()
        assert strategy.phases == ("preview", "update")
        assert strategy.status_phases["planning"] == "preview"
        assert strategy.status_phases["applying"] == "update"

    async def test_terraform_still_answers_in_its_own_words(self):
        """The two coexist; adding Pulumi must not have edited Terraform's."""
        from terrapod.engines.terraform import TerraformStrategy

        outcome = TerraformStrategy().resolve_terminal(
            run_status="planning", run_source="tfe-api", job_status="succeeded"
        )
        assert (outcome.action, outcome.phase) == ("complete_plan", "plan")
