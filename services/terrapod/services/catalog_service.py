"""Service catalog (#535): provider-template + catalog-item CRUD, provision form
derivation, wrapper-config generation, and the provision orchestration.

Provisioning a catalog item creates an **ordinary agent-mode, non-VCS
workspace** whose configuration is a server-generated wrapper that calls the
blessed registry module:

    variable "<input>" { type = <type> }          # one per supplied input
    module "this" {
      source  = "<host>/<ns>/<name>/<provider>"
      version = "<pin>"                            # omitted when floating
      <input> = var.<input>
    }
    # providers.tf — each provider template body, verbatim (references var.*)
    output "<name>" { value = module.this.<name> } # re-export

The wrapper is the *code* (uploaded as a ConfigurationVersion, value-free); the
supplied input values are the *data* — every input, sensitive or not, becomes an
ordinary workspace Terraform variable, which the runner renders into
``terrapod.auto.tfvars`` and delivers via the per-run vars Secret (no plaintext
in the Job spec, #540). No server-side interpolation — params are plain Terraform
variables. The workspace is marked catalog-managed (``catalog_item_id``), which
makes the RBAC clamp and config-managed guardrails apply.
"""

import asyncio
import io
import json
import os
import posixpath
import tarfile
import tempfile
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.config import settings
from terrapod.db.models import (
    CatalogItem,
    ModuleWorkspaceLink,
    ProviderTemplate,
    RegistryModule,
    RegistryModuleVersion,
    Workspace,
)
from terrapod.logging_config import get_logger
from terrapod.services import pool_set, run_service, variable_service

logger = get_logger(__name__)


class CatalogError(Exception):
    """Raised for catalog validation failures (mapped to 4xx by the router)."""

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code


# ── Host + version resolution ──────────────────────────────────────────


def _registry_host() -> str:
    """Bare hostname used in generated `module` source addresses.

    Derived from ``external_url`` (scheme + path stripped). Falls back to a
    local-dev default so generated config is never sourced from an empty host.
    """
    url = (settings.external_url or "").strip()
    if not url:
        return "terrapod.local"
    host = url.split("://", 1)[-1]
    return host.split("/", 1)[0].rstrip("/") or "terrapod.local"


