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
    def test_module_block_untyped_vars_value_free(self):
        files = catalog_service.render_wrapper_hcl(
            _module(),
            version="1.2.0",
            var_decls=["cidr", "name", "secret"],
            module_wired=["cidr", "name", "secret"],
            sensitive_names={"secret"},
            module_outputs=[{"name": "vpc_id"}],
            provider_templates=[],
        )
        main = files["main.tf"]
        assert 'module "this" {' in main
        assert 'source = "terrapod.local/default/vpc/aws"' in main
        assert 'version = "1.2.0"' in main
        assert "cidr = var.cidr" in main
        # Variables are untyped (valid HCL for any module input type).
        assert 'variable "cidr" {}' in main
        assert "type =" not in main
        # Sensitive var declared sensitive.
        assert 'variable "secret" {\n  sensitive = true\n}' in main
        # The config version is value-free — no values baked anywhere.
        assert "terraform.auto.tfvars.json" not in files
        assert "10.0.0.0/16" not in main
        # outputs re-exported
        assert "value     = module.this.vpc_id" in files["outputs.tf"]

    def test_no_tfvars_file_is_ever_emitted(self):
        files = catalog_service.render_wrapper_hcl(
            _module(),
            version="1.0.0",
            var_decls=["ports", "config"],
            module_wired=["ports", "config"],
            sensitive_names=set(),
            module_outputs=[],
            provider_templates=[],
        )
        # Values are supplied as workspace variables (delivered via the per-run
        # vars Secret), never baked into the config version.
        assert not any(name.endswith(".tfvars.json") for name in files)

    def test_floating_version_omits_version(self):
        files = catalog_service.render_wrapper_hcl(
            _module(),
            version=None,
            var_decls=["secret"],
            module_wired=["secret"],
            sensitive_names={"secret"},
            module_outputs=[],
            provider_templates=[],
        )
        assert "version =" not in files["main.tf"]
        assert "outputs.tf" not in files
        assert "terraform.auto.tfvars.json" not in files

    def test_provider_template_body_rendered(self):
        tmpl = _tmpl(body='provider "aws" {\n  region = var.region\n}')
        files = catalog_service.render_wrapper_hcl(
            _module(),
            version="1.0.0",
            var_decls=["region"],
            module_wired=[],  # provider param: declared but NOT wired to module
            sensitive_names=set(),
            module_outputs=[],
            provider_templates=[tmpl],
        )
        assert 'provider "aws" {' in files["providers.tf"]
        # region is declared in main.tf but not passed into the module block.
        assert 'variable "region" {}' in files["main.tf"]
        module_block = files["main.tf"].split('module "this"')[1]
        assert "region = var.region" not in module_block

    def test_sensitive_output_marked(self):
        files = catalog_service.render_wrapper_hcl(
            _module(),
            version="1.0.0",
            var_decls=[],
            module_wired=[],
            sensitive_names=set(),
            module_outputs=[{"name": "secret", "sensitive": True}],
            provider_templates=[],
        )
        assert "sensitive = true" in files["outputs.tf"]


class TestInputVarRepr:
    """A resolved input value → (workspace-variable value, hcl flag)."""

    def test_string_is_quoted_not_hcl(self):
        assert catalog_service._input_var_repr("10.0.0.0/16") == ("10.0.0.0/16", False)

    def test_string_that_looks_like_json_stays_string(self):
        # A literal "[a, b]" must remain a string, not become a list.
        assert catalog_service._input_var_repr("[a, b]") == ("[a, b]", False)

    def test_list_is_json_hcl(self):
        assert catalog_service._input_var_repr([80, 443]) == ("[80, 443]", True)

    def test_object_is_json_hcl(self):
        v, hcl = catalog_service._input_var_repr({"enabled": True})
        assert hcl is True
        import json as _json

        assert _json.loads(v) == {"enabled": True}

    def test_number_and_bool_are_hcl(self):
        assert catalog_service._input_var_repr(8080) == ("8080", True)
        assert catalog_service._input_var_repr(True) == ("true", True)


# ── _build_tarball ─────────────────────────────────────────────────────


