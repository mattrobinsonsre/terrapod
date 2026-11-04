"""Human archive access is stricter than CV metadata and runner artifact access."""

import io
import tarfile
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.auth import download_tickets
from terrapod.auth.capabilities import CONFIG_READ, REGISTRY_READ
from terrapod.db.models import ConfigurationVersion, ProviderTemplate, RegistryModule, Workspace
from terrapod.db.session import get_db
from terrapod.services import catalog_service


@pytest.fixture
def archive_api():
    user = AuthenticatedUser(
        email="reader@example.com",
        display_name="Reader",
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )
    ws_id, module_id = uuid.uuid4(), uuid.uuid4()
    ws = SimpleNamespace(id=ws_id, catalog_item_id=None)
    module = SimpleNamespace(
        id=module_id, name="private", labels={"team": "security"}, owner_email="owner@example.com"
    )
    cvs = {}
    permissions = {"workspace": True, "registry": False, "module": True}
    db = AsyncMock()
    db.add = MagicMock()

    async def get(model, key):
        if model is Workspace:
            return ws if key == ws_id else None
        if model is RegistryModule:
            return module if permissions["module"] and key == module_id else None
        return None

    db.get.side_effect = get
    app = create_application()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    storage = MagicMock()
    storage.head = AsyncMock()

    async def get_stream(_key):
        yield b"archive"

    storage.get_stream = get_stream

    with (
        patch(
            "terrapod.api.routers.config_versions.run_service.get_configuration_version",
            new_callable=AsyncMock,
            side_effect=lambda _db, cv_id: cvs.get(cv_id),
        ),
        patch(
            "terrapod.api.routers.config_versions.resolve_workspace_capabilities_for",
            new_callable=AsyncMock,
            side_effect=lambda *_: {CONFIG_READ} if permissions["workspace"] else set(),
        ),
        patch(
            "terrapod.api.routers.config_versions.resolve_registry_capabilities_for",
            new_callable=AsyncMock,
            side_effect=lambda *_: {REGISTRY_READ} if permissions["registry"] else set(),
        ) as registry_resolve,
        patch("terrapod.api.routers.config_versions.get_storage", return_value=storage),
        patch("terrapod.auth.download_tickets._get_signing_key", return_value=b"test-key"),
    ):
        yield app, cvs, ws_id, module_id, permissions, registry_resolve, storage


