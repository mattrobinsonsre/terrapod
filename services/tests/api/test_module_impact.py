"""Tests for module impact analysis: workspace links, download override, retry."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.db.session import get_db
from terrapod.storage import get_storage

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}


def _admin_user(email="admin@example.com"):
    return AuthenticatedUser(
        email=email,
        display_name="Admin",
        roles=["admin"],
        provider_name="local",
        auth_method="session",
    )


def _runner_user(run_id: str):
    return AuthenticatedUser(
        email="runner",
        display_name="runner",
        roles=["everyone"],
        provider_name="runner_token",
        auth_method="runner_token",
        run_id=run_id,
    )


def _mock_module(module_id=None, name="eks", provider="aws", namespace="default"):
    m = MagicMock()
    m.id = module_id or uuid.uuid4()
    m.name = name
    m.provider = provider
    m.namespace = namespace
    m.status = "setup_complete"
    m.labels = {}
    m.owner_email = "admin@example.com"
    m.source = "vcs"
    m.vcs_connection_id = uuid.uuid4()
    m.vcs_repo_url = "https://github.com/org/terraform-module-eks"
    m.vcs_branch = ""
    m.vcs_tag_pattern = "v*"
    m.vcs_last_tag = ""
    m.vcs_last_pr_shas = None
    m.versions = []
    m.workspace_links = []
    m.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    m.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    return m


def _mock_workspace(ws_id=None, name="test-ws"):
    ws = MagicMock()
    ws.id = ws_id or uuid.uuid4()
    ws.name = name
    ws.agent_pool_id = None
    ws.vcs_last_polled_at = None
    ws.vcs_last_error = None
    ws.vcs_last_error_at = None
    return ws


def _mock_link(link_id=None, module_id=None, ws_id=None, ws_name="test-ws"):
    link = MagicMock()
    link.id = link_id or uuid.uuid4()
    link.module_id = module_id or uuid.uuid4()
    link.workspace_id = ws_id or uuid.uuid4()
    link.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    link.created_by = "admin@example.com"
    ws = MagicMock()
    ws.name = ws_name
    link.workspace = ws
    return link


def _mock_run(
    run_id=None,
    status="pending",
    source="tfe-api",
    module_overrides=None,
    ws_id=None,
):
    run = MagicMock()
    run.id = run_id or uuid.uuid4()
    run.workspace_id = ws_id or uuid.uuid4()
    run.status = status
    run.source = source
    run.module_overrides = module_overrides
    run.message = ""
    run.is_destroy = False
    run.auto_apply = False
    run.plan_only = False
    run.execution_backend = "tofu"
    run.terraform_version = "1.11"
    run.error_message = ""
    run.is_drift_detection = False
    run.has_changes = None
    run.vcs_commit_sha = ""
    run.vcs_branch = ""
    run.vcs_pull_request_number = None
    run.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    run.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    run.plan_started_at = None
    run.plan_finished_at = None
    run.apply_started_at = None
    run.apply_finished_at = None
    run.listener_id = None
    run.target_addrs = None
    run.replace_addrs = None
    run.refresh_only = False
    run.refresh = True
    run.allow_empty_apply = False
    run.resource_cpu = "1"
    run.resource_memory = "2Gi"
    run.configuration_version_id = None
    return run


def _make_app(user, mock_db=None, mock_storage=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    if mock_storage is not None:
        app.dependency_overrides[get_storage] = lambda: mock_storage
    return app, mock_db


# ── Run JSON includes module-overrides ──────────────────────────────


class TestRunJsonModuleOverrides:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.run_service.get_run")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission")
    async def test_run_json_includes_module_overrides(self, mock_resolve, mock_get_run, *mocks):
        overrides = {"default/eks/aws": "module_overrides/abc123/default/eks/aws.tar.gz"}
        run = _mock_run(module_overrides=overrides, source="module-test")
        mock_get_run.return_value = run
        mock_resolve.return_value = "read"

        app, _ = _make_app(_admin_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.get(f"/api/v2/runs/run-{run.id}", headers=_AUTH)

        assert resp.status_code == 200
        attrs = resp.json()["data"]["attributes"]
        assert attrs["module-overrides"] == overrides
        assert attrs["source"] == "module-test"


# ── Module download override ────────────────────────────────────────


class TestModuleDownloadOverride:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.registry_module_service.get_module")
    async def test_download_with_override_returns_override_url(self, mock_get_module, *mocks):
        """When run has overrides for this module, serve the override tarball."""
        run_id = uuid.uuid4()
        overrides = {"default/eks/aws": "module_overrides/abc123/default/eks/aws.tar.gz"}
        run = _mock_run(run_id=run_id, module_overrides=overrides)

        mock_storage = AsyncMock()
        presigned = MagicMock()
        presigned.url = "https://storage.example.com/override-tarball"
        mock_storage.presigned_get_url.return_value = presigned

        mock_db = AsyncMock()
        mock_db.get.return_value = run

        from terrapod.services.registry_module_service import get_module_download_url

        url = await get_module_download_url(
            mock_db,
            mock_storage,
            "default",
            "eks",
            "aws",
            "1.0.0",
            run_id=str(run_id),
        )

        assert url == "https://storage.example.com/override-tarball"
        mock_storage.presigned_get_url.assert_called_once_with(overrides["default/eks/aws"])

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.services.registry_module_service.get_module")
    async def test_download_without_override_returns_normal_url(self, mock_get_module, *mocks):
        """When run has no overrides for this module, serve the published version."""
        run_id = uuid.uuid4()
        run = _mock_run(run_id=run_id, module_overrides=None)

        mock_module = _mock_module()
        mock_get_module.return_value = mock_module

        mock_version = MagicMock()
        mock_version.version = "1.0.0"

        mock_storage = AsyncMock()
        presigned = MagicMock()
        presigned.url = "https://storage.example.com/normal-tarball"
        mock_storage.presigned_get_url.return_value = presigned

        mock_db = AsyncMock()
        mock_db.get.return_value = run

        # Mock the version lookup
        version_result = MagicMock()
        version_result.scalars.return_value.first.return_value = mock_version
        mock_db.execute.return_value = version_result

        from terrapod.services.registry_module_service import get_module_download_url

        url = await get_module_download_url(
            mock_db,
            mock_storage,
            "default",
            "eks",
            "aws",
            "1.0.0",
            run_id=str(run_id),
        )

        assert url == "https://storage.example.com/normal-tarball"


# ── Retry copies module_overrides ───────────────────────────────────


class TestRetryRunCopiesOverrides:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.runs.run_service.queue_run")
    @patch("terrapod.api.routers.runs.run_service.create_run")
    @patch("terrapod.api.routers.runs.run_service.get_run")
    @patch("terrapod.api.routers.runs.resolve_workspace_permission")
    async def test_retry_copies_module_overrides(
        self, mock_resolve, mock_get_run, mock_create_run, mock_queue, *mocks
    ):
        overrides = {"default/eks/aws": "module_overrides/abc123/default/eks/aws.tar.gz"}
        original = _mock_run(
            status="errored",
            source="module-test",
            module_overrides=overrides,
        )
        mock_get_run.return_value = original
        mock_resolve.return_value = "plan"

        new_run = _mock_run()
        mock_create_run.return_value = new_run
        mock_queue.return_value = new_run

        mock_db = AsyncMock()
        mock_db.get.return_value = _mock_workspace()

        app, _ = _make_app(_admin_user(), mock_db=mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.post(
                f"/api/v2/runs/run-{original.id}/actions/retry",
                headers=_AUTH,
            )

        assert resp.status_code == 201
        # Verify module_overrides was copied
        assert new_run.module_overrides == overrides


# ── Workspace Link CRUD ─────────────────────────────────────────────


class TestWorkspaceLinkCRUD:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.registry_modules.resolve_registry_permission")
    @patch("terrapod.api.routers.registry_modules.get_module")
    async def test_list_workspace_links(self, mock_get_module, mock_resolve, *mocks):
        module = _mock_module()
        mock_get_module.return_value = module
        mock_resolve.return_value = "read"

        link = _mock_link(module_id=module.id)

        mock_db = AsyncMock()
        result = MagicMock()
        result.scalars.return_value.all.return_value = [link]
        mock_db.execute.return_value = result

        app, _ = _make_app(_admin_user(), mock_db=mock_db)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.get(
                "/api/v2/organizations/default/registry-modules/private/default/eks/aws/workspace-links",
                headers=_AUTH,
            )

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert len(data) == 1
        assert data[0]["attributes"]["workspace-name"] == "test-ws"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    @patch("terrapod.api.routers.registry_modules.resolve_registry_permission")
    @patch("terrapod.api.routers.registry_modules.get_module")
    async def test_create_workspace_link_requires_admin(
        self, mock_get_module, mock_resolve, *mocks
    ):
        module = _mock_module()
        mock_get_module.return_value = module
        mock_resolve.return_value = "write"  # Not admin

        app, _ = _make_app(_admin_user())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
            resp = await client.post(
                "/api/v2/organizations/default/registry-modules/private/default/eks/aws/workspace-links",
                headers={**_AUTH, "Content-Type": "application/vnd.api+json"},
                json={
                    "data": {
                        "type": "workspace-links",
                        "attributes": {"workspace_id": str(uuid.uuid4())},
                    }
                },
            )

        assert resp.status_code == 403


# ── Storage Key ─────────────────────────────────────────────────────


class TestModuleOverrideKey:
    def test_key_format(self):
        from terrapod.storage.keys import module_override_key

        key = module_override_key("abc123", "default", "eks", "aws")
        assert key == "module_overrides/abc123/default/eks/aws.tar.gz"

    def test_different_shas_produce_different_keys(self):
        from terrapod.storage.keys import module_override_key

        key1 = module_override_key("sha1", "default", "eks", "aws")
        key2 = module_override_key("sha2", "default", "eks", "aws")
        assert key1 != key2
