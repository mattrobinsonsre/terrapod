"""Self-contained catalog configuration versions are validated root archives."""

import io
import tarfile
import uuid
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from terrapod.services import catalog_service
from terrapod.services.catalog_service import CatalogError
from terrapod.storage.protocol import ObjectNotFoundError


def _root_files():
    return {
        "main.tf": 'terraform { backend "s3" {} }\nvariable "provider_configs" { type = map(string) }\n',
        "modules/child/main.tf": 'output "name" { value = "child" }\n',
        ".terraform.lock.hcl": "# retained dependency lock\n",
    }


# Each case names the refusal it is written for, because a bare
# `pytest.raises(CatalogError)` lets a case be satisfied by a DIFFERENT
# refusal than the one it exists to pin -- and that was happening: with the
# `".terraform" in parts` clause deleted, `.terraform/terraform.tfstate` was
# still refused by the `.tfstate` suffix beside it, so the clause looked
# covered and was not. Four elements of these conditions had no case at all
# (`terraform.tfvars.json`, `*.auto.tfvars.json`, `.tfstate.backup`, and any
# `.terraform/` member that is not itself a state file), and two whole
# predicates had none either (control characters, and a member whose path
# normalises to nothing).
_UNSAFE = "Root archive contains an unsafe path"
_STATE = "Root archive contains local Terraform state"
_CONFLICT = "Root archive conflicts with catalog-managed files"


@pytest.mark.parametrize(
    ("filename", "message"),
    [
        ("../escape.tf", _UNSAFE),
        ("/absolute.tf", _UNSAFE),
        ("modules/../../escape.tf", _UNSAFE),
        ("modules\\escape.tf", _UNSAFE),
        # A control character in a member name. Nothing exercised this branch.
        ("bad\x01name.tf", _UNSAFE),
        ("terrapod.auto.tfvars", _CONFLICT),
        ("zzzzz.auto.tfvars", _CONFLICT),
        # The `.json` halves of two tuples, each previously unexercised.
        ("zzzzz.auto.tfvars.json", _CONFLICT),
        ("terraform.tfvars", _CONFLICT),
        ("terraform.tfvars.json", _CONFLICT),
        (".terraform/terraform.tfstate", _STATE),
        # A `.terraform/` member that is NOT a state file, which is the only
        # thing that can pin the directory clause rather than the suffix.
        (
            ".terraform/providers/registry.terraform.io/hashicorp/aws/"
            "5.0.0/linux_amd64/terraform-provider-aws",
            _STATE,
        ),
        ("terraform.tfstate", _STATE),
        ("terraform.tfstate.backup", _STATE),
        ("zzzz_terrapod_backend_override.tf", _CONFLICT),
        ("zzzzz_override.tf", _CONFLICT),
        ("override.tf", _CONFLICT),
    ],
)
def test_archive_rejects_unsafe_and_conflicting_files(tmp_path, filename, message):
    archive = tmp_path / "bad.tar.gz"
    archive.write_bytes(catalog_service._build_tarball({**_root_files(), filename: "x"}))
    with pytest.raises(CatalogError, match=message):
        catalog_service._validate_root_archive(str(archive))


def test_a_member_that_normalises_to_nothing_is_refused_not_raised_through(tmp_path):
    """The `not parts` guard, which had no case and whose absence is a 500.

    A member named `./` is a file rather than the directory entry the loop
    skips, so its path normalises to no parts at all. Without the guard the
    next condition indexes `parts[-1]` and raises `IndexError`, which is NOT
    in this function's `except (tarfile.TarError, EOFError, OSError,
    zlib.error)` -- so it would escape as a 500 instead of a refusal that
    names what is wrong with the archive.
    """
    archive = tmp_path / "empty-parts.tar.gz"
    archive.write_bytes(catalog_service._build_tarball({**_root_files(), "./": "x"}))
    with pytest.raises(CatalogError, match=_UNSAFE):
        catalog_service._validate_root_archive(str(archive))


