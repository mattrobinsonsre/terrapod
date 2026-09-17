"""Integration: post-plan decisions in the Terraform Enterprise vocabulary (#1704).

Real Postgres for what mocks cannot prove: a run's post-plan task stage exists
from the moment the run is created (the CLI reads stages once, right then), it
is started when the plan finishes and cancelled if the run ends first, and a
held run's document and policy checks come out of the real rows.

Each of these was found or confirmed by driving `tofu apply` against a live
stack; the tests pin the server half.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from terrapod.config import settings
from terrapod.db.models import Run, TaskStage, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import run_service, run_task_service, security_scan_service
from tests.integration.conftest import AUTH, admin_user, set_auth
from tests.integration.test_run_state_machine import _create_run, _create_workspace

pytestmark = pytest.mark.integration

TFE = {"X-Terrapod-Post-Plan-Decisions": "tfe", **AUTH}


@pytest.fixture
def tfe_vocabulary():
    with patch.object(settings.runs, "tfe_post_plan_decisions", True):
        yield


async def _add_post_plan_task(client, ws_id: str) -> None:
    resp = await client.post(
        f"/api/v1/workspaces/{ws_id}/run-tasks",
        json={
            "data": {
                "type": "run-tasks",
                "attributes": {
                    "name": "change-board",
                    "url": "https://change-board.example.invalid/hook",
                    "stage": "post_plan",
                    "enforcement-level": "mandatory",
                    "enabled": True,
                },
            }
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text


async def _stages(run_uuid: uuid.UUID) -> list[TaskStage]:
    async with get_db_session() as db:
        return list(
            (await db.execute(select(TaskStage).where(TaskStage.run_id == run_uuid)))
            .scalars()
            .all()
        )


def _uuid(run_id: str) -> uuid.UUID:
    return uuid.UUID(run_id.removeprefix("run-"))


class TestTheStageExistsFromTheStart:
    async def test_a_new_run_has_its_post_plan_stage_reserved(self, app, client, tfe_vocabulary):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "ppd-reserve")
        await _add_post_plan_task(client, ws_id)

        run = await _create_run(client, ws_id)

        stages = await _stages(_uuid(run["id"]))
        assert [(s.stage, s.status) for s in stages] == [("post_plan", "pending")]
        resp = await client.get(f"/api/v2/runs/{run['id']}", headers=TFE)
        ids = [d["id"] for d in resp.json()["data"]["relationships"]["task-stages"]["data"]]
        assert ids == [f"ts-{stages[0].id}"]

    async def test_1_x_reserves_nothing(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "ppd-no-reserve")
        await _add_post_plan_task(client, ws_id)

        run = await _create_run(client, ws_id)

        assert await _stages(_uuid(run["id"])) == []

    async def test_the_plan_finishing_starts_the_reserved_stage(self, app, client, tfe_vocabulary):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "ppd-start")
        await _add_post_plan_task(client, ws_id)
        run = await _create_run(client, ws_id)

        with patch("terrapod.services.scheduler.enqueue_trigger", AsyncMock()) as enqueue:
            async with get_db_session() as db:
                row = (await db.execute(select(Run).where(Run.id == _uuid(run["id"])))).scalar_one()
                stage = await run_task_service.create_task_stage(
                    db, row.id, row.workspace_id, "post_plan"
                )
                await db.commit()

        assert stage.status == "running"
        # The reserved stage is started, not joined by a second one.
        assert len(await _stages(_uuid(run["id"]))) == 1
        enqueue.assert_awaited_once()

    async def test_a_run_that_ends_first_cancels_the_stage(self, app, client, tfe_vocabulary):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "ppd-cancel")
        await _add_post_plan_task(client, ws_id)
        run = await _create_run(client, ws_id)

        resp = await client.post(f"/api/v2/runs/{run['id']}/actions/cancel", headers=AUTH)
        assert resp.status_code in (200, 202), resp.text

        # Left `pending`, the CLI would poll the stage forever.
        assert [s.status for s in await _stages(_uuid(run["id"]))] == ["canceled"]


async def _hold_at_scan_gate(run_id: str) -> None:
    rid = _uuid(run_id)
    async with get_db_session() as db, db.begin():
        run = (await db.execute(select(Run).where(Run.id == rid))).scalar_one()
        ws = await db.get(Workspace, run.workspace_id)
        ws.security_scan_enforcement = "enforced"
        run.status = "planning"
        run.plan_started_at = datetime.now(UTC)
        run.plan_finished_at = datetime.now(UTC)
        run.has_changes = True
        await security_scan_service.record_scan_result(
            db,
            run_id=rid,
            engine="checkov",
            enforcement_level="enforced",
            severity_threshold="high",
            outcome="failed",
            findings=[{"rule_id": "CKV_AWS_24", "severity": "high", "title": "Open SSH"}],
            summary={"blocking": 1, "total": 1},
        )


class TestAHeldRunInTheTfeVocabulary:
    async def test_reports_policy_override_and_its_check(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "ppd-held")
        run = await _create_run(client, ws_id)
        await _hold_at_scan_gate(run["id"])

        doc = (await client.get(f"/api/v2/runs/{run['id']}", headers=TFE)).json()["data"]
        assert doc["attributes"]["status"] == "policy_override"
        assert doc["attributes"]["blocked-by"] == "security-scan"
        check_id = f"polchk-scan-{_uuid(run['id'])}"
        assert doc["relationships"]["policy-checks"]["data"] == [
            {"id": check_id, "type": "policy-checks"}
        ]

        legacy = (
            await client.get(
                f"/api/v2/runs/{run['id']}",
                headers={"X-Terrapod-Post-Plan-Decisions": "legacy", **AUTH},
            )
        ).json()["data"]
        assert legacy["attributes"]["status"] == "planning"
        assert "data" not in legacy["relationships"]["policy-checks"]

        check = (await client.get(f"/api/v2/policy-checks/{check_id}", headers=AUTH)).json()
        assert check["data"]["attributes"]["status"] == "soft_failed"
        output = await client.get(f"/api/v2/policy-checks/{check_id}/output", headers=AUTH)
        assert "CKV_AWS_24" in output.text

    async def test_overriding_the_check_moves_the_run_on(self, app, client):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "ppd-override")
        run = await _create_run(client, ws_id)
        await _hold_at_scan_gate(run["id"])
        check_id = f"polchk-scan-{_uuid(run['id'])}"

        resp = await client.post(f"/api/v2/policy-checks/{check_id}/actions/override", headers=AUTH)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["status"] == "overridden"

        doc = (await client.get(f"/api/v2/runs/{run['id']}", headers=TFE)).json()["data"]
        # Confirmable straight away: the CLI reads the run just after overriding.
        assert doc["attributes"]["status"] == "planned"
        assert doc["attributes"]["blocked-by"] is None

        again = await client.post(
            f"/api/v2/policy-checks/{check_id}/actions/override", headers=AUTH
        )
        assert again.status_code == 409

    async def test_a_failed_mandatory_task_holds_the_run(self, app, client, tfe_vocabulary):
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, "ppd-task-held")
        await _add_post_plan_task(client, ws_id)
        run = await _create_run(client, ws_id)
        rid = _uuid(run["id"])

        async with get_db_session() as db:
            row = (await db.execute(select(Run).where(Run.id == rid))).scalar_one()
            row.status = "planning"
            row.plan_started_at = datetime.now(UTC)
            await db.commit()
            with (
                patch.object(run_task_service, "_start_reserved_stage", AsyncMock()),
                patch.object(run_task_service, "resolve_stage", AsyncMock(return_value="failed")),
            ):
                await run_service.complete_plan(db, row, has_changes=True)
            stage = (await _stages(rid))[0]
            stage_row = await db.get(TaskStage, stage.id)
            stage_row.status = "failed"
            await db.commit()

        doc = (await client.get(f"/api/v2/runs/{run['id']}", headers=TFE)).json()["data"]
        assert doc["attributes"]["status"] == "post_plan_awaiting_decision"
        assert doc["attributes"]["actions"]["is-discardable"] is True

        included = (
            await client.get(f"/api/v2/runs/{run['id']}?include=task_stages", headers=TFE)
        ).json()["included"]
        assert [(r["type"], r["attributes"]["status"]) for r in included] == [
            ("task-stages", "awaiting_override"),
            ("task-results", "pending"),
        ]
