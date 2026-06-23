"""Unit tests for catalog_service pure helpers (#535): form derivation, wrapper
HCL generation, tarball packing, registry-host resolution, and the lifecycle
orchestration (destroy / reconfigure)."""

import io
import tarfile
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import catalog_service
from terrapod.services.catalog_service import CatalogError


def _module(namespace="default", name="vpc", provider="aws"):
    return SimpleNamespace(namespace=namespace, name=name, provider=provider)


def _item(variable_options=None):
    return SimpleNamespace(variable_options=variable_options or [])


def _tmpl(name="aws-default", parameters=None, body='provider "aws" { region = var.region }'):
    return SimpleNamespace(name=name, parameters=parameters or [], body=body)


# ── _registry_host ─────────────────────────────────────────────────────


class TestRegistryHost:
    def test_strips_scheme_and_path(self, monkeypatch):
        monkeypatch.setattr(
            catalog_service.settings, "external_url", "https://terrapod.example.com/app"
        )
        assert catalog_service._registry_host() == "terrapod.example.com"

    def test_empty_falls_back(self, monkeypatch):
        monkeypatch.setattr(catalog_service.settings, "external_url", "")
        assert catalog_service._registry_host() == "terrapod.local"


# ── derive_form ────────────────────────────────────────────────────────


class TestDeriveForm:
    def test_module_inputs_become_fields(self):
        inputs = [
            {"name": "cidr", "type": "string", "required": True},
            {"name": "tags", "type": "map(string)", "required": False, "sensitive": False},
        ]
        fields = catalog_service.derive_form(_item(), inputs, [])
        names = {f["name"] for f in fields}
        assert names == {"cidr", "tags"}
        cidr = next(f for f in fields if f["name"] == "cidr")
        assert cidr["required"] is True
        assert cidr["source"] == "module"

    def test_hidden_input_excluded(self):
        inputs = [{"name": "region", "type": "string", "required": True}]
        item = _item([{"name": "region", "hidden": True, "default": "eu-west-1"}])
        fields = catalog_service.derive_form(item, inputs, [])
        assert fields == []

    def test_provider_params_included(self):
        tmpl = _tmpl(parameters=[{"name": "region", "type": "string", "required": True}])
        fields = catalog_service.derive_form(_item(), [], [tmpl])
        assert len(fields) == 1
        assert fields[0]["name"] == "region"
        assert fields[0]["source"] == "provider:aws-default"

    def test_variable_option_overrides_default(self):
        inputs = [{"name": "cidr", "type": "string", "default": "10.0.0.0/16"}]
        item = _item([{"name": "cidr", "default": "172.16.0.0/16", "options": ["172.16.0.0/16"]}])
        fields = catalog_service.derive_form(item, inputs, [])
        assert fields[0]["default"] == "172.16.0.0/16"
        assert fields[0]["options"] == ["172.16.0.0/16"]


# ── render_wrapper_hcl ─────────────────────────────────────────────────


class TestRenderWrapper:
    def test_module_block_and_variables(self):
        files = catalog_service.render_wrapper_hcl(
            _module(),
            version="1.2.0",
            wired_inputs=["cidr", "name"],
            module_outputs=[{"name": "vpc_id"}],
            field_types={"cidr": "string", "name": "string"},
            provider_templates=[],
        )
        main = files["main.tf"]
        assert 'module "this" {' in main
        assert 'source = "terrapod.local/default/vpc/aws"' in main
        assert 'version = "1.2.0"' in main
        assert "cidr = var.cidr" in main
        assert "name = var.name" in main
        assert 'variable "cidr" {' in main
        # outputs re-exported
        assert "outputs.tf" in files
        assert "value     = module.this.vpc_id" in files["outputs.tf"]

    def test_floating_version_omits_version(self):
        files = catalog_service.render_wrapper_hcl(
            _module(),
            version=None,
            wired_inputs=["cidr"],
            module_outputs=[],
            field_types={"cidr": "string"},
            provider_templates=[],
        )
        assert "version =" not in files["main.tf"]
        assert "outputs.tf" not in files  # no outputs → no file

    def test_provider_template_rendered(self):
        tmpl = _tmpl(
            parameters=[{"name": "region", "type": "string"}],
            body='provider "aws" {\n  region = var.region\n}',
        )
        files = catalog_service.render_wrapper_hcl(
            _module(),
            version="1.0.0",
            wired_inputs=[],
            module_outputs=[],
            field_types={"region": "string"},
            provider_templates=[tmpl],
        )
        assert "providers.tf" in files
        providers = files["providers.tf"]
        assert 'variable "region" {' in providers
        assert 'provider "aws" {' in providers

    def test_sensitive_output_marked(self):
        files = catalog_service.render_wrapper_hcl(
            _module(),
            version="1.0.0",
            wired_inputs=[],
            module_outputs=[{"name": "secret", "sensitive": True}],
            field_types={},
            provider_templates=[],
        )
        assert "sensitive = true" in files["outputs.tf"]


# ── _build_tarball ─────────────────────────────────────────────────────


class TestBuildTarball:
    def test_packs_files(self):
        data = catalog_service._build_tarball({"main.tf": "content-a", "providers.tf": "content-b"})
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            names = sorted(tar.getnames())
            assert names == ["main.tf", "providers.tf"]
            extracted = tar.extractfile("main.tf").read().decode()
            assert extracted == "content-a"

    def test_deterministic(self):
        files = {"main.tf": "x"}
        assert catalog_service._build_tarball(files) == catalog_service._build_tarball(files)


