"""Integration: the plan-JSON upload against real Postgres.

The resource list needs the workspace's sensitive variables, so the upload runs a
database lookup before it commits. A lookup that fails at the database level
leaves the session needing a rollback, which a mock cannot reproduce; only a real
engine shows whether the upload survives it.
"""

import json
from unittest.mock import patch

import pytest
from sqlalchemy import select

from terrapod.auth.runner_tokens import generate_runner_token
from terrapod.db.models import AgentPool, ConfigurationVersion, Run, TaskStage, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import pool_set, run_service, variable_service
from tests.integration.conftest import admin_user, set_auth

pytestmark = pytest.mark.integration

PLAN = {
    "format_version": "1.2",
    "resource_changes": [
        {
            "address": "null_resource.a",
            "type": "null_resource",
            "change": {"actions": ["create"], "before": None, "after": {"id": "x"}},
        },
        {
            "address": "null_resource.b",
            "type": "null_resource",
            "change": {"actions": ["delete"], "before": {"id": "y"}, "after": None},
        },
    ],
}


async def _seed_run():
    """A run a pull request can reference, so the upload resolves variables."""
    async with get_db_session() as db:
        pool = AgentPool(name="plan-json-poison-pool")
        db.add(pool)
        await db.flush()
        ws = Workspace(name="plan-json-poison", execution_mode="agent")
        pool_set.set_workspace_pools(ws, [pool.id])
        db.add(ws)
        await db.flush()
        cv = ConfigurationVersion(workspace_id=ws.id, status="uploaded", source="tfe-api")
        db.add(cv)
        await db.flush()
        await db.commit()
        ws = (await db.execute(select(Workspace).where(Workspace.id == ws.id))).scalar_one()
        run = await run_service.create_run(db, ws, configuration_version_id=cv.id)
        run.vcs_pull_request_number = 7
        run.vcs_commit_sha = "a" * 40
        run_id = run.id
        await db.commit()
        return run_id


async def _reload(run_id) -> Run:
    async with get_db_session() as db:
        return (await db.execute(select(Run).where(Run.id == run_id))).scalar_one()


class TestAFailedVariableLookupCannotLoseTheUpload:
    async def test_a_poisoned_lookup_still_commits_the_counts(self, app, client):
        set_auth(app, admin_user())
        run_id = await _seed_run()
        token = generate_runner_token(run_id, phase="plan")

        # A failed FLUSH is what leaves the session needing a rollback; raising
        # after a successful query does not, so it would prove nothing.
        async def _poison(db, workspace_id):
            db.add(TaskStage(run_id=None, stage="pre_plan", status="running"))
            await db.flush()  # NOT NULL violation -> session needs rollback

        with patch.object(variable_service, "resolve_variables", _poison):
            resp = await client.put(
                f"/api/terrapod/v1/runs/{run_id}/artifacts/plan-json-output",
                content=json.dumps(PLAN).encode(),
                headers={"Authorization": f"Bearer {token}"},
            )

        assert resp.status_code == 204, resp.text
        stored = await _reload(run_id)
        assert stored.has_json_output is True
        assert (stored.resource_additions, stored.resource_destructions) == (1, 1)
        assert stored.plan_resources is None
