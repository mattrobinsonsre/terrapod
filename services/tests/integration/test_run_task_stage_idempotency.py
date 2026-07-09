"""
Integration test — task-stage creation is idempotent per (run, stage) (#739).

Regression guard for the bug where `run_task_service.create_task_stage` was
called on every reconciler tick by `run_service.complete_plan` while a run sat
in `planning`, and each call unconditionally inserted a *new* `TaskStage` with
a fresh, still-`running` webhook. That wedged the run in `planning` forever and
accumulated one dead stage per tick (observed live: 98 duplicate `post_plan`
stages, run never reaching `planned`). The fix makes creation idempotent: a run
has exactly one stage per boundary, so a second call returns the existing row.

These need a real database (unique rows, FK to `runs`, row counts), so they
live in the integration tier.
"""

import uuid

import pytest
from sqlalchemy import func, select

from terrapod.db.models import TaskStage, TaskStageResult
from terrapod.db.session import get_db_session
from terrapod.services import run_task_service
from tests.integration.conftest import AUTH, admin_user, set_auth

pytestmark = pytest.mark.integration

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"
RUNS_ENDPOINT = "/api/v2/runs"


async def _upload_cv(client, ws_id: str) -> None:
    resp = await client.post(
        f"/api/v2/workspaces/{ws_id}/configuration-versions",
        json={"data": {"type": "configuration-versions", "attributes": {"auto-queue-runs": False}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    cv_id = resp.json()["data"]["id"]
    resp = await client.put(
        f"/api/v2/configuration-versions/{cv_id}/upload",
        content=b"placeholder-tarball-for-tests",
        headers={"Content-Type": "application/x-tar"},
    )
    assert resp.status_code in (200, 204), resp.text


async def _create_workspace(client, name: str) -> str:
    resp = await client.post(
        WS_ENDPOINT,
        json={"data": {"type": "workspaces", "attributes": {"name": name}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    ws_id = resp.json()["data"]["id"]
    await _upload_cv(client, ws_id)
    return ws_id


async def _create_run(client, ws_id: str) -> str:
    resp = await client.post(
        RUNS_ENDPOINT,
        json={
            "data": {
                "type": "runs",
                "attributes": {},
                "relationships": {
                    "workspace": {"data": {"id": ws_id, "type": "workspaces"}},
                },
            }
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]["id"]


async def _create_post_plan_task(client, ws_id: str, name: str) -> None:
    resp = await client.post(
        f"/api/terrapod/v1/workspaces/{ws_id}/run-tasks",
        json={
            "data": {
                "type": "run-tasks",
                "attributes": {
                    "name": name,
                    # Deliberately unreachable — mirrors the misconfig that
                    # first surfaced the bug (an advisory task whose webhook
                    # never resolves within one reconciler tick).
                    "url": "https://cost-api.example.invalid/validate",
                    "stage": "post_plan",
                    "enforcement-level": "advisory",
                    "enabled": True,
                },
            }
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text


def _rid(run_id_str: str) -> uuid.UUID:
    return uuid.UUID(run_id_str.removeprefix("run-"))


def _wid(ws_id_str: str) -> uuid.UUID:
    return uuid.UUID(ws_id_str.removeprefix("ws-"))


class TestTaskStageIdempotency:
    async def test_second_create_returns_same_stage_no_duplicate(self, app, client):
        """The gate is re-driven every reconciler tick; creation must reuse the
        existing stage rather than accumulate duplicates (#739)."""
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, f"ts-idem-{uuid.uuid4().hex[:8]}")
        await _create_post_plan_task(client, ws_id, "opa-cost-check")
        run_id = await _create_run(client, ws_id)

        rid, wid = _rid(run_id), _wid(ws_id)

        async with get_db_session() as db:
            first = await run_task_service.create_task_stage(db, rid, wid, "post_plan")
            assert first is not None
            await db.commit()
            first_id = first.id

        # Simulate many subsequent reconciler ticks re-driving the gate.
        for _ in range(5):
            async with get_db_session() as db:
                again = await run_task_service.create_task_stage(db, rid, wid, "post_plan")
                assert again is not None
                assert again.id == first_id, "create_task_stage must reuse the existing stage"
                await db.commit()

        # Exactly one post_plan stage exists for this run — no accumulation.
        async with get_db_session() as db:
            count = await db.scalar(
                select(func.count())
                .select_from(TaskStage)
                .where(TaskStage.run_id == rid, TaskStage.stage == "post_plan")
            )
            assert count == 1

    async def test_no_applicable_tasks_returns_none(self, app, client):
        """Workspaces without a matching run task get no stage (caller proceeds)."""
        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, f"ts-none-{uuid.uuid4().hex[:8]}")
        run_id = await _create_run(client, ws_id)

        async with get_db_session() as db:
            ts = await run_task_service.create_task_stage(
                db, _rid(run_id), _wid(ws_id), "post_plan"
            )
            assert ts is None

    async def test_delivery_trigger_enqueued_after_result_committed(self, app, client):
        """The result row must be committed and visible to the (separate-session)
        delivery consumer *before* its trigger is enqueued (#739).

        Enqueuing while the row is only flushed-not-committed races the
        consumer, which then reads "task stage result not found" and silently
        drops the webhook — wedging the run in `planning`. We assert the fix by
        patching the enqueue and, from a *fresh* session, confirming the
        TaskStageResult is already visible at enqueue time.
        """
        import terrapod.services.scheduler as scheduler_mod

        set_auth(app, admin_user())
        ws_id = await _create_workspace(client, f"ts-commit-{uuid.uuid4().hex[:8]}")
        await _create_post_plan_task(client, ws_id, "opa-cost-check")
        run_id = await _create_run(client, ws_id)

        visibility: list[bool] = []
        orig = scheduler_mod.enqueue_trigger

        async def _spy_enqueue(trigger_type, payload, **kwargs):
            # A brand-new session — sees only committed data.
            async with get_db_session() as fresh:
                tsr_uuid = uuid.UUID(payload["task_stage_result_id"])
                row = await fresh.get(TaskStageResult, tsr_uuid)
                visibility.append(row is not None)
            # Don't actually enqueue (no consumer running in this test).

        scheduler_mod.enqueue_trigger = _spy_enqueue
        try:
            async with get_db_session() as db:
                ts = await run_task_service.create_task_stage(
                    db, _rid(run_id), _wid(ws_id), "post_plan"
                )
                assert ts is not None
        finally:
            scheduler_mod.enqueue_trigger = orig

        assert visibility, "expected at least one delivery trigger to be enqueued"
        assert all(visibility), (
            "result row must be committed/visible before its trigger is enqueued"
        )
