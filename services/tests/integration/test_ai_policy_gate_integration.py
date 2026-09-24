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