class TestResolveInputsOptions:
    """`options` (constrained-choice) must be enforced server-side — the form
    advertises the allow-list, so a value outside it is a governance bypass if
    only the UI checks it."""

    _MODULE_INPUTS = [{"name": "region", "type": "string", "required": False}]

    def _item(self):
        return _item(variable_options=[{"name": "region", "options": ["us-east-1", "eu-west-1"]}])

    def test_value_outside_options_rejected(self):
        with pytest.raises(CatalogError, match="allowed options"):
            catalog_service._resolve_inputs(
                self._item(), self._MODULE_INPUTS, [], {"region": "mars-1"}
            )

    def test_value_in_options_accepted(self):
        _, effective, _, _ = catalog_service._resolve_inputs(
            self._item(), self._MODULE_INPUTS, [], {"region": "us-east-1"}
        )
        assert effective["region"] == "us-east-1"

    def test_unconstrained_field_accepts_anything(self):
        # No `options` overlay → no constraint.
        _, effective, _, _ = catalog_service._resolve_inputs(
            _item(), self._MODULE_INPUTS, [], {"region": "anywhere"}
        )
        assert effective["region"] == "anywhere"


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


class TestCoerceDefault:
    """Module-interface defaults are serialized JSON strings — decode them back
    to real typed values (#535 live-smoke bug: a map default "{}" passed through
    as the string "{}" and failed at plan)."""

    def test_json_map_default(self):
        assert catalog_service._coerce_default("{}") == {}

    def test_json_list_default(self):
        assert catalog_service._coerce_default("[80, 443]") == [80, 443]

    def test_json_object_default(self):
        assert catalog_service._coerce_default('{"enabled": true}') == {"enabled": True}

    def test_non_json_string_passthrough(self):
        # A plain unquoted string default survives.
        assert catalog_service._coerce_default("us-east-1") == "us-east-1"

    def test_non_string_passthrough(self):
        assert catalog_service._coerce_default(42) == 42
        assert catalog_service._coerce_default(None) is None


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
        # Set explicitly, and NOT noise to delete: `_materialise` reads the
        # item's Ansible half (#2019), and on a MagicMock an unset attribute is
        # a truthy auto-mock -- so omitting these makes provisioning try to copy
        # an Ansible subtree out of the module and fail on a tarfile it never
        # downloaded, which reads as a storage fault rather than a fixture one.
        item.configure_enabled = False
        item.ansible_directory = ""
        item.default_playbook = ""
        item.default_limit = ""

        ws = MagicMock()
        ws.id = uuid.uuid4()
        ws.catalog_item_id = uuid.uuid4()

        # A sensitive string input and a non-sensitive complex input — both must
        # become workspace terraform variables (the per-run vars Secret delivers
        # them; nothing is baked into the config version).
        mv = SimpleNamespace(
            version="1.0.0",
            inputs=[
                {"name": "cidr", "type": "string", "required": True, "sensitive": True},
                {"name": "ports", "type": "list(number)", "required": False, "sensitive": False},
            ],
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
                input_values={"cidr": "10.0.0.0/16", "ports": [80, 443]},
                version_pin="1.0.0",
                auto_apply=False,
            )

        assert result is run
        # Old variable deleted; BOTH inputs recreated as workspace terraform vars.
        mock_del.assert_awaited_once_with(db, old_var)
        created = {c.kwargs["key"]: c.kwargs for c in mock_create_var.await_args_list}
        assert set(created) == {"cidr", "ports"}
        # Sensitive string → sensitive var, quoted (structured=False).
        assert created["cidr"]["sensitive"] is True
        assert created["cidr"]["structured"] is False
        assert created["cidr"]["value"] == "10.0.0.0/16"
        # Non-sensitive complex → hcl var carrying the JSON-encoded value.
        assert created["ports"]["sensitive"] is False
        assert created["ports"]["structured"] is True
        assert created["ports"]["value"] == "[80, 443]"
        # Only the NON-sensitive input is snapshotted in the plaintext
        # input-values column; the sensitive one is write-only.
        assert ws.catalog_input_values == {"ports": [80, 443]}
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
        # Set explicitly, and NOT noise to delete: `_materialise` reads the
        # item's Ansible half (#2019), and on a MagicMock an unset attribute is
        # a truthy auto-mock -- so omitting these makes provisioning try to copy
        # an Ansible subtree out of the module and fail on a tarfile it never
        # downloaded, which reads as a storage fault rather than a fixture one.
        item.configure_enabled = False
        item.ansible_directory = ""
        item.default_playbook = ""
        item.default_limit = ""
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


