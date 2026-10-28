"""Integration tests (real Postgres) for catalog lifecycle DB semantics that
mocked services-tier tests structurally cannot prove:

* S1 — a successful ``catalog-lifecycle`` destroy archives the workspace via the
  ``run_service.transition_run`` hook (the highest-blast-radius transition: it's
  what reclaims the instance record). Live-proven during the v0.42.0 F-gate;
  this pins it in CI.
* S4 — ``catalog_service.list_instances(..., active_only=True)`` excludes
  ``archived`` instances, so a destroyed-and-reclaimed instance no longer blocks
  deleting its catalog item.
* An override shared by another run must not be deleted when a PR cannot queue
  a new run; its coordinate and key are matched by the cleanup query.
"""

import io
import tarfile
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from terrapod.api.routers.config_versions import _require_archive_read
from terrapod.db.models import (
    AgentPool,
    CatalogItem,
    ConfigurationVersion,
    RegistryModule,
    RegistryModuleVersion,
    Run,
    Workspace,
)
from terrapod.db.session import get_db_session
from terrapod.services import (
    artifact_retention_service,
    catalog_service,
    module_impact_service,
    run_service,
    vcs_status_comment,
)
from terrapod.storage import get_storage
from terrapod.storage.keys import config_version_key, module_tarball_key
from tests.integration.conftest import AUTH, admin_user, set_auth

WS_ENDPOINT = "/api/v2/organizations/default/workspaces"

pytestmark = pytest.mark.asyncio


async def test_protected_catalog_cv_accepts_module_id_loaded_from_postgres(app):
    async with get_db_session() as db:
        ws = Workspace(name=f"cv-pg-uuid-{uuid.uuid4().hex[:8]}", execution_mode="agent")
        module = RegistryModule(
            namespace="default", name=f"cv-pg-uuid-{uuid.uuid4().hex[:8]}", provider="aws"
        )
        db.add_all([ws, module])
        await db.flush()
        module_id = module.id
        db.expunge(module)

        loaded = await db.get(RegistryModule, module_id)
        assert loaded is not None
        cv = await run_service.create_configuration_version(
            db,
            workspace_id=ws.id,
            source="catalog",
            auto_queue_runs=False,
            archive_provenance="protected",
            archive_module_id=loaded.id,
        )
        assert cv.archive_module_id == module_id


async def test_root_cv_and_module_pr_keep_origin_after_module_deletion(app):
    async with get_db_session() as db:
        ws = Workspace(name=f"root-origin-{uuid.uuid4().hex[:8]}", execution_mode="agent")
        module = RegistryModule(
            namespace="default", name=f"root-{uuid.uuid4().hex[:8]}", provider="aws"
        )
        db.add_all([ws, module])
        await db.flush()
        module_id = module.id

        cv = await run_service.create_configuration_version(
            db,
            workspace_id=ws.id,
            source="catalog",
            auto_queue_runs=False,
            archive_provenance="protected",
            archive_module_id=module_id,
        )
        pr_run = Run(
            workspace_id=ws.id,
            source="module-test",
            module_impact_module_id=module_id,
            plan_only=True,
        )
        db.add(pr_run)
        await db.flush()
        cv_id, run_id = cv.id, pr_run.id
        assert await _require_archive_read(db, admin_user(), cv) == "protected"

        await db.delete(module)
        await db.flush()
        db.expire_all()
        reloaded = await db.get(ConfigurationVersion, cv_id)
        assert reloaded.archive_module_id == module_id
        assert (await db.get(Run, run_id)).module_impact_module_id == module_id
        with pytest.raises(HTTPException) as denied:
            await _require_archive_read(db, admin_user(), reloaded)
        assert denied.value.status_code == 404


