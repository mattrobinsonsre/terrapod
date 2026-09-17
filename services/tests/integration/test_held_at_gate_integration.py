"""Integration: a run held at a post-plan gate, against real Postgres (#1725).

The services tests pin each piece with mocks; this drives the real rows. A run
whose plan has finished but which an enforced security scan holds must report
what holds it, survive the reconciler once its Job has been cleaned up, and be
discardable.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from terrapod.db.models import Run, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import security_scan_service
from terrapod.services.run_reconciler import _reconcile_one
from tests.integration.conftest import AUTH, admin_user, set_auth
from tests.integration.test_run_state_machine import _create_run, _create_workspace

pytestmark = pytest.mark.integration


async def _hold_at_scan_gate(run_id: str) -> uuid.UUID:
    """Put the run where `complete_plan` leaves it when an enforced scan fails."""
    rid = uuid.UUID(run_id.removeprefix("run-"))
    async with get_db_session() as db, db.begin():
        run = (await db.execute(select(Run).where(Run.id == rid))).scalar_one()
        # The gate only holds a run on a workspace set to `enforced`; with
        # `off` or `advisory`, `complete_plan` would rightly release it.
        ws = await db.get(Workspace, run.workspace_id)
        ws.security_scan_enforcement = "enforced"
        run.status = "planning"
        run.plan_started_at = datetime.now(UTC)
        run.plan_finished_at = datetime.now(UTC)
        run.has_changes = True
        run.job_name = "tprun-held-plan"
        await security_scan_service.record_scan_result(
            db,
            run_id=rid,
            engine="checkov",
            enforcement_level="enforced",
            severity_threshold="high",
            outcome="failed",
            findings=[{"check_id": "CKV_AWS_24", "severity": "high"}],
            summary={"high": 1},
        )
    return rid


async def _status(rid: uuid.UUID) -> str:
    async with get_db_session() as db:
        return (await db.execute(select(Run.status).where(Run.id == rid))).scalar_one()


class TestARunHeldByAnEnforcedScan:
    async def test_reports_the_gate_and_a_finished_plan(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "held-report")
        run = await _create_run(client, ws_id)
        await _hold_at_scan_gate(run["id"])

        resp = await client.get(f"/api/v2/runs/{run['id']}", headers=AUTH)
        assert resp.status_code == 200, resp.text
        attrs = resp.json()["data"]["attributes"]
        assert attrs["status"] == "planning"
        assert attrs["blocked-by"] == "security-scan"
        assert attrs["actions"]["is-discardable"] is True
        assert attrs["actions"]["is-confirmable"] is False

        plan_id = resp.json()["data"]["relationships"]["plan"]["data"]["id"]
        plan = await client.get(f"/api/v2/plans/{plan_id}", headers=AUTH)
        assert plan.status_code == 200, plan.text
        assert plan.json()["data"]["attributes"]["status"] == "finished"

    async def test_an_unheld_run_reports_no_gate(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "not-held")
        run = await _create_run(client, ws_id)

        resp = await client.get(f"/api/v2/runs/{run['id']}", headers=AUTH)
        assert resp.json()["data"]["attributes"]["blocked-by"] is None

    async def test_survives_the_reconciler_after_its_job_is_cleaned_up(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "held-reconcile")
        run = await _create_run(client, ws_id)
        rid = await _hold_at_scan_gate(run["id"])

        with (
            patch(
                "terrapod.redis.client.get_job_status_from_redis",
                AsyncMock(return_value="deleted"),
            ),
            patch("terrapod.redis.client.publish_listener_event", AsyncMock()),
        ):
            async with get_db_session() as db:
                row = (await db.execute(select(Run).where(Run.id == rid))).scalar_one()
                await _reconcile_one(db, row, "terraform")
                await db.commit()

        # Before #1725 this became `errored` with "Job deleted".
        assert await _status(rid) == "planning"

    async def test_can_be_discarded(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "held-discard")
        run = await _create_run(client, ws_id)
        rid = await _hold_at_scan_gate(run["id"])

        resp = await client.post(f"/api/v2/runs/{run['id']}/actions/discard", headers=AUTH)
        assert resp.status_code in (200, 202), resp.text
        assert await _status(rid) == "discarded"
