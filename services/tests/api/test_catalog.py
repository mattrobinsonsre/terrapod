"""Services-API tests for the catalog router (#535): feature gate, admin gating
on management endpoints, catalog-RBAC on read/use, and provision validation."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.api.routers import catalog as catalog_router
from terrapod.auth.capabilities import caps_for_level
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


def _no_rows():
    """A result shaped like SQLAlchemy's, answering "nothing matched".

    Needed because `provision_instance` is patched out in these tests while the
    router still runs the GHSA-49q6-pm68-3xgw self-join check after it — and that
    check asks real queries. A bare `AsyncMock` makes `.scalars().all()` a
    coroutine, which fails in a way that names neither the check nor the mock.
    """
    r = MagicMock()
    r.scalars.return_value.all.return_value = []
    r.scalar_one_or_none.return_value = None
    r.scalar_one.return_value = None
    r.first.return_value = None
    r.all.return_value = []
    return r


def _make_app(user, mock_db=None):
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: user
    if mock_db is None:
        mock_db = AsyncMock()
        # Default to "no rows" rather than an AsyncMock's async children, so a
        # router that asks an unrelated question gets a usable answer instead of a
        # coroutine. Tests that script `execute` themselves override this.
        mock_db.execute = AsyncMock(return_value=_no_rows())
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
    @patch("terrapod.api.routers.catalog.resolve_pool_capabilities_for")
    @patch("terrapod.api.routers.catalog.resolve_catalog_capabilities_for")
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
        mock_cat.return_value = caps_for_level("use")
        mock_pool.return_value = caps_for_level("write")

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
        ws.agent_pool_links = [SimpleNamespace(agent_pool_id=pool_uuid, ordinal=0, agent_pool=None)]
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

    @patch("terrapod.api.routers.catalog.resolve_catalog_capabilities_for")
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
        mock_perm.return_value = caps_for_level("read")  # read, not use

        app, _ = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/terrapod/v1/catalog-items/{uuid.uuid4()}/provision",
                json={"data": {"attributes": {"name": "x", "agent-pool-id": str(uuid.uuid4())}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.routers.catalog.resolve_pool_capabilities_for")
    @patch("terrapod.api.routers.catalog.resolve_catalog_capabilities_for")
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
        mock_cat_perm.return_value = caps_for_level("use")
        mock_pool_perm.return_value = caps_for_level("write")

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

    @patch("terrapod.api.routers.catalog.resolve_pool_capabilities_for")
    @patch("terrapod.api.routers.catalog.resolve_catalog_capabilities_for")
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
        mock_cat_perm.return_value = caps_for_level("use")
        mock_pool_perm.return_value = caps_for_level("read")  # not write

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

    @patch("terrapod.api.routers.catalog.resolve_catalog_capabilities_for")
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
        mock_perm.return_value = caps_for_level("read")  # read, not use

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/catalog-instances/ws-{ws.id}",
                json={"data": {"attributes": {"input-values": {"cidr": "10.0.0.0/16"}}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.routers.catalog.catalog_service.reconfigure_instance")
    @patch("terrapod.api.routers.catalog.resolve_catalog_capabilities_for")
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
        mock_perm.return_value = caps_for_level("use")
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
    @patch("terrapod.api.routers.catalog.resolve_catalog_capabilities_for")
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
        mock_perm.return_value = caps_for_level("use")
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

    @patch("terrapod.api.routers.catalog.resolve_catalog_capabilities_for")
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
        mock_perm.return_value = caps_for_level("read")

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/terrapod/v1/catalog-instances/ws-{ws.id}/destroy",
                json={"data": {"attributes": {}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.routers.catalog.resolve_catalog_capabilities_for")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_delete_instance_without_orphan_flag_409(self, _db, _redis, _storage, mock_perm):
        """A catalog instance can't be deleted without either destroying or
        explicitly orphaning — refuses (409) and never touches the DB."""
        app, mock_db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        item = MagicMock(labels={}, owner_email="")
        item.name = "vpc"
        mock_db.get = AsyncMock(side_effect=[ws, item])
        mock_perm.return_value = caps_for_level("admin")

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(f"/api/terrapod/v1/catalog-instances/ws-{ws.id}", headers=_AUTH)
        assert resp.status_code == 409
        assert "destroy" in resp.json()["detail"].lower()
        mock_db.delete.assert_not_called()

    @patch("terrapod.api.routers.catalog.resolve_catalog_capabilities_for")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_orphan_instance_deletes_workspace(self, _db, _redis, _storage, mock_perm):
        """orphan=true deletes the workspace record (abandoning infra)."""
        app, mock_db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        item = MagicMock(labels={}, owner_email="")
        item.name = "vpc"
        mock_db.get = AsyncMock(side_effect=[ws, item])
        mock_perm.return_value = caps_for_level("admin")

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(
                f"/api/terrapod/v1/catalog-instances/ws-{ws.id}?orphan=true", headers=_AUTH
            )
        assert resp.status_code == 204
        mock_db.delete.assert_awaited_once_with(ws)

    @patch("terrapod.api.routers.catalog.resolve_catalog_capabilities_for")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_orphan_requires_catalog_admin(self, _db, _redis, _storage, mock_perm):
        """orphan needs catalog admin, not merely 'use'."""
        app, mock_db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        item = MagicMock(labels={}, owner_email="")
        item.name = "vpc"
        mock_db.get = AsyncMock(side_effect=[ws, item])
        mock_perm.return_value = caps_for_level("use")  # use is enough to destroy, not to orphan

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete(
                f"/api/terrapod/v1/catalog-instances/ws-{ws.id}?orphan=true", headers=_AUTH
            )
        assert resp.status_code == 403
        mock_db.delete.assert_not_called()


# ── Variable-options validation (#535 review fixes) ─────────────────────


class TestVariableOptionsValidation:
    """`variable_options` shape is validated at item create/update — malformed
    entries and hidden-without-default are rejected (else a hidden required
    input fails opaquely at plan time)."""

    async def test_hidden_without_default_rejected(self):
        with pytest.raises(HTTPException) as ei:
            await catalog_router._coerce_item(
                AsyncMock(),
                {"variable-options": [{"name": "secret", "hidden": True}]},
                on_create=False,
            )
        assert ei.value.status_code == 422
        assert "default" in str(ei.value.detail).lower()

    async def test_malformed_entry_rejected(self):
        with pytest.raises(HTTPException) as ei:
            await catalog_router._coerce_item(
                AsyncMock(), {"variable-options": [{"no_name": 1}]}, on_create=False
            )
        assert ei.value.status_code == 422

    async def test_options_must_be_list(self):
        with pytest.raises(HTTPException) as ei:
            await catalog_router._coerce_item(
                AsyncMock(),
                {"variable-options": [{"name": "region", "options": "us-east-1"}]},
                on_create=False,
            )
        assert ei.value.status_code == 422

    async def test_valid_overlay_passes(self):
        out = await catalog_router._coerce_item(
            AsyncMock(),
            {
                "variable-options": [
                    {"name": "region", "options": ["a", "b"], "default": "a"},
                    {"name": "secret", "hidden": True, "default": "x"},
                ]
            },
            on_create=False,
        )
        assert len(out["variable_options"]) == 2


# ── Management PATCH/DELETE RBAC negatives ──────────────────────────────


class TestManagementRBACNegatives:
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_update_provider_template_requires_admin(self, *mocks):
        app, _ = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                "/api/terrapod/v1/provider-templates/pt-1",
                json={"data": {"attributes": {"name": "x"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_delete_provider_template_requires_admin(self, *mocks):
        app, _ = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete("/api/terrapod/v1/provider-templates/pt-1", headers=_AUTH)
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_update_catalog_item_requires_admin(self, *mocks):
        app, _ = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                "/api/terrapod/v1/catalog-items/ci-1",
                json={"data": {"attributes": {"enabled": False}}},
                headers=_AUTH,
            )
        assert resp.status_code == 403

    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_delete_catalog_item_requires_admin(self, *mocks):
        app, _ = _make_app(_user(roles=["everyone"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.delete("/api/terrapod/v1/catalog-items/ci-1", headers=_AUTH)
        assert resp.status_code == 403


# ── Confirm / discard (catalog-surface, closes the non-auto-apply hole) ──


def _planned_run(status="planned", is_destroy=False, source="catalog", plan_only=False):
    run = MagicMock()
    run.id = uuid.uuid4()
    run.status = status
    run.is_destroy = is_destroy
    run.source = source
    run.plan_only = plan_only
    return run


def _exec_returns(obj):
    res = MagicMock()
    res.scalar_one_or_none.return_value = obj
    return res


class TestConfirmDiscard:
    @patch("terrapod.api.routers.catalog.run_service.confirm_run")
    @patch("terrapod.api.routers.catalog.resolve_catalog_capabilities_for")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_confirm_planned_run(self, _db, _redis, _storage, mock_perm, mock_confirm):
        app, mock_db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        item = MagicMock(labels={}, owner_email="")
        item.name = "vpc"
        mock_db.get = AsyncMock(side_effect=[ws, item])
        mock_perm.return_value = caps_for_level("use")
        planned = _planned_run()
        mock_db.execute = AsyncMock(return_value=_exec_returns(planned))
        mock_confirm.return_value = _planned_run(status="confirmed")

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/terrapod/v1/catalog-instances/ws-{ws.id}/confirm", headers=_AUTH
            )
        assert resp.status_code == 200
        mock_confirm.assert_awaited_once()

    @patch("terrapod.api.routers.catalog.resolve_catalog_capabilities_for")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_confirm_no_planned_run_409(self, _db, _redis, _storage, mock_perm):
        app, mock_db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        item = MagicMock(labels={}, owner_email="")
        item.name = "vpc"
        mock_db.get = AsyncMock(side_effect=[ws, item])
        mock_perm.return_value = caps_for_level("use")
        mock_db.execute = AsyncMock(return_value=_exec_returns(None))  # no run

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/terrapod/v1/catalog-instances/ws-{ws.id}/confirm", headers=_AUTH
            )
        assert resp.status_code == 409

    @patch("terrapod.api.routers.catalog.resolve_catalog_capabilities_for")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_reconfigure_archived_instance_409(self, _db, _redis, _storage, mock_perm):
        """An archived (already-destroyed) instance can't be reconfigured/destroyed."""
        app, mock_db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        ws.lifecycle_state = "archived"
        mock_db.get = AsyncMock(return_value=ws)  # archived check fires before item/perm

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/catalog-instances/ws-{ws.id}",
                json={"data": {"attributes": {"input-values": {}}}},
                headers=_AUTH,
            )
        assert resp.status_code == 409
        assert "archived" in resp.json()["detail"].lower()