@pytest.mark.asyncio
async def test_single_chunk_generator():
    chunks = [c async for c in catalog_service._single_chunk(b"hello")]
    assert chunks == [b"hello"]


# ── Lifecycle orchestration ────────────────────────────────────────────


class TestDestroyInstance:
    @pytest.mark.asyncio
    async def test_creates_destroy_run_with_lifecycle_source(self):
        ws = MagicMock()
        ws.id = uuid.uuid4()
        ws.catalog_item_id = uuid.uuid4()
        cv = MagicMock(id=uuid.uuid4())
        run = MagicMock()
        db = AsyncMock()

        with (
            patch.object(
                catalog_service.run_service, "get_latest_uploaded_cv", AsyncMock(return_value=cv)
            ),
            patch.object(
                catalog_service.run_service, "create_run", AsyncMock(return_value=run)
            ) as mock_create,
            patch.object(catalog_service.run_service, "queue_run", AsyncMock(return_value=run)),
        ):
            result = await catalog_service.destroy_instance(
                db, user_email="u@test.com", ws=ws, auto_apply=True
            )

        assert result is run
        kwargs = mock_create.await_args.kwargs
        assert kwargs["source"] == "catalog-lifecycle"
        assert kwargs["is_destroy"] is True
        assert kwargs["auto_apply"] is True
        assert kwargs["configuration_version_id"] == cv.id

    @pytest.mark.asyncio
    async def test_non_catalog_workspace_rejected(self):
        ws = MagicMock()
        ws.catalog_item_id = None
        with pytest.raises(CatalogError) as exc:
            await catalog_service.destroy_instance(
                AsyncMock(), user_email="u@test.com", ws=ws, auto_apply=True
            )
        assert exc.value.status_code == 409


class TestReconfigureInstance:
    @pytest.mark.asyncio
    async def test_replaces_vars_and_queues_catalog_run(self):
        item = MagicMock()
        item.module = SimpleNamespace(
            id=uuid.uuid4(), namespace="default", name="vpc", provider="aws"
        )
        item.provider_template_ids = []
        item.variable_options = []
        item.default_version_pin = None

        ws = MagicMock()
        ws.id = uuid.uuid4()
        ws.catalog_item_id = uuid.uuid4()

        mv = SimpleNamespace(
            version="1.0.0",
            inputs=[{"name": "cidr", "type": "string", "required": True}],
            outputs=[],
        )
        old_var = MagicMock()
        cv = MagicMock(id=uuid.uuid4())
        run = MagicMock()

        db = AsyncMock()
        db.get = AsyncMock(return_value=item)
        storage = MagicMock()
        storage.put_stream = AsyncMock()

        with (
            patch.object(catalog_service, "_resolve_module_version", AsyncMock(return_value=mv)),
            patch.object(
                catalog_service.variable_service,
                "list_variables",
                AsyncMock(return_value=[old_var]),
            ),
            patch.object(
                catalog_service.variable_service, "delete_variable", AsyncMock()
            ) as mock_del,
            patch.object(
                catalog_service.variable_service, "create_variable", AsyncMock()
            ) as mock_create_var,
            patch.object(
                catalog_service.run_service,
                "create_configuration_version",
                AsyncMock(return_value=cv),
            ),
            patch.object(catalog_service.run_service, "mark_configuration_uploaded", AsyncMock()),
            patch.object(
                catalog_service.run_service, "create_run", AsyncMock(return_value=run)
            ) as mock_create_run,
            patch.object(catalog_service.run_service, "queue_run", AsyncMock(return_value=run)),
            patch("terrapod.storage.get_storage", return_value=storage),
        ):
            result = await catalog_service.reconfigure_instance(
                db,
                user_email="u@test.com",
                ws=ws,
                input_values={"cidr": "10.0.0.0/16"},
                version_pin="1.0.0",
                auto_apply=False,
            )

        assert result is run
        # Old variable deleted, new one created, snapshot + pin updated.
        mock_del.assert_awaited_once_with(db, old_var)
        assert mock_create_var.await_args.kwargs["key"] == "cidr"
        assert ws.catalog_input_values == {"cidr": "10.0.0.0/16"}
        assert ws.catalog_version_pin == "1.0.0"
        assert mock_create_run.await_args.kwargs["source"] == "catalog"
        storage.put_stream.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_required_input_rejected(self):
        item = MagicMock()
        item.module = SimpleNamespace(
            id=uuid.uuid4(), namespace="default", name="vpc", provider="aws"
        )
        item.provider_template_ids = []
        item.variable_options = []
        item.default_version_pin = None
        ws = MagicMock()
        ws.id = uuid.uuid4()
        ws.catalog_item_id = uuid.uuid4()
        mv = SimpleNamespace(
            version="1.0.0",
            inputs=[{"name": "cidr", "type": "string", "required": True}],
            outputs=[],
        )
        db = AsyncMock()
        db.get = AsyncMock(return_value=item)

        with patch.object(catalog_service, "_resolve_module_version", AsyncMock(return_value=mv)):
            with pytest.raises(CatalogError) as exc:
                await catalog_service.reconfigure_instance(
                    db,
                    user_email="u@test.com",
                    ws=ws,
                    input_values={},  # missing required cidr
                    version_pin=None,
                    auto_apply=False,
                )
        assert "Missing required" in str(exc.value)
