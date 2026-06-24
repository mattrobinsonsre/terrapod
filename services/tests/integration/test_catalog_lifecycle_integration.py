"""Integration tests (real Postgres) for catalog lifecycle DB semantics that
mocked services-tier tests structurally cannot prove:

* S1 — a successful ``catalog-lifecycle`` destroy archives the workspace via the
  ``run_service.transition_run`` hook (the highest-blast-radius transition: it's
  what reclaims the instance record). Live-proven during the v0.42.0 F-gate;
  this pins it in CI.
* S4 — ``catalog_service.list_instances(..., active_only=True)`` excludes
  ``archived`` instances, so a destroyed-and-reclaimed instance no longer blocks
  deleting its catalog item.
"""

import uuid

import pytest

from terrapod.db.models import CatalogItem, RegistryModule, Workspace
from terrapod.db.session import get_db_session
from terrapod.services import catalog_service, run_service
from tests.integration.conftest import AUTH, admin_user, set_auth

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"

pytestmark = pytest.mark.asyncio


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


async def test_catalog_lifecycle_destroy_archives_workspace(app, client):
    """S1: target 'applied' on an is_destroy, non-plan_only, catalog-lifecycle run
    flips the workspace to lifecycle_state='archived'."""
    set_auth(app, admin_user())
    ws_id = await _seed_ws_with_cv(client, f"cat-archive-{uuid.uuid4().hex[:8]}")

    async with get_db_session() as db:
        ws = await db.get(Workspace, ws_id)
        cv = await run_service.get_latest_uploaded_cv(db, ws_id)
        run = await run_service.create_run(
            db,
            workspace=ws,
            is_destroy=True,
            auto_apply=True,
            plan_only=False,
            source="catalog-lifecycle",
            configuration_version_id=cv.id,
        )
        # Walk it to the apply boundary, then cross into 'applied'.
        run.status = "applying"
        await db.commit()
        await run_service.transition_run(db, run, "applied")
        await db.commit()

    async with get_db_session() as db:
        ws = await db.get(Workspace, ws_id)
        assert ws.lifecycle_state == "archived"
        assert "catalog" in (ws.lifecycle_reason or "")


async def test_list_instances_active_only_excludes_archived(app, client):
    """S4: an archived (destroyed) instance is excluded by active_only, so it no
    longer permanently blocks deleting its catalog item."""
    set_auth(app, admin_user())
    ws_id = await _seed_ws_with_cv(client, f"cat-active-{uuid.uuid4().hex[:8]}")

    async with get_db_session() as db:
        module = RegistryModule(
            id=uuid.uuid4(),
            namespace="default",
            name=f"catmod-{uuid.uuid4().hex[:8]}",
            provider="null",
            owner_email="admin@test.com",
        )
        db.add(module)
        await db.flush()
        item = CatalogItem(
            id=uuid.uuid4(),
            module_id=module.id,
            name=f"catitem-{uuid.uuid4().hex[:8]}",
            owner_email="admin@test.com",
        )
        db.add(item)
        ws = await db.get(Workspace, ws_id)
        ws.catalog_item_id = item.id
        await db.commit()
        item_id = item.id

    # Active instance is listed by both.
    async with get_db_session() as db:
        assert len(await catalog_service.list_instances(db, item_id)) == 1
        assert len(await catalog_service.list_instances(db, item_id, active_only=True)) == 1

    # Archive it (as a successful destroy would) → excluded by active_only.
    async with get_db_session() as db:
        ws = await db.get(Workspace, ws_id)
        ws.lifecycle_state = "archived"
        await db.commit()

    async with get_db_session() as db:
        assert len(await catalog_service.list_instances(db, item_id)) == 1
        assert await catalog_service.list_instances(db, item_id, active_only=True) == []
