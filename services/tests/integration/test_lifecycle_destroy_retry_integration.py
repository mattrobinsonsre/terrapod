"""Integration test (real Postgres) for the lifecycle-destroy auto-retry.

The services-tier tests mock the DB, so the actual SQL — the source filter, the
`updated_at < cutoff` backoff predicate, and the latest-run ordering — is never
exercised against a real engine. This closes that gap end-to-end.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from terrapod.db.models import Run, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import lifecycle_destroy_retry as mod
from terrapod.services import run_service
from tests.integration.conftest import AUTH, admin_user, set_auth

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"

pytestmark = pytest.mark.asyncio


def _cfg(retries=2, backoff=45):
    return SimpleNamespace(
        lifecycle_destroy_retries=retries,
        lifecycle_destroy_retry_backoff_seconds=backoff,
    )


async def _seed_ws_with_cv(client, name: str) -> uuid.UUID:
    resp = await client.post(
        WS_ENDPOINT,
        json={"data": {"type": "workspaces", "attributes": {"name": name}}},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    ws_id = resp.json()["data"]["id"].removeprefix("ws-")
    cv = await client.post(
        f"/api/v2/workspaces/{ws_id}/configuration-versions",
        json={"data": {"type": "configuration-versions", "attributes": {"auto-queue-runs": False}}},
        headers=AUTH,
    )
    assert cv.status_code == 201, cv.text
    cv_id = cv.json()["data"]["id"]
    up = await client.put(
        f"/api/v2/configuration-versions/{cv_id}/upload",
        content=b"placeholder",
        headers={"Content-Type": "application/x-tar"},
    )
    assert up.status_code in (200, 204), up.text
    return uuid.UUID(ws_id)


async def _seed_errored_destroy(ws_id: uuid.UUID, *, age_seconds: int, source: str) -> None:
    async with get_db_session() as db:
        ws = await db.get(Workspace, ws_id)
        cv = await run_service.get_latest_uploaded_cv(db, ws_id)
        run = await run_service.create_run(
            db,
            workspace=ws,
            is_destroy=True,
            auto_apply=True,
            plan_only=False,
            source=source,
            configuration_version_id=cv.id,
        )
        run.status = "errored"
        run.updated_at = datetime.now(UTC) - timedelta(seconds=age_seconds)
        await db.commit()


async def _runs(ws_id: uuid.UUID) -> list[Run]:
    async with get_db_session() as db:
        result = await db.execute(select(Run).where(Run.workspace_id == ws_id))
        return list(result.scalars().all())


async def test_retries_old_errored_lifecycle_destroy(app, client):
    set_auth(app, admin_user())
    ws_id = await _seed_ws_with_cv(client, "retry-eligible")
    await _seed_errored_destroy(ws_id, age_seconds=120, source="catalog-lifecycle")

    with patch.object(mod, "load_runner_config", return_value=_cfg(retries=2, backoff=45)):
        await mod.lifecycle_destroy_retry_cycle()

    runs = await _runs(ws_id)
    assert len(runs) == 2  # original errored + one retry
    retry = [r for r in runs if r.status != "errored"]
    assert len(retry) == 1
    assert retry[0].is_destroy and retry[0].source == "catalog-lifecycle"


async def test_no_retry_within_backoff(app, client):
    set_auth(app, admin_user())
    ws_id = await _seed_ws_with_cv(client, "retry-too-recent")
    await _seed_errored_destroy(ws_id, age_seconds=5, source="catalog-lifecycle")

    with patch.object(mod, "load_runner_config", return_value=_cfg(retries=2, backoff=45)):
        await mod.lifecycle_destroy_retry_cycle()

    # updated_at is within the backoff window → not yet eligible.
    assert len(await _runs(ws_id)) == 1


async def test_no_retry_for_user_cli_destroy(app, client):
    set_auth(app, admin_user())
    ws_id = await _seed_ws_with_cv(client, "retry-user-destroy")
    await _seed_errored_destroy(ws_id, age_seconds=120, source="tfe-api")

    with patch.object(mod, "load_runner_config", return_value=_cfg(retries=2, backoff=45)):
        await mod.lifecycle_destroy_retry_cycle()

    # A user's own CLI destroy is never auto-retried (source filter excludes it).
    assert len(await _runs(ws_id)) == 1