class TestTheAnsibleHalfACatalogItemProvisions:
    """A catalog item may provision the Ansible half of a workspace (#2019).

    The subtree is COPIED out of the module into the generated configuration
    version rather than referenced, and that is forced rather than preferred: a
    composite run is two runner Jobs, so the configure Job re-fetches the CV and
    never runs `init`. A `config_directory` naming `.terraform/modules/this/...`
    passes every validator -- measured, only `..`, a leading or trailing slash
    and stray whitespace are refused -- and fails at run time on a directory
    nothing created.

    These drive the real extractor against a real gzipped tar, because the thing
    worth pinning is what ends up in the tarball a runner will extract, not the
    shape of a dict on the way there.
    """

    def _module_tarball(self, members: dict[str, bytes], *, symlink: str | None = None) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for name, data in members.items():
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
            if symlink is not None:
                link = tarfile.TarInfo(name=symlink)
                link.type = tarfile.SYMTYPE
                link.linkname = "../../../etc/passwd"
                tar.addfile(link)
        return buf.getvalue()

    def _item(self, **kw):
        item = MagicMock()
        item.module = SimpleNamespace(
            id=uuid.uuid4(), namespace="default", name="vpc", provider="aws"
        )
        item.provider_template_ids = []
        item.variable_options = []
        item.default_version_pin = None
        item.configure_enabled = kw.get("configure_enabled", True)
        item.ansible_directory = kw.get("ansible_directory", "ansible")
        item.default_playbook = kw.get("default_playbook", "site.yml")
        item.default_limit = kw.get("default_limit", "")
        return item

    async def _provision(self, item, tarball: bytes | None, ws=None):
        """Drive `reconfigure_instance` and return (ws, the CV's member names)."""
        ws = ws or MagicMock()
        ws.id = uuid.uuid4()
        ws.catalog_item_id = uuid.uuid4()
        mv = SimpleNamespace(version="1.0.0", inputs=[], outputs=[])
        cv = MagicMock(id=uuid.uuid4())
        run = MagicMock()

        async def _get_stream(_key):
            yield tarball if tarball is not None else b""

        storage = MagicMock()
        storage.put_stream = AsyncMock()
        storage.get_stream = _get_stream

        db = AsyncMock()
        db.get = AsyncMock(return_value=item)
        with (
            patch.object(catalog_service, "_resolve_module_version", AsyncMock(return_value=mv)),
            patch.object(
                catalog_service.variable_service, "list_variables", AsyncMock(return_value=[])
            ),
            patch.object(catalog_service.variable_service, "create_variable", AsyncMock()),
            patch.object(
                catalog_service.run_service,
                "create_configuration_version",
                AsyncMock(return_value=cv),
            ),
            patch.object(catalog_service.run_service, "mark_configuration_uploaded", AsyncMock()),
            patch.object(catalog_service.run_service, "create_run", AsyncMock(return_value=run)),
            patch.object(catalog_service.run_service, "queue_run", AsyncMock(return_value=run)),
            patch("terrapod.storage.get_storage", return_value=storage),
        ):
            await catalog_service.reconfigure_instance(
                db,
                user_email="u@test.com",
                ws=ws,
                input_values={},
                version_pin="1.0.0",
                auto_apply=False,
            )
        sent = storage.put_stream.await_args.args[1]
        return ws, sent

    async def _cv_members(self, sent) -> dict[str, bytes]:
        chunks = [c async for c in sent]
        with tarfile.open(fileobj=io.BytesIO(b"".join(chunks)), mode="r:gz") as tar:
            return {
                m.name: (tar.extractfile(m).read() if tar.extractfile(m) else b"")
                for m in tar.getmembers()
            }

    # -- the subtree reaches the configuration version --------------------

    @pytest.mark.asyncio
    async def test_the_subtree_lands_at_its_module_path(self) -> None:
        """At the SAME relative path, which is why `config_directory` is simply
        the directory name with nothing to translate."""
        tb = self._module_tarball(
            {
                "main.tf": b"# the module\n",
                "ansible/site.yml": b"- hosts: all\n",
                "ansible/roles/web/tasks/main.yml": b"- debug:\n",
            }
        )
        _ws, sent = await self._provision(self._item(), tb)
        members = await self._cv_members(sent)
        assert "ansible/site.yml" in members
        assert "ansible/roles/web/tasks/main.yml" in members
        assert members["ansible/site.yml"] == b"- hosts: all\n"
        # The module's own Terraform is NOT dragged along -- only the subtree.
        assert "main.tf" in members  # the generated wrapper's, not the module's
        assert members["main.tf"] != b"# the module\n"

    @pytest.mark.asyncio
    async def test_binary_content_survives_the_copy(self) -> None:
        """A role may ship a binary file; re-encoding one through `str` would
        corrupt it silently, which is why the tarball builder takes bytes."""
        blob = bytes(range(256))
        tb = self._module_tarball({"ansible/files/blob.bin": blob, "ansible/site.yml": b"x\n"})
        _ws, sent = await self._provision(self._item(), tb)
        assert (await self._cv_members(sent))["ansible/files/blob.bin"] == blob

    @pytest.mark.asyncio
    async def test_a_symlink_member_is_not_copied(self) -> None:
        """This repacks into a tarball a runner extracts, and a link is how a tar
        carries a path outside its own tree."""
        tb = self._module_tarball({"ansible/site.yml": b"x\n"}, symlink="ansible/escape")
        members = await self._cv_members((await self._provision(self._item(), tb))[1])
        assert "ansible/site.yml" in members
        assert "ansible/escape" not in members

    # -- the workspace settings -------------------------------------------

    @pytest.mark.asyncio
    async def test_all_four_settings_are_templated(self) -> None:
        tb = self._module_tarball({"ansible/site.yml": b"x\n"})
        item = self._item(default_playbook="site.yml", default_limit="canary")
        ws, _sent = await self._provision(item, tb)
        assert ws.configure_enabled is True
        assert ws.config_directory == "ansible"
        assert ws.default_playbook == "site.yml"
        assert ws.default_limit == "canary"

    @pytest.mark.asyncio
    async def test_the_off_case_clears_a_previous_configure(self) -> None:
        """An item whose switch is turned OFF must leave a reconfigured workspace
        with the operation gone, not with a stale config_directory and a playbook
        it no longer ships -- which is what an `if enabled:` guard would leave."""
        ws = MagicMock()
        ws.configure_enabled = True
        ws.config_directory = "ansible"
        ws.default_playbook = "site.yml"
        ws.default_limit = "canary"
        item = self._item(configure_enabled=False)
        ws, _sent = await self._provision(item, None, ws=ws)
        assert ws.configure_enabled is False
        assert ws.config_directory == ""
        assert ws.default_playbook == ""
        assert ws.default_limit == ""

    @pytest.mark.asyncio
    async def test_the_switch_off_copies_nothing_even_with_a_path(self) -> None:
        """The boolean says whether and the path says where (#1966's correction),
        so a path with the switch off provisions no configure."""
        item = self._item(configure_enabled=False, ansible_directory="ansible")
        _ws, sent = await self._provision(item, None)
        assert not [n for n in (await self._cv_members(sent)) if n.startswith("ansible/")]

    # -- refusals, at provision rather than at run ------------------------

    @pytest.mark.asyncio
    async def test_an_absent_subtree_is_refused_naming_what_the_module_ships(self) -> None:
        """A provisioning-time refusal on purpose: the alternative is a workspace
        whose config_directory names nothing, failing later and reporting it as a
        runner problem."""
        tb = self._module_tarball({"main.tf": b"x\n", "playbooks/site.yml": b"y\n"})
        with pytest.raises(catalog_service.CatalogError) as e:
            await self._provision(self._item(ansible_directory="ansible"), tb)
        assert "ansible" in str(e.value)
        assert "playbooks" in str(e.value)  # what it DOES ship
        assert e.value.status_code == 422

    @pytest.mark.parametrize("root_form", ["", ".", "ansible/"])
    @pytest.mark.asyncio
    async def test_a_root_form_directory_is_refused_naming_the_cause(self, root_form) -> None:
        """The forms that would copy the module over the generated wrapper.

        Refused by their FORM rather than left to fall through to "holds no
        files", which is true for all three and names the wrong cause -- the
        module is fine, the path is not. An earlier version of this test
        asserted a collision check instead, and the mutation check showed it was
        passing via the absent-subtree refusal: the collision is structurally
        impossible, because every copied member keeps its `<dir>/...` path while
        the wrapper's files are at the root.
        """
        tb = self._module_tarball({"main.tf": b"x\n", "ansible/site.yml": b"y\n"})
        with pytest.raises(catalog_service.CatalogError) as e:
            await self._provision(self._item(ansible_directory=root_form), tb)
        assert e.value.status_code == 422
        assert "subdirectory" in str(e.value)

    @pytest.mark.asyncio
    async def test_a_copied_member_can_never_shadow_the_generated_wrapper(self) -> None:
        """The property that makes a collision check unnecessary, asserted so
        that a change to the copied paths has to confront it."""
        tb = self._module_tarball({"ansible/main.tf": b"module own\n", "ansible/site.yml": b"y\n"})
        _ws, sent = await self._provision(self._item(), tb)
        members = await self._cv_members(sent)
        assert "ansible/main.tf" in members
        assert members["main.tf"] != b"module own\n"  # the wrapper's, untouched
        assert all(
            n.startswith("ansible/") or n in {"main.tf", "providers.tf", "outputs.tf"}
            for n in members
        ), sorted(members)