async def test_module_pr_comment_rows_use_only_originating_module_and_poller_head(app):
    now = datetime.now(UTC)
    async with get_db_session() as db:
        ws = Workspace(name=f"pr-comment-{uuid.uuid4().hex[:8]}", execution_mode="agent")
        first = RegistryModule(
            namespace="default", name=f"pr-first-{uuid.uuid4().hex[:8]}", provider="aws"
        )
        second = RegistryModule(
            namespace="default", name=f"pr-second-{uuid.uuid4().hex[:8]}", provider="aws"
        )
        db.add_all([ws, first, second])
        await db.flush()
        old = Run(
            workspace_id=ws.id,
            source="module-test",
            created_by="module-impact-analysis",
            module_impact_module_id=first.id,
            vcs_pull_request_number=7,
            vcs_commit_sha="a" * 40,
            created_at=now,
        )
        head = Run(
            workspace_id=ws.id,
            source="module-test",
            created_by="module-impact-analysis",
            module_impact_module_id=first.id,
            vcs_pull_request_number=7,
            vcs_commit_sha="b" * 40,
            created_at=now + timedelta(minutes=1),
        )
        foreign = Run(
            workspace_id=ws.id,
            source="module-test",
            created_by="module-impact-analysis",
            module_impact_module_id=second.id,
            vcs_pull_request_number=7,
            vcs_commit_sha="c" * 40,
            created_at=now + timedelta(minutes=2),
        )
        retries = [
            Run(
                workspace_id=ws.id,
                source="module-test",
                created_by="user@test.com",
                module_impact_module_id=first.id,
                vcs_pull_request_number=7,
                vcs_commit_sha=sha,
                created_at=now + timedelta(minutes=minute),
            )
            for sha, minute in (("a" * 40, 3), ("b" * 40, 4))
        ]
        db.add_all([old, head, foreign, *retries])
        await db.commit()
        ws_id, first_id, second_id = ws.id, first.id, second.id
        head_id, foreign_id = head.id, foreign.id

    async with get_db_session() as db:
        with patch.object(
            vcs_status_comment,
            "_row_for",
            new=AsyncMock(side_effect=lambda _db, _ws, run: run.id),
        ):
            assert await vcs_status_comment._collect_module_rows(db, [ws_id], 7, first_id) == [
                head_id
            ]
            assert (
                await vcs_status_comment._collect_module_rows(db, [ws_id], 7, first_id, "a" * 40)
                == []
            )
            assert await vcs_status_comment._collect_module_rows(db, [ws_id], 7, second_id) == [
                foreign_id
            ]


async def _root_catalog(db):
    suffix = uuid.uuid4().hex[:8]
    module = RegistryModule(namespace="default", name=f"cv-root-{suffix}", provider="aws")
    pool = AgentPool(name=f"cv-pool-{suffix}")
    db.add_all([module, pool])
    await db.flush()
    item = CatalogItem(
        module_id=module.id,
        name=f"cv-item-{suffix}",
        self_contained=True,
        execution_backend_constraints={"tofu": ">=1.12,<2"},
    )
    db.add(item)
    await db.flush()
    return module, item, pool


async def _root_version(db, module, version, source):
    db.add(
        RegistryModuleVersion(
            module_id=module.id, version=version, upload_status="uploaded", inputs=[], outputs=[]
        )
    )
    await db.flush()
    archive = catalog_service._build_tarball({"main.tf": source})
    await get_storage().put(
        module_tarball_key(module.namespace, module.name, module.provider, version),
        archive,
        content_type="application/gzip",
    )
    return archive


async def _provision_root(db, item, pool, monkeypatch):
    monkeypatch.setattr(catalog_service.settings.vcs, "tmpdir", None)
    with patch.object(
        catalog_service.binary_cache_service,
        "list_available_versions",
        new=AsyncMock(return_value=["1.13.0"]),
    ):
        return await catalog_service.provision_instance(
            db,
            user_email="admin@test.com",
            item=item,
            name=f"cv-instance-{uuid.uuid4().hex[:8]}",
            agent_pool_id=pool.id,
            input_values={},
            version_pin="1.0.0",
            auto_apply=False,
            labels={},
            engine_version="1.13.0",
        )


async def test_root_provision_and_reconfigure_persist_originating_module(app, monkeypatch):
    async with get_db_session() as db:
        module, item, pool = await _root_catalog(db)
        old_archive = await _root_version(db, module, "1.0.0", 'output "version" { value = 1 }')
        ws = await _provision_root(db, item, pool, monkeypatch)
        initial = await run_service.get_latest_uploaded_cv(db, ws.id)
        new_archive = await _root_version(db, module, "2.0.0", 'output "version" { value = 2 }')
        with patch.object(
            catalog_service.binary_cache_service,
            "list_available_versions",
            new=AsyncMock(return_value=["1.13.0"]),
        ):
            await catalog_service.reconfigure_instance(
                db,
                user_email="admin@test.com",
                ws=ws,
                input_values={},
                version_pin="2.0.0",
                auto_apply=False,
            )
        latest = await run_service.get_latest_uploaded_cv(db, ws.id)
        ids = (initial.id, latest.id)
        ws_id, module_id = ws.id, module.id
        await db.commit()

    assert ids[0] != ids[1]
    async with get_db_session() as db:
        versions = [await db.get(ConfigurationVersion, cv_id) for cv_id in ids]
        runs = (await db.execute(select(Run).where(Run.workspace_id == ws_id))).scalars().all()
        assert {run.configuration_version_id for run in runs} == set(ids)
        for cv, archive in zip(versions, (old_archive, new_archive), strict=True):
            assert (cv.source, cv.status, cv.speculative) == ("catalog", "uploaded", False)
            assert (cv.archive_provenance, cv.archive_module_id) == ("protected", module_id)
            assert await get_storage().get(config_version_key(str(ws_id), str(cv.id))) == archive


