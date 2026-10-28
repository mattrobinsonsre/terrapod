"""Services-API tests for the catalog router (#535): feature gate, admin gating
on management endpoints, catalog-RBAC on read/use, and provision validation."""

import pathlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient, ConnectError, MockTransport, Response

from terrapod import http_retry
from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.api.routers import catalog as catalog_router
from terrapod.auth.capabilities import caps_for_level
from terrapod.config import settings
from terrapod.db.models import CatalogItem, ProviderTemplate, RegistryModule, Workspace
from terrapod.db.session import get_db
from terrapod.services import binary_cache_service, catalog_service

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
        mock_db.add = MagicMock()
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
        assert resp.json()["data"]["attributes"]["body"] is None

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


class TestCatalogSensitiveReads:
    async def test_template_list_and_show_redact_even_for_admin_and_audit(self):
        template = SimpleNamespace(
            id=uuid.uuid4(),
            name="aws",
            provider_type="aws",
            body="secret_key = 'private'",
            parameters=[
                {"name": "token", "sensitive": True, "default": "private", "options": ["private"]},
                {"name": "region", "default": "east", "options": ["east"]},
            ],
            labels={},
            owner_email="",
            created_at=None,
            updated_at=None,
        )
        for role in ("admin", "audit"):
            app, _ = _make_app(_user(roles=[role]))
            with (
                patch.object(
                    catalog_router.catalog_service,
                    "list_provider_templates",
                    AsyncMock(return_value=[template]),
                ),
                patch.object(
                    catalog_router.catalog_service,
                    "get_provider_template",
                    AsyncMock(return_value=template),
                ),
            ):
                async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                    listing = await c.get("/api/v1/provider-templates", headers=_AUTH)
                    shown = await c.get(f"/api/v1/provider-templates/{template.id}", headers=_AUTH)
                    denied = await c.get(
                        f"/api/v1/provider-templates/{template.id}?include-sensitive-values=true",
                        headers=_AUTH,
                    )
            for response in (listing, shown):
                assert response.status_code == 200, response.text
                attrs = (
                    response.json()["data"][0]["attributes"]
                    if response is listing
                    else response.json()["data"]["attributes"]
                )
                assert attrs["body"] is None
                assert attrs["parameters"][0] == {"name": "token", "sensitive": True}
                assert attrs["parameters"][1]["default"] == "east"
                assert "private" not in response.text
            if role == "admin":
                assert denied.status_code == 200
                assert denied.json()["data"]["attributes"]["body"] == template.body
                assert denied.json()["data"]["attributes"]["parameters"] == template.parameters
            else:
                assert denied.status_code == 403
        assert template.parameters[0]["default"] == "private"

    def test_template_body_without_sensitive_parameters_is_still_opaque_hcl(self):
        template = SimpleNamespace(
            id=uuid.uuid4(),
            name="aws",
            provider_type="aws",
            body='provider "aws" { token = "inline-secret" }',
            parameters=[],
            labels={},
            owner_email="",
            created_at=None,
            updated_at=None,
        )
        assert catalog_router._template_json(template)["attributes"]["body"] is None
        assert (
            catalog_router._template_json(template, include_sensitive_values=True)["attributes"][
                "body"
            ]
            == template.body
        )

    async def test_item_list_and_show_redact_module_option_and_hidden_values(self):
        module = RegistryModule(id=uuid.uuid4(), name="mod", provider="aws")
        options = [
            {"name": "module_secret", "default": "private-1", "options": ["private-1"]},
            {"name": "overlay_secret", "sensitive": True, "default": "private-2"},
            {"name": "hidden", "hidden": True, "default": "private-3"},
            {
                "name": "region",
                "default": "east",
                "options": ["east"],
                "description": "Region",
                "type": "string",
                "required": True,
                "metadata": {"default": "private-nested", "options": ["private-nested"]},
            },
        ]
        item = CatalogItem(
            id=uuid.uuid4(),
            module_id=module.id,
            module=module,
            name="example",
            variable_options=options,
            provider_template_ids=[],
            labels={},
            owner_email="u@test.com",
        )
        version = SimpleNamespace(
            inputs=[
                {"name": "module_secret", "sensitive": True},
                {"name": "region", "sensitive": False},
            ],
            interface_error=None,
        )
        for role in ("admin", "everyone"):
            app, _ = _make_app(_user(roles=[role]))
            with (
                patch.object(
                    catalog_router.catalog_service,
                    "list_catalog_items",
                    AsyncMock(return_value=[item]),
                ),
                patch.object(
                    catalog_router.catalog_service, "get_catalog_item", AsyncMock(return_value=item)
                ),
                patch.object(
                    catalog_router.catalog_service,
                    "_resolve_module_version",
                    AsyncMock(return_value=version),
                ),
                patch.object(
                    catalog_router,
                    "resolve_catalog_capabilities_for",
                    AsyncMock(return_value=caps_for_level("admin")),
                ),
            ):
                async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                    listing = await c.get("/api/v1/catalog-items", headers=_AUTH)
                    shown = await c.get(f"/api/v1/catalog-items/{item.id}", headers=_AUTH)
                    authoring = await c.get(
                        f"/api/v1/catalog-items/{item.id}?include-sensitive-values=true",
                        headers=_AUTH,
                    )
            for response in (listing, shown):
                assert response.status_code == 200, response.text
                attrs = (
                    response.json()["data"][0]["attributes"]
                    if response is listing
                    else response.json()["data"]["attributes"]
                )
                assert attrs["variable-options"] == [
                    {"name": "module_secret"},
                    {"name": "overlay_secret", "sensitive": True},
                    {"name": "hidden", "hidden": True},
                    {
                        "name": "region",
                        "description": "Region",
                        "type": "string",
                        "required": True,
                    },
                ]
                assert "private" not in response.text
            if role == "admin":
                assert authoring.status_code == 200
                assert authoring.json()["data"]["attributes"]["variable-options"] == options
            else:
                assert authoring.status_code == 403
        assert options[0]["default"] == "private-1"

    async def test_pin_switch_sensitive_to_nonsensitive_does_not_release_stored_option_values(self):
        module = RegistryModule(id=uuid.uuid4(), name="mod", provider="aws")
        options = [
            {
                "name": "token",
                "default": "stored-private",
                "options": ["stored-private"],
                "sensitive": False,
            },
            {"name": "region", "default": "east", "options": ["east", "west"]},
        ]
        item = CatalogItem(
            id=uuid.uuid4(),
            module_id=module.id,
            module=module,
            name="example",
            default_version_pin="1.0.0",
            variable_options=options,
            provider_template_ids=[],
            labels={},
            owner_email="u@test.com",
        )
        versions = {
            "1.0.0": SimpleNamespace(
                version="1.0.0",
                inputs=[{"name": "token", "sensitive": True}],
                outputs=[],
                interface_error=None,
            ),
            "2.0.0": SimpleNamespace(
                version="2.0.0",
                inputs=[
                    {"name": "token", "sensitive": False},
                    {"name": "region", "sensitive": False},
                ],
                outputs=[],
                interface_error=None,
            ),
        }

        async def resolve(_db, _module_id, pin):
            return versions[pin]

        for role in ("admin", "everyone"):
            app, _ = _make_app(_user(roles=[role]))
            with (
                patch.object(
                    catalog_router.catalog_service,
                    "list_catalog_items",
                    AsyncMock(return_value=[item]),
                ),
                patch.object(
                    catalog_router.catalog_service, "get_catalog_item", AsyncMock(return_value=item)
                ),
                patch.object(
                    catalog_router.catalog_service, "_resolve_module_version", side_effect=resolve
                ) as resolve_version,
                patch.object(
                    catalog_router,
                    "resolve_catalog_capabilities_for",
                    AsyncMock(return_value=caps_for_level("read")),
                ),
            ):
                async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                    for pin in ("1.0.0", "2.0.0"):
                        item.default_version_pin = pin
                        listing = await c.get("/api/v1/catalog-items", headers=_AUTH)
                        shown = await c.get(f"/api/v1/catalog-items/{item.id}", headers=_AUTH)
                        for response in (listing, shown):
                            assert response.status_code == 200, response.text
                            data = response.json()["data"]
                            attrs = (data[0] if response is listing else data)["attributes"]
                            assert attrs["variable-options"] == [
                                {"name": "token", "sensitive": False},
                                {"name": "region"},
                            ]
                            assert "stored-private" not in response.text
                            assert "east" not in response.text
                        resolve_version.assert_not_called()

                    form = await c.get(
                        f"/api/v1/catalog-items/{item.id}/form?version-pin=2.0.0", headers=_AUTH
                    )
                    assert form.status_code == 200, form.text
                    assert form.json()["data"]["attributes"]["resolved-version"] == "2.0.0"
                    fields = {f["name"]: f for f in form.json()["data"]["attributes"]["fields"]}
                    assert fields["region"]["default"] == "east"
                    assert fields["region"]["options"] == ["east", "west"]
                    resolve_version.assert_awaited_once_with(
                        app.dependency_overrides[get_db](), module.id, "2.0.0"
                    )

                    authoring = await c.get(
                        f"/api/v1/catalog-items/{item.id}?include-sensitive-values=true",
                        headers=_AUTH,
                    )
                    if role == "admin":
                        assert authoring.status_code == 200, authoring.text
                        assert authoring.json()["data"]["attributes"]["variable-options"] == options
                    else:
                        assert authoring.status_code == 403

    @pytest.mark.parametrize("inputs", [None, []])
    async def test_item_without_matching_interface_input_fails_closed_for_option_values(
        self, inputs
    ):
        module = RegistryModule(id=uuid.uuid4(), name="mod", provider="aws")
        item = CatalogItem(
            id=uuid.uuid4(),
            module=module,
            module_id=module.id,
            name="example",
            variable_options=[{"name": "unknown", "default": "private"}],
        )
        app, _ = _make_app(_user(roles=["admin"]))
        with (
            patch.object(
                catalog_router.catalog_service, "get_catalog_item", AsyncMock(return_value=item)
            ),
            patch.object(
                catalog_router.catalog_service,
                "_resolve_module_version",
                AsyncMock(
                    return_value=SimpleNamespace(inputs=inputs, interface_error=None)
                    if inputs is not None
                    else None
                ),
            ),
            patch.object(
                catalog_router,
                "resolve_catalog_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(f"/api/v1/catalog-items/{item.id}", headers=_AUTH)
        assert resp.status_code == 200, resp.text
        assert "private" not in resp.text


class TestSelfContainedCatalogItems:
    @pytest.mark.parametrize("self_contained", [False, True])
    async def test_create_persists_and_returns_mode(self, self_contained):
        module_id = uuid.uuid4()
        app, db = _make_app(_user(roles=["admin"]))
        db.get = AsyncMock(return_value=RegistryModule(id=module_id, name="root", provider="aws"))
        db.add = MagicMock()
        constraints = {"tofu": ">=1.12,<2"} if self_contained else None

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v1/catalog-items",
                json={
                    "data": {
                        "attributes": {
                            "name": "root",
                            "module-id": str(module_id),
                            "self-contained": self_contained,
                            "execution-backend-constraints": constraints,
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 201, resp.text
        assert resp.json()["data"]["attributes"]["self-contained"] is self_contained
        assert resp.json()["data"]["attributes"]["execution-backend-constraints"] == constraints
        item = next(
            obj for obj in db.add.call_args_list if isinstance(obj.args[0], CatalogItem)
        ).args[0]
        assert item.self_contained is self_contained
        assert item.execution_backend_constraints == constraints

    @pytest.mark.parametrize("value", ["true", 1, None])
    async def test_create_rejects_non_boolean_mode(self, value):
        app, db = _make_app(_user(roles=["admin"]))
        db.get = AsyncMock(return_value=MagicMock())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v1/catalog-items",
                json={
                    "data": {
                        "attributes": {
                            "name": "root",
                            "module-id": str(uuid.uuid4()),
                            "self-contained": value,
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 422
        db.add.assert_not_called()

    async def test_create_rejects_provider_templates_for_self_contained_item(self):
        app, db = _make_app(_user(roles=["admin"]))
        db.get = AsyncMock(return_value=MagicMock())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v1/catalog-items",
                json={
                    "data": {
                        "attributes": {
                            "name": "root",
                            "module-id": str(uuid.uuid4()),
                            "self-contained": True,
                            "provider-template-ids": [str(uuid.uuid4())],
                            "execution-backend-constraints": {"tofu": ">=1.12"},
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 422
        assert "provider templates" in resp.json()["detail"]
        db.add.assert_not_called()

    @pytest.mark.parametrize(
        "constraints",
        [{}, {"pulumi": ">=3"}, {"tofu": ">>1"}, {"tofu": 1}, []],
    )
    async def test_create_rejects_invalid_backend_constraints(self, constraints):
        app, db = _make_app(_user(roles=["admin"]))
        db.get = AsyncMock(return_value=MagicMock())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v1/catalog-items",
                json={
                    "data": {
                        "attributes": {
                            "name": "root",
                            "module-id": str(uuid.uuid4()),
                            "execution-backend-constraints": constraints,
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 422
        db.add.assert_not_called()

    async def test_self_contained_item_requires_backend_constraints(self):
        app, db = _make_app(_user(roles=["admin"]))
        db.get = AsyncMock(return_value=MagicMock())
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v1/catalog-items",
                json={
                    "data": {
                        "attributes": {
                            "name": "root",
                            "module-id": str(uuid.uuid4()),
                            "self-contained": True,
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 422

    @pytest.mark.parametrize(
        ("old_mode", "attrs", "status"),
        [
            (
                False,
                {"self-contained": True, "execution-backend-constraints": {"tofu": ">=1"}},
                200,
            ),
            (True, {"provider-template-ids": [str(uuid.uuid4())]}, 422),
            (
                False,
                {
                    "self-contained": True,
                    "provider-template-ids": [str(uuid.uuid4())],
                    "execution-backend-constraints": {"tofu": ">=1"},
                },
                422,
            ),
        ],
    )
    async def test_update_checks_combined_mode_and_templates(self, old_mode, attrs, status):
        module = RegistryModule(id=uuid.uuid4(), name="root", provider="aws")
        item = CatalogItem(
            id=uuid.uuid4(),
            name="root",
            module=module,
            module_id=module.id,
            self_contained=old_mode,
            execution_backend_constraints={"tofu": ">=1"} if old_mode else None,
            provider_template_ids=[],
            variable_options=[],
        )
        app, db = _make_app(_user(roles=["admin"]))
        with (
            patch.object(
                catalog_router.catalog_service, "get_catalog_item", AsyncMock(return_value=item)
            ),
            patch.object(
                catalog_router.catalog_service, "list_instances", AsyncMock(return_value=[])
            ),
        ):
            db.get = AsyncMock(return_value=MagicMock())
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.patch(
                    f"/api/v1/catalog-items/{item.id}",
                    json={"data": {"attributes": attrs}},
                    headers=_AUTH,
                )
        assert resp.status_code == status, resp.text
        assert item.self_contained is (status == 200 or old_mode)
        if status == 422:
            db.commit.assert_not_awaited()

    @pytest.mark.parametrize("old_mode", [False, True])
    async def test_cannot_change_mode_with_active_instances(self, old_mode):
        item = CatalogItem(
            id=uuid.uuid4(),
            name="root",
            self_contained=old_mode,
            provider_template_ids=[],
            execution_backend_constraints={"tofu": ">=1"} if old_mode else None,
        )
        app, db = _make_app(_user(roles=["admin"]))
        with (
            patch.object(
                catalog_router.catalog_service, "get_catalog_item", AsyncMock(return_value=item)
            ),
            patch.object(
                catalog_router.catalog_service,
                "list_instances",
                AsyncMock(return_value=[MagicMock()]),
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.patch(
                    f"/api/v1/catalog-items/{item.id}",
                    json={
                        "data": {
                            "attributes": {
                                "self-contained": not old_mode,
                                "execution-backend-constraints": {"tofu": ">=1"},
                            }
                        }
                    },
                    headers=_AUTH,
                )
        assert resp.status_code == 409
        assert item.self_contained is old_mode
        db.commit.assert_not_awaited()

    async def test_unrelated_patch_on_active_self_contained_item_keeps_mode(self):
        module = RegistryModule(id=uuid.uuid4(), name="root", provider="aws")
        item = CatalogItem(
            id=uuid.uuid4(),
            name="root",
            module=module,
            module_id=module.id,
            self_contained=True,
            execution_backend_constraints={"tofu": ">=1"},
            provider_template_ids=[],
        )
        app, db = _make_app(_user(roles=["admin"]))
        with (
            patch.object(
                catalog_router.catalog_service, "get_catalog_item", AsyncMock(return_value=item)
            ),
            patch.object(
                catalog_router.catalog_service,
                "list_instances",
                AsyncMock(return_value=[MagicMock()]),
            ) as list_instances,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.patch(
                    f"/api/v1/catalog-items/{item.id}",
                    json={"data": {"attributes": {"description": "updated"}}},
                    headers=_AUTH,
                )
        assert resp.status_code == 200, resp.text
        assert item.self_contained is True
        assert item.description == "updated"
        list_instances.assert_not_awaited()


# ── Provision validation ───────────────────────────────────────────────


class TestProvision:
    @pytest.mark.parametrize("failure", ["transport", "status"])
    async def test_version_index_outage_does_not_expose_url_in_503_or_retry_logs(
        self, monkeypatch, failure
    ):
        url = "https://user:password@mirror.example/index.json?token=index-secret"
        monkeypatch.setattr(settings.registry, "cache_only", False)
        monkeypatch.setattr(settings.registry.binary_cache, "tofu_version_index_url", url)
        item = MagicMock()
        item.id = uuid.uuid4()
        item.module = SimpleNamespace(id=uuid.uuid4())
        item.enabled = True
        item.name = "root"
        item.labels = {}
        item.owner_email = ""
        item.allowed_agent_pool_ids = None
        item.execution_backend_constraints = {"tofu": ">=1.12"}
        pool_id = uuid.uuid4()
        pool = MagicMock(id=pool_id, name="pool", labels={}, owner_email=None)
        app, db = _make_app(_user())
        db.get = AsyncMock(return_value=pool)
        db.add = MagicMock()
        attempts = []

        def upstream(request):
            attempts.append(request)
            if failure == "transport":
                raise ConnectError(f"index unavailable at {request.url}", request=request)
            return Response(503, request=request)

        with (
            patch.object(catalog_service, "get_catalog_item", AsyncMock(return_value=item)),
            patch.object(
                catalog_router,
                "resolve_catalog_capabilities_for",
                AsyncMock(return_value=caps_for_level("use")),
            ),
            patch.object(
                catalog_router,
                "resolve_pool_capabilities_for",
                AsyncMock(return_value=caps_for_level("write")),
            ),
            patch("terrapod.redis.client.get_redis_client", side_effect=RuntimeError("no cache")),
            patch.object(
                binary_cache_service.httpx,
                "AsyncClient",
                side_effect=lambda **kwargs: AsyncClient(transport=MockTransport(upstream)),
            ),
            patch.object(http_retry, "_backoff_seconds", return_value=0),
            patch.object(http_retry.logger, "warning") as retry_log,
            patch.object(catalog_service.logger, "warning") as catalog_log,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as client:
                resp = await client.post(
                    f"/api/v1/catalog-items/{item.id}/provision",
                    json={
                        "data": {
                            "attributes": {
                                "name": "instance",
                                "agent-pool-id": str(pool_id),
                                "execution-backend": "tofu",
                                "engine-version": "1.12",
                            }
                        }
                    },
                    headers=_AUTH,
                )

        assert len(attempts) == http_retry.DEFAULT_RETRIES + 1
        assert all(str(request.url) == url for request in attempts)
        assert resp.status_code == 503
        body = resp.json()
        assert body["detail"] == "Could not check available tofu versions; retry shortly"
        assert body["errors"][0]["detail"] == body["detail"]
        assert len(retry_log.call_args_list) == http_retry.DEFAULT_RETRIES
        catalog_log.assert_called_once()
        for text in (resp.text, str(retry_log.call_args_list), str(catalog_log.call_args_list)):
            assert url not in text
            assert "password" not in text
            assert "index-secret" not in text
        db.add.assert_not_called()

    @pytest.mark.parametrize(
        ("pin_attr", "expected_pin"),
        [({}, "1.2.0"), ({"version-pin": None}, None)],
    )
    @patch("terrapod.api.routers.catalog.catalog_service.provision_instance")
    @patch("terrapod.api.routers.catalog.resolve_pool_capabilities_for")
    @patch("terrapod.api.routers.catalog.resolve_catalog_capabilities_for")
    @patch("terrapod.api.routers.catalog.catalog_service.get_catalog_item")
    @patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
    @patch("terrapod.api.app.init_redis")
    @patch("terrapod.api.app.init_db")
    async def test_provision_accepts_apool_prefixed_pool_id(
        self,
        _db,
        _redis,
        _storage,
        mock_get,
        mock_cat,
        mock_pool,
        mock_prov,
        pin_attr,
        expected_pin,
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
        item.default_version_pin = "1.2.0"
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
        ws.catalog_update_block = None
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
                            "execution-backend": "terraform",
                            "engine-version": "1.9.8",
                            **pin_attr,
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 201
        # The service received the bare UUID (prefix stripped).
        assert mock_prov.await_args.kwargs["agent_pool_id"] == pool_uuid
        assert mock_prov.await_args.kwargs["execution_backend"] == "terraform"
        assert mock_prov.await_args.kwargs["engine_version"] == "1.9.8"
        assert mock_prov.await_args.kwargs["version_pin"] == expected_pin

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
    ws.catalog_version_pin = None
    ws.catalog_update_block = None
    return ws


def _catalog_cv_result(created_at):
    result = MagicMock()
    result.scalar_one_or_none.return_value = created_at
    return result


class TestInstanceLifecycle:
    async def test_blocked_publish_is_visible_on_instance_and_list(self):
        app, db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        ws.catalog_update_block = {"version": "2.0.0", "reason": "inputs"}
        ws.name = "instance"
        ws.owner_email = "u@test.com"
        ws.labels = {}
        ws.agent_pool_links = []
        ws.catalog_input_values = {"old": "value"}
        item = MagicMock(id=ws.catalog_item_id, name="vpc", labels={}, owner_email="")
        item.variable_options = []
        ws.created_at = datetime(2026, 1, 2, tzinfo=UTC)
        item.updated_at = datetime(2026, 1, 1, tzinfo=UTC)
        item.self_contained = False
        item.module_id = uuid.uuid4()
        item.provider_template_ids = []
        db.get = AsyncMock(side_effect=[ws, item, item])
        rows = MagicMock()
        rows.all.return_value = [("old", False, "static")]
        db.execute = AsyncMock(
            side_effect=[_catalog_cv_result(datetime(2026, 1, 2, tzinfo=UTC)), rows]
        )
        permissions = AsyncMock(return_value=caps_for_level("read"))
        version = SimpleNamespace(
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            inputs=[{"name": "old", "sensitive": False}],
            interface_error=None,
        )
        with (
            patch.object(catalog_router, "resolve_catalog_capabilities_for", permissions),
            patch.object(
                catalog_router.catalog_service,
                "_resolve_module_version",
                AsyncMock(return_value=version),
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                detail = await c.get(f"/api/v1/catalog-instances/{ws.id}", headers=_AUTH)
        assert detail.status_code == 200
        assert (
            detail.json()["data"]["attributes"]["catalog-update-block"] == ws.catalog_update_block
        )
        assert detail.json()["data"]["attributes"]["input-values"] == {"old": "value"}

        with (
            patch.object(
                catalog_router.catalog_service, "get_catalog_item", AsyncMock(return_value=item)
            ),
            patch.object(
                catalog_router.catalog_service, "list_instances", AsyncMock(return_value=[ws])
            ),
            patch.object(catalog_router, "resolve_catalog_capabilities_for", permissions),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                listing = await c.get(f"/api/v1/catalog-items/{item.id}/instances", headers=_AUTH)
        assert listing.status_code == 200
        assert (
            listing.json()["data"][0]["attributes"]["catalog-update-block"]
            == ws.catalog_update_block
        )

    async def test_legacy_instance_snapshot_filters_hidden_sensitive_and_unclassified_values(self):
        app, db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        ws.name, ws.owner_email, ws.labels, ws.agent_pool_links = "instance", "", {}, []
        ws.created_at = datetime(2026, 1, 2, tzinfo=UTC)
        ws.catalog_input_values = {
            "safe": "public",
            "sensitive": "private-1",
            "hidden": "private-2",
            "overlay": "private-3",
            "missing": "private-4",
            "brokered": "private-5",
        }
        item = SimpleNamespace(
            id=ws.catalog_item_id,
            name="vpc",
            labels={},
            owner_email="",
            variable_options=[
                {"name": "hidden", "hidden": True, "default": "private-2"},
                {"name": "overlay", "sensitive": True, "default": "private-3"},
            ],
            self_contained=False,
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            module_id=uuid.uuid4(),
            provider_template_ids=[],
        )
        db.get = AsyncMock(side_effect=[ws, item, item])
        rows = MagicMock()
        rows.all.return_value = [
            ("safe", False, "static"),
            ("sensitive", True, "static"),
            ("hidden", False, "static"),
            ("overlay", False, "static"),
            ("brokered", False, "vault"),
        ]
        db.execute = AsyncMock(
            side_effect=[_catalog_cv_result(datetime(2026, 1, 2, tzinfo=UTC)), rows]
        )
        version = SimpleNamespace(
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            inputs=[{"name": name, "sensitive": False} for name in ws.catalog_input_values],
            interface_error=None,
        )
        with (
            patch.object(
                catalog_router,
                "resolve_catalog_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
            patch.object(
                catalog_router.catalog_service,
                "_resolve_module_version",
                AsyncMock(return_value=version),
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(f"/api/v1/catalog-instances/{ws.id}", headers=_AUTH)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["input-values"] == {"safe": "public"}
        assert "private" not in resp.text
        assert ws.catalog_input_values["hidden"] == "private-2"

    async def test_instance_snapshot_classifies_native_and_legacy_terraform_variables(self):
        app, db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        ws.name, ws.owner_email, ws.labels, ws.agent_pool_links = "instance", "", {}, []
        ws.created_at = datetime(2026, 1, 2, tzinfo=UTC)
        ws.catalog_input_values = {
            "native": "public",
            "legacy": "public-old",
            "duplicate": "private",
            "other-category": "private-other",
            "missing": "private-missing",
        }
        item = SimpleNamespace(
            id=ws.catalog_item_id,
            name="vpc",
            labels={},
            owner_email="",
            updated_at=ws.created_at - timedelta(days=1),
            self_contained=False,
            variable_options=[],
            module_id=uuid.uuid4(),
            provider_template_ids=[],
        )
        db.get = AsyncMock(side_effect=[ws, item, item])
        rows = MagicMock()
        rows.all.return_value = [
            ("native", False, "static"),
            ("legacy", False, "static"),
            ("duplicate", False, "static"),
            ("duplicate", True, "static"),
        ]

        async def execute(stmt):
            if "FROM configuration_versions" in str(stmt):
                sql = str(stmt)
                assert "configuration_versions.source" in sql
                assert "configuration_versions.status" in sql
                assert "configuration_versions.speculative IS false" in sql
                assert "configuration_versions.created_at DESC" in sql
                assert "configuration_versions.id DESC" in sql
                assert "catalog" in stmt.compile().params.values()
                assert "uploaded" in stmt.compile().params.values()
                return _catalog_cv_result(datetime(2026, 1, 2, tzinfo=UTC))
            assert "variables.category IN" in str(stmt)
            assert any(
                set(v) == {"native", "terraform"}
                for v in stmt.compile().params.values()
                if isinstance(v, (list, tuple))
            )
            return rows

        db.execute = AsyncMock(side_effect=execute)
        version = SimpleNamespace(
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            inputs=[{"name": name, "sensitive": False} for name in ws.catalog_input_values],
            interface_error=None,
        )
        with (
            patch.object(
                catalog_router,
                "resolve_catalog_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
            patch.object(
                catalog_router.catalog_service,
                "_resolve_module_version",
                AsyncMock(return_value=version),
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(f"/api/v1/catalog-instances/{ws.id}", headers=_AUTH)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["input-values"] == {
            "native": "public",
            "legacy": "public-old",
        }
        assert "private" not in resp.text

    @pytest.mark.parametrize(
        "case", ["pinned", "floating", "missing", "unknown", "unavailable", "changed"]
    )
    async def test_self_contained_legacy_snapshot_fails_closed_when_unclassifiable(self, case):
        app, db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        ws.name, ws.owner_email, ws.labels, ws.agent_pool_links = "instance", "", {}, []
        ws.created_at = datetime(2026, 1, 2, tzinfo=UTC)
        ws.catalog_version_pin = None if case == "floating" else "1.0.0"
        ws.catalog_input_values = {
            "safe": "public",
            "secret": "private",
            "unknown": "private-unknown",
        }
        item = SimpleNamespace(
            id=ws.catalog_item_id,
            name="root",
            labels={},
            owner_email="",
            module_id=uuid.uuid4(),
            self_contained=True,
            variable_options=[],
            provider_template_ids=[],
            updated_at=ws.created_at + timedelta(days=2)
            if case == "changed"
            else ws.created_at - timedelta(days=1),
        )
        db.get = AsyncMock(side_effect=[ws, item, item])
        rows = MagicMock()
        rows.all.return_value = [
            ("safe", False, "static"),
            ("secret", False, "static"),
            ("unknown", False, "static"),
        ]
        db.execute = AsyncMock(
            side_effect=[_catalog_cv_result(ws.created_at + timedelta(days=1)), rows]
        )
        interface = {
            "inputs": [
                {"name": "safe", "sensitive": False},
                {"name": "secret", "sensitive": True},
            ],
            "error": None,
        }
        if case == "unknown":
            interface = {"inputs": None, "error": "unreadable"}
        with (
            patch.object(
                catalog_router,
                "resolve_catalog_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
            patch.object(
                catalog_router.catalog_service,
                "_resolve_module_version",
                AsyncMock(
                    return_value=None
                    if case == "missing"
                    else SimpleNamespace(
                        version="1.0.0", updated_at=ws.created_at - timedelta(days=1)
                    )
                ),
            ) as resolve,
            patch.object(
                catalog_router,
                "_catalog_version_interface",
                AsyncMock(side_effect=HTTPException(status_code=503, detail="archive busy"))
                if case == "unavailable"
                else AsyncMock(return_value=interface),
            ) as archive,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(f"/api/v1/catalog-instances/{ws.id}", headers=_AUTH)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["input-values"] == (
            {"safe": "public"} if case == "pinned" else {}
        )
        assert "private" not in resp.text
        if case in ("changed", "floating"):
            resolve.assert_not_awaited()
            archive.assert_not_awaited()

    async def test_item_change_withholds_old_snapshot_even_if_variables_now_look_safe(self):
        app, db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        ws.name, ws.owner_email, ws.labels, ws.agent_pool_links = "instance", "", {}, []
        ws.catalog_input_values = {"formerly_hidden": "private"}
        ws.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        item = SimpleNamespace(
            id=ws.catalog_item_id,
            name="root",
            labels={},
            owner_email="",
            self_contained=False,
            variable_options=[],
            updated_at=ws.created_at + timedelta(days=1),
        )
        db.get = AsyncMock(side_effect=[ws, item, item])
        db.execute.return_value = _catalog_cv_result(ws.created_at)
        with patch.object(
            catalog_router,
            "resolve_catalog_capabilities_for",
            AsyncMock(return_value=caps_for_level("read")),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(f"/api/v1/catalog-instances/{ws.id}", headers=_AUTH)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["input-values"] == {}
        db.execute.assert_awaited_once()

    @pytest.mark.parametrize(
        "template_state", ["current", "changed", "missing", "unknown", "invalid"]
    )
    async def test_template_sensitive_change_after_catalog_cv_cannot_expose_old_snapshot(
        self, template_state
    ):
        app, db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        ws.name, ws.owner_email, ws.labels, ws.agent_pool_links = "instance", "", {}, []
        ws.catalog_input_values = {"safe": "public", "region": "east", "token": "private"}
        cv_time = datetime(2026, 1, 3, tzinfo=UTC)
        stable_id, template_id = uuid.uuid4(), uuid.uuid4()
        item = SimpleNamespace(
            id=ws.catalog_item_id,
            name="module",
            labels={},
            owner_email="",
            self_contained=False,
            variable_options=[],
            provider_template_ids=[stable_id, template_id],
            module_id=uuid.uuid4(),
            updated_at=cv_time - timedelta(days=2),
        )
        template = SimpleNamespace(
            updated_at=None
            if template_state == "unknown"
            else cv_time + timedelta(days=1)
            if template_state == "changed"
            else cv_time - timedelta(days=1),
            # The old materialisation used sensitive=false for token.
            parameters=[
                {"name": "token", "sensitive": True},
                {"name": "region", "sensitive": False},
            ],
        )
        if template_state == "invalid":
            template.parameters[0]["sensitive"] = "false"

        async def get(model, key):
            if model is Workspace:
                return ws
            if model is CatalogItem:
                return item
            if model is ProviderTemplate and key == stable_id:
                return SimpleNamespace(updated_at=cv_time - timedelta(days=1), parameters=[])
            if model is ProviderTemplate and key == template_id:
                return None if template_state == "missing" else template
            return None

        db.get = AsyncMock(side_effect=get)
        variable_rows = MagicMock()
        variable_rows.all.return_value = [
            (name, False, "static") for name in ws.catalog_input_values
        ]
        db.execute = (
            AsyncMock(side_effect=[_catalog_cv_result(cv_time), variable_rows])
            if template_state == "current"
            else AsyncMock(return_value=_catalog_cv_result(cv_time))
        )
        version = SimpleNamespace(
            inputs=[{"name": "safe", "sensitive": False}],
            interface_error=None,
            updated_at=cv_time - timedelta(days=1),
        )
        with (
            patch.object(
                catalog_router,
                "resolve_catalog_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
            patch.object(
                catalog_router.catalog_service,
                "_resolve_module_version",
                AsyncMock(return_value=version),
            ) as resolve,
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(f"/api/v1/catalog-instances/{ws.id}", headers=_AUTH)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["input-values"] == (
            {"safe": "public", "region": "east"} if template_state == "current" else {}
        )
        assert "private" not in resp.text
        db.get.assert_any_await(ProviderTemplate, stable_id)
        db.get.assert_any_await(ProviderTemplate, template_id)
        if template_state != "current":
            resolve.assert_not_awaited()
            assert db.execute.await_count == 1

    async def test_reconfigure_catalog_cv_reenables_safe_snapshot_after_item_edit(self):
        app, db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        ws.name, ws.owner_email, ws.labels, ws.agent_pool_links = "instance", "", {}, []
        ws.created_at = datetime(2026, 1, 1, tzinfo=UTC)
        ws.catalog_input_values = {"safe": "public"}
        item = SimpleNamespace(
            id=ws.catalog_item_id,
            name="module",
            labels={},
            owner_email="",
            self_contained=False,
            variable_options=[],
            provider_template_ids=[],
            module_id=uuid.uuid4(),
            updated_at=ws.created_at + timedelta(days=1),
        )
        db.get = AsyncMock(side_effect=lambda model, _key: ws if model is Workspace else item)
        snapshot_time = ws.created_at
        variables = MagicMock()
        variables.all.return_value = [("safe", False, "static")]

        async def execute(stmt):
            if "FROM configuration_versions" in str(stmt):
                return _catalog_cv_result(snapshot_time)
            return variables

        db.execute = AsyncMock(side_effect=execute)
        version = SimpleNamespace(
            inputs=[{"name": "safe", "sensitive": False}],
            interface_error=None,
            updated_at=ws.created_at,
        )
        with (
            patch.object(
                catalog_router,
                "resolve_catalog_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
            patch.object(
                catalog_router.catalog_service,
                "_resolve_module_version",
                AsyncMock(return_value=version),
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                before = await c.get(f"/api/v1/catalog-instances/{ws.id}", headers=_AUTH)
                snapshot_time = item.updated_at + timedelta(days=1)
                after = await c.get(f"/api/v1/catalog-instances/{ws.id}", headers=_AUTH)
        assert before.json()["data"]["attributes"]["input-values"] == {}
        assert after.json()["data"]["attributes"]["input-values"] == {"safe": "public"}
        assert db.execute.await_count == 3  # old CV, new CV, then its variables

    async def test_missing_catalog_cv_withholds_even_apparently_safe_snapshot(self):
        app, db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        ws.name, ws.owner_email, ws.labels, ws.agent_pool_links = "instance", "", {}, []
        ws.catalog_input_values = {"safe": "private"}
        item = SimpleNamespace(
            id=ws.catalog_item_id,
            name="module",
            labels={},
            owner_email="",
            updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db.get = AsyncMock(side_effect=[ws, item, item])
        db.execute.return_value = _catalog_cv_result(None)
        with patch.object(
            catalog_router,
            "resolve_catalog_capabilities_for",
            AsyncMock(return_value=caps_for_level("read")),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(f"/api/v1/catalog-instances/{ws.id}", headers=_AUTH)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["input-values"] == {}
        db.execute.assert_awaited_once()

    async def test_module_metadata_newer_than_catalog_cv_withholds_wrapper_snapshot(self):
        app, db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        ws.name, ws.owner_email, ws.labels, ws.agent_pool_links = "instance", "", {}, []
        ws.catalog_input_values = {"safe": "private"}
        cv_time = datetime(2026, 1, 2, tzinfo=UTC)
        item = SimpleNamespace(
            id=ws.catalog_item_id,
            name="module",
            labels={},
            owner_email="",
            self_contained=False,
            variable_options=[],
            provider_template_ids=[],
            module_id=uuid.uuid4(),
            updated_at=cv_time - timedelta(days=1),
        )
        db.get = AsyncMock(side_effect=[ws, item, item])
        variables = MagicMock()
        variables.all.return_value = [("safe", False, "static")]
        db.execute = AsyncMock(side_effect=[_catalog_cv_result(cv_time), variables])
        version = SimpleNamespace(
            inputs=[{"name": "safe", "sensitive": False}],
            interface_error=None,
            updated_at=cv_time + timedelta(days=1),
        )
        with (
            patch.object(
                catalog_router,
                "resolve_catalog_capabilities_for",
                AsyncMock(return_value=caps_for_level("read")),
            ),
            patch.object(
                catalog_router.catalog_service,
                "_resolve_module_version",
                AsyncMock(return_value=version),
            ),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
                resp = await c.get(f"/api/v1/catalog-instances/{ws.id}", headers=_AUTH)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["attributes"]["input-values"] == {}

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
                            "execution-backend": "terraform",
                            "engine-version": "1.9.8",
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert resp.json()["data"]["type"] == "runs"
        assert mock_reconfig.await_args.kwargs["version_pin"] == "1.2.0"
        assert mock_reconfig.await_args.kwargs["auto_apply"] is True
        assert mock_reconfig.await_args.kwargs["input_values_implicit"] is False
        assert mock_reconfig.await_args.kwargs["execution_backend"] == "terraform"
        assert mock_reconfig.await_args.kwargs["engine_version"] == "1.9.8"

    @patch("terrapod.api.routers.catalog.catalog_service.reconfigure_instance")
    @patch("terrapod.api.routers.catalog.resolve_catalog_capabilities_for")
    async def test_omitted_inputs_reuse_saved_values_for_selected_version(
        self, mock_perm, mock_reconfig
    ):
        app, db = _make_app(_user(roles=["everyone"]))
        ws = _catalog_ws()
        ws.catalog_version_pin = None
        ws.catalog_input_values = {"kept": "saved", "removed": "old"}
        item = MagicMock(name="vpc", labels={}, owner_email="")
        db.get = AsyncMock(side_effect=[ws, item])
        mock_perm.return_value = caps_for_level("use")
        run = MagicMock(id=uuid.uuid4(), status="queued", is_destroy=False)
        mock_reconfig.return_value = run

        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.patch(
                f"/api/v1/catalog-instances/ws-{ws.id}",
                json={"data": {"attributes": {"version-pin": None}}},
                headers=_AUTH,
            )
        assert resp.status_code == 200
        assert mock_reconfig.await_args.kwargs["input_values"] == ws.catalog_input_values
        assert mock_reconfig.await_args.kwargs["input_values_implicit"] is True
        assert mock_reconfig.await_args.kwargs["version_pin"] is None

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

    @pytest.mark.parametrize("value", ["true", "false", 1, 0, None, []])
    async def test_sensitive_flag_must_be_boolean(self, value):
        with pytest.raises(HTTPException) as ei:
            await catalog_router._coerce_item(
                AsyncMock(),
                {"variable-options": [{"name": "token", "sensitive": value}]},
                on_create=False,
            )
        assert ei.value.status_code == 422
        assert "sensitive" in ei.value.detail

    @pytest.mark.parametrize("value", [True, False])
    async def test_boolean_sensitive_flag_is_preserved(self, value):
        options = [{"name": "token", "sensitive": value}]
        fields = await catalog_router._coerce_item(
            AsyncMock(), {"variable-options": options}, on_create=False
        )
        assert fields["variable_options"] == options


class TestProviderParameterValidation:
    @pytest.mark.parametrize("value", ["false", 1, None, []])
    def test_sensitive_flag_must_be_boolean(self, value):
        with pytest.raises(HTTPException) as ei:
            catalog_router._coerce_template(
                {"parameters": [{"name": "token", "sensitive": value}]}, on_create=False
            )
        assert ei.value.status_code == 422

    @pytest.mark.parametrize("value", [True, False])
    def test_boolean_sensitive_flag_is_preserved(self, value):
        params = [{"name": "token", "sensitive": value}]
        fields = catalog_router._coerce_template({"parameters": params}, on_create=False)
        assert fields["parameters"] == params

    async def test_api_rejects_non_boolean_parameter_on_create(self):
        app, db = _make_app(_user(roles=["admin"]))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v1/provider-templates",
                json={
                    "data": {
                        "attributes": {
                            "name": "aws",
                            "provider-type": "aws",
                            "body": "provider {}",
                            "parameters": [{"name": "token", "sensitive": "false"}],
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 422
        db.add.assert_not_called()


class TestCatalogOptionSensitiveWrite:
    async def test_api_rejects_non_boolean_option_on_create(self):
        app, db = _make_app(_user(roles=["admin"]))
        module_id = uuid.uuid4()
        db.get = AsyncMock(return_value=RegistryModule(id=module_id, name="root", provider="aws"))
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.post(
                "/api/v1/catalog-items",
                json={
                    "data": {
                        "attributes": {
                            "name": "root",
                            "module-id": str(module_id),
                            "variable-options": [{"name": "token", "sensitive": "false"}],
                        }
                    }
                },
                headers=_AUTH,
            )
        assert resp.status_code == 422
        db.add.assert_not_called()


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
        item.self_contained = False
        item.provider_template_ids = []
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
        ws.catalog_update_block = None
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


class TestTheAnsibleHalfOnACatalogItem:
    """The four attributes an item declares its Ansible half with (#2019).

    Validated and stored byte for byte, never normalised: a provider writes the
    server's response back into state, so a trimmed or lower-cased value fails
    the apply with "Provider produced inconsistent result after apply" -- the
    v1.9.0 advisory batch in miniature.
    """

    async def _coerce(self, attrs, *, on_create=True, current=None):
        # A bare AsyncMock db is enough: the only lookup `_coerce_item` makes is
        # `db.get(RegistryModule, ...)`, which on a mock returns non-None.
        from terrapod.api.routers import catalog as cat

        return await cat._coerce_item(
            AsyncMock(),
            {"module-id": str(uuid.uuid4()), "name": "x", **attrs},
            on_create=on_create,
            current=current,
        )

    # -- stored verbatim ---------------------------------------------------

    @pytest.mark.parametrize(
        "path", ["ansible", "deploy/ansible", "a/b/c", "Ansible-Prod", "ansible.d"]
    )
    async def test_an_accepted_path_is_stored_unchanged(self, path) -> None:
        out = await self._coerce({"configure-enabled": True, "ansible-directory": path})
        assert out["ansible_directory"] == path

    @pytest.mark.parametrize(
        "bad", ["/ansible", "ansible/", "../ansible", "a/../b", " ansible", "ansible "]
    )
    async def test_a_refused_path_is_refused_not_normalised(self, bad) -> None:
        with pytest.raises(HTTPException) as e:
            await self._coerce({"configure-enabled": True, "ansible-directory": bad})
        assert e.value.status_code == 422

    async def test_a_non_string_path_is_422_not_500(self) -> None:
        """A sibling attribute answering 500 where every other answers 422 is
        the shape v1.9.0 shipped on `owner-email`."""
        with pytest.raises(HTTPException) as e:
            await self._coerce({"ansible-directory": 7})
        assert e.value.status_code == 422

    async def test_a_stringified_boolean_is_refused(self) -> None:
        """With a DIRECTORY supplied, and asserting the MESSAGE.

        Both matter. Without the directory the cross-field invariant fires
        instead -- `"true"` is truthy, so it refuses for a missing directory and
        the test passes with the type check deleted. The mutation check caught
        exactly that: a status code alone cannot tell two refusals apart when
        more than one can fire.
        """
        with pytest.raises(HTTPException) as e:
            await self._coerce({"configure-enabled": "true", "ansible-directory": "ansible"})
        assert e.value.status_code == 422
        assert "boolean" in str(e.value.detail)

    # -- the cross-field invariant ----------------------------------------

    async def test_enabled_with_no_directory_is_refused(self) -> None:
        """Incoherent rather than a no-op: it would provision a workspace whose
        Ansible root is the configuration version's root, where only the
        generated wrapper lives."""
        with pytest.raises(HTTPException) as e:
            await self._coerce({"configure-enabled": True})
        assert e.value.status_code == 422
        assert "ansible-directory" in str(e.value.detail)

    async def test_a_directory_with_the_switch_off_is_accepted(self) -> None:
        """The path says where and the switch says whether, so a path with the
        switch off is an item not yet turned on -- not an error."""
        out = await self._coerce({"configure-enabled": False, "ansible-directory": "ansible"})
        assert out["configure_enabled"] is False
        assert out["ansible_directory"] == "ansible"

    async def test_the_invariant_reads_the_RESULTING_values_on_a_patch(self) -> None:
        """A PATCH turning the switch on, against a stored directory it does not
        restate, must be ACCEPTED -- and the same PATCH against a stored empty
        directory refused. Submitted-values-only would accept both."""
        stored = SimpleNamespace(configure_enabled=False, ansible_directory="ansible")
        out = await self._coerce({"configure-enabled": True}, on_create=False, current=stored)
        assert out["configure_enabled"] is True

        empty = SimpleNamespace(configure_enabled=False, ansible_directory="")
        with pytest.raises(HTTPException) as e:
            await self._coerce({"configure-enabled": True}, on_create=False, current=empty)
        assert e.value.status_code == 422

    async def test_an_untouched_patch_revalidates_nothing(self) -> None:
        """A row written by a migration must stay editable (#316): a PATCH that
        does not mention these must not be refused over a stored value."""
        stored = SimpleNamespace(configure_enabled=True, ansible_directory="ansible")
        out = await self._coerce({"labels": {}}, on_create=False, current=stored)
        assert "ansible_directory" not in out
        assert "configure_enabled" not in out

    # -- the call site, which no test of the helper can see ----------------

    def test_the_update_route_passes_the_CURRENT_row(self) -> None:
        """The cross-field invariant reads stored values, so the route must hand
        them over. A helper test cannot see this: `current` defaults to None, so
        with it omitted every PATCH would compare against "no stored directory"
        and refuse a legitimate switch-on.
        """
        from terrapod.api.routers import catalog as cat

        src = pathlib.Path(cat.__file__).read_text(encoding="utf-8")
        calls = [
            ln.strip() for ln in src.splitlines() if "_coerce_item(" in ln and "def " not in ln
        ]
        update_calls = [c for c in calls if "on_create=False" in c]
        assert update_calls, calls
        for c in update_calls:
            assert "current=" in c, c