def test_the_archive_is_validated_before_the_hcl_parser_reads_it():
    """The ordering the feature's own merge commit called out by name.

    `test_archive_limits_total_root_hcl_before_parsing` is named for this and
    cannot see it: it calls `_validate_root_archive` directly, so it pins the
    size cap and says nothing about where the call sits. Swapping the two
    lines in `_stage_root_archive` -- so the interface parser reads an
    unvalidated archive first -- passes 262 tests, which is why this is a
    POSITIONAL assertion rather than a behavioural one. The same shape as the
    `db.flush()`-inside-the-`try` gate: a handler cannot protect a call that
    runs above it.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(catalog_service))
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_stage_root_archive"
    )
    calls = {}
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Name) and arg.id in (
                "_validate_root_archive",
                "extract_module_interface_result_from_file",
            ):
                calls.setdefault(arg.id, node.lineno)

    assert "_validate_root_archive" in calls, (
        "_stage_root_archive no longer validates the archive at all"
    )
    assert "extract_module_interface_result_from_file" in calls, (
        "the interface parser moved; teach this gate the new shape rather than deleting it"
    )
    assert calls["_validate_root_archive"] < calls["extract_module_interface_result_from_file"], (
        "the HCL parser reads the archive BEFORE it is validated -- an unvalidated "
        "archive must never reach the parser"
    )


def test_archive_rejects_links_and_duplicate_names(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    for kind in ("duplicate", "symlink", "hardlink"):
        with tarfile.open(archive, "w:gz") as tar:
            data = b'variable "name" {}'
            info = tarfile.TarInfo("main.tf")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
            if kind == "duplicate":
                tar.addfile(info, io.BytesIO(data))
            else:
                link = tarfile.TarInfo("modules/child.tf")
                link.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
                link.linkname = "../../escape.tf" if kind == "symlink" else "main.tf"
                tar.addfile(link)
        with pytest.raises(CatalogError):
            catalog_service._validate_root_archive(str(archive))


def test_archive_rejects_empty_root_and_corrupt_gzip(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    archive.write_bytes(catalog_service._build_tarball({"modules/child/main.tf": ""}))
    with pytest.raises(CatalogError, match="no .tf files at its root"):
        catalog_service._validate_root_archive(str(archive))
    archive.write_bytes(b"not a tarball")
    with pytest.raises(CatalogError, match="not a readable tar.gz"):
        catalog_service._validate_root_archive(str(archive))


def test_self_contained_configure_requires_files_in_its_ansible_directory(tmp_path):
    archive = tmp_path / "root.tar.gz"
    archive.write_bytes(catalog_service._build_tarball(_root_files()))
    with pytest.raises(CatalogError, match="ansible-directory 'ansible' holds no files"):
        catalog_service._validate_root_archive(str(archive), "ansible")

    archive.write_bytes(
        catalog_service._build_tarball({**_root_files(), "ansible/site.yml": "- hosts: all"})
    )
    catalog_service._validate_root_archive(str(archive), "ansible")


def test_archive_rejects_excessive_uncompressed_size(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_service, "_MAX_ROOT_CONTENT_BYTES", 10)
    archive = tmp_path / "large.tar.gz"
    archive.write_bytes(catalog_service._build_tarball(_root_files()))
    with pytest.raises(CatalogError, match="size limit"):
        catalog_service._validate_root_archive(str(archive))


def test_archive_limits_total_root_hcl_before_parsing(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_service, "_MAX_ROOT_HCL_BYTES", 18)
    archive = tmp_path / "large-root-hcl.tar.gz"
    archive.write_bytes(
        catalog_service._build_tarball({"one.tf": "variable {}", "two.tf": "output {}"})
    )
    with pytest.raises(CatalogError, match="Root HCL exceeds the size limit") as exc:
        catalog_service._validate_root_archive(str(archive))
    assert exc.value.status_code == 413

    archive.write_bytes(
        catalog_service._build_tarball({"main.tf": "variable {}", "nested/file.txt": "x" * 128})
    )
    catalog_service._validate_root_archive(str(archive))


def test_archive_rejects_excessive_member_count(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_service, "_MAX_ROOT_MEMBERS", 1)
    archive = tmp_path / "many.tar.gz"
    archive.write_bytes(catalog_service._build_tarball(_root_files()))
    with pytest.raises(CatalogError, match="too many entries"):
        catalog_service._validate_root_archive(str(archive))


def test_repeated_root_directory_entries_still_count_against_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_service, "_MAX_ROOT_MEMBERS", 2)
    archive = tmp_path / "repeated-root.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for _ in range(3):
            root = tarfile.TarInfo("./")
            root.type = tarfile.DIRTYPE
            tar.addfile(root)
    with pytest.raises(CatalogError, match="too many entries"):
        catalog_service._validate_root_archive(str(archive))


def test_mixed_hcl_and_tf_json_cannot_hide_required_inputs(tmp_path):
    archive = tmp_path / "mixed.tar.gz"
    archive.write_bytes(
        catalog_service._build_tarball(
            {**_root_files(), "vars.tf.json": '{"variable": {"required": {}}}'}
        )
    )
    with pytest.raises(CatalogError, match=r"\.tf\.json files are not supported"):
        catalog_service._validate_root_archive(str(archive))


async def _chunks(data):
    yield data


@pytest.mark.parametrize("cleanup_fails", [False, True])
@pytest.mark.parametrize("version_pin", ["1.2.3", None])
@pytest.mark.parametrize("configure", [False, True])
async def test_root_config_version_contains_the_exact_registry_archive(
    tmp_path, monkeypatch, cleanup_fails, version_pin, configure
):
    monkeypatch.setattr(catalog_service.settings.vcs, "tmpdir", str(tmp_path))
    archive = catalog_service._build_tarball(
        {**_root_files(), **({"ansible/site.yml": "- hosts: all"} if configure else {})}
    )
    module = SimpleNamespace(id=uuid.uuid4(), namespace="default", name="root", provider="aws")
    item = SimpleNamespace(
        module=module,
        self_contained=True,
        configure_enabled=configure,
        ansible_directory="ansible" if configure else "",
        default_playbook="site.yml" if configure else "",
        default_limit="",
        hard_limit="",
        default_version_pin="0.9.0",
        provider_template_ids=[],
        variable_options=[],
    )
    mv = SimpleNamespace(version="1.2.3", inputs=[{"name": "old"}], outputs=[])
    ws = SimpleNamespace(id=uuid.uuid4(), catalog_input_values=None)
    db = AsyncMock()
    cv = SimpleNamespace(id=uuid.uuid4())
    run = MagicMock()
    storage = MagicMock()
    storage.get_stream = MagicMock(side_effect=lambda key: _chunks(archive))
    uploaded = []

    async def capture_upload(key, chunks, *, content_type):
        uploaded.append((key, b"".join([part async for part in chunks]), content_type))

    storage.put_stream = AsyncMock(side_effect=capture_upload)
    with (
        patch.object(
            catalog_service, "_resolve_module_version", AsyncMock(return_value=mv)
        ) as resolve,
        patch.object(
            catalog_service.variable_service, "list_variables", AsyncMock(return_value=[])
        ),
        patch.object(
            catalog_service.variable_service, "create_variable", AsyncMock()
        ) as create_var,
        patch.object(
            catalog_service.run_service, "create_configuration_version", AsyncMock(return_value=cv)
        ) as create_cv,
        patch.object(catalog_service.run_service, "mark_configuration_uploaded", AsyncMock()),
        patch.object(catalog_service.run_service, "create_run", AsyncMock(return_value=run)),
        patch.object(catalog_service.run_service, "queue_run", AsyncMock()),
        patch("terrapod.storage.get_storage", return_value=storage),
        patch.object(catalog_service.logger, "warning") as warning,
        patch.object(catalog_service.os, "unlink", side_effect=OSError("disk error"))
        if cleanup_fails
        else nullcontext(),
    ):
        result = await catalog_service._materialise(
            db,
            ws,
            item,
            input_values={"provider_configs": {"east": "us-east-1"}},
            version_pin=version_pin,
            auto_apply=False,
            message="Root run",
            user_email="u@test.com",
        )

    assert result is run
    assert resolve.await_args.args[2] == version_pin
    assert len(uploaded) == 1
    assert uploaded[0][1:] == (archive, "application/gzip")
    assert uploaded[0][0] == f"config/{ws.id}/{cv.id}.tar.gz"
    assert create_cv.await_args.kwargs["archive_provenance"] == "protected"
    assert create_cv.await_args.kwargs["archive_module_id"] == module.id
    storage.get_stream.assert_called_once_with("registry/modules/default/root/aws/1.2.3.tar.gz")
    assert ws.catalog_input_values == {"provider_configs": {"east": "us-east-1"}}
    assert ws.configure_enabled is configure
    assert ws.config_directory == ("ansible" if configure else "")
    assert create_var.await_args.kwargs["structured"] is True
    assert bool(list(tmp_path.iterdir())) is cleanup_fails
    if cleanup_fails:
        warning.assert_called_once()
    else:
        warning.assert_not_called()


@pytest.mark.parametrize("failure", ["missing_required", "invalid_hcl", "missing_archive"])
async def test_bad_root_leaves_no_config_version_or_tempfile(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(catalog_service.settings.vcs, "tmpdir", str(tmp_path))
    archive = catalog_service._build_tarball(
        {"main.tf": 'variable "required" {}'}
        if failure == "missing_required"
        else {"main.tf": 'variable "required" {'}
    )
    module = SimpleNamespace(id=uuid.uuid4(), namespace="default", name="root", provider="aws")
    item = SimpleNamespace(
        module=module,
        self_contained=True,
        configure_enabled=False,
        default_version_pin=None,
        provider_template_ids=[],
        variable_options=[],
    )
    storage = MagicMock()
    if failure == "missing_archive":

        async def missing(key):
            raise ObjectNotFoundError(key)
            yield b""  # pragma: no cover

        storage.get_stream = MagicMock(side_effect=missing)
    else:
        storage.get_stream = MagicMock(side_effect=lambda key: _chunks(archive))

    with (
        patch.object(
            catalog_service,
            "_resolve_module_version",
            AsyncMock(return_value=SimpleNamespace(version="1.2.3", inputs=[], outputs=[])),
        ),
        patch.object(
            catalog_service.run_service, "create_configuration_version", AsyncMock()
        ) as create_cv,
        patch("terrapod.storage.get_storage", return_value=storage),
        pytest.raises(CatalogError),
    ):
        await catalog_service._materialise(
            AsyncMock(),
            SimpleNamespace(id=uuid.uuid4()),
            item,
            input_values={},
            version_pin=None,
            auto_apply=False,
            message="Root run",
            user_email="u@test.com",
        )
    create_cv.assert_not_awaited()
    assert not list(tmp_path.iterdir())


async def test_root_archive_must_declare_a_sensitive_catalog_option(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_service.settings.vcs, "tmpdir", str(tmp_path))
    archive = catalog_service._build_tarball({"main.tf": 'variable "token" {}'})
    module = SimpleNamespace(id=uuid.uuid4(), namespace="default", name="root", provider="aws")
    item = SimpleNamespace(
        module=module,
        self_contained=True,
        configure_enabled=False,
        provider_template_ids=[],
        variable_options=[{"name": "token", "sensitive": True}],
    )
    storage = MagicMock()
    storage.get_stream = MagicMock(side_effect=lambda _: _chunks(archive))
    with (
        patch.object(
            catalog_service,
            "_resolve_module_version",
            AsyncMock(return_value=SimpleNamespace(version="1.2.3", inputs=[], outputs=[])),
        ),
        patch.object(
            catalog_service.run_service, "create_configuration_version", AsyncMock()
        ) as create_cv,
        patch("terrapod.storage.get_storage", return_value=storage),
        pytest.raises(CatalogError, match="must declare sensitive = true"),
    ):
        await catalog_service._materialise(
            AsyncMock(),
            SimpleNamespace(id=uuid.uuid4()),
            item,
            input_values={"token": "example-only"},
            version_pin=None,
            auto_apply=False,
            message="Root run",
            user_email="u@test.com",
        )
    create_cv.assert_not_awaited()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("template_loaded", [False, True])
async def test_generated_wrapper_provenance_follows_loaded_templates(template_loaded):
    module = SimpleNamespace(id=uuid.uuid4(), namespace="default", name="root", provider="aws")
    template_id = uuid.uuid4()
    template = SimpleNamespace(
        name="example",
        body='provider "aws" { access_key = "fake-inline-credential" }',
        parameters=[],
    )
    item = SimpleNamespace(
        module=module,
        self_contained=False,
        configure_enabled=False,
        provider_template_ids=[str(template_id)],
        variable_options=[],
    )
    ws = SimpleNamespace(id=uuid.uuid4(), catalog_input_values=None)
    db = AsyncMock()
    db.get.return_value = template if template_loaded else None
    cv = SimpleNamespace(id=uuid.uuid4())
    storage = MagicMock()
    uploaded = []

    async def capture_upload(_key, chunks, *, content_type):
        uploaded.append(b"".join([part async for part in chunks]))

    storage.put_stream = AsyncMock(side_effect=capture_upload)
    with (
        patch.object(catalog_service, "_resolve_module_version", AsyncMock(return_value=None)),
        patch.object(
            catalog_service.variable_service, "list_variables", AsyncMock(return_value=[])
        ),
        patch.object(
            catalog_service.run_service, "create_configuration_version", AsyncMock(return_value=cv)
        ) as create_cv,
        patch.object(catalog_service.run_service, "mark_configuration_uploaded", AsyncMock()),
        patch.object(
            catalog_service.run_service, "create_run", AsyncMock(return_value=MagicMock())
        ),
        patch.object(catalog_service.run_service, "queue_run", AsyncMock()),
        patch("terrapod.storage.get_storage", return_value=storage),
    ):
        await catalog_service._materialise(
            db,
            ws,
            item,
            input_values={},
            version_pin=None,
            auto_apply=False,
            message="Wrapper run",
            user_email="u@test.com",
        )

    assert create_cv.await_args.kwargs["archive_provenance"] == (
        "admin-only" if template_loaded else "unprotected"
    )
    assert create_cv.await_args.kwargs["archive_module_id"] is None
    with tarfile.open(fileobj=io.BytesIO(uploaded[0]), mode="r:gz") as tar:
        assert ("providers.tf" in tar.getnames()) is template_loaded
        if template_loaded:
            assert b"fake-inline-credential" in tar.extractfile("providers.tf").read()


async def test_oversized_download_leaves_no_tempfile(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_service.settings.vcs, "tmpdir", str(tmp_path))
    monkeypatch.setattr(catalog_service, "_MAX_ROOT_ARCHIVE_BYTES", 1)
    storage = MagicMock()
    storage.get_stream = MagicMock(side_effect=lambda key: _chunks(b"too large"))
    with pytest.raises(CatalogError, match="compressed size limit") as exc:
        await catalog_service._stage_root_archive(
            storage, "registry/modules/default/root/aws/1.2.3.tar.gz"
        )
    assert exc.value.status_code == 413
    assert not list(tmp_path.iterdir())


async def test_cleanup_failure_does_not_replace_archive_validation_error(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_service.settings.vcs, "tmpdir", str(tmp_path))
    storage = MagicMock()
    storage.get_stream = MagicMock(
        side_effect=lambda key: _chunks(catalog_service._build_tarball({"main.tf": "broken {"}))
    )
    with (
        patch.object(catalog_service.os, "unlink", side_effect=OSError("disk error")),
        patch.object(catalog_service.logger, "warning") as warning,
        pytest.raises(CatalogError, match="interface could not be read"),
    ):
        await catalog_service._stage_root_archive(
            storage, "registry/modules/default/root/aws/1.2.3.tar.gz"
        )
    warning.assert_called_once()