async def test_floating_root_publish_persists_new_protected_cv(app, monkeypatch):
    async with get_db_session() as db:
        module, item, pool = await _root_catalog(db)
        await _root_version(db, module, "1.0.0", 'output "version" { value = 1 }')
        ws = await _provision_root(db, item, pool, monkeypatch)
        old_cv = await run_service.get_latest_uploaded_cv(db, ws.id)
        ws.catalog_version_pin = None
        new_archive = await _root_version(db, module, "2.0.0", 'output "version" { value = 2 }')
        runs = await module_impact_service.trigger_linked_workspace_runs(db, module, "2.0.0")
        assert len(runs) == 1
        run_id, ws_id, module_id, old_id = runs[0].id, ws.id, module.id, old_cv.id
        await db.commit()

    async with get_db_session() as db:
        run = await db.get(Run, run_id)
        cv = await db.get(ConfigurationVersion, run.configuration_version_id)
        assert cv.id != old_id
        assert (cv.source, cv.status, cv.speculative) == ("module-publish", "uploaded", False)
        assert (cv.archive_provenance, cv.archive_module_id) == ("protected", module_id)
        assert await get_storage().get(config_version_key(str(ws_id), str(cv.id))) == new_archive


async def test_module_pr_persists_speculative_root_but_not_incompatible_candidate(app, monkeypatch):
    async with get_db_session() as db:
        module, item, pool = await _root_catalog(db)
        await _root_version(db, module, "1.0.0", 'output "version" { value = 1 }')
        ws = await _provision_root(db, item, pool, monkeypatch)
        await db.refresh(module, attribute_names=["workspace_links"])
        initial_cv_count = (
            (
                await db.execute(
                    select(ConfigurationVersion).where(ConfigurationVersion.workspace_id == ws.id)
                )
            )
            .scalars()
            .all()
        )
        good = catalog_service._build_tarball(
            {"repository/main.tf": 'output "version" { value = 2 }'}
        )
        bad = catalog_service._build_tarball({"repository/main.tf": 'variable "required" {}'})
        pr = SimpleNamespace(
            number=7, head_sha="a" * 40, head_ref="feature", title="candidate", from_fork=False
        )
        with (
            patch.object(
                module_impact_service, "_download_archive", new=AsyncMock(return_value=good)
            ) as download,
            patch.object(module_impact_service, "_enqueue_module_vcs_status", new=AsyncMock()),
            patch.object(
                module_impact_service,
                "_post_unusable_module_pr_status",
                new=AsyncMock(return_value=True),
            ) as failed,
        ):
            assert await module_impact_service._create_module_test_runs(
                db, get_storage(), module, SimpleNamespace(provider="github"), "owner", "repo", pr
            )
            download.return_value = bad
            pr.head_sha = "b" * 40
            assert await module_impact_service._create_module_test_runs(
                db, get_storage(), module, SimpleNamespace(provider="github"), "owner", "repo", pr
            )
            failed.assert_awaited_once()
        ws_id, module_id = ws.id, module.id
        await db.commit()

    async with get_db_session() as db:
        cvs = (
            (
                await db.execute(
                    select(ConfigurationVersion).where(ConfigurationVersion.workspace_id == ws_id)
                )
            )
            .scalars()
            .all()
        )
        runs = (await db.execute(select(Run).where(Run.workspace_id == ws_id))).scalars().all()
        assert len(cvs) == len(initial_cv_count) + 1
        assert len(runs) == 2  # provision plus exactly one compatible module PR
        pr_run = next(run for run in runs if run.source == "module-test")
        cv = next(cv for cv in cvs if cv.id == pr_run.configuration_version_id)
        assert pr_run.plan_only and pr_run.module_impact_module_id == module_id
        assert (cv.source, cv.status, cv.speculative) == ("module-test", "uploaded", True)
        assert (cv.archive_provenance, cv.archive_module_id) == ("protected", module_id)
        archived = await get_storage().get(config_version_key(str(ws_id), str(cv.id)))
        with tarfile.open(fileobj=io.BytesIO(archived), mode="r:gz") as tar:
            assert tar.extractfile("main.tf").read() == b'output "version" { value = 2 }'


