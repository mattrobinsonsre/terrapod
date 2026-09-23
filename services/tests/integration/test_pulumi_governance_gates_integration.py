"""Integration: governance never holds a Pulumi apply for a result that never comes (#1567).

Real Postgres, real rows. A global mandatory policy set that denies everything
and an enforced security scan are both in force.

The two gates now differ, and the difference is the whole point of this file:

  - **Policy sets apply to Pulumi**, since the runner builds an OPA input from
    the preview's engine event log. A missing result holds the run, exactly as
    it does for Terraform -- the safety net firing for its real reason, a
    runner that did not report.
  - **Security scans still do not.** Checkov and Trivy read Terraform plan
    JSON, so a scan must never be what holds a Pulumi apply; that is the
    original defect, and an enforced scan cannot even be turned on.

Terraform failing closed on both throughout is the part worth proving just as
hard.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from terrapod.db.models import Policy, PolicyEvaluation, PolicySet, SecurityScanResult, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import run_service
from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration

NATIVE = "/api/v1/workspaces"
DENY_ALL = 'package terrapod\n\ndeny contains "nothing may change" if true\n'


async def _create(client, name: str, engine: str, **attrs) -> tuple[int, dict]:
    r = await client.post(
        NATIVE,
        json={
            "data": {"type": "workspaces", "attributes": {"name": name, "engine": engine, **attrs}}
        },
        headers=AUTH,
    )
    return r.status_code, r.json()


async def _deny_everything_globally() -> None:
    """A global mandatory policy set, written as rows: no OPA needed to store it."""
    async with get_db_session() as db:
        ps = PolicySet(
            name=f"deny-all-{uuid.uuid4().hex[:6]}",
            enforcement_level="mandatory",
            global_scope=True,
        )
        db.add(ps)
        await db.flush()
        db.add(Policy(policy_set_id=ps.id, name="deny-all", rego=DENY_ALL))
        await db.commit()


async def _plan_finishes(ws_id: str) -> tuple[str, int, int]:
    """Create an apply run, finish its plan, and report what the gates did."""
    wid = uuid.UUID(ws_id.removeprefix("ws-"))
    async with get_db_session() as db:
        ws = await db.get(Workspace, wid)
        # Enforced regardless of what the API allows: a row from before #1567,
        # or one restored from a deleted workspace, can still carry it.
        ws.security_scan_enforcement = "enforced"
        run = await run_service.create_run(db, ws, message="governance")
        run.status = "planning"
        run.plan_started_at = datetime.now(UTC)
        await db.commit()
        run = await run_service.complete_plan(db, run, has_changes=True)
        await db.commit()
        evals = await db.scalar(
            select(func.count())
            .select_from(PolicyEvaluation)
            .where(PolicyEvaluation.run_id == run.id)
        )
        scans = await db.scalar(
            select(func.count())
            .select_from(SecurityScanResult)
            .where(SecurityScanResult.run_id == run.id)
        )
        return run.status, evals, scans


class TestAPulumiWorkspaceUnderGovernance:
    async def test_its_apply_is_held_by_policy_exactly_as_terraform_is(self, app, client):
        """The inverse of what this asserted before #1567's second half.

        A mandatory set used to pass a Pulumi run, because nothing could
        evaluate it and holding the apply forever was the worse failure. The
        runner now builds an OPA input from the preview's event log, so a
        missing result is the safety net firing for its real reason -- a runner
        that did not report -- and must hold the run exactly as Terraform's
        does. One evaluation is recorded; the scan is still not reached, and
        would not be recorded for a Pulumi workspace anyway.
        """
        set_auth(app, admin_user())
        await _deny_everything_globally()
        status, body = await _create(client, "proj::governed", "pulumi")
        assert status == 201, body

        run_status, evals, scans = await _plan_finishes(body["data"]["id"])

        assert run_status == "planning"
        assert (evals, scans) == (1, 0)

    async def test_its_apply_is_still_never_held_by_a_scan(self, app, client):
        """The half of #1567 that has NOT changed.

        Checkov and Trivy read Terraform plan JSON; whether they have a
        meaningful Pulumi input at all is #1569. Until then a scan must not be
        what holds a Pulumi apply -- which is the original defect, and is why
        this stays pinned separately now that the policy half has moved.
        """
        set_auth(app, admin_user())
        status, body = await _create(client, "proj::unscanned", "pulumi")
        assert status == 201, body

        _run_status, _evals, scans = await _plan_finishes(body["data"]["id"])

        assert scans == 0

    async def test_the_same_setup_still_holds_a_terraform_apply(self, app, client):
        set_auth(app, admin_user())
        await _deny_everything_globally()
        status, body = await _create(client, "tf-governed", "terraform")
        assert status == 201, body

        run_status, evals, scans = await _plan_finishes(body["data"]["id"])

        # No runner result: the policy gate fails closed and holds the run,
        # before the scan gate is reached.
        assert run_status == "planning"
        assert (evals, scans) == (1, 0)

    async def test_a_scan_cannot_be_turned_on(self, app, client):
        set_auth(app, admin_user())
        status, body = await _create(client, "proj::scanned", "pulumi")
        assert status == 201, body
        assert body["data"]["attributes"]["security-scan-enforcement"] == "off"

        status, body = await _create(
            client, "proj::enforced", "pulumi", **{"security-scan-enforcement": "enforced"}
        )
        assert status == 422, body

        ws_id = (await _create(client, "proj::patched", "pulumi"))[1]["data"]["id"]
        r = await client.patch(
            f"{NATIVE}/{ws_id}",
            json={
                "data": {
                    "type": "workspaces",
                    "attributes": {"security-scan-enforcement": "advisory"},
                }
            },
            headers=AUTH,
        )
        assert r.status_code == 422, r.text
