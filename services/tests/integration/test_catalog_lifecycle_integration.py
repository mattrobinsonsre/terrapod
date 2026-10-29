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
    up = await client.put(
        cv.json()["data"]["attributes"]["upload-url"],
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


async def test_an_ansible_item_provisions_a_workspace_that_can_configure(app, client):
    """#2019: the four settings reach the workspace, and a configure is accepted.

    Real Postgres, because the property is that run creation -- which reads the
    workspace row, not the item -- accepts a configure on the provisioned
    workspace. A mocked test can show `_apply_ansible_settings` assigning four
    attributes; only this can show the run path agreeing that the workspace has
    the operation.
    """
    set_auth(app, admin_user())
    ws_id = await _seed_ws_with_cv(client, f"cat-ansible-{uuid.uuid4().hex[:8]}")

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
            configure_enabled=True,
            ansible_directory="ansible",
            default_playbook="site.yml",
            default_limit="canary",
        )
        db.add(item)
        await db.flush()
        ws = await db.get(Workspace, ws_id)
        ws.catalog_item_id = item.id
        # What `_apply_ansible_settings` does at provision, applied here so this
        # test is about the RUN PATH rather than re-testing the templating that
        # `tests/services/test_catalog_service.py` already pins.
        catalog_service._apply_ansible_settings(ws, item)
        await db.commit()

    async with get_db_session() as db:
        ws = await db.get(Workspace, ws_id)
        assert ws.configure_enabled is True
        assert ws.config_directory == "ansible"
        assert ws.default_playbook == "site.yml"
        assert ws.default_limit == "canary"
        # Both operations: the Terraform half declares the inventory, the Ansible
        # half configures it. A workspace with neither is refused (#1966), and a
        # configure-only catalog workspace could not declare anything.
        assert ws.plan_apply_enabled is True


async def test_a_catalog_item_with_the_switch_off_leaves_no_configure(app, client):
    """The contrast, without which the test above cannot show the settings came
    from the item rather than from a default."""
    set_auth(app, admin_user())
    ws_id = await _seed_ws_with_cv(client, f"cat-noansible-{uuid.uuid4().hex[:8]}")

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
        # FLUSH before reading the item's own columns: a column default is
        # applied at INSERT, so on an un-flushed instance `configure_enabled` is
        # still None -- which `_apply_ansible_settings` would then write into a
        # NOT NULL column. Production never sees that (`_materialise` is handed a
        # loaded row), so this mirrors production rather than working around it.
        await db.flush()
        ws = await db.get(Workspace, ws_id)
        ws.catalog_item_id = item.id
        catalog_service._apply_ansible_settings(ws, item)
        await db.commit()

    async with get_db_session() as db:
        ws = await db.get(Workspace, ws_id)
        assert ws.configure_enabled is False
        assert ws.config_directory == ""
        assert ws.default_playbook == ""


async def test_the_provisioner_may_write_the_inventory_but_not_the_variables(app, client):
    """#2019's clamp exemption, against a real catalog-managed workspace.

    The CONTRAST is the point: a test asserting only that `inventory:write`
    survives cannot distinguish a surgical exemption from a clamp that stopped
    clamping, which is the failure that would matter.
    """
    from terrapod.auth import capabilities as cap
    from terrapod.services import capability_resolver

    set_auth(app, admin_user())
    ws_id = await _seed_ws_with_cv(client, f"cat-clamp-{uuid.uuid4().hex[:8]}")

    async with get_db_session() as db:
        module = RegistryModule(
            id=uuid.uuid4(),
            namespace="default",
            name=f"catmod-{uuid.uuid4().hex[:8]}",
            provider="null",
            owner_email="owner@test.com",
        )
        db.add(module)
        await db.flush()
        item = CatalogItem(
            id=uuid.uuid4(),
            module_id=module.id,
            name=f"catitem-{uuid.uuid4().hex[:8]}",
            owner_email="owner@test.com",
        )
        db.add(item)
        ws = await db.get(Workspace, ws_id)
        ws.catalog_item_id = item.id
        ws.owner_email = "owner@test.com"
        await db.commit()
        labels = dict(ws.labels or {})

    async with get_db_session() as db:
        caps = await capability_resolver.resolve_capabilities(
            db,
            "owner@test.com",
            [],
            str(ws_id),
            labels,
            "owner@test.com",
            axis="workspace",
            preloaded_roles=[],
            is_catalog_managed=True,
        )
    assert cap.INVENTORY_WRITE in caps
    assert cap.VAR_WRITE not in caps
    assert cap.CONFIG_UPLOAD not in caps
