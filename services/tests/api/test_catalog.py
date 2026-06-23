"""Services-API tests for the catalog router (#535): feature gate, admin gating
on management endpoints, catalog-RBAC on read/use, and provision validation."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.config import settings
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}


def _user(email="u@test.com", roles=None):
    return AuthenticatedUser(
        email=email,
        display_name="U",
        roles=roles or ["everyone"],
        provider_name="local",
        auth_method="session",
    )


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: mock_db
    return app, mock_db


@pytest.fixture(autouse=True)
def _enable_catalog():
    original = settings.catalog.enabled
    settings.catalog.enabled = True
    yield
    settings.catalog.enabled = original


# ── Feature gate ───────────────────────────────────────────────────────


class TestFeatureGate:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_disabled_returns_404(self, *mocks):
        settings.catalog.enabled = False
        app, _ = _make_app(_user(roles=["admin"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/catalog-items", headers=_AUTH)
        assert resp.status_code == 404


# ── Provider template admin gating ─────────────────────────────────────


class TestProviderTemplateRBAC:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_requires_admin(self, *mocks):
        app, _ = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/provider-templates",
                json={"data": {"attributes": {"name": "x", "provider-type": "aws", "body": "b"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.routers.catalog.ProviderTemplate")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_happy_path_admin(self, _db, _redis, _storage, mock_tmpl_cls):
        app, mock_db = _make_app(_user(roles=["admin"]))
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()
        tmpl = MagicMock()
        tmpl.id = uuid.uuid4()
        tmpl.name = "aws-default"
        tmpl.provider_type = "aws"
        tmpl.body = 'provider "aws" {}'
        tmpl.parameters = []
        tmpl.labels = {}
        tmpl.owner_email = "u@test.com"
        tmpl.created_at = None
        tmpl.updated_at = None
        mock_tmpl_cls.return_value = tmpl

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/provider-templates",
                json={
                    "data": {
                        "attributes": {
                            "name": "aws-default",
                            "provider-type": "aws",
                            "body": 'provider "aws" {}',
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 201
        assert resp.json()["data"]["attributes"]["name"] == "aws-default"

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_missing_body_422(self, *mocks):
        app, _ = _make_app(_user(roles=["admin"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/provider-templates",
                json={"data": {"attributes": {"name": "x", "provider-type": "aws"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 422


# ── Catalog item create gating + list RBAC ─────────────────────────────


class TestCatalogItemRBAC:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_create_requires_admin(self, *mocks):
        app, _ = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/terrapod/v1/catalog-items",
                json={"data": {"attributes": {"name": "vpc", "module-id": str(uuid.uuid4())}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.routers.catalog.catalog_service.list_catalog_items")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_list_filters_by_catalog_read(self, _db, _redis, _storage, mock_list):
        """A user with no catalog grant sees an empty list even when items exist."""
        item = MagicMock()
        item.id = uuid.uuid4()
        item.name = "vpc"
        item.labels = {}
        item.owner_email = "someone@else.com"
        mock_list.return_value = [item]

        app, _ = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get("/api/terrapod/v1/catalog-items", headers=_AUTH)
        assert resp.status_code == 200
        assert resp.json()["data"] == []


# ── Provision validation ───────────────────────────────────────────────


class TestProvision:
    @patch("terrapod.api.routers.catalog.catalog_service.provision_instance")
    @patch("terrapod.api.routers.catalog.resolve_pool_permission_for")
    @patch("terrapod.api.routers.catalog.resolve_catalog_permission_for")
    @patch("terrapod.api.routers.catalog.catalog_service.get_catalog_item")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_provision_accepts_apool_prefixed_pool_id(
        self, _db, _redis, _storage, mock_get, mock_cat, mock_pool, mock_prov
    ):
        """Regression (#535 live-smoke): the UI/API emit pool ids as
        'apool-{uuid}'; the provision endpoint must strip the prefix, not 422."""
        pool_uuid = uuid.uuid4()
        item = MagicMock()
        item.enabled = True
        item.name = "vpc"
        item.labels = {}
        item.owner_email = ""
        item.allowed_agent_pool_ids = None
        mock_get.return_value = item
        mock_cat.return_value = "use"
        mock_pool.return_value = "write"

        pool = MagicMock()
        pool.id = pool_uuid
        pool.name = "p"
        pool.labels = {}
        pool.owner_email = None
        ws = MagicMock()
        ws.id = uuid.uuid4()
        ws.name = "smoke"
        ws.catalog_item_id = uuid.uuid4()
        ws.catalog_version_pin = None
        ws.agent_pool_id = pool_uuid
        ws.owner_email = "u@test.com"
        ws.labels = {}
        mock_prov.return_value = ws

        app, mock_db = _make_app(_user(roles=["everyone"]))
        mock_db.get = AsyncMock(return_value=pool)
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/terrapod/v1/catalog-items/{uuid.uuid4()}/provision",
                json={
                    "data": {
                        "attributes": {
                            "name": "smoke",
                            "agent-pool-id": f"apool-{pool_uuid}",
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 201
        # The service received the bare UUID (prefix stripped).
        assert mock_prov.await_args.kwargs["agent_pool_id"] == pool_uuid

    @patch("terrapod.api.routers.catalog.catalog_service.get_catalog_item")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_provision_disabled_item_409(self, _db, _redis, _storage, mock_get):
        item = MagicMock()
        item.enabled = False
        mock_get.return_value = item
        app, _ = _make_app(_user(roles=["admin"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/terrapod/v1/catalog-items/{uuid.uuid4()}/provision",
                json={"data": {"attributes": {"name": "x", "agent-pool-id": str(uuid.uuid4())}}},
                headers=_AUTH,
            )
        assert resp.status_code == 409

    @patch("terrapod.api.routers.catalog.resolve_catalog_permission_for")
    @patch("terrapod.api.routers.catalog.catalog_service.get_catalog_item")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_provision_requires_catalog_use(self, _db, _redis, _storage, mock_get, mock_perm):
        item = MagicMock()
        item.enabled = True
        item.name = "vpc"
        item.labels = {}
        item.owner_email = ""
        mock_get.return_value = item
        mock_perm.return_value = "read"  # read, not use

        app, _ = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/terrapod/v1/catalog-items/{uuid.uuid4()}/provision",
                json={"data": {"attributes": {"name": "x", "agent-pool-id": str(uuid.uuid4())}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.routers.catalog.resolve_pool_permission_for")
    @patch("terrapod.api.routers.catalog.resolve_catalog_permission_for")
    @patch("terrapod.api.routers.catalog.catalog_service.get_catalog_item")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_provision_pool_not_allowed_403(
        self, _db, _redis, _storage, mock_get, mock_cat_perm, mock_pool_perm
    ):
        allowed = uuid.uuid4()
        chosen = uuid.uuid4()
        item = MagicMock()
        item.enabled = True
        item.name = "vpc"
        item.labels = {}
        item.owner_email = ""
        item.allowed_agent_pool_ids = [str(allowed)]
        mock_get.return_value = item
        mock_cat_perm.return_value = "use"
        mock_pool_perm.return_value = "write"

        pool = MagicMock()
        pool.id = chosen
        pool.name = "p"
        pool.labels = {}
        pool.owner_email = None

        app, mock_db = _make_app(_user(roles=["everyone"]))
        mock_db.get = AsyncMock(return_value=pool)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/terrapod/v1/catalog-items/{uuid.uuid4()}/provision",
                json={"data": {"attributes": {"name": "x", "agent-pool-id": str(chosen)}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403
        assert "not allowed" in resp.json()["detail"]

    @patch("terrapod.api.routers.catalog.resolve_pool_permission_for")
    @patch("terrapod.api.routers.catalog.resolve_catalog_permission_for")
    @patch("terrapod.api.routers.catalog.catalog_service.get_catalog_item")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_provision_needs_pool_write(
        self, _db, _redis, _storage, mock_get, mock_cat_perm, mock_pool_perm
    ):
        chosen = uuid.uuid4()
        item = MagicMock()
        item.enabled = True
        item.name = "vpc"
        item.labels = {}
        item.owner_email = ""
        item.allowed_agent_pool_ids = None  # any pool allowed
        mock_get.return_value = item
        mock_cat_perm.return_value = "use"
        mock_pool_perm.return_value = "read"  # not write

        pool = MagicMock()
        pool.id = chosen
        pool.name = "p"
        pool.labels = {}
        pool.owner_email = None

        app, mock_db = _make_app(_user(roles=["everyone"]))
        mock_db.get = AsyncMock(return_value=pool)

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/terrapod/v1/catalog-items/{uuid.uuid4()}/provision",
                json={"data": {"attributes": {"name": "x", "agent-pool-id": str(chosen)}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403
        assert "agent pool" in resp.json()["detail"]


# ── Instance lifecycle (#535 P2) ───────────────────────────────────────


def _catalog_ws(catalog_item_id=None):
    ws = MagicMock()
    ws.id = uuid.uuid4()
    ws.catalog_item_id = catalog_item_id or uuid.uuid4()
    return ws


class TestInstanceLifecycle:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_reconfigure_non_catalog_ws_404(self, *mocks):
        app, mock_db = _make_app(_user(roles=["admin"]))
        ws = MagicMock()
        ws.id = uuid.uuid4()
        ws.catalog_item_id = None  # not catalog-managed
        mock_db.get = AsyncMock(return_value=ws)
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/catalog-instances/ws-{ws.id}",
                json={"data": {"attributes": {"input-values": {}}}},
                headers=_AUTH,
            )
        assert resp.status_code == 404

    @patch("terrapod.api.routers.catalog.resolve_catalog_permission_for")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_reconfigure_requires_catalog_use(self, _db, _redis, _storage, mock_perm):
        app, mock_db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        item = MagicMock()
        item.name = "vpc"
        item.labels = {}
        item.owner_email = ""
        mock_db.get = AsyncMock(side_effect=[ws, item])
        mock_perm.return_value = "read"  # read, not use

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/catalog-instances/ws-{ws.id}",
                json={"data": {"attributes": {"input-values": {"cidr": "10.0.0.0/16"}}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.routers.catalog.catalog_service.reconfigure_instance")
    @patch("terrapod.api.routers.catalog.resolve_catalog_permission_for")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_reconfigure_happy_path(self, _db, _redis, _storage, mock_perm, mock_reconfig):
        app, mock_db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        item = MagicMock()
        item.name = "vpc"
        item.labels = {}
        item.owner_email = ""
        mock_db.get = AsyncMock(side_effect=[ws, item])
        mock_perm.return_value = "use"
        run = MagicMock()
        run.id = uuid.uuid4()
        run.status = "queued"
        run.is_destroy = False
        mock_reconfig.return_value = run

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/catalog-instances/ws-{ws.id}",
                json={
                    "data": {
                        "attributes": {
                            "input-values": {"cidr": "10.0.0.0/16"},
                            "version-pin": "1.2.0",
                            "auto-apply": True,
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert resp.json()["data"]["type"] == "runs"
        assert mock_reconfig.await_args.kwargs["version_pin"] == "1.2.0"
        assert mock_reconfig.await_args.kwargs["auto_apply"] is True

    @patch("terrapod.api.routers.catalog.catalog_service.destroy_instance")
    @patch("terrapod.api.routers.catalog.resolve_catalog_permission_for")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_destroy_happy_path(self, _db, _redis, _storage, mock_perm, mock_destroy):
        app, mock_db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        item = MagicMock()
        item.name = "vpc"
        item.labels = {}
        item.owner_email = ""
        mock_db.get = AsyncMock(side_effect=[ws, item])
        mock_perm.return_value = "use"
        run = MagicMock()
        run.id = uuid.uuid4()
        run.status = "queued"
        run.is_destroy = True
        mock_destroy.return_value = run

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/terrapod/v1/catalog-instances/ws-{ws.id}/destroy",
                json={"data": {"attributes": {"auto-apply": True}}},
                headers=_AUTH,
            )
        assert resp.status_code == 201
        assert resp.json()["data"]["attributes"]["is-destroy"] is True
        assert mock_destroy.await_args.kwargs["auto_apply"] is True

    @patch("terrapod.api.routers.catalog.resolve_catalog_permission_for")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_destroy_requires_catalog_use(self, _db, _redis, _storage, mock_perm):
        app, mock_db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        item = MagicMock()
        item.name = "vpc"
        item.labels = {}
        item.owner_email = ""
        mock_db.get = AsyncMock(side_effect=[ws, item])
        mock_perm.return_value = "read"

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/terrapod/v1/catalog-instances/ws-{ws.id}/destroy",
                json={"data": {"attributes": {}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403