class TestIsConfirmableCatalogRun:
    """S2: the catalog confirm/discard endpoints must act ONLY on catalog-
    initiated, apply-capable, planned runs — never promote a speculative
    module-impact (`module-test`, plan_only) run to apply. Catalog instances
    carry a ModuleWorkspaceLink, so such runs CAN land on them."""

    def test_catalog_planned_apply_run_is_confirmable(self):
        assert catalog_router._is_confirmable_catalog_run(
            _planned_run(source="catalog", plan_only=False)
        )

    def test_catalog_lifecycle_destroy_run_is_confirmable(self):
        assert catalog_router._is_confirmable_catalog_run(
            _planned_run(source="catalog-lifecycle", plan_only=False)
        )

    def test_none_is_not_confirmable(self):
        assert not catalog_router._is_confirmable_catalog_run(None)

    def test_non_planned_is_not_confirmable(self):
        assert not catalog_router._is_confirmable_catalog_run(
            _planned_run(status="applied", source="catalog")
        )

    def test_plan_only_run_is_not_confirmable(self):
        # A speculative plan must never be promotable to apply via the catalog.
        assert not catalog_router._is_confirmable_catalog_run(
            _planned_run(source="catalog", plan_only=True)
        )

    def test_module_test_source_is_not_confirmable(self):
        # module-impact analysis queues these on linked (incl. catalog) workspaces.
        assert not catalog_router._is_confirmable_catalog_run(
            _planned_run(source="module-test", plan_only=True)
        )

    def test_module_publish_source_is_not_confirmable(self):
        # An apply-capable module-publish run is NOT a catalog-initiated change.
        assert not catalog_router._is_confirmable_catalog_run(
            _planned_run(source="module-publish", plan_only=False)
        )


