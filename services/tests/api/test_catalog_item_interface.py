"""GET /catalog-items/{id}/interface (#1585).

A catalog item's module interface: the inputs and outputs of the module version
the item resolves to — its pin, or the latest uploaded version — derived from
the registry, never stored on the item. Reading it needs catalog read on the
item, like the rest of the item's read surface.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from terrapod.api.app import create_application as create_app
from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.auth import capabilities as cap
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.services import catalog_service
from terrapod.storage.protocol import ObjectNotFoundError

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
    lock = MagicMock()
    lock.acquire = AsyncMock(return_value=True)
    lock.extend = AsyncMock(return_value=True)
    lock.release = AsyncMock()
    with patch("terrapod.api.routers.catalog.get_redis_client") as redis:
        redis.return_value.lock.return_value = lock
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
    item.self_contained = False
    return item


def _version(v="1.2.0", interface_error=None):
    mv = MagicMock()
    mv.version = v
    mv.inputs = INPUTS
    mv.outputs = OUTPUTS
    mv.interface_error = interface_error
    return mv


async def _get(item_id, query=""):
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
        return await c.get(
            f"/api/terrapod/v1/catalog-items/{item_id}/interface{query}", headers=_AUTH
        )


_R = "terrapod.api.routers.catalog"


async def _archive_chunks(data):
    yield data


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
            "interface-error": None,
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

    @pytest.mark.parametrize("endpoint", ["interface", "form"])
    async def test_explicit_latest_bypasses_default_pin_for_both_reads(
        self, _db, _redis, _storage, get_item, caps, resolve, endpoint
    ):
        item = _item(pin="1.2.0")
        item.provider_template_ids = []
        item.variable_options = []
        get_item.return_value, caps.return_value = item, CAN_READ
        resolve.return_value = _version("2.0.1")

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/v1/catalog-items/{item.id}/{endpoint}?version-pin=latest", headers=_AUTH
            )

        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["resolved-version"] == "2.0.1"
        assert resolve.await_args.args[1:] == (item.module_id, None)

    @pytest.mark.parametrize("endpoint", ["interface", "form"])
    async def test_selected_version_drives_the_returned_interface(
        self, _db, _redis, _storage, get_item, caps, resolve, endpoint
    ):
        item = _item(pin="1.2.0")
        item.provider_template_ids = []
        item.variable_options = []
        get_item.return_value, caps.return_value = item, CAN_READ
        resolve.return_value = _version("1.8.0")

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/v1/catalog-items/{item.id}/{endpoint}?version-pin=1.8.0", headers=_AUTH
            )

        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["resolved-version"] == "1.8.0"
        if endpoint == "form":
            assert [f["name"] for f in resp.json()["data"]["attributes"]["fields"]] == [
                "cidr",
                "tags",
            ]
        else:
            assert resp.json()["data"]["attributes"]["inputs"] == INPUTS
        assert resolve.await_args.args[1:] == (item.module_id, "1.8.0")

    @pytest.mark.parametrize("endpoint", ["interface", "form"])
    async def test_explicit_missing_version_is_an_error_not_an_empty_form(
        self, _db, _redis, _storage, get_item, caps, resolve, endpoint
    ):
        item = _item()
        get_item.return_value, caps.return_value, resolve.return_value = item, CAN_READ, None
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.get(
                f"/api/v1/catalog-items/{item.id}/{endpoint}?version-pin=9.9.9", headers=_AUTH
            )
        assert resp.status_code == 422

    async def test_selected_version_still_requires_catalog_read(
        self, _db, _redis, _storage, get_item, caps, resolve
    ):
        item = _item()
        item.self_contained = True
        get_item.return_value, caps.return_value = item, frozenset()
        resp = await _get(uuid.uuid4(), "?version-pin=1.8.0")
        assert resp.status_code == 403
        resolve.assert_not_awaited()

    async def test_busy_read_refuses_before_staging_archive(
        self, _db, _redis, _storage, get_item, caps, resolve
    ):
        item = _item()
        item.self_contained = True
        item.module = SimpleNamespace(namespace="default", name="root", provider="aws")
        get_item.return_value, caps.return_value, resolve.return_value = item, CAN_READ, _version()
        with (
            patch("terrapod.api.routers.catalog.get_redis_client") as redis,
            patch("terrapod.api.routers.catalog.catalog_service._stage_root_archive") as stage,
        ):
            redis.return_value.lock.return_value.acquire = AsyncMock(return_value=False)
            resp = await _get(item.id)
        assert resp.status_code == 503
        assert resp.headers["Retry-After"] == "1"
        stage.assert_not_called()

    async def test_unavailable_admission_does_not_stage_archive(
        self, _db, _redis, _storage, get_item, caps, resolve
    ):
        item = _item()
        item.self_contained = True
        item.module = SimpleNamespace(namespace="default", name="root", provider="aws")
        get_item.return_value, caps.return_value, resolve.return_value = item, CAN_READ, _version()
        with (
            patch(
                "terrapod.api.routers.catalog.get_redis_client", side_effect=RuntimeError("down")
            ),
            patch("terrapod.api.routers.catalog.catalog_service._stage_root_archive") as stage,
        ):
            resp = await _get(item.id)
        assert resp.status_code == 503
        stage.assert_not_called()

    async def test_concurrent_reads_share_one_cross_replica_permit(
        self, _db, _redis, _storage, get_item, caps, resolve, tmp_path, monkeypatch
    ):
        import asyncio

        monkeypatch.setattr(settings.vcs, "tmpdir", str(tmp_path))
        item = _item()
        item.self_contained = True
        item.module = SimpleNamespace(namespace="default", name="root", provider="aws")
        get_item.return_value, caps.return_value, resolve.return_value = item, CAN_READ, _version()
        started, finish = asyncio.Event(), asyncio.Event()
        lock = MagicMock()
        held = False

        async def acquire():
            nonlocal held
            if held:
                return False
            held = True
            return True

        async def release():
            nonlocal held
            held = False

        lock.acquire = AsyncMock(side_effect=acquire)
        lock.release = AsyncMock(side_effect=release)
        lock.extend = AsyncMock(return_value=True)
        archive = catalog_service._build_tarball({"main.tf": 'variable "name" {}'})
        storage = MagicMock()

        async def get_stream(_key):
            started.set()
            await finish.wait()
            yield archive

        storage.get_stream = MagicMock(side_effect=get_stream)
        with (
            patch("terrapod.api.routers.catalog.get_redis_client") as redis,
            patch("terrapod.storage.get_storage", return_value=storage),
        ):
            redis.return_value.lock.return_value = lock
            async with asyncio.TaskGroup() as group:
                first = group.create_task(_get(item.id))
                try:
                    await started.wait()
                    busy = await _get(item.id)
                    assert busy.status_code == 503
                finally:
                    finish.set()
            assert first.result().status_code == 200
            assert (await _get(item.id)).status_code == 200
        assert storage.get_stream.call_count == 2
        assert not list(tmp_path.iterdir())

    @pytest.mark.parametrize("endpoint", ["form", "interface"])
    async def test_self_contained_reads_selected_archive_not_stale_metadata(
        self, _db, _redis, _storage, get_item, caps, resolve, endpoint, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(settings.vcs, "tmpdir", str(tmp_path))
        monkeypatch.setattr(settings.registry.module_interface, "enabled", False)
        item = _item(pin="1.2.0")
        item.self_contained = True
        item.module = SimpleNamespace(
            id=item.module_id, namespace="default", name="root", provider="aws"
        )
        item.provider_template_ids = []
        item.variable_options = []
        mv = _version("2.0.1", interface_error="stale interface")
        mv.inputs = None
        mv.outputs = None
        get_item.return_value, caps.return_value, resolve.return_value = item, CAN_READ, mv
        archive = catalog_service._build_tarball(
            {
                "main.tf": 'variable "fresh" { type = string }\noutput "fresh_id" { value = var.fresh }'
            }
        )
        storage = MagicMock()
        storage.get_stream = MagicMock(side_effect=lambda _key: _archive_chunks(archive))

        with patch("terrapod.storage.get_storage", return_value=storage):
            async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
                resp = await c.get(
                    f"/api/v1/catalog-items/{item.id}/{endpoint}?version-pin=latest", headers=_AUTH
                )

        assert resp.status_code == 200, resp.text
        attrs = resp.json()["data"]["attributes"]
        assert attrs["resolved-version"] == "2.0.1"
        assert attrs["interface-error"] is None
        if endpoint == "form":
            assert [f["name"] for f in attrs["fields"]] == ["fresh"]
        else:
            assert [i["name"] for i in attrs["inputs"]] == ["fresh"]
            assert [o["name"] for o in attrs["outputs"]] == ["fresh_id"]
        storage.get_stream.assert_called_once_with("registry/modules/default/root/aws/2.0.1.tar.gz")
        assert not list(tmp_path.iterdir())

    @pytest.mark.parametrize("endpoint", ["form", "interface"])
    @pytest.mark.parametrize("failure", ["missing", "invalid", "oversized-hcl"])
    async def test_self_contained_read_rejects_missing_or_invalid_archive(
        self,
        _db,
        _redis,
        _storage,
        get_item,
        caps,
        resolve,
        endpoint,
        failure,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr(settings.vcs, "tmpdir", str(tmp_path))
        item = _item()
        item.self_contained = True
        item.module = SimpleNamespace(namespace="default", name="root", provider="aws")
        item.provider_template_ids = []
        get_item.return_value, caps.return_value, resolve.return_value = item, CAN_READ, _version()
        storage = MagicMock()
        if failure == "missing":

            async def missing(key):
                raise ObjectNotFoundError(key)
                yield b""  # pragma: no cover

            storage.get_stream = MagicMock(side_effect=missing)
        else:
            if failure == "oversized-hcl":
                monkeypatch.setattr(catalog_service, "_MAX_ROOT_HCL_BYTES", 1)
                archive = catalog_service._build_tarball({"main.tf": 'variable "name" {}'})
            else:
                archive = b"not a tar"
            storage.get_stream = MagicMock(side_effect=lambda _key: _archive_chunks(archive))
        with patch("terrapod.storage.get_storage", return_value=storage):
            async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
                resp = await c.get(f"/api/v1/catalog-items/{item.id}/{endpoint}", headers=_AUTH)
        assert resp.status_code == (413 if failure == "oversized-hcl" else 422)
        assert ("Root HCL exceeds" if failure == "oversized-hcl" else "archive") in resp.json()[
            "detail"
        ]
        assert not list(tmp_path.iterdir())

    async def test_self_contained_read_with_no_uploaded_version_is_not_an_empty_form(
        self, _db, _redis, _storage, get_item, caps, resolve
    ):
        item = _item()
        item.self_contained = True
        get_item.return_value, caps.return_value, resolve.return_value = item, CAN_READ, None
        resp = await _get(item.id)
        assert resp.status_code == 422
        assert "no uploaded module archive" in resp.json()["detail"]

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
            "interface-error": None,
        }

    async def test_a_failed_parse_carries_its_reason(
        self, _db, _redis, _storage, get_item, caps, resolve
    ):
        """#1707: the reason travels with the interface, so an item whose
        module could not be parsed is not mistaken for one with no inputs."""
        item = _item()
        get_item.return_value, caps.return_value = item, CAN_READ
        resolve.return_value = _version(interface_error="main.tf: invalid HCL")

        resp = await _get(item.id)

        assert resp.json()["data"]["attributes"]["interface-error"] == "main.tf: invalid HCL"

    async def test_the_provision_form_carries_the_reason_too(
        self, _db, _redis, _storage, get_item, caps, resolve
    ):
        """The provision form is built from the parsed inputs; with none it has
        no fields, so it must say why rather than render an empty form."""
        item = _item()
        item.provider_template_ids = []
        item.variable_options = {}
        get_item.return_value, caps.return_value = item, CAN_READ
        resolve.return_value = _version(interface_error="variables.tf: invalid HCL")

        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.get(f"/api/terrapod/v1/catalog-items/{item.id}/form", headers=_AUTH)

        assert resp.status_code == 200
        assert resp.json()["data"]["attributes"]["interface-error"] == "variables.tf: invalid HCL"

        resolve.return_value = _version()
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            resp = await c.get(f"/api/terrapod/v1/catalog-items/{item.id}/form", headers=_AUTH)
        assert resp.json()["data"]["attributes"]["interface-error"] is None

    async def test_catalog_form_and_interface_hide_sensitive_defaults_without_changing_registry_metadata(
        self, _db, _redis, _storage, get_item, caps, resolve
    ):
        item = _item()
        item.provider_template_ids = []
        item.variable_options = [
            {
                "name": "overlay",
                "sensitive": True,
                "default": "private-overlay",
                "options": ["private-overlay"],
            },
            {"name": "hidden", "hidden": True, "default": "private-hidden"},
        ]
        inputs = [
            {"name": "declared", "sensitive": True, "default": "private-declared"},
            {"name": "overlay", "sensitive": False, "default": "public-module"},
            {"name": "hidden", "sensitive": False, "default": None},
            {"name": "plain", "sensitive": False, "default": "public"},
        ]
        version = _version()
        version.inputs = inputs
        get_item.return_value, caps.return_value, resolve.return_value = item, CAN_READ, version
        async with AsyncClient(transport=ASGITransport(app=_app()), base_url=_BASE) as c:
            form = await c.get(f"/api/v1/catalog-items/{item.id}/form", headers=_AUTH)
            iface = await c.get(f"/api/v1/catalog-items/{item.id}/interface", headers=_AUTH)
        assert form.status_code == iface.status_code == 200
        fields = {f["name"]: f for f in form.json()["data"]["attributes"]["fields"]}
        assert "hidden" not in fields
        assert fields["declared"]["default"] is None
        assert fields["overlay"]["default"] is None
        assert fields["overlay"]["options"] is None
        assert fields["plain"]["default"] == "public"
        shown = {f["name"]: f for f in iface.json()["data"]["attributes"]["inputs"]}
        assert shown["declared"]["default"] is None
        assert shown["overlay"]["default"] is None
        assert shown["hidden"]["default"] is None
        assert shown["plain"]["default"] == "public"
        assert "private" not in form.text + iface.text
        assert inputs[0]["default"] == "private-declared"

    async def test_form_masks_sensitive_template_parameter_default_and_options(
        self, _db, _redis, _storage, get_item, caps, resolve
    ):
        item = _item()
        template_id = uuid.uuid4()
        item.provider_template_ids = [template_id]
        item.variable_options = []
        template = SimpleNamespace(
            name="aws",
            parameters=[
                {"name": "token", "sensitive": True, "default": "private", "options": ["private"]},
                {"name": "region", "sensitive": False, "default": "east", "options": ["east"]},
            ],
        )
        app = _app()
        db = AsyncMock()
        db.get = AsyncMock(return_value=template)
        app.dependency_overrides[get_db] = lambda: db
        get_item.return_value, caps.return_value, resolve.return_value = item, CAN_READ, _version()
        async with AsyncClient(transport=ASGITransport(app=app), base_url=_BASE) as c:
            resp = await c.get(f"/api/v1/catalog-items/{item.id}/form", headers=_AUTH)
        assert resp.status_code == 200, resp.text
        fields = {f["name"]: f for f in resp.json()["data"]["attributes"]["fields"]}
        assert fields["token"]["default"] is None
        assert fields["token"]["options"] is None
        assert fields["region"]["default"] == "east"
        assert "private" not in resp.text
        assert template.parameters[0]["default"] == "private"

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
