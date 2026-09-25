"""Integration: the AI policy gate holding and releasing a run, real Postgres.

The services tests pin each piece with mocks. This drives the real rows through
the real state machine, which is where the gate's blast radius actually lives: a
held run keeps its workspace lock, the reconciler re-enters it on every tick,
and until #1815 the documented way out answered 409.

Written because the gate had **no** integration coverage at all while being the
one post-plan gate that can hold every apply-capable run in a deployment —
`test_held_at_gate_integration.py` covers the security scan and this is its
sibling.

The case that matters most here is the run held with NO evaluation row. It
cannot be reached through the service tests' mocks in a way that proves the
endpoint releases it, because the release depends on a row being *created*
inside the same transaction the run transition reads.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from terrapod.db.models import AIPolicyEvaluation, Run, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import ai_policy_service
from tests.integration.conftest import AUTH, admin_user, set_auth
from tests.integration.test_run_state_machine import _create_run, _create_workspace

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _mandatory_gate():
    """Turn the gate on, deployment-wide and mandatory.

    Without this every test here passes for the wrong reason: the shipped
    default is `policy.enabled: false`, so `effective_enforcement` returns
    "off", nothing is ever held, and an override of a gate that was not
    gating proves nothing. The first version of this module did exactly that
    and looked green.
    """
    from terrapod.config import settings

    policy = settings.ai_summary.policy
    before = (policy.enabled, policy.enforcement_level, policy.risk_threshold)
    policy.enabled = True
    policy.enforcement_level = "mandatory"
    policy.risk_threshold = "high"
    yield
    policy.enabled, policy.enforcement_level, policy.risk_threshold = before


async def _hold_at_ai_gate(run_id: str, *, with_verdict: bool) -> uuid.UUID:
    """Leave the run where `complete_plan` leaves it under a mandatory gate.

    `with_verdict=False` is the state the fix is about: the plan finished, the
    gate is mandatory, and no evaluation was ever recorded — the summariser
    never ran, or raised before settling.
    """
    rid = uuid.UUID(run_id.removeprefix("run-"))
    async with get_db_session() as db, db.begin():
        run = (await db.execute(select(Run).where(Run.id == rid))).scalar_one()
        ws = await db.get(Workspace, run.workspace_id)
        ws.ai_policy_mode = "default"
        run.status = "planning"
        run.plan_started_at = datetime.now(UTC)
        run.plan_finished_at = datetime.now(UTC)
        run.has_changes = True
        run.job_name = "tprun-held-ai"
        if with_verdict:
            await ai_policy_service.record_evaluation(
                db,
                run_id=rid,
                enforcement_level="mandatory",
                outcome="denied",
                verdict={"decision": "deny", "reasons": [{"criterion": "no public buckets"}]},
                risk_level="high",
            )
    return rid


async def _evaluation(rid: uuid.UUID) -> AIPolicyEvaluation | None:
    async with get_db_session() as db:
        return (
            await db.execute(select(AIPolicyEvaluation).where(AIPolicyEvaluation.run_id == rid))
        ).scalar_one_or_none()


async def _status(rid: uuid.UUID) -> str:
    async with get_db_session() as db:
        return (await db.execute(select(Run.status).where(Run.id == rid))).scalar_one()


class TestARunHeldWithNoVerdictAtAll:
    """The path that used to be a dead end. A mandatory gate holds a run it has
    no ruling for -- correctly, silence is not consent -- but when the verdict
    can never arrive, the override refusing to act left discard as the only
    exit while the run kept its workspace lock."""

    async def test_the_override_releases_it_and_records_who(self, app, client):
        set_auth(app, admin_user())
        ws = await _create_workspace(client, f"ai-gate-noverdict-{uuid.uuid4().hex[:8]}")
        run = await _create_run(client, ws)
        rid = await _hold_at_ai_gate(run["id"], with_verdict=False)

        assert await _evaluation(rid) is None, "precondition: nothing has ruled"

        resp = await client.post(
            f"/api/terrapod/v1/runs/run-{rid}/actions/override-ai-policy", headers=AUTH
        )
        assert resp.status_code == 200, resp.text

        row = await _evaluation(rid)
        assert row is not None, "the release must leave a record, not just unblock"
        assert row.overridden_by == admin_user().email
        assert row.overridden_at is not None

    async def test_the_record_is_honest_rather_than_a_forged_pass(self, app, client):
        """An auditor has to be able to tell this apart from a gate that ran
        and was overruled. `passed` here would be a lie told to a real DB."""
        set_auth(app, admin_user())
        ws = await _create_workspace(client, f"ai-gate-honest-{uuid.uuid4().hex[:8]}")
        run = await _create_run(client, ws)
        rid = await _hold_at_ai_gate(run["id"], with_verdict=False)

        await client.post(
            f"/api/terrapod/v1/runs/run-{rid}/actions/override-ai-policy", headers=AUTH
        )

        row = await _evaluation(rid)
        assert row.outcome == "overridden"
        assert row.verdict in (None, {}), "no verdict was ever reached; do not invent one"
        assert row.error and "never ruled" in row.error

    async def test_the_run_no_longer_reports_the_gate_as_blocking(self, app, client):
        set_auth(app, admin_user())
        ws = await _create_workspace(client, f"ai-gate-release-{uuid.uuid4().hex[:8]}")
        run = await _create_run(client, ws)
        rid = await _hold_at_ai_gate(run["id"], with_verdict=False)

        # `... is False or True` would pass whatever happened. Assert the real
        # precondition instead: under a mandatory gate with no evaluation, the
        # run IS held -- which is what makes the release below meaningful.
        async with get_db_session() as db:
            run_row = (await db.execute(select(Run).where(Run.id == rid))).scalar_one()
            ws_row = await db.get(Workspace, run_row.workspace_id)
            assert ai_policy_service.effective_enforcement(ws_row) == "mandatory"
            assert await ai_policy_service.evaluate_post_plan(db, run_row) is not None

        await client.post(
            f"/api/terrapod/v1/runs/run-{rid}/actions/override-ai-policy", headers=AUTH
        )

        async with get_db_session() as db:
            assert await ai_policy_service.run_is_ai_policy_blocked(db, rid) is False


class TestARunHeldByARealDeny:
    """The ordinary case, which must keep working: a verdict exists and says
    deny. Overriding records who overruled it WITHOUT erasing what was decided."""

    async def test_the_deny_survives_the_override(self, app, client):
        set_auth(app, admin_user())
        ws = await _create_workspace(client, f"ai-gate-deny-{uuid.uuid4().hex[:8]}")
        run = await _create_run(client, ws)
        rid = await _hold_at_ai_gate(run["id"], with_verdict=True)

        before = await _evaluation(rid)
        assert before.outcome == "denied"

        resp = await client.post(
            f"/api/terrapod/v1/runs/run-{rid}/actions/override-ai-policy", headers=AUTH
        )
        assert resp.status_code == 200, resp.text

        after = await _evaluation(rid)
        assert after.outcome == "denied", "the ruling is overruled, not rewritten"
        assert after.overridden_by == admin_user().email
        assert after.verdict["decision"] == "deny"

    async def test_only_one_evaluation_row_ever_exists_per_run(self, app, client):
        """`record_evaluation` is a read-modify-write against a unique
        constraint. Overriding a run that already has a row must not attempt a
        second insert -- that is an IntegrityError on a real database, which no
        mock would show."""
        set_auth(app, admin_user())
        ws = await _create_workspace(client, f"ai-gate-unique-{uuid.uuid4().hex[:8]}")
        run = await _create_run(client, ws)
        rid = await _hold_at_ai_gate(run["id"], with_verdict=True)

        for _ in range(3):
            resp = await client.post(
                f"/api/terrapod/v1/runs/run-{rid}/actions/override-ai-policy", headers=AUTH
            )
            assert resp.status_code == 200, resp.text

        async with get_db_session() as db:
            rows = (
                (
                    await db.execute(
                        select(AIPolicyEvaluation).where(AIPolicyEvaluation.run_id == rid)
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1, f"expected one evaluation row, found {len(rows)}"


class TestTheOverrideIsAdminOnly:
    async def test_a_reader_cannot_release_a_held_run(self, app, client):
        """The gate exists to stop an apply. Read access must not lift it --
        pinned here against real RBAC resolution, not a patched capability set."""
        from tests.integration.conftest import regular_user

        set_auth(app, admin_user())
        ws = await _create_workspace(client, f"ai-gate-rbac-{uuid.uuid4().hex[:8]}")
        run = await _create_run(client, ws)
        rid = await _hold_at_ai_gate(run["id"], with_verdict=True)

        set_auth(app, regular_user())
        resp = await client.post(
            f"/api/terrapod/v1/runs/run-{rid}/actions/override-ai-policy", headers=AUTH
        )
        assert resp.status_code == 403, resp.text
        assert (await _evaluation(rid)).overridden_by is None


class TestTwoWritersRaceForTheOneEvaluationRow:
    """`run_id` is UNIQUE, and the two writers converge on exactly the state
    the override exists for: a mandatory gate holding a run with no verdict.
    The operator clicks Override while the summariser's model call returns.

    Both read None, both insert, and before this the loser raised
    `IntegrityError` out of the endpoint -- a 500 that told the operator
    nothing about whether the run had been released.

    Real Postgres, because the defect IS the unique constraint; a mocked
    session cannot raise it. A real run too, because the row carries a foreign
    key -- which is also why the retry is narrowed to the unique violation.
    """

    async def test_two_concurrent_writers_produce_one_row_and_no_error(self, app, client):
        """GENUINELY concurrent, which the first version of this test was not.

        Writing it sequentially proves nothing: the second writer's SELECT
        finds the first writer's committed row and takes the update path, so
        the insert never races and the test passes with the fix removed. Both
        transactions have to be open at once — the second INSERT then blocks on
        the unique index until the first commits, and is rejected.
        """
        import asyncio

        from terrapod.db.session import get_db_session
        from terrapod.services import ai_policy_service

        set_auth(app, admin_user())
        ws = await _create_workspace(client, f"ai-race-{uuid.uuid4().hex[:8]}")
        run = await _create_run(client, ws)
        rid = uuid.UUID(run["id"].removeprefix("run-"))

        async def writer(outcome: str) -> None:
            async with get_db_session() as db:
                await ai_policy_service.record_evaluation(
                    db, run_id=rid, enforcement_level="mandatory", outcome=outcome
                )

        # Neither may raise. Before the fix the loser raised IntegrityError out
        # of its commit, which reached the override endpoint as a 500.
        await asyncio.gather(writer("failed"), writer("passed"))

        async with get_db_session() as db:
            rows = (
                (
                    await db.execute(
                        select(AIPolicyEvaluation).where(AIPolicyEvaluation.run_id == rid)
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1, f"the race produced {len(rows)} rows"

    async def test_the_savepoint_does_not_discard_the_callers_other_work(self, app, client):
        """The reason this is a savepoint and not a bare `db.rollback()`.

        The override endpoint has the release itself pending when it records
        the evaluation; rolling the whole transaction back to recover from the
        race would throw that away and silently fail to release the run.
        """
        from terrapod.db.session import get_db_session
        from terrapod.services import ai_policy_service

        set_auth(app, admin_user())
        ws = await _create_workspace(client, f"ai-race2-{uuid.uuid4().hex[:8]}")
        run = await _create_run(client, ws)
        rid = uuid.UUID(run["id"].removeprefix("run-"))

        async with get_db_session() as db:
            await ai_policy_service.record_evaluation(
                db, run_id=rid, enforcement_level="mandatory", outcome="failed"
            )

        marker = f"race-marker-{uuid.uuid4().hex[:8]}"
        async with get_db_session() as db:
            other = (await db.execute(select(Run).where(Run.id == rid))).scalar_one()
            other.message = marker  # pending work the caller cares about
            await ai_policy_service.record_evaluation(
                db, run_id=rid, enforcement_level="mandatory", outcome="passed"
            )

        async with get_db_session() as db:
            after = (await db.execute(select(Run).where(Run.id == rid))).scalar_one()
        assert after.message == marker, "the retry rolled back the caller's pending work"

    async def test_a_foreign_key_violation_is_not_retried(self, app, client):
        """Narrow the catch, or the retry turns "no such run" into the same
        error raised twice from a confusing place."""
        from sqlalchemy.exc import IntegrityError

        from terrapod.db.session import get_db_session
        from terrapod.services import ai_policy_service

        with pytest.raises(IntegrityError):
            async with get_db_session() as db:
                await ai_policy_service.record_evaluation(
                    db,
                    run_id=uuid.uuid4(),  # no such run
                    enforcement_level="mandatory",
                    outcome="failed",
                )


class TestOverrideAttributionSurvivesTheRightReRuling:
    """Who released a run is the half an auditor cannot reconstruct.

    `record_evaluation` clears `overridden_by` on a re-ruling, because an
    override belongs to the verdict it released and regenerating a summary
    must not launder a fresh deny through a decision made about a different
    one. The exception is an override that has ALREADY released a run --
    the #1815 case, where the run was held with no verdict at all.

    The predicate that tells those apart asks about the row as it stood
    BEFORE the write. Evaluating it after the write inverts it exactly, and
    an inverted guard is worse than no guard: it preserves the attribution in
    the one case that does not need it and destroys it in the only case that
    does. Both directions are pinned here, because a test for either one
    alone passes with the bug present.
    """

    async def test_an_override_that_already_released_a_run_keeps_its_attribution(self, app, client):
        set_auth(app, admin_user())
        ws = await _create_workspace(client, f"ai-attr-a-{uuid.uuid4().hex[:8]}")
        run = await _create_run(client, ws)
        rid = uuid.UUID(run["id"].removeprefix("run-"))

        # The #1815 shape: held with nothing to rule on, released by a person.
        async with get_db_session() as db:
            row = await ai_policy_service.record_evaluation(
                db, run_id=rid, enforcement_level="mandatory", outcome="overridden"
            )
            row.overridden_by = "admin@example.com"
            row.overridden_at = datetime.now(UTC)

        # The verdict lands afterwards, carrying a real body -- the ordinary
        # case, and the one the post-mutation read got wrong.
        async with get_db_session() as db:
            await ai_policy_service.record_evaluation(
                db,
                run_id=rid,
                enforcement_level="mandatory",
                outcome="failed",
                verdict={"decision": "deny", "reason": "landed late"},
                risk_level="high",
            )

        async with get_db_session() as db:
            after = (
                await db.execute(select(AIPolicyEvaluation).where(AIPolicyEvaluation.run_id == rid))
            ).scalar_one()

        assert after.outcome == "failed", "the re-ruling itself must still be recorded"
        assert after.overridden_by == "admin@example.com", (
            "the attribution was cleared, so the row now reads `failed` with no "
            "override beside a run a person released -- which reads as the gate "
            "having failed to stop it"
        )
        assert after.overridden_at is not None

    async def test_an_override_of_a_real_verdict_does_not_carry_over(self, app, client):
        """The other direction, and the reason the guard is narrow.

        Here the override was made about a verdict that existed. A later
        re-ruling is a different verdict, so the decision does not travel with
        it -- otherwise regenerating a summary would silently release a deny
        nobody has looked at.
        """
        set_auth(app, admin_user())
        ws = await _create_workspace(client, f"ai-attr-b-{uuid.uuid4().hex[:8]}")
        run = await _create_run(client, ws)
        rid = uuid.UUID(run["id"].removeprefix("run-"))

        async with get_db_session() as db:
            row = await ai_policy_service.record_evaluation(
                db,
                run_id=rid,
                enforcement_level="mandatory",
                outcome="failed",
                verdict={"decision": "deny", "reason": "the one that was overridden"},
                risk_level="high",
            )
            row.overridden_by = "admin@example.com"
            row.overridden_at = datetime.now(UTC)

        async with get_db_session() as db:
            await ai_policy_service.record_evaluation(
                db,
                run_id=rid,
                enforcement_level="mandatory",
                outcome="failed",
                verdict={"decision": "deny", "reason": "a freshly regenerated one"},
                risk_level="high",
            )

        async with get_db_session() as db:
            after = (
                await db.execute(select(AIPolicyEvaluation).where(AIPolicyEvaluation.run_id == rid))
            ).scalar_one()

        assert after.overridden_by is None, (
            "a new verdict inherited a decision an admin made about a different one"
        )
        assert after.overridden_at is None
