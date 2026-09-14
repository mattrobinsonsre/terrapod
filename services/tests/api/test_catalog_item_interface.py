"""GET /catalog-items/{id}/interface (#1585).

A catalog item's module interface: the inputs and outputs of the module version
the item resolves to — its pin, or the latest uploaded version — derived from
the registry, never stored on the item. Reading it needs catalog read on the
item, like the rest of the item's read surface.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.auth import capabilities as cap
from terrapod.config import settings
from terrapod.db.session import get_db

_BASE = "http://test"
_AUTH = {"Authorization": "Bearer dummy"}

INPUTS = [
    {
        "name": "cidr",
        "type": "string",
        "description": "VPC CIDR block",
        "default": None,
        "required": True,
        "sensitive": False,
    },
    {
        "name": "tags",
        "type": "map(string)",
        "description": "",
        "default": "{}",
        "required": False,
        "sensitive": False,
    },
]
OUTPUTS = [{"name": "vpc_id", "description": "The VPC's ID", "sensitive": False}]

CAN_READ = frozenset({cap.CATALOG_READ})


@pytest.fixture(autouse=True)
def _enable_catalog():
    original = settings.catalog.enabled
    settings.catalog.enabled = True
    yield
    settings.catalog.enabled = original


def _app():
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
        email="u@test.com",
        display_name="U",
        roles=["everyone"],
        provider_name="local",
        auth_method="session",
    )
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    return app


def _item(pin=None):
    item = MagicMock()
    item.id = uuid.uuid4()
    item.name = "vpc"
    item.labels = {}
    item.owner_email = "owner@test.com"
    item.module_id = uuid.uuid4()
    item.default_version_pin = pin
    return item


def _version(v="1.2.0"):
    mv = MagicMock()
    mv.version = v
    mv.inputs = INPUTS
    mv.outputs = OUTPUTS
    return mv


async def _get(item_id):
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
        return await c.get(f"/api/terrapod/v1/catalog-items/{item_id}/interface", headers=_AUTH)


_R = "terrapod.api.routers.catalog"


@patch(f"{_R}.catalog_service._resolve_module_version", new_callable=AsyncMock)
@patch(f"{_R}.resolve_catalog_capabilities_for", new_callable=AsyncMock)
@patch(f"{_R}.catalog_service.get_catalog_item", new_callable=AsyncMock)
@patch("terrapod.api.app.init_storage", new_callable=AsyncMock)
@patch("terrapod.api.app.init_redis")
@patch("terrapod.api.app.init_db")
class TestCatalogItemInterface:
    async def test_returns_the_pinned_versions_inputs_and_outputs(
        self, _db, _redis, _storage, get_item, caps, resolve
    ):
        item = _item(pin="1.2.0")
        get_item.return_value, caps.return_value, resolve.return_value = item, CAN_READ, _version()

        resp = await _get(item.id)

        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["type"] == "catalog-item-interfaces"
        assert data["id"] == str(item.id)
        assert data["attributes"] == {
            "resolved-version": "1.2.0",
            "inputs": INPUTS,
            "outputs": OUTPUTS,
        }
        # Resolved the same way /form does: this module, at the item's pin.
        assert resolve.await_args.args[1:] == (item.module_id, "1.2.0")

    async def test_an_unpinned_item_resolves_the_latest_version(
        self, _db, _redis, _storage, get_item, caps, resolve
    ):
        item = _item(pin=None)
        get_item.return_value, caps.return_value = item, CAN_READ
        resolve.return_value = _version("2.0.1")

        resp = await _get(item.id)

        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["resolved-version"] == "2.0.1"
        assert resolve.await_args.args[1:] == (item.module_id, None)

    async def test_no_uploaded_version_gives_nulls_not_an_error(
        self, _db, _redis, _storage, get_item, caps, resolve
    ):
        item = _item()
        get_item.return_value, caps.return_value, resolve.return_value = item, CAN_READ, None

        resp = await _get(item.id)

        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"] == {
            "resolved-version": None,
            "inputs": None,
            "outputs": None,
        }

    async def test_without_catalog_read_it_is_refused(
        self, _db, _redis, _storage, get_item, caps, resolve
    ):
        get_item.return_value, caps.return_value = _item(), frozenset()

        resp = await _get(uuid.uuid4())

        assert resp.status_code == 403
        resolve.assert_not_awaited()

    async def test_an_unknown_item_is_404(self, _db, _redis, _storage, get_item, caps, resolve):
        get_item.return_value = None

        resp = await _get(uuid.uuid4())

        assert resp.status_code == 404
        resolve.assert_not_awaited()

    async def test_a_malformed_id_is_404(self, _db, _redis, _storage, get_item, caps, resolve):
        resp = await _get("not-a-uuid")

        assert resp.status_code == 404
        get_item.assert_not_awaited()

    async def test_with_the_catalog_disabled_it_is_404(
        self, _db, _redis, _storage, get_item, caps, resolve
    ):
        settings.catalog.enabled = False

        resp = await _get(uuid.uuid4())

        assert resp.status_code == 404
        get_item.assert_not_awaited()