async def _resolve_module_version(
    db: AsyncSession, module_id: uuid.UUID, pin: str | None
) -> RegistryModuleVersion | None:
    """Resolve the module version row for a pin, or the latest uploaded one.

    Returns None when the module has no uploaded version yet (floating) — the
    generated module block then omits ``version`` and terraform resolves the
    newest available at init time.
    """
    if pin:
        result = await db.execute(
            select(RegistryModuleVersion).where(
                RegistryModuleVersion.module_id == module_id,
                RegistryModuleVersion.version == pin,
                RegistryModuleVersion.upload_status == "uploaded",
            )
        )
        return result.scalar_one_or_none()
    result = await db.execute(
        select(RegistryModuleVersion)
        .where(
            RegistryModuleVersion.module_id == module_id,
            RegistryModuleVersion.upload_status == "uploaded",
        )
        .order_by(RegistryModuleVersion.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


# ── Provision-form derivation ──────────────────────────────────────────


def _coerce_default(raw: object) -> object:
    """Decode a module-interface default (a serialized JSON string) back to its
    real typed value. Non-string or non-JSON values pass through unchanged, so a
    plain string default like ``hello`` (stored unquoted) survives."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return raw
    return raw


def _option_overrides(item: CatalogItem) -> dict[str, dict]:
    """Index ``variable_options`` (a list of per-input override dicts) by name."""
    overrides: dict[str, dict] = {}
    for opt in item.variable_options or []:
        name = opt.get("name")
        if name:
            overrides[name] = opt
    return overrides


def derive_form(
    item: CatalogItem,
    module_inputs: list[dict],
    provider_templates: list[ProviderTemplate],
) -> list[dict]:
    """Build the unified provision form: module inputs (minus hidden ones) plus
    every provider-template parameter.

    Each field: {name, type, description, required, sensitive, default,
    options, source}. ``source`` is "module" or "provider:<template name>".
    Hidden inputs (variable_options entry with ``hidden: true``) are excluded —
    their fixed ``default`` is wired without being presented.
    """
    overrides = _option_overrides(item)
    fields: list[dict] = []

    for inp in module_inputs:
        name = inp.get("name")
        if not name:
            continue
        ov = overrides.get(name, {})
        if ov.get("hidden"):
            continue
        fields.append(
            {
                "name": name,
                "type": inp.get("type", "string"),
                "description": inp.get("description", ""),
                "required": bool(inp.get("required", False)),
                "sensitive": bool(inp.get("sensitive", False)),
                "default": ov.get("default", inp.get("default")),
                "options": ov.get("options"),
                "source": "module",
            }
        )

    for tmpl in provider_templates:
        for param in tmpl.parameters or []:
            name = param.get("name")
            if not name:
                continue
            fields.append(
                {
                    "name": name,
                    "type": param.get("type", "string"),
                    "description": param.get("description", ""),
                    "required": bool(param.get("required", False)),
                    "sensitive": bool(param.get("sensitive", False)),
                    "default": param.get("default"),
                    "options": param.get("options"),
                    "source": f"provider:{tmpl.name}",
                }
            )

    return fields


# ── Wrapper config generation ──────────────────────────────────────────


def _module_source(module: RegistryModule) -> str:
    return f"{_registry_host()}/{module.namespace}/{module.name}/{module.provider}"


def render_wrapper_hcl(
    module: RegistryModule,
    version: str | None,
    *,
    var_decls: list[str],
    module_wired: list[str],
    sensitive_names: set[str],
    module_outputs: list[dict],
    provider_templates: list[ProviderTemplate],
) -> dict[str, str]:
    """Render the wrapper config files as {filename: content}.

    Wrapper root variables are declared **untyped** (`variable "x" {}`) — the
    module interface's `type` string is not reliably valid HCL for complex
    types (objects/tuples), and the module enforces its own types anyway. The
    config version is purely **structural** (variable decls + the module call +
    providers + output re-export); it bakes in **no values**. Every input value
    — sensitive and not — is supplied as an ordinary workspace **terraform
    variable**, which the runner renders into `terrapod.auto.tfvars` (honouring
    `structured`) and delivers via the per-run vars Secret (#540). Those values flow
    structurally into the `any`-typed wrapper variables.

    ``var_decls`` is every wrapper variable (module inputs + provider params);
    ``module_wired`` is the subset passed into the `module "this"` block (module
    inputs only — provider params are referenced by the provider bodies, not the
    module). ``sensitive_names`` get `sensitive = true`.
    """
    # main.tf — root variable decls + the module call.
    lines: list[str] = [
        "# Generated by Terrapod service catalog. Do not edit by hand.",
        "",
    ]
    for name in var_decls:
        if name in sensitive_names:
            lines.append(f'variable "{name}" {{')
            lines.append("  sensitive = true")
            lines.append("}")
        else:
            lines.append(f'variable "{name}" {{}}')
        lines.append("")

    lines.append('module "this" {')
    lines.append(f'  source = "{_module_source(module)}"')
    if version:
        lines.append(f'  version = "{version}"')
    for name in module_wired:
        lines.append(f"  {name} = var.{name}")
    lines.append("}")
    lines.append("")

    files = {"main.tf": "\n".join(lines)}

    # No values are baked into the config version — every input is supplied as a
    # workspace terraform variable (rendered into terrapod.auto.tfvars by the
    # runner, #540), so the wrapper CV stays purely structural and reusable.

    # providers.tf — each provider template body verbatim (references var.*).
    if provider_templates:
        provider_lines: list[str] = [
            "# Generated by Terrapod service catalog. Do not edit by hand.",
            "",
        ]
        for tmpl in provider_templates:
            provider_lines.append(f"# provider template: {tmpl.name}")
            provider_lines.append(tmpl.body.strip())
            provider_lines.append("")
        files["providers.tf"] = "\n".join(provider_lines)

    # outputs.tf — re-export module outputs.
    if module_outputs:
        out_lines = [
            "# Generated by Terrapod service catalog. Do not edit by hand.",
            "",
        ]
        for out in module_outputs:
            oname = out.get("name")
            if not oname:
                continue
            out_lines.append(f'output "{oname}" {{')
            out_lines.append(f"  value     = module.this.{oname}")
            if out.get("sensitive"):
                out_lines.append("  sensitive = true")
            out_lines.append("}")
            out_lines.append("")
        files["outputs.tf"] = "\n".join(out_lines)

    return files


def _build_tarball(files: dict[str, str | bytes]) -> bytes:
    """Pack {filename: content} into a gzipped tar (sync — run in a thread).

    The payload is a few KB of generated .tf text (small-file exemption), so
    building it in memory is fine; tarfile is still sync CPU work, hence the
    caller wraps this in asyncio.to_thread (HARD RULE 13).

    Takes `bytes` as well as `str` because an item's Ansible subtree is copied
    out of the module verbatim (#2019) and a playbook tree is not necessarily
    all text -- a role may ship a binary file, and re-encoding one through `str`
    would corrupt it silently.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mtime = 0  # deterministic
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _resolve_tmpdir() -> str | None:
    """Reuse the VCS tmpdir setting — same PVC, same sweep behaviour."""
    configured = settings.vcs.tmpdir
    if configured and os.path.isdir(configured):
        return configured
    return None


def _subtree_members(path: str, subdir: str) -> dict[str, bytes]:
    """Every regular file under `subdir/` in the module tarball at `path`.

    Sync: `tarfile` plus the reads, so the caller runs it in a thread (rule 13).
    Keys are the member names UNCHANGED, so the subtree lands in the generated
    configuration version at the same relative path it has in the module and
    the workspace's `config_directory` is simply `subdir`.

    **Only regular files.** A symlink, a hard link, a device or a directory
    entry is dropped rather than copied: this repacks into a tarball a runner
    extracts, and a link is how a tar carries a path outside its own tree. The
    names are additionally checked, because a member name is attacker-controlled
    in the sense that matters here -- it comes from whatever was published.
    """
    prefix = f"{subdir}/"
    out: dict[str, bytes] = {}
    with tarfile.open(path, mode="r:*") as tar:
        for member in tar.getmembers():
            name = member.name
            # Normalise a leading `./`, which some packers emit and which would
            # otherwise make an identical tree miss the prefix test.
            if name.startswith("./"):
                name = name[2:]
            if not name.startswith(prefix):
                continue
            if not member.isfile():
                continue
            if name != posixpath.normpath(name) or name.startswith("/") or ".." in name:
                continue
            fh = tar.extractfile(member)
            if fh is None:
                continue
            out[name] = fh.read()
    return out


def _tarball_top_level(path: str) -> list[str]:
    """The tarball's top-level entries, for a refusal that is actionable.

    Named so an operator whose item points at a directory the module does not
    ship is told what it DOES ship, rather than only that their path was wrong.
    """
    top: set[str] = set()
    with tarfile.open(path, mode="r:*") as tar:
        for member in tar.getmembers():
            name = member.name[2:] if member.name.startswith("./") else member.name
            head = name.split("/", 1)[0]
            if head and head != ".":
                top.add(head)
    return sorted(top)


async def ansible_files_from_module(
    *,
    namespace: str,
    name: str,
    provider: str,
    version: str,
    subdir: str,
) -> dict[str, bytes]:
    """Copy a module version's Ansible subtree, for the generated config version.

    **Copied rather than referenced, and that is forced rather than chosen.** A
    composite run is two runner Jobs -- `job_entrypoint` says so in as many
    words -- so the configure Job re-fetches the configuration version and never
    runs `init`. `.terraform/modules/this/` does not exist in it, so a
    `config_directory` pointing there passes every validator (measured: only
    `..`, a leading or trailing slash and stray whitespace are refused) and
    fails at run time on a directory nothing created.

    The tarball goes to the PVC tempdir rather than `/tmp` (HARD RULE 14): a
    module tarball is operator content with no size bound, and `/tmp` on the API
    pod is RAM-backed.

    Raises `CatalogError` 422 naming the directory, and what the module does
    ship, when the subtree is absent. That is a provisioning-time refusal on
    purpose: the alternative is a workspace whose `config_directory` names
    nothing, which fails later and reports it as a runner problem.
    """
    # Local imports, matching `_materialise`'s own: `terrapod.storage` pulls in
    # a backend SDK and this module is imported by the API at startup.
    from terrapod.storage import get_storage
    from terrapod.storage.keys import module_tarball_key

    storage = get_storage()
    key = module_tarball_key(namespace, name, provider, version)
    tmpdir = _resolve_tmpdir()
    fd, path = await asyncio.to_thread(tempfile.mkstemp, suffix=".module.tar.gz", dir=tmpdir)
    try:
        with os.fdopen(fd, "wb") as out:
            async for chunk in storage.get_stream(key):
                await asyncio.to_thread(out.write, chunk)
        files = await asyncio.to_thread(_subtree_members, path, subdir)
        if not files:
            top = await asyncio.to_thread(_tarball_top_level, path)
            raise CatalogError(
                f"the catalog item's ansible-directory {subdir!r} holds no files in "
                f"{namespace}/{name}/{provider} {version}. That module's top-level "
                f"entries are: {', '.join(top) or '(none)'}",
                status_code=422,
            )
        return files
    finally:
        await asyncio.to_thread(_unlink_quietly, path)


def _require_subdirectory(subdir: str) -> None:
    """`ansible_directory` must name a SUBDIRECTORY of the module.

    The root forms -- `""`, `"."`, and anything with a trailing slash -- would
    copy the module's own root into the generated configuration version, where
    the wrapper's `main.tf` lives. Refused here rather than left to fall through
    to "holds no files", which is true for all of them and names the wrong
    cause: the module is fine, the path form is not.

    This is also what makes a collision check in the caller unnecessary rather
    than merely unwritten -- see the comment there.
    """
    if not subdir or subdir == "." or subdir.endswith("/"):
        raise CatalogError(
            f"the catalog item's ansible-directory must name a subdirectory of the "
            f"module, not its root: {subdir!r} would copy the module over the "
            f"generated configuration",
            status_code=422,
        )


def _apply_ansible_settings(ws: Workspace, item: CatalogItem) -> None:
    """Template the item's Ansible half onto the workspace (#2019).

    One writer, called from `_materialise`, so provision and reconfigure cannot
    disagree -- the shape every other catalog-managed workspace setting has.

    `config_directory` IS `ansible_directory`: the subtree is copied into the
    configuration version at the path it has in the module, so the workspace's
    Ansible root is that same path and there is nothing to translate.

    **It writes all four every time, including the off case.** An item that had
    the switch on and has it turned off must leave a reconfigured workspace with
    the operation gone, not with a stale `config_directory` and a playbook it no
    longer has -- which is what an `if item.configure_enabled:` guard here would
    have left behind.
    """
    ws.configure_enabled = item.configure_enabled
    ws.config_directory = item.ansible_directory if item.configure_enabled else ""
    ws.default_playbook = item.default_playbook if item.configure_enabled else ""
    ws.default_limit = item.default_limit if item.configure_enabled else ""


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# ── Provision orchestration ────────────────────────────────────────────


async def _single_chunk(data: bytes):
    yield data


def _input_var_repr(value: object) -> tuple[str, bool]:
    """Map a resolved input value to ``(variable_value, structured)`` for storage as a
    workspace terraform variable.

    Strings render quoted (``structured=False``); everything else — lists, objects,
    numbers, bools — is JSON-encoded and rendered as raw HCL (``structured=True``),
    because JSON is valid HCL for those types and the runner's tfvars renderer
    emits ``key = <raw>`` for structured vars. This reproduces the structural typing the
    old baked ``terraform.auto.tfvars.json`` gave, but via the per-run vars
    Secret (#540) instead of the config version.
    """
    if isinstance(value, str):
        return value, False
    return json.dumps(value), True


def _resolve_inputs(
    item: CatalogItem,
    module_inputs: list[dict],
    templates: list[ProviderTemplate],
    input_values: dict,
) -> tuple[list[str], dict, set[str], dict[str, str]]:
    """Validate input_values against the derived form and compute the effective
    wiring.

    Returns ``(wired_inputs, effective, sensitive_names, field_types)``. Raises
    CatalogError on a missing-required or unknown input.
    """
    form = derive_form(item, module_inputs, templates)
    field_types = {f["name"]: f["type"] for f in form}

    # Collect fixed (hidden) overrides not present in the form.
    overrides = _option_overrides(item)
    hidden_values: dict[str, object] = {}
    for inp in module_inputs:
        iname = inp.get("name")
        ov = overrides.get(iname, {})
        if iname and ov.get("hidden") and ov.get("default") is not None:
            hidden_values[iname] = ov["default"]
            field_types.setdefault(iname, inp.get("type", "string"))

    # Validate required fields are supplied.
    missing = [
        f["name"]
        for f in form
        if f["required"]
        and f["default"] is None
        and not str(input_values.get(f["name"], "")).strip()
    ]
    if missing:
        raise CatalogError(f"Missing required input(s): {', '.join(sorted(missing))}")

    # Reject unknown inputs (typo / injection of a non-exposed variable).
    known = {f["name"] for f in form}
    unknown = [k for k in input_values if k not in known]
    if unknown:
        raise CatalogError(f"Unknown input(s): {', '.join(sorted(unknown))}")

    # Enforce constrained-choice (`options`) allow-lists server-side. The form
    # advertises them, so a supplied value outside the list is a governance
    # bypass if only the UI checks it — reject here (422). Compare directly and
    # by string so a numeric option list still matches a JSON-typed value.
    bad_choice = []
    for f in form:
        opts = f.get("options")
        fname = f["name"]
        if not opts or fname not in input_values:
            continue
        val = input_values[fname]
        if str(val).strip() == "":
            continue
        if val not in opts and str(val) not in {str(o) for o in opts}:
            bad_choice.append(fname)
    if bad_choice:
        raise CatalogError(f"Value(s) not in the allowed options: {', '.join(sorted(bad_choice))}")

    # Effective values: form value or its default; plus hidden fixed values.
    # Module-interface defaults are stored as serialized JSON strings (e.g. a
    # map default is the string "{}", a list is "[80, 443]"), so a default that
    # parses as JSON is decoded back to its real typed value — otherwise a
    # `map(string)`/`list(...)`/`object(...)` input would receive a string and
    # fail at plan. Form-supplied values already arrive correctly typed (JSON
    # request body), so they pass through unchanged.
    effective: dict[str, object] = {}
    sensitive_names: set[str] = set()
    for f in form:
        fname = f["name"]
        if fname in input_values and str(input_values[fname]).strip() != "":
            effective[fname] = input_values[fname]
        elif f["default"] is not None:
            effective[fname] = _coerce_default(f["default"])
        if f["sensitive"]:
            sensitive_names.add(fname)
    effective.update(hidden_values)

    return sorted(effective.keys()), effective, sensitive_names, field_types


async def _materialise(
    db: AsyncSession,
    ws: Workspace,
    item: CatalogItem,
    *,
    input_values: dict,
    version_pin: str | None,
    auto_apply: bool,
    message: str,
    user_email: str,
):
    """Render + upload the wrapper config, replace the workspace's variables, and
    queue a run. Shared by provision (create) and reconfigure (update).

    Returns the queued ``Run``. The caller owns the commit.
    """
    module: RegistryModule = item.module
    if module is None:
        raise CatalogError("Catalog item has no backing module", status_code=409)

    # Resolve the version (pin > item default > floating-latest).
    pin = version_pin or item.default_version_pin
    mv = await _resolve_module_version(db, module.id, pin)
    if pin and mv is None:
        raise CatalogError(f"Module {module.name} has no uploaded version {pin}", status_code=422)
    resolved_version = mv.version if mv else None
    module_inputs = (mv.inputs if mv else None) or []
    module_outputs = (mv.outputs if mv else None) or []

    templates: list[ProviderTemplate] = []
    for tid in item.provider_template_ids or []:
        t = await db.get(ProviderTemplate, uuid.UUID(str(tid)))
        if t is not None:
            templates.append(t)

    wired_inputs, effective, sensitive_names, _field_types = _resolve_inputs(
        item, module_inputs, templates, input_values
    )

    # Only module inputs are passed into the module block; provider-template
    # params are referenced by the provider bodies, not the module.
    module_input_names = {i.get("name") for i in module_inputs if i.get("name")}
    module_wired = sorted(n for n in effective if n in module_input_names)

    # Snapshot only the NON-sensitive resolved inputs onto the workspace for the
    # instance API to return. Sensitive inputs are write-only (like TFE sensitive
    # variables) — they live only in the (encrypted-at-rest) workspace variables,
    # never in this plaintext JSONB column.
    nonsensitive_values = {k: v for k, v in effective.items() if k not in sensitive_names}
    ws.catalog_input_values = dict(nonsensitive_values)

    # Replace the workspace's variables — a catalog workspace's variable set is
    # wholly catalog-managed (the RBAC clamp blocks user var edits), so a full
    # replace keeps it in lockstep with the wrapper config. EVERY input —
    # sensitive and not — is stored as a workspace terraform variable; the runner
    # delivers them all via the per-run vars Secret (rendered into
    # terrapod.auto.tfvars), never plaintext in the Job spec (#540). Non-string
    # values (lists/objects/numbers/bools) carry structured=true so they render as raw
    # HCL — json.dumps emits valid HCL for these; strings render quoted.
    for existing_var in await variable_service.list_variables(db, ws.id):
        await variable_service.delete_variable(db, existing_var)
    for vname in sorted(effective):
        var_value, structured = _input_var_repr(effective[vname])
        await variable_service.create_variable(
            db,
            workspace_id=ws.id,
            key=vname,
            value=var_value,
            category="terraform",
            structured=structured,
            sensitive=vname in sensitive_names,
        )

    # Generate + upload the wrapper config version.
    files = render_wrapper_hcl(
        module,
        resolved_version,
        var_decls=wired_inputs,
        module_wired=module_wired,
        sensitive_names=sensitive_names,
        module_outputs=module_outputs,
        provider_templates=templates,
    )
    # The Ansible half (#2019). Copied out of the module into this very
    # configuration version, because the configure Job re-fetches the CV and
    # never runs `init` -- see `ansible_files_from_module`.
    #
    # Gated on `configure_enabled` ALONE, never on the path being non-empty:
    # the boolean says whether and the path says where, so an item with a path
    # and the switch off provisions no configure, which is what it asked for --
    # and an item with the switch ON and no path is incoherent rather than a
    # no-op, so it must reach `_require_subdirectory` and be refused. Gating on
    # both would skip the block instead, leaving a workspace whose Ansible root
    # is the configuration version's root, where only the generated wrapper
    # lives: a configure that fails at run time for want of a playbook.
    #
    # The same cross-field shape as #1966's `validate_operations`, which takes
    # the RESULTING values rather than the submitted ones.
    cv_files: dict[str, str | bytes] = dict(files)
    if item.configure_enabled:
        if resolved_version is None:
            raise CatalogError(
                f"Module {module.name} has no uploaded version to copy the catalog "
                f"item's ansible-directory {item.ansible_directory!r} from",
                status_code=422,
            )
        _require_subdirectory(item.ansible_directory)
        ansible_files = await ansible_files_from_module(
            namespace=module.namespace,
            name=module.name,
            provider=module.provider,
            version=resolved_version,
            subdir=item.ansible_directory,
        )
        # No collision check, and that is a measured absence rather than an
        # oversight: every copied member keeps its `<ansible_directory>/...`
        # path while the generated wrapper's files are always at the root, so a
        # module shipping `ansible/main.tf` lands at `ansible/main.tf` and
        # cannot shadow the root module. The root form that COULD collide is
        # refused above, by `_require_subdirectory`. A check here would be a
        # gate that cannot fire -- which this codebase has shipped before and
        # which reads as protection while protecting nothing.
        cv_files.update(ansible_files)

    _apply_ansible_settings(ws, item)

    tarball = await asyncio.to_thread(_build_tarball, cv_files)

    cv = await run_service.create_configuration_version(
        db, workspace_id=ws.id, source="catalog", auto_queue_runs=False
    )
    await db.flush()
    from terrapod.storage import get_storage
    from terrapod.storage.keys import config_version_key

    storage = get_storage()
    key = config_version_key(str(ws.id), str(cv.id))
    await storage.put_stream(key, _single_chunk(tarball), content_type="application/gzip")
    await run_service.mark_configuration_uploaded(db, cv)

    try:
        run = await run_service.create_run(
            db,
            workspace=ws,
            message=message,
            auto_apply=auto_apply,
            plan_only=False,
            source="catalog",
            configuration_version_id=cv.id,
            created_by=user_email,
        )
    except run_service.PlanApplyNotEnabled as exc:
        raise CatalogError(str(exc), status_code=422) from exc
    await run_service.queue_run(db, run)
    return run


async def provision_instance(
    db: AsyncSession,
    *,
    user_email: str,
    item: CatalogItem,
    name: str,
    agent_pool_id: uuid.UUID,
    input_values: dict,
    version_pin: str | None,
    auto_apply: bool,
    labels: dict,
) -> Workspace:
    """Create a catalog-managed workspace from a catalog item and queue its run.

    Caller is responsible for the RBAC checks (catalog 'use' on the item, pool
    'write' on ``agent_pool_id``, and ``agent_pool_id`` ∈ item.allowed pools).
    Commits are left to the caller.
    """
    module: RegistryModule = item.module
    if module is None:
        raise CatalogError("Catalog item has no backing module", status_code=409)

    # Workspace name uniqueness.
    existing = await db.execute(select(Workspace).where(Workspace.name == name))
    if existing.scalar_one_or_none() is not None:
        raise CatalogError(f"Workspace '{name}' already exists", status_code=409)

    ws = Workspace(
        name=name,
        execution_mode="agent",
        # Both columns, always (#1301). `auto_apply_mode` carries the intent
        # and `auto_apply` is its boolean projection; the model comment asserts
        # they cannot disagree, and every other write path pairs them. Setting
        # only the boolean left catalog rows persistently inconsistent — benign
        # today because `resolve_auto_apply_mode` composes asymmetrically and
        # errs towards less automation either way, but a trap for anyone who
        # later reads the column directly. The catalog offers no conditional
        # modes, so the pair is always one of the two flat ones.
        auto_apply=auto_apply,
        auto_apply_mode="always" if auto_apply else "never",
        execution_backend=settings.default_execution_backend,
        engine_version=settings.default_terraform_version,
        labels=labels or {},
        owner_email=user_email,
        catalog_item_id=item.id,
        catalog_version_pin=version_pin,  # None = float; explicit pin sticks
        # catalog_input_values is set by _materialise to the non-sensitive
        # resolved subset (secrets are write-only and never snapshotted here).
    )
    pool_set.set_workspace_pools(ws, [agent_pool_id])
    db.add(ws)
    await db.flush()  # assign ws.id

    # Link the module → workspace so module-impact analysis fires on it.
    db.add(ModuleWorkspaceLink(module_id=module.id, workspace_id=ws.id, created_by=user_email))
    await db.flush()

    await _materialise(
        db,
        ws,
        item,
        input_values=input_values,
        version_pin=version_pin,
        auto_apply=auto_apply,
        message=f"Catalog provision: {item.name}",
        user_email=user_email,
    )

    logger.info(
        "catalog.provisioned",
        workspace_id=str(ws.id),
        catalog_item=item.name,
        module=module.name,
        auto_apply=auto_apply,
    )
    return ws


async def reconfigure_instance(
    db: AsyncSession,
    *,
    user_email: str,
    ws: Workspace,
    input_values: dict,
    version_pin: str | None,
    auto_apply: bool,
):
    """Update a catalog instance's inputs and/or version pin, then queue a run.

    Re-renders the wrapper config against the (possibly new) version, replaces
    the workspace variables, and snapshots the new pin + input values. Caller
    owns the RBAC check (catalog 'use' on the originating item) and the commit.
    Returns the queued ``Run``.
    """
    if ws.catalog_item_id is None:
        raise CatalogError("Workspace is not catalog-managed", status_code=409)
    item = await db.get(CatalogItem, ws.catalog_item_id)
    if item is None:
        raise CatalogError("Catalog item no longer exists", status_code=409)

    ws.catalog_version_pin = version_pin
    # Both columns, always — see the construction above (#1301).
    ws.auto_apply = auto_apply
    ws.auto_apply_mode = "always" if auto_apply else "never"
    # catalog_input_values (non-sensitive only) is refreshed inside _materialise.

    run = await _materialise(
        db,
        ws,
        item,
        input_values=input_values,
        version_pin=version_pin,
        auto_apply=auto_apply,
        message=f"Catalog reconfigure: {item.name}",
        user_email=user_email,
    )
    logger.info(
        "catalog.reconfigured",
        workspace_id=str(ws.id),
        catalog_item=item.name,
        auto_apply=auto_apply,
    )
    return run


async def destroy_instance(
    db: AsyncSession,
    *,
    user_email: str,
    ws: Workspace,
    auto_apply: bool,
):
    """Queue a destroy run for a catalog instance. On a successful apply the
    run reconciler archives the workspace (run_service.transition_run keys on
    ``source == "catalog-lifecycle"``).

    Caller owns the RBAC check (catalog 'use' on the originating item) and the
    commit. Returns the queued ``Run``.
    """
    if ws.catalog_item_id is None:
        raise CatalogError("Workspace is not catalog-managed", status_code=409)

    # Destroy the latest generated config; without a CV the runner has no code.
    cv = await run_service.get_latest_uploaded_cv(db, ws.id)
    try:
        run = await run_service.create_run(
            db,
            workspace=ws,
            message="Catalog destroy",
            is_destroy=True,
            auto_apply=auto_apply,
            plan_only=False,
            source="catalog-lifecycle",
            configuration_version_id=cv.id if cv else None,
            created_by=user_email,
        )
    except run_service.PlanApplyNotEnabled as exc:
        raise CatalogError(str(exc), status_code=422) from exc
    await run_service.queue_run(db, run)
    logger.info("catalog.destroy_queued", workspace_id=str(ws.id), auto_apply=auto_apply)
    return run


# ── CRUD helpers ───────────────────────────────────────────────────────


async def get_catalog_item(db: AsyncSession, item_id: uuid.UUID) -> CatalogItem | None:
    return await db.get(CatalogItem, item_id)


async def list_catalog_items(db: AsyncSession) -> list[CatalogItem]:
    result = await db.execute(select(CatalogItem).order_by(CatalogItem.name))
    return list(result.scalars().all())


async def list_instances(
    db: AsyncSession, item_id: uuid.UUID, *, active_only: bool = False
) -> list[Workspace]:
    """List workspaces provisioned from a catalog item. ``active_only`` excludes
    ``archived`` instances (destroyed-and-reclaimed) — used by the item-delete
    guard so a successfully-destroyed instance doesn't permanently block deleting
    its item (the FK is ``ondelete=SET NULL``, so archived rows survive cleanly)."""
    stmt = select(Workspace).where(Workspace.catalog_item_id == item_id)
    if active_only:
        stmt = stmt.where(Workspace.lifecycle_state != "archived")
    result = await db.execute(stmt.order_by(Workspace.name))
    return list(result.scalars().all())


async def get_provider_template(
    db: AsyncSession, template_id: uuid.UUID
) -> ProviderTemplate | None:
    return await db.get(ProviderTemplate, template_id)


async def list_provider_templates(db: AsyncSession) -> list[ProviderTemplate]:
    result = await db.execute(select(ProviderTemplate).order_by(ProviderTemplate.name))
    return list(result.scalars().all())


def _now() -> datetime:
    return datetime.now(UTC)