def _cv(ws_id, module_id=None, provenance="unprotected"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        workspace_id=ws_id,
        status="uploaded",
        archive_provenance=provenance,
        archive_module_id=module_id,
        source="catalog",
        auto_queue_runs=False,
        speculative=False,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


@pytest.mark.parametrize("prefix", ["/api/v1", "/api/terrapod/v1"])
async def test_protected_download_and_mint_need_both_grants_and_existing_module(
    archive_api, prefix
):
    app, cvs, ws_id, module_id, perms, registry_resolve, storage = archive_api
    cv = _cv(ws_id, module_id, "protected")
    cvs[cv.id] = cv
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for endpoint, method in (("download", client.get), ("download-ticket", client.post)):
            path = f"{prefix}/configuration-versions/cv-{cv.id}/{endpoint}"
            for workspace, registry, module, expected in (
                (False, True, True, 404),
                (True, False, True, 404),
                (True, True, False, 404),
                (True, True, True, 200),
            ):
                perms.update(workspace=workspace, registry=registry, module=module)
                resp = await method(path)
                assert resp.status_code == expected
                if expected != 200:
                    assert resp.json()["errors"][0]["status"] == "404"
                    assert resp.json()["detail"] == "Configuration version not found"
                elif endpoint == "download":
                    assert resp.content == b"archive"
                else:
                    payload = download_tickets.verify_ticket(
                        resp.json()["data"]["attributes"]["ticket"]
                    )
                    assert payload.resource_kind == download_tickets.PROTECTED_CV_KIND
        registry_resolve.assert_awaited()
        assert registry_resolve.await_args.args[2:] == (
            "private",
            {"team": "security"},
            "owner@example.com",
        )
    assert storage.head.await_count == 1


async def test_unknown_provenance_denies_human_bytes_even_with_both_grants(archive_api):
    app, cvs, ws_id, _module_id, perms, _resolve, storage = archive_api
    perms["registry"] = True
    cv = _cv(ws_id, provenance="unknown")
    cvs[cv.id] = cv
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for suffix, method in (("download", client.get), ("download-ticket", client.post)):
            resp = await method(f"/api/v1/configuration-versions/{cv.id}/{suffix}")
            assert resp.status_code == 404
    storage.head.assert_not_awaited()


async def test_old_replica_ordinary_cv_stays_readable_only_outside_catalog(archive_api):
    app, cvs, ws_id, _module_id, _perms, _resolve, _storage = archive_api
    cv = _cv(ws_id, provenance="unknown")
    cv.source = "vcs"
    cvs[cv.id] = cv
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        path = f"/api/v1/configuration-versions/cv-{cv.id}"
        assert (await client.get(f"{path}/download")).content == b"archive"
        response = await client.post(f"{path}/download-ticket")
        assert response.status_code == 200
        ticket = response.json()["data"]["attributes"]["ticket"]
        assert (
            await client.get(f"/api/v1/configuration-versions/download-by-ticket/{ticket}")
        ).content == b"archive"

        # Catalog sources must still fail closed with the same old ticket.
        cv.source = "catalog"
        assert (await client.get(f"{path}/download")).status_code == 404
        assert (
            await client.get(f"/api/v1/configuration-versions/download-by-ticket/{ticket}")
        ).status_code == 401

    from terrapod.api.routers.config_versions import _effective_archive_provenance

    cv.source = "vcs"
    assert (
        _effective_archive_provenance(cv, SimpleNamespace(catalog_item_id=uuid.uuid4()))
        == "unknown"
    )


async def test_metadata_is_unchanged_for_unknown_archive(archive_api):
    app, cvs, ws_id, _module_id, _perms, _resolve, _storage = archive_api
    cv = _cv(ws_id, provenance="unknown")
    cvs[cv.id] = cv
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/v1/configuration-versions/cv-{cv.id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["attributes"]["source"] == "catalog"
    assert "archive-provenance" not in resp.json()["data"]["attributes"]
    assert "archive-module-id" not in resp.json()["data"]["attributes"]


async def test_configuration_version_metadata_requires_workspace_read(archive_api):
    app, cvs, ws_id, _module_id, perms, _resolve, _storage = archive_api
    cv = _cv(ws_id)
    cvs[cv.id] = cv
    perms["workspace"] = False
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/v1/configuration-versions/cv-{cv.id}")
    assert resp.status_code == 404


async def test_run_scoped_artifact_can_fetch_unknown_archive(archive_api):
    app, _cvs, ws_id, _module_id, _perms, _resolve, _storage = archive_api
    run_id, cv_id = uuid.uuid4(), uuid.uuid4()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        email="runner",
        display_name="Runner",
        roles=["everyone"],
        provider_name="runner_token",
        auth_method="runner_token",
        run_id=str(run_id),
    )
    run = SimpleNamespace(workspace_id=ws_id, configuration_version_id=cv_id)
    run_storage = MagicMock()
    run_storage.presigned_get_url = AsyncMock(
        return_value=SimpleNamespace(url="https://storage.test/archive")
    )
    with (
        patch("terrapod.api.routers.run_artifacts.get_storage", return_value=run_storage),
        patch(
            "terrapod.api.routers.run_artifacts._get_run", new_callable=AsyncMock, return_value=run
        ),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get(
                f"/api/v1/runs/run-{run_id}/artifacts/config", follow_redirects=False
            )
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://storage.test/archive"
    run_storage.presigned_get_url.assert_awaited_once()


async def test_diff_checks_provenance_on_both_sides(archive_api):
    app, cvs, ws_id, module_id, perms, _resolve, _storage = archive_api
    ordinary = _cv(ws_id)
    protected = _cv(ws_id, module_id, "protected")
    cvs.update({ordinary.id: ordinary, protected.id: protected})
    body = {"data": {"attributes": {"from-id": f"cv-{ordinary.id}", "to-id": f"cv-{protected.id}"}}}
    with patch(
        "terrapod.api.routers.config_versions.cv_diff_service.diff_tarballs",
        new_callable=AsyncMock,
        return_value={"files": []},
    ) as diff:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            for from_cv, to_cv in ((ordinary, protected), (protected, ordinary)):
                body["data"]["attributes"].update(
                    {"from-id": str(from_cv.id), "to-id": str(to_cv.id)}
                )
                resp = await client.post("/api/v1/configuration-versions/diff", json=body)
                assert resp.status_code == 404
            diff.assert_not_awaited()
            perms["registry"] = True
            resp = await client.post("/api/v1/configuration-versions/diff", json=body)
            assert resp.status_code == 200
            diff.assert_awaited_once()


async def test_materialised_template_wrapper_needs_platform_admin_for_all_human_bytes(archive_api):
    app, cvs, ws_id, module_id, perms, registry_resolve, storage = archive_api
    db = app.dependency_overrides[get_db]()
    original_get = db.get.side_effect
    template_id = uuid.uuid4()
    template = SimpleNamespace(
        name="example",
        body='provider "aws" { access_key = "fake-inline-credential" }',
        parameters=[],
    )

    async def get_with_template(model, key):
        if model is ProviderTemplate and key == template_id:
            return template
        return await original_get(model, key)

    db.get.side_effect = get_with_template
    db.add = MagicMock()

    async def flush():
        for call in db.add.call_args_list:
            obj = call.args[0]
            if isinstance(obj, ConfigurationVersion) and obj.id is None:
                obj.id = uuid.uuid4()
                obj.created_at = datetime(2026, 1, 1, tzinfo=UTC)
                cvs[obj.id] = obj

    db.flush.side_effect = flush
    archives = {}

    async def put_stream(key, chunks, *, content_type):
        assert content_type == "application/gzip"
        archives[key] = b"".join([part async for part in chunks])

    async def get_stream(key):
        yield archives[key]

    storage.put_stream = AsyncMock(side_effect=put_stream)
    storage.get_stream = get_stream
    item = SimpleNamespace(
        module=SimpleNamespace(id=module_id, namespace="default", name="example", provider="aws"),
        self_contained=False,
        configure_enabled=False,
        ansible_directory="",
        default_playbook="",
        default_limit="",
        provider_template_ids=[str(template_id)],
        variable_options=[],
    )
    with (
        patch.object(catalog_service, "_resolve_module_version", AsyncMock(return_value=None)),
        patch.object(
            catalog_service.variable_service, "list_variables", AsyncMock(return_value=[])
        ),
        patch.object(
            catalog_service.run_service, "create_run", AsyncMock(return_value=MagicMock())
        ),
        patch.object(catalog_service.run_service, "queue_run", AsyncMock()),
        patch("terrapod.storage.get_storage", return_value=storage),
    ):
        await catalog_service._materialise(
            db,
            SimpleNamespace(id=ws_id),
            item,
            input_values={},
            version_pin=None,
            auto_apply=False,
            message="Wrapper run",
            user_email="reader@example.com",
        )

    cv = next(iter(cvs.values()))
    assert cv.archive_provenance == "admin-only"
    assert cv.archive_module_id is None
    archive = next(iter(archives.values()))
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        assert b"fake-inline-credential" in tar.extractfile("providers.tf").read()

    ordinary = _cv(ws_id)
    cvs[ordinary.id] = ordinary
    body = {"data": {"attributes": {"from-id": str(ordinary.id), "to-id": str(cv.id)}}}
    principal = AuthenticatedUser(
        email="reader@example.com",
        display_name="Reader",
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )
    app.dependency_overrides[get_current_user] = lambda: principal
    perms["registry"] = True  # Registry read does not grant access to provider-template HCL.
    with patch(
        "terrapod.api.routers.config_versions.cv_diff_service.diff_tarballs",
        new_callable=AsyncMock,
        return_value={"files": []},
    ) as diff:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            download = f"/api/v1/configuration-versions/cv-{cv.id}/download"
            mint = f"/api/v1/configuration-versions/cv-{cv.id}/download-ticket"
            for from_cv, to_cv in ((ordinary, cv), (cv, ordinary)):
                body["data"]["attributes"].update(
                    {"from-id": str(from_cv.id), "to-id": str(to_cv.id)}
                )
                assert (await client.get(download)).status_code == 404
                assert (await client.post(mint)).status_code == 404
                assert (
                    await client.post("/api/v1/configuration-versions/diff", json=body)
                ).status_code == 404
            diff.assert_not_awaited()
            storage.head.assert_not_awaited()
            registry_resolve.assert_not_awaited()

            # A bound token cannot use an unpinned live admin role.
            principal.roles = ["admin"]
            principal.kind = "service_bound"
            principal.pinned_roles = ["everyone"]
            assert (await client.get(download)).status_code == 404
            principal.kind = "interactive"
            perms["workspace"] = False
            assert (await client.get(download)).status_code == 404
            assert (await client.post(mint)).status_code == 404
            perms["workspace"] = True

            assert (await client.get(download)).content == archive
            minted = await client.post(mint)
            assert minted.status_code == 200
            ticket = minted.json()["data"]["attributes"]["ticket"]
            assert (
                download_tickets.verify_ticket(ticket).resource_kind
                == download_tickets.ADMIN_ONLY_CV_KIND
            )
            assert (
                await client.post("/api/v1/configuration-versions/diff", json=body)
            ).status_code == 200
            body["data"]["attributes"].update({"from-id": str(ordinary.id), "to-id": str(cv.id)})
            assert (
                await client.post("/api/v1/configuration-versions/diff", json=body)
            ).status_code == 200
            assert diff.await_count == 2

            old_ticket = download_tickets.mint_ticket("cv", str(cv.id), principal.email)
            protected_ticket = download_tickets.mint_ticket(
                download_tickets.PROTECTED_CV_KIND, str(cv.id), principal.email
            )
            principal.roles = ["everyone"]
            perms["workspace"] = False
            for invalid in (old_ticket, protected_ticket):
                assert (
                    await client.get(f"/api/v1/configuration-versions/download-by-ticket/{invalid}")
                ).status_code == 401
            assert (
                await client.get(f"/api/v1/configuration-versions/download-by-ticket/{ticket}")
            ).content == archive
            expires_at = download_tickets.verify_ticket(ticket).expires_at
            with patch("terrapod.auth.download_tickets.time.time", return_value=expires_at + 1):
                assert (
                    await client.get(f"/api/v1/configuration-versions/download-by-ticket/{ticket}")
                ).status_code == 401


async def test_ticket_scope_rejects_old_and_unknown_but_survives_revocation(archive_api):
    app, cvs, ws_id, module_id, perms, _resolve, storage = archive_api
    protected = _cv(ws_id, module_id, "protected")
    ordinary = _cv(ws_id)
    unknown = _cv(ws_id, provenance="unknown")
    cvs.update({cv.id: cv for cv in (protected, ordinary, unknown)})
    old = download_tickets.mint_ticket("cv", str(protected.id), "reader@example.com")
    unknown_old = download_tickets.mint_ticket("cv", str(unknown.id), "reader@example.com")
    admin_only_on_protected = download_tickets.mint_ticket(
        download_tickets.ADMIN_ONLY_CV_KIND, str(protected.id), "reader@example.com"
    )
    admin_only_on_ordinary = download_tickets.mint_ticket(
        download_tickets.ADMIN_ONLY_CV_KIND, str(ordinary.id), "reader@example.com"
    )
    perms["registry"] = True
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        mint = await client.post(
            f"/api/v1/configuration-versions/{protected.id}/download-ticket",
            json={"data": {"attributes": {"ttl-seconds": 99999}}},
        )
        assert mint.status_code == 200
        ticket = mint.json()["data"]["attributes"]["ticket"]
        assert (
            download_tickets.verify_ticket(ticket).expires_at - int(datetime.now(UTC).timestamp())
            <= 1800
        )
        perms.update(workspace=False, registry=False)
        for t, status in (
            (old, 401),
            (unknown_old, 401),
            (admin_only_on_protected, 401),
            (admin_only_on_ordinary, 401),
            (ticket, 200),
        ):
            resp = await client.get(f"/api/v1/configuration-versions/download-by-ticket/{t}")
            assert resp.status_code == status
            if status == 200:
                assert resp.content == b"archive"
        ordinary_ticket = download_tickets.mint_ticket("cv", str(ordinary.id), "reader@example.com")
        assert (
            await client.get(f"/api/v1/configuration-versions/download-by-ticket/{ordinary_ticket}")
        ).status_code == 200
        perms["module"] = False
        resp = await client.get(f"/api/v1/configuration-versions/download-by-ticket/{ticket}")
        assert resp.status_code == 410
        assert resp.json()["errors"][0]["status"] == "410"
    assert storage.head.await_count == 2


async def test_api_created_cv_source_cannot_classify_registry_archive(archive_api):
    app, _cvs, ws_id, _module_id, perms, _resolve, _storage = archive_api
    db = app.dependency_overrides[get_db]()
    db.execute.return_value = MagicMock()
    db.execute.return_value.scalar_one_or_none.return_value = SimpleNamespace(
        id=ws_id,
        catalog_item_id=None,
    )
    perms["workspace"] = True

    async def flush():
        cv = db.add.call_args.args[0]
        cv.id = uuid.uuid4()
        cv.created_at = datetime(2026, 1, 1, tzinfo=UTC)

    db.flush.side_effect = flush
    with (
        patch(
            "terrapod.api.routers.config_versions.resolve_workspace_capabilities_for",
            new_callable=AsyncMock,
            return_value={"config:upload"},
        ),
        patch("terrapod.auth.capability_urls.mint", return_value="upload-cap"),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                f"/api/v1/workspaces/{ws_id}/configuration-versions",
                json={
                    "data": {
                        "attributes": {
                            "source": "catalog",
                            "archive-provenance": "protected",
                            "archive-module-id": str(uuid.uuid4()),
                        }
                    }
                },
            )
    assert resp.status_code == 201
    cv = db.add.call_args.args[0]
    assert cv.source == "catalog"
    assert cv.archive_provenance == "unprotected"
    assert cv.archive_module_id is None
    db.flush.assert_awaited()
