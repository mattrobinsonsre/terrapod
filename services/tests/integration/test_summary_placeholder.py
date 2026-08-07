"""Integration tests: the queued-summary placeholder (#1295).

`GET /runs/{id}/plan-summary` used to 404 for the entire time the work sat in
the trigger queue, because the row was only written when a consumer *started*
the item. A queued summary was therefore indistinguishable from a run that had
none and never would — which is how a twelve-minute backlog got diagnosed as a
broken feature.

`mark_summary_queued` closes that by inserting a `pending` placeholder at
enqueue time. These live against real Postgres rather than a mock because the
whole behaviour *is* a database semantic: an insert that must lose to any row
already present, resolved by a unique constraint rather than by a read the
caller performs first. With the AI lane now running several consumers at once
(#1296), that read-then-write window is not theoretical.
"""

import uuid

import pytest
from sqlalchemy import select

from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"


async def _make_run(client, name: str) -> uuid.UUID:
    """Create a workspace + run via the API, return the run's UUID."""
    resp = await client.post(
        WS_ENDPOINT,
        json={"data": {"type": "workspaces", "attributes": {"name": name}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text

    from terrapod.db.models import ConfigurationVersion, Run, Workspace
    from terrapod.db.session import get_db_session

    async with get_db_session() as db:
        ws = (await db.execute(select(Workspace).where(Workspace.name == name))).scalar_one()
        cv = ConfigurationVersion(workspace_id=ws.id, source="api", status="uploaded")
        db.add(cv)
        await db.flush()
        run = Run(
            workspace_id=ws.id,
            configuration_version_id=cv.id,
            status="planned",
            created_by="tests",
        )
        db.add(run)
        await db.commit()
        return run.id


async def test_placeholder_is_created_when_no_summary_exists(app, client):
    """The gap case: enqueue happened, no consumer has touched it yet."""
    set_auth(app, admin_user())
    from terrapod.db.models import PlanSummary
    from terrapod.db.session import get_db_session
    from terrapod.services.summariser import mark_summary_queued

    run_id = await _make_run(client, f"ph-gap-{uuid.uuid4().hex[:8]}")

    async with get_db_session() as db:
        mark = await db.execute(select(PlanSummary).where(PlanSummary.run_id == run_id))
        assert mark.scalar_one_or_none() is None, "precondition: no row yet"

        await mark_summary_queued(db, run_id=run_id)
        await db.commit()

    async with get_db_session() as db:
        row = (
            await db.execute(select(PlanSummary).where(PlanSummary.run_id == run_id))
        ).scalar_one()
        assert row.status == "pending"
        assert row.kind == "plan_summary"


async def test_placeholder_never_overwrites_a_finished_summary(app, client):
    """The race that matters: a consumer finished before the placeholder landed.

    The AI lane runs several consumers concurrently, so a fast handler really
    can complete before the enqueuing request gets round to its insert. The
    real result must survive; a placeholder is only ever allowed to fill a gap.
    """
    set_auth(app, admin_user())
    from terrapod.db.models import PlanSummary
    from terrapod.db.session import get_db_session
    from terrapod.services.summariser import _upsert_summary, mark_summary_queued

    run_id = await _make_run(client, f"ph-race-{uuid.uuid4().hex[:8]}")

    async with get_db_session() as db:
        await _upsert_summary(
            db,
            run_id=run_id,
            kind="plan_summary",
            status="ready",
            description="the real answer",
            risk_level="high",
        )
        await db.commit()

    async with get_db_session() as db:
        await mark_summary_queued(db, run_id=run_id)
        await db.commit()

    async with get_db_session() as db:
        row = (
            await db.execute(select(PlanSummary).where(PlanSummary.run_id == run_id))
        ).scalar_one()
        assert row.status == "ready", "a placeholder must never demote a finished summary"
        assert row.description == "the real answer"
        assert row.risk_level == "high"


async def test_placeholder_does_not_clobber_a_terminal_failure(app, client):
    """`errored` is an answer too — the user should keep seeing why it failed."""
    set_auth(app, admin_user())
    from terrapod.db.models import PlanSummary
    from terrapod.db.session import get_db_session
    from terrapod.services.summariser import _upsert_summary, mark_summary_queued

    run_id = await _make_run(client, f"ph-err-{uuid.uuid4().hex[:8]}")

    async with get_db_session() as db:
        await _upsert_summary(
            db,
            run_id=run_id,
            kind="plan_summary",
            status="errored",
            error_message="model refused",
        )
        await db.commit()

    async with get_db_session() as db:
        await mark_summary_queued(db, run_id=run_id)
        await db.commit()

    async with get_db_session() as db:
        row = (
            await db.execute(select(PlanSummary).where(PlanSummary.run_id == run_id))
        ).scalar_one()
        assert row.status == "errored"
        assert row.error_message == "model refused"


async def test_it_is_idempotent(app, client):
    """A re-enqueue (dedup expired, or a retry) must not duplicate the row.

    `run_id` is unique, so a second plain insert would raise rather than
    quietly no-op — which is exactly what ON CONFLICT DO NOTHING prevents.
    """
    set_auth(app, admin_user())
    from terrapod.db.models import PlanSummary
    from terrapod.db.session import get_db_session
    from terrapod.services.summariser import mark_summary_queued

    run_id = await _make_run(client, f"ph-idem-{uuid.uuid4().hex[:8]}")

    async with get_db_session() as db:
        await mark_summary_queued(db, run_id=run_id)
        await mark_summary_queued(db, run_id=run_id)
        await mark_summary_queued(db, run_id=run_id)
        await db.commit()

    async with get_db_session() as db:
        rows = (
            (await db.execute(select(PlanSummary).where(PlanSummary.run_id == run_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1


async def test_the_endpoint_stops_404ing_once_queued(app, client):
    """The user-visible invariant: after enqueue, never 404.

    This is the whole point of #1295 — a 404 reads as "this feature is broken
    for this run", and that reading was wrong for minutes at a time.
    """
    set_auth(app, admin_user())
    from terrapod.db.session import get_db_session
    from terrapod.services.summariser import mark_summary_queued

    run_id = await _make_run(client, f"ph-api-{uuid.uuid4().hex[:8]}")

    before = await client.get(f"/api/terrapod/v1/runs/run-{run_id}/plan-summary", headers=AUTH)
    assert before.status_code == 404, "precondition: nothing enqueued yet"

    async with get_db_session() as db:
        await mark_summary_queued(db, run_id=run_id)
        await db.commit()

    after = await client.get(f"/api/terrapod/v1/runs/run-{run_id}/plan-summary", headers=AUTH)
    assert after.status_code == 200, after.text
    assert after.json()["data"]["attributes"]["status"] == "pending"