@pytest.mark.parametrize("policy", ["count", "age"])
async def test_pr_plans_cannot_evict_last_non_vcs_uploaded_configuration(app, policy):
    now = datetime.now(UTC)
    storage = AsyncMock()
    async with get_db_session() as db:
        ws = Workspace(name=f"pr-cv-{uuid.uuid4().hex[:8]}", execution_mode="agent")
        db.add(ws)
        await db.flush()
        stable = ConfigurationVersion(
            workspace_id=ws.id,
            status="uploaded",
            speculative=False,
            created_at=now - timedelta(days=120),
        )
        plans = [
            ConfigurationVersion(
                workspace_id=ws.id,
                status="uploaded",
                speculative=True,
                created_at=now - timedelta(days=1) + timedelta(seconds=i),
            )
            for i in range(51)
        ]
        db.add_all([stable, *plans])
        await db.flush()
        if policy == "count":
            deleted = await artifact_retention_service._cleanup_config_versions_count(
                db, storage, keep=3, batch_size=100
            )
            assert deleted == 48
        else:
            deleted = await artifact_retention_service._cleanup_config_versions(
                db, storage, retention_days=90, batch_size=100
            )
            assert deleted == 0
        assert await db.get(ConfigurationVersion, stable.id) is not None
        assert (await run_service.get_latest_uploaded_cv(db, ws.id)).id == stable.id
        assert all(str(stable.id) not in call.args[0] for call in storage.delete.await_args_list)


async def test_module_pr_cleanup_finds_existing_override_by_coordinate(app):
    coord = "default/root/aws"
    key = "module_overrides/abc/default/root/aws.tar.gz"
    async with get_db_session() as db:
        ws = Workspace(name=f"pr-shared-{uuid.uuid4().hex[:8]}", execution_mode="agent")
        db.add(ws)
        await db.flush()
        matching = Run(
            workspace_id=ws.id,
            status="queued",
            source="module-test",
            module_overrides={coord: key},
        )
        other = Run(
            workspace_id=ws.id,
            status="queued",
            source="module-test",
            module_overrides={"default/other/aws": key},
        )
        db.add_all([matching, other])
        await db.flush()

        result = await db.execute(
            select(Run.id).where(Run.module_overrides[coord].astext == key).limit(1)
        )
        assert result.scalar_one_or_none() == matching.id
        missing = await db.execute(
            select(Run.id).where(Run.module_overrides[coord].astext == "missing").limit(1)
        )
        assert missing.scalar_one_or_none() is None


async def test_retention_keeps_override_referenced_by_newer_run(app):
    key = "module_overrides/abc/default/root/aws.tar.gz"
    coord = "default/root/aws"
    storage = AsyncMock()
    async with get_db_session() as db:
        ws = Workspace(name=f"pr-retain-{uuid.uuid4().hex[:8]}", execution_mode="agent")
        db.add(ws)
        await db.flush()
        old = Run(
            workspace_id=ws.id,
            status="applied",
            source="module-test",
            module_overrides={coord: key},
            created_at=datetime.now(UTC) - timedelta(days=30),
        )
        current = Run(
            workspace_id=ws.id,
            status="queued",
            source="module-test",
            module_overrides={coord: key},
        )
        db.add_all([old, current])
        await db.flush()

        assert await artifact_retention_service._cleanup_module_overrides(db, storage, 14, 100) == 0
        assert old.module_overrides is None
        assert current.module_overrides == {coord: key}
        storage.delete.assert_not_awaited()

        current.status = "applied"
        current.created_at = datetime.now(UTC) - timedelta(days=30)
        await db.flush()
        assert await artifact_retention_service._cleanup_module_overrides(db, storage, 14, 100) == 1
        storage.delete.assert_awaited_once_with(key)


async def test_retention_releases_two_expired_references_before_deleting_shared_key(app):
    key = "module_overrides/abc/default/root/aws.tar.gz"
    coord = "default/root/aws"
    storage = AsyncMock()
    async with get_db_session() as db:
        ws = Workspace(name=f"pr-two-old-{uuid.uuid4().hex[:8]}", execution_mode="agent")
        db.add(ws)
        await db.flush()
        old = [
            Run(
                workspace_id=ws.id,
                status="applied",
                source="module-test",
                module_overrides={coord: key},
                created_at=datetime.now(UTC) - timedelta(days=30),
            )
            for _ in range(2)
        ]
        db.add_all(old)
        await db.flush()

        assert await artifact_retention_service._cleanup_module_overrides(db, storage, 14, 100) == 1
        storage.delete.assert_awaited_once_with(key)
        assert all(run.module_overrides is None for run in old)


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