class TestConfirmDiscardRunSourceGuard:
    """S2 at the endpoint layer: a latest run that is a speculative module-test
    (plan_only, non-catalog source) is rejected 409 by confirm/discard."""

    @patch("terrapod.api.routers.catalog.run_service.confirm_run")
    @patch("terrapod.api.routers.catalog.resolve_catalog_capabilities_for")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_confirm_rejects_speculative_module_test_run(
        self, _db, _redis, _storage, mock_perm, mock_confirm
    ):
        app, mock_db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        item = MagicMock(labels={}, owner_email="")
        item.name = "vpc"
        mock_db.get = AsyncMock(side_effect=[ws, item])
        mock_perm.return_value = caps_for_level("use")
        speculative = _planned_run(source="module-test", plan_only=True)
        mock_db.execute = AsyncMock(return_value=_exec_returns(speculative))

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                f"/api/terrapod/v1/catalog-instances/ws-{ws.id}/confirm", headers=_AUTH
            )
        assert resp.status_code == 409
        mock_confirm.assert_not_awaited()


class TestManagementRenameConflict:
    """S3: renaming a provider template / catalog item to an existing name
    surfaces as 409 (IntegrityError handled), not an unhandled 500."""

    @patch("terrapod.api.routers.catalog.catalog_service.get_provider_template")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_provider_template_rename_conflict_409(self, _db, _redis, _storage, mock_get):
        from sqlalchemy.exc import IntegrityError

        app, mock_db = _make_app(_user(roles=["admin"]))
        tmpl = MagicMock()
        mock_get.return_value = tmpl
        mock_db.commit = AsyncMock(side_effect=IntegrityError("dup", {}, Exception()))
        mock_db.rollback = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/provider-templates/{uuid.uuid4()}",
                json={"data": {"attributes": {"name": "taken"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 409
        mock_db.rollback.assert_awaited()

    @patch("terrapod.api.routers.catalog.catalog_service.get_catalog_item")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_catalog_item_rename_conflict_409(self, _db, _redis, _storage, mock_get):
        from sqlalchemy.exc import IntegrityError

        app, mock_db = _make_app(_user(roles=["admin"]))
        item = MagicMock()
        mock_get.return_value = item
        mock_db.commit = AsyncMock(side_effect=IntegrityError("dup", {}, Exception()))
        mock_db.rollback = AsyncMock()

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/terrapod/v1/catalog-items/{uuid.uuid4()}",
                json={"data": {"attributes": {"name": "taken"}}},
                headers=_AUTH,
            )
        assert resp.status_code == 409
        mock_db.rollback.assert_awaited()


# ── The self-join guard on the provision path (GHSA-49q6-pm68-3xgw) ─────


class TestProvisionConsultsTheSelfJoinGuard:
    """Provisioning takes caller-supplied `labels` and needs only catalog `use` plus
    pool `write` — nowhere near platform admin — so it is a second door to the
    escalation the ordinary workspace-create path is gated against: label a
    provisioned workspace into another team's rule-assigned variable set and receive
    its secrets in the run the provision queues.

    **The guard was pinned only by `inspect.getsource(catalog)` containing
    `"refuse_varset_growth("`, and every provision test here runs on `_no_rows()`,
    which answers "nothing matched" to every query the guard asks.** So the guard
    could not fire in any test, and the only thing asserted was that a string
    appeared in the module. Deleting the whole `refuse_varset_growth` block leaves
    `test_provision_accepts_apool_prefixed_pool_id` green; so does leaving the call
    in place and breaking anything it depends on.

    These answer the guard's queries with the rows that make it fire, and assert the
    effect: a 403 naming the set, and a rolled-back transaction. The fake reads each
    statement instead of counting calls, so it cannot pass against a guard that
    asks a different question — a fake that answers from a fixture regardless of the
    query tests the fixture.
    """

    #: What the guard's three-query sequence asks, keyed on the rendered SQL.
    @staticmethod
    def _varset_rows(*, secret_name: str | None, set_id):
        """Build a query-aware `execute` for the self-join check.

        `applicable_varsets` asks for explicitly-assigned sets, then global sets,
        then rule-bearing ones, then (per rule) `SELECT EXISTS (...)` for this
        workspace; `_sets_holding_secrets` then asks which of the gained sets hold a
        `sensitive` or brokered variable, and the refusal asks for their names.
        """
        varset = MagicMock()
        varset.id = set_id
        varset.name = secret_name or "plain-set"
        varset.assignment_rule = {"labels": {"team": "a"}}
        varset.global_set = False
        varset.priority = False

        def _execute(stmt, *_a, **_kw):
            sql = " ".join(str(stmt).split())
            r = MagicMock()
            r.scalars.return_value.all.return_value = []
            r.scalar_one_or_none.return_value = None
            r.scalar.return_value = False
            r.all.return_value = []
            r.first.return_value = None

            if "variable_set_workspaces" in sql:
                pass  # no explicit assignment
            elif "variable_sets.global_set IS true" in sql:
                pass  # no global sets
            elif "variable_sets.assignment_rule IS NOT NULL" in sql:
                r.scalars.return_value.all.return_value = [varset]
            elif sql.startswith("SELECT EXISTS"):
                r.scalar.return_value = True  # the rule selects this workspace
            elif "variable_set_variables.variable_set_id" in sql:
                # Which gained sets hold something worth taking.
                r.all.return_value = [(set_id,)] if secret_name else []
            elif sql.startswith("SELECT variable_sets.name FROM"):
                r.all.return_value = [(secret_name,)] if secret_name else []
            return r

        return AsyncMock(side_effect=_execute)

    async def _provision(self, *, secret_name: str | None, roles=None):
        """Drive the real provision route with the guard's rows in place."""
        pool_uuid = uuid.uuid4()
        set_id = uuid.uuid4()

        item = MagicMock()
        item.enabled = True
        item.name = "vpc"
        item.labels = {}
        item.owner_email = ""
        item.allowed_agent_pool_ids = None

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
        ws.agent_pool_links = [SimpleNamespace(agent_pool_id=pool_uuid, ordinal=0, agent_pool=None)]
        ws.owner_email = "u@test.com"
        ws.labels = {"team": "a"}

        app, mock_db = _make_app(_user(roles=roles or ["everyone"]))
        mock_db.get = AsyncMock(return_value=pool)
        mock_db.execute = self._varset_rows(secret_name=secret_name, set_id=set_id)
        mock_db.flush = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.rollback = AsyncMock()
        mock_db.refresh = AsyncMock()

        with (
            patch("terrapod.api.app.init_db"),
            patch("terrapod.api.app.init_redis"),
            patch("terrapod.api.app.init_storage", new_callable=AsyncMock),
            patch(
                "terrapod.api.routers.catalog.catalog_service.get_catalog_item",
                AsyncMock(return_value=item),
            ),
            patch(
                "terrapod.api.routers.catalog.resolve_catalog_capabilities_for",
                AsyncMock(return_value=caps_for_level("use")),
            ),
            patch(
                "terrapod.api.routers.catalog.resolve_pool_capabilities_for",
                AsyncMock(return_value=caps_for_level("write")),
            ),
            patch(
                "terrapod.api.routers.catalog.catalog_service.provision_instance",
                AsyncMock(return_value=ws),
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.post(
                    f"/api/terrapod/v1/catalog-items/{uuid.uuid4()}/provision",
                    json={
                        "data": {
                            "attributes": {
                                "name": "smoke",
                                "agent-pool-id": f"apool-{pool_uuid}",
                                "labels": {"team": "a"},
                            }
                        }
                    },
                    headers=_AUTH,
                )
        return resp, mock_db

    async def test_a_provision_that_joins_a_secret_bearing_set_is_refused(self):
        resp, mock_db = await self._provision(secret_name="teamA-creds")
        assert resp.status_code == 403, resp.text
        assert "teamA-creds" in resp.text, "the refusal does not name the set to ask about"
        assert "GHSA-49q6" in resp.text

    async def test_and_the_provision_is_rolled_back(self):
        """The guard runs after a flush and before the commit, so a refusal that did
        not roll back would leave the workspace half-created — the router's own
        `except: rollback; raise` is the thing being driven here."""
        resp, mock_db = await self._provision(secret_name="teamA-creds")
        assert resp.status_code == 403
        mock_db.rollback.assert_awaited()
        mock_db.commit.assert_not_awaited()

    async def test_a_set_of_plain_configuration_still_provisions(self):
        """The narrowing is deliberate: the catalog is entirely non-admin
        self-service, so refusing every rule match would close the feature. Same
        rows, no secret."""
        resp, mock_db = await self._provision(secret_name=None)
        assert resp.status_code == 201, resp.text
        mock_db.commit.assert_awaited()

    async def test_a_platform_admin_is_exempt(self):
        """An admin already reads every variable set, so there is nothing to
        escalate to — and they are the only principal who can set the rule up."""
        resp, _ = await self._provision(secret_name="teamA-creds", roles=["admin", "everyone"])
        assert resp.status_code == 201, resp.text
