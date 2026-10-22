"""Resolving an inventory by running ansible (#1967).

Terrapod renders the input and reads the output. Ansible performs the merge, the
precedence, the group DAG, the derivation of `all` and `ungrouped` and the
expansion of `--limit` -- which is why `inventory_resolution` is now only name
validation, and why there is no second implementation of any of that here.

    GET .../resolved[?limit=…]
      key = tp:inv_resolved:{workspace}:{platform_rev}:{git_sha}[:{limit_hash}]
      hit  -> return
      miss -> render the declared rows to ONE inventory YAML on the PVC
              fetch the VCS source, chmod -x it, refuse any plugin config
              ansible-inventory --list [--limit …] -i <vcs> -i <platform.yml>
              store, return

Either source may be absent: declared rows alone, a repository alone, both, or
neither -- and neither is a defined empty result rather than an error.

## The cache key is content-addressed, so there is nothing to invalidate

`platform_rev` is a cheap aggregate over the row tables and `git_sha` is the
resolved commit, so a write changes the key and the old entry simply expires.
The alternative -- invalidate on write -- is a call site in every one of the
seven structures' writers, and missing one is a silent stale read. A TTL still
bounds the entry, because a git sha is only knowable by fetching.

**`platform_rev` is a count AND a maximum timestamp, not a timestamp alone.** A
delete changes the resolution and leaves the maximum where it was, so a
timestamp-only stamp would go on serving a host that is gone. Asked of every
table, because a variable row moving changes the resolved inventory just as a
host row does.

## Merge order is fixed: the declared rows win

`-i <vcs> -i <platform.yml>`, so a conflicting variable goes to the declared
row -- the same relationship a Terraform variable has to a default coded in the
configuration. The committed inventory is the baseline; what Terraform declares
overrides it. Documented rather than configurable; if the reverse is ever wanted
it is one column.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any

import structlog
import yaml

from terrapod.config import settings

# The DOCUMENT is built by its own module because the RUNNER needs the same
# structure, and a second copy of a precedence-bearing builder is how the two
# sides of one run come to disagree about what a configure targeted (#1972).
# Serialising stays here: that module must import nothing, because the runner
# image ships no PyYAML (see its docstring).
from terrapod.services.inventory_render import build_inventory_document
from terrapod.storage.keys import config_version_key, resolved_inventory_key

logger = structlog.get_logger(__name__)


class AnsibleUnavailable(RuntimeError):
    """`ansible-inventory` is not in this image, so no inventory can be resolved.

    Raised rather than degraded, deliberately: a silently partial host set is
    worse than a refusal, and a configure must never target a set the platform
    could not resolve.
    """


#: The API's `ansible-inventory`, found on PATH.
#:
#: **The API's ansible-core is a property of the IMAGE, pinned in
#: `services/pyproject.toml` and installed from the committed hash-pinned lock
#: at build time.** It is deliberately not a Helm value, not a per-workspace
#: value, and not fetched at run time -- the three things the machinery this
#: replaced did, which is why it was wrong:
#:
#: * it created a virtualenv under the PVC tempdir on first use and
#:   `pip install ansible-core==<version>`-ed into it, so what the API ran
#:   depended on what a workspace had pinned and on reaching an index;
#: * it stood up an in-process HTTP shim serving pip a PEP 503 index off the
#:   PyPI pull-through, i.e. ansible-specific machinery layered on a cache that
#:   exists for Pulumi's language SDKs;
#: * it keyed the staged venv per version, so a fleet pinning several lines
#:   installed several ansibles into the API pod.
#:
#: A workspace's `ansible_version` selects its **execution environment** -- the
#: image its playbooks run in, where the ansible-core line genuinely matters
#: because it decides which target Pythons are supported (#2154). It has never
#: had anything to do with how the API parses an inventory FILE: that is a
#: static parse of a stable format, done once in the API, which then hands the
#: runner a finalised document (#2021).
#:
#: **There is no `version` parameter, and there must not be one** -- a parameter
#: is exactly how the per-workspace read got in last time.
_ANSIBLE_INVENTORY = "ansible-inventory"


def ansible_inventory_binary() -> str:
    """Path to the image's `ansible-inventory`, or raise.

    A plain PATH lookup, because the binary is installed in the image. Nothing
    is downloaded, staged, resolved or cached here.
    """
    found = shutil.which(_ANSIBLE_INVENTORY)
    if not found:
        raise AnsibleUnavailable(
            f"{_ANSIBLE_INVENTORY} is not on PATH in this API image. It is installed "
            "from services/pyproject.toml at build time, so this means the image was "
            "built without it rather than that anything needs configuring."
        )
    return found


#: How long a resolution may be served from Redis.
#:
#: Bounded only because of the git source: a `platform_rev` is DERIVED from the
#: rows it summarises, so it cannot be stale -- but a git sha is only knowable
#: by fetching, so without a bound a deleted branch or a force-push would be
#: served from a key nothing will ever change. Short, because resolving is
#: cheap and being wrong about a target set is not.
CACHE_TTL_SECONDS = 300

#: The Redis key space. Content-addressed, so entries are superseded rather than
#: invalidated.
_KEY_PREFIX = "tp:inv_resolved"

#: How long `ansible-inventory` may take before the read gives up. Generous: it
#: parses files and resolves a group graph, with no network of its own.
_ANSIBLE_TIMEOUT_SECONDS = 120

#: Inventory source files whose top-level `plugin:` key makes them a PLUGIN
#: config rather than a static inventory. Refused -- see `_refuse_plugin_sources`.
_YAML_SUFFIXES = (".yml", ".yaml")

#: Groups ansible derives rather than accepts, so they are not part of the
#: declared group set a reader is comparing against. The same two names
#: `inventory_resolution.DERIVED_GROUPS` refuses at the write.
_DERIVED = frozenset({"all", "ungrouped"})


class InventoryResolutionFailed(RuntimeError):
    """An inventory could not be resolved, so no host set can be served.

    Never degraded into an empty or partial result. A silently short target set
    is the failure mode this whole surface exists to prevent, and unlike the
    runner's policy gate there is no later evaluation to catch it.
    """


class InventorySourceRefused(InventoryResolutionFailed):
    """A fetched source is one Terrapod will not let ansible parse.

    Its own class because the operator action differs: the resolution did not
    fail, it was declined, and the message names the file and what to do with
    it instead.
    """


def _tmpdir() -> str | None:
    """The ephemeral PVC, or None for the system default in dev and tests.

    The four-line resolver every rule-14 path uses. A fetched repository and a
    rendered inventory are both "could realistically be large", which is the
    line that rule draws.
    """
    configured = settings.vcs.tmpdir
    if configured and os.path.isdir(configured):
        return configured
    return None


# ── Rendering the declared rows ──────────────────────────────────────────────


def render_inventory_yaml(**kwargs: Any) -> str:
    """The declared rows as one YAML inventory file, for `ansible-inventory`.

    The API's serialisation of `build_inventory_document`. YAML rather than the
    runner's JSON because this is the file a human debugging the resolver opens,
    and this process has PyYAML while the runner's does not.

    `default_flow_style=False` and `sort_keys=True` so the same rows render the
    same bytes -- which is what makes a rendered file comparable between two
    reads when something looks wrong.
    """
    return yaml.safe_dump(
        build_inventory_document(**kwargs), default_flow_style=False, sort_keys=True
    )


# ── Refusing a plugin source ─────────────────────────────────────────────────


def _refuse_plugin_sources(root: Path) -> None:
    """Refuse any fetched YAML that is a plugin config, and strip the exec bit.

    Three layers close dynamic inventory (#1970, closed NOT_PLANNED), and this
    is the one that matters:

    1. **The plugin is not installed.** `ansible-core` ships no collections, so
       `amazon.aws.aws_ec2` does not exist here. Decisive but silent.
    2. **`ANSIBLE_INVENTORY_ENABLED=yaml,ini` in the environment** -- no
       `auto`, which is the generic dispatcher that reads a file's `plugin:` key
       and runs whatever it names, and no `script`.
    3. **This.** Disabling the plugin does not make the file inert, it makes it
       GARBAGE INPUT: a `*.aws_ec2.yml` ends in `.yml`, so the `yaml` plugin's
       `verify_file` claims it whatever is inside and then treats `plugin:`,
       `regions:` and `filters:` as GROUP NAMES. An operator would get a
       confusing parse result rather than an answer, so the file is named and
       refused instead.

    The exec bit is stripped in the same walk. #1970 measured a static
    `hosts.yml` beside an executable `sneaky.sh` resolving to the union of both:
    "the exec bit is the whole decision". A production repository confirmed the
    other half -- nineteen inventory files, none executable, which is the only
    reason `script` being enabled there was harmless.
    """
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue

        mode = path.stat().st_mode
        if mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
            path.chmod(mode & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))

        if path.suffix.lower() not in _YAML_SUFFIXES:
            continue
        try:
            # One document, and only the top level matters. A file too large or
            # not parseable as YAML is left to ansible to report.
            document = yaml.safe_load(path.read_text(errors="replace"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(document, dict) and "plugin" in document:
            plugin = document.get("plugin")
            raise InventorySourceRefused(
                f"{path.relative_to(root)} is an inventory PLUGIN configuration "
                f"(plugin: {plugin!r}), and Terrapod does not run inventory plugins. "
                f"Cloud discovery is declared instead: a Terraform data source, "
                f"`for_each`, and a `terrapod_inventory_host` per machine -- which is "
                f"what a plugin gives you, except versioned in git, reviewed in a pull "
                f"request and visible in a plan. Remove this file from the inventory "
                f"directory."
            )


# ── Running ansible ──────────────────────────────────────────────────────────


def _ansible_env(scratch: Path) -> dict[str, str]:
    """The environment `ansible-inventory` resolves under.

    A named function rather than a literal inside the call so that the tests
    that DRIVE real ansible run under exactly this, rather than rebuilding
    their own and verifying a configuration production does not use.
    """
    # Every inherited `ANSIBLE_*` is REMOVED before ours are set, not merely
    # overridden. Env beats `ansible.cfg`, so an inherited setting is not untidy
    # but load-bearing: `ANSIBLE_CONFIG` would redirect the parse away from the
    # repository's own config, and `ANSIBLE_INVENTORY_ENABLED=auto` would
    # re-enable the plugin dispatcher layer 2 exists to disable. Overriding only
    # the keys we happen to name leaves every other one live, and the set of
    # ansible settings is far larger than the set we think about.
    inherited = {k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")}
    return {
        **inherited,
        # ── Our non-negotiables, as ENV rather than a generated config ──
        #
        # **There is deliberately no `ANSIBLE_CONFIG` here (#2035).**
        # `ansible.cfg` is single-file with no merging, so pointing
        # `ANSIBLE_CONFIG` at one we wrote meant the operator's own file was
        # not read at all -- measured: their `roles_path` and
        # `inventory_ignore_extensions` vanished from
        # `ansible-config dump`. Their `inventory_ignore_extensions` decides
        # WHICH FILES IN A DIRECTORY ARE SOURCES, so discarding it makes the
        # API resolve a different source set from the runner, which honours
        # their file -- the same divergence shelling out exists to prevent,
        # arriving by a route that has nothing to do with the version.
        #
        # Env beats `ansible.cfg` and leaves the rest of it standing, so
        # these four win and nothing else of theirs is lost. Names taken
        # from `ansible-config list`, not from memory.
        #
        # Layer 2 of the plugin refusal: no `auto` (the dispatcher that runs
        # whatever a file's `plugin:` key names) and no `script` (an
        # executable source). See `_refuse_plugin_sources`.
        "ANSIBLE_INVENTORY_ENABLED": "yaml,ini",
        # A source that cannot be parsed FAILS the resolution rather than
        # silently contributing nothing (#1967). Measured as env: exit 1 on
        # an unparseable source, 0 when False -- so the move off the
        # generated file keeps the property rather than trading it away.
        "ANSIBLE_INVENTORY_ANY_UNPARSED_IS_FAILED": "True",
        # Nothing here should ever reach a remote host: this is a parse.
        "ANSIBLE_HOST_KEY_CHECKING": "False",
        "ANSIBLE_RETRY_FILES_ENABLED": "False",
        # Ansible writes a few things of its own and the root filesystem is
        # read-only, so it is pointed at the scratch directory.
        #
        # `ANSIBLE_HOME`, NOT `HOME`. Clobbering `HOME` moves where the
        # interpreter looks for user site-packages, so an ansible installed
        # there stops being importable -- `--version` still works, because
        # it is answered before the import, and every real invocation dies
        # with `ModuleNotFoundError: No module named 'ansible'`. Production
        # installs into its own virtualenv and so would not have noticed;
        # the test harness did it too and took eleven tests down with it.
        "ANSIBLE_HOME": str(scratch / ".ansible"),
        "ANSIBLE_LOCAL_TEMP": str(scratch / ".ansible-tmp"),
        # Nothing is being connected to, so a missing interpreter on a
        # host is not a thing that can happen -- and the warning would
        # otherwise land in stderr and look like a failure.
        "ANSIBLE_DEPRECATION_WARNINGS": "False",
    }


async def run_ansible_inventory(
    sources: list[Path],
    *,
    scratch: Path,
    limit: str | None = None,
    cwd: Path | None = None,
) -> dict[str, Any]:
    """`ansible-inventory --list` over `sources`, in `-i` order.

    The sources are passed in the order given, which is the order that decides a
    conflicting variable -- see the module docstring. `--limit` is passed
    straight through, so the expansion is ansible's own, `~regex` included.

    `cwd` is NOT cosmetic and must be the config root (#1929). Measured against
    ansible-core 2.18.3, `group_vars`/`host_vars` are found in exactly two
    places: inside the inventory directory, or **at the working directory** --
    never merely beside the inventory directory. A real repository layout puts
    `group_vars/` at the config root beside the inventory, so without this the
    resolution returns the right hosts, the right groups, exit 0 and **every
    variable missing**, with nothing unparsed for `any_unparsed_is_failed` to
    catch. Relative paths in the operator's `ansible.cfg` also resolve against
    the cwd rather than against the config file.
    """
    argv = [ansible_inventory_binary(), "--list"]
    if limit:
        argv += ["--limit", limit]
    for source in sources:
        argv += ["-i", str(source)]

    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd) if cwd else None,
        env=_ansible_env(scratch),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_ANSIBLE_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        proc.kill()
        raise InventoryResolutionFailed(
            f"ansible-inventory did not finish within {_ANSIBLE_TIMEOUT_SECONDS}s"
        ) from exc

    if proc.returncode != 0:
        detail = err.decode(errors="replace").strip()[-1200:]
        raise InventoryResolutionFailed(
            f"ansible-inventory failed (exit {proc.returncode}): {detail}"
        )

    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise InventoryResolutionFailed(
            "ansible-inventory produced output that is not JSON"
        ) from exc


# ── Reading ansible's output ─────────────────────────────────────────────────


def normalise(document: dict[str, Any]) -> dict[str, Any]:
    """Ansible's `--list` output as a host set, a group map and the nesting.

    Three facts about ansible's shape, all measured against 2.21.5 and none of
    them documented. Each one loses something different if ignored.

    **A host with no variables is absent from `_meta.hostvars` entirely.** It
    appears only in a group's membership list, so the host set is the UNION of
    every group's `hosts` and the `_meta` keys -- either source alone loses
    hosts, the first missing a group-less host that has variables, the second
    missing a var-less one.

    **A group with no members and no variables has no top-level entry.** It
    appears only in `all.children`, so the group map is seeded from there --
    otherwise a group an operator has just declared would be missing from the
    view they declared it in, which reads as the write having failed.

    **A group's `hosts` is DIRECT membership; nesting is NOT flattened into
    it.** A parent whose only members come through a child reports no `hosts` at
    all. So `children` is carried through rather than resolved here: taking the
    transitive closure would be Terrapod computing the group DAG, which is
    ansible's job and not ours.

    The effective target set of a group is therefore `?limit=<group>`, which
    ansible expands through the nesting -- measured: limiting to a parent whose
    members are all inherited returns exactly those hosts. That is the
    authoritative answer to "what would this target", and it is why the limit is
    a parameter on this read rather than something a consumer assembles.
    """
    meta = document.get("_meta") or {}
    hostvars = meta.get("hostvars") or {}

    # Seeded from `all.children` so an empty declared group survives.
    declared = {
        name
        for name in ((document.get("all") or {}).get("children") or [])
        if isinstance(name, str) and name not in _DERIVED
    }
    groups: dict[str, list[str]] = {name: [] for name in declared}
    children: dict[str, list[str]] = {}

    named: set[str] = set()
    for key, value in document.items():
        if key == "_meta" or not isinstance(value, dict):
            continue

        members = value.get("hosts")
        members = [m for m in members if isinstance(m, str)] if isinstance(members, list) else []
        named.update(members)

        nested = value.get("children")
        nested = (
            [c for c in nested if isinstance(c, str) and c not in _DERIVED]
            if isinstance(nested, list)
            else []
        )

        if key not in _DERIVED:
            groups[key] = sorted(members)
            if nested:
                children[key] = sorted(nested)

    hosts = {name: dict(hostvars.get(name) or {}) for name in sorted(named | set(hostvars))}
    return {
        "hosts": hosts,
        "groups": dict(sorted(groups.items())),
        "children": dict(sorted(children.items())),
    }


# ── The cache key ────────────────────────────────────────────────────────────


def cache_key(workspace_id: uuid.UUID, platform_rev: str, git_sha: str, limit: str | None) -> str:
    """The content-addressed key.

    The limit is hashed rather than embedded: it is operator-supplied text that
    would otherwise put arbitrary characters -- spaces, colons, `!` -- into a
    Redis key, and colons are the key separator.
    """
    key = f"{_KEY_PREFIX}:{workspace_id}:{platform_rev}:{git_sha or '-'}"
    if limit:
        key += ":" + hashlib.sha256(limit.encode()).hexdigest()[:16]
    return key


async def platform_rev(db, workspace_id: uuid.UUID) -> str:
    """An equality token for "what the declared rows currently say".

    A count AND a maximum `updated_at` per table. The count is what makes a
    DELETE visible: removing a row changes the resolution and leaves the maximum
    exactly where it was, so a timestamp-only stamp would go on serving a host
    that is gone.

    Asked of every table that contributes, because a variable row moving changes
    the resolved inventory just as much as a host row does. It is DERIVED from
    the rows it summarises, which is why it needs no expiry of its own -- a
    derived stamp cannot be stale.
    """
    from sqlalchemy import func, select

    from terrapod.db.models import (
        InventoryGlobalVar,
        InventoryGroup,
        InventoryGroupChild,
        InventoryGroupVar,
        InventoryHost,
        InventoryHostGroup,
        InventoryHostVar,
        Workspace,
    )

    parts: list[str] = []
    for model in (
        InventoryHost,
        InventoryGroup,
        InventoryHostGroup,
        InventoryGroupChild,
        InventoryHostVar,
        InventoryGroupVar,
        InventoryGlobalVar,
    ):
        newest = getattr(model, "updated_at", None) or model.created_at
        result = await db.execute(
            select(func.count(), func.max(newest)).where(model.workspace_id == workspace_id)
        )
        count, stamp = result.one()
        parts.append(f"{int(count or 0)}@{stamp.isoformat() if stamp else '-'}")

    # The workspace's own inventory settings too (#1929): flipping
    # `inventory_include_platform`, or moving a root, changes the resolution
    # without touching a single inventory row. `updated_at` moves on any
    # workspace edit, which is wider than needed and safe in the only direction
    # that matters -- a spurious miss costs one resolution, a missed change
    # serves a stale host set.
    result = await db.execute(
        select(
            Workspace.inventory_include_platform,
            Workspace.config_directory,
            Workspace.inventory_paths,
            Workspace.updated_at,
        ).where(Workspace.id == workspace_id)
    )
    row = result.first()
    if row:
        parts.append(
            f"s{int(bool(row[0]))}:{row[1] or '-'}:{','.join(row[2] or [])}"
            f"@{row[3].isoformat() if row[3] else '-'}"
        )
    else:
        parts.append("s-")

    # Hashed so the key stays a sensible length whatever the table count grows
    # to. Opaque by design: nothing parses it, orders it or reads a time out of
    # it -- the only question asked is whether it equals the last one.
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:24]


def make_tempdir() -> str:
    """A scratch directory on the PVC for one resolution."""
    return tempfile.mkdtemp(prefix="tp-inv-", dir=_tmpdir())


# ── The orchestrator ─────────────────────────────────────────────────────────


async def _declared_sources(db, workspace_id: uuid.UUID) -> str:
    """The declared rows, rendered to one inventory YAML."""
    from terrapod.services import inventory_service as inv

    hosts = await inv.list_hosts(db, workspace_id)
    groups = await inv.list_groups(db, workspace_id)

    host_vars: dict[uuid.UUID, dict[str, Any]] = {}
    for host in hosts:
        host_vars[host.id] = {v.key: _coerce(v) for v in await inv.list_host_vars(db, host.id)}
    group_vars: dict[uuid.UUID, dict[str, Any]] = {}
    for group in groups:
        group_vars[group.id] = {v.key: _coerce(v) for v in await inv.list_group_vars(db, group.id)}

    host_name = {h.id: h.name for h in hosts}
    group_name = {g.id: g.name for g in groups}

    memberships = [
        (group_name[link.group_id], host_name[link.host_id])
        for link in await inv.list_host_groups(db)
        if link.workspace_id == workspace_id
        and link.group_id in group_name
        and link.host_id in host_name
    ]
    children = [
        (group_name[link.parent_group_id], group_name[link.child_group_id])
        for link in await inv.list_group_children(db)
        if link.workspace_id == workspace_id
        and link.parent_group_id in group_name
        and link.child_group_id in group_name
    ]

    return render_inventory_yaml(
        hosts=[(h.name, host_vars.get(h.id, {})) for h in hosts],
        groups=[(g.name, group_vars.get(g.id, {})) for g in groups],
        memberships=memberships,
        children=children,
        global_vars={v.key: _coerce(v) for v in await inv.list_global_vars(db, workspace_id)},
    )


def _coerce(var) -> Any:  # type: ignore[no-untyped-def]
    """A variable's value as ansible should see it.

    `structured` means the stored text is a typed expression rather than a
    plain string -- a list, a number, a nested object -- which is the same
    question `Variable.structured` answers (#1435) and the same thing ansible's
    own `group_vars` carries natively.

    Parsed as YAML rather than JSON because YAML is a superset and this is going
    straight back out as YAML: `[80, 443]` and `- 80` both work, so an operator
    writing either gets what they meant. A value that does not parse is passed
    through as the string it is, because refusing at read time would make a
    stored row unreadable -- the write is where a bad value is refused.
    """
    if not var.structured:
        return var.value
    try:
        return yaml.safe_load(var.value)
    except yaml.YAMLError:
        return var.value


async def write_run_artifact(db, run, *, limit: str | None = None) -> tuple[str, dict[str, Any]]:
    """Resolve the run's inventory and store it as the run's own artifact (#2021).

    Returns the storage key AND the document it stored. Called from run
    creation and from the composite hand-over, read by the configure phase
    through `GET /runs/{run_id}/artifacts/resolved-inventory`.

    **The document comes back because the caller that narrows is the caller
    that wrote it (#2229), and the two must agree.** Narrowing diffs the
    resolved inventory against the stored baseline and emits a `--limit`
    naming hosts; resolving a second time to get that document would let the
    limit name a host the artifact does not contain, and a `--limit` matching
    nothing exits 1 (measured). One resolution, used for both.

    **It raises rather than degrading, and that is deliberate.** A configure
    whose target set could not be resolved must not be dispatched: the Job would
    start, fail inside the phase, and have consumed a runner slot to tell us
    something the API already knew. The caller is run creation, so the refusal
    reaches whoever asked for the run, with the reason.

    **An empty inventory is written, not skipped.** A workspace that resolves to
    no hosts gets the ordinary empty document, so the phase can tell "nothing to
    target" from "could not read it" -- the second being the absence of the
    object. Writing nothing would make those two indistinguishable, which is
    the one thing the issue asked this to get right.
    """
    from terrapod.storage import get_storage

    document = await resolve_for_run(db, run, limit=limit)
    key = resolved_inventory_key(str(run.workspace_id), str(run.id))
    storage = get_storage()
    await storage.put(
        key,
        json.dumps(document).encode(),
        content_type="application/json",
    )
    logger.info(
        "resolved inventory stored for run",
        run_id=str(run.id),
        workspace_id=str(run.workspace_id),
        hosts=len(document.get("hosts") or {}),
        groups=len(document.get("groups") or {}),
        from_config_version=str(run.configuration_version_id or ""),
    )
    return key, document


async def resolve_for_run(db, run, *, limit: str | None = None) -> dict[str, Any]:
    """The inventory a RUN targets, pinned to the commit the run is running (#2021).

    The run path, and deliberately not `resolve` with an argument:

    * **Its git half comes from the run's configuration version**, which the
      poller collected when the code changed, so launching a configure fetches
      nothing. That is the Terraform shape -- poll or webhook, build a CV, run
      against it -- applied to Ansible code and inventory.
    * **It is not Redis-cached.** `resolve`'s cache exists because the branch
      head cannot be derived from rows we hold, so the entry needs a TTL. A CV
      is immutable and the declared rows are read once here, so there is
      nothing to go stale and nothing to invalidate -- and the result is
      persisted as the run's own artifact, which is the only cache this needs.
    * **A run with no configuration version resolves the declared rows alone.**
      That is the ordinary shape for a workspace whose inventory is entirely
      Terraform-declared: there is no git half to be missing.

    Raises `InventoryResolutionFailed` for anything that leaves the answer
    unknown, so a caller can refuse to dispatch rather than hand a configure an
    inventory it cannot vouch for.
    """
    from terrapod.services import inventory_service as inv

    ws = await inv.get_inventory_workspace(db, run.workspace_id)
    include_platform = ws.inventory_include_platform if ws else True

    # Same gate as `resolve`, minus the branch-head lookup: a CV is only a git
    # source for a workspace that has declared a configure operation (#1966),
    # because `workspace_fetch_paths` only puts the config root in the tarball
    # when `configure_enabled`. Without that the CV holds the Terraform tree and
    # no inventory, and extracting it would look like an empty inventory rather
    # than a misconfiguration.
    cv_id = None
    if ws is not None and ws.configure_enabled and run.configuration_version_id is not None:
        cv_id = run.configuration_version_id

    return await _resolve_uncached(
        db,
        run.workspace_id,
        ws=ws,
        include_platform=include_platform,
        git_sha="",
        limit=limit,
        config_version_id=cv_id,
    )


async def resolve(db, workspace_id: uuid.UUID, *, limit: str | None = None) -> dict[str, Any]:
    """The workspace's inventory, resolved by ansible, cached in Redis.

    Reads nothing from, and writes nothing to, any inventory table: the
    resolution is a function of the rows, so there is no record of it to keep
    and no staleness for a reader to reason about.
    """
    from terrapod.redis.client import get_redis_client
    from terrapod.services import inventory_service as inv

    ws = await inv.get_inventory_workspace(db, workspace_id)
    include_platform = ws.inventory_include_platform if ws else True
    git_sha = ""
    # A git source needs a binding AND a config root: `config_directory` being
    # `configure_enabled` is the declaration (#1966): a workspace that has not
    # declared a configure operation has nothing to fetch here even when it has
    # a repository for its Terraform. Not an emptiness test on
    # `config_directory` -- `""` is the ordinary way to say "at the root", so
    # that would skip the fetch for exactly the workspaces that want it most.
    if ws is not None and ws.vcs_connection_id and ws.vcs_repo_url and ws.configure_enabled:
        git_sha = await _resolve_git_sha(db, ws)

    rev = await platform_rev(db, workspace_id)
    key = cache_key(workspace_id, rev, git_sha, limit)

    redis = get_redis_client()
    cached = await redis.get(key)
    if cached:
        return json.loads(cached)

    resolved = await _resolve_uncached(
        db,
        workspace_id,
        ws=ws,
        include_platform=include_platform,
        git_sha=git_sha,
        limit=limit,
    )
    await redis.setex(key, CACHE_TTL_SECONDS, json.dumps(resolved))
    return resolved


async def _resolve_git_sha(db, ws) -> str:  # type: ignore[no-untyped-def]
    """The commit the bound branch currently points at.

    The one input that cannot be derived from rows we hold, which is why the
    cache entry needs a TTL at all.
    """
    from sqlalchemy import select

    from terrapod.db.models import VCSConnection
    from terrapod.services import vcs_provider

    result = await db.execute(select(VCSConnection).where(VCSConnection.id == ws.vcs_connection_id))
    conn = result.scalar_one_or_none()
    if conn is None:
        raise InventoryResolutionFailed(
            "the VCS connection this inventory is bound to no longer exists, so the "
            "repository half cannot be fetched. Rebind it or clear the binding."
        )

    parsed = vcs_provider.parse_repo_url(conn, ws.vcs_repo_url)
    if parsed is None:
        raise InventoryResolutionFailed(
            f"could not parse {ws.vcs_repo_url!r} as a repository for this "
            f"{conn.provider} connection"
        )
    owner, repo = parsed
    branch = ws.vcs_branch or await vcs_provider.get_default_branch(conn, owner, repo)
    if not branch:
        raise InventoryResolutionFailed(
            f"could not determine a branch for {owner}/{repo}: the inventory sets none "
            f"and the repository's default branch could not be read"
        )
    sha = await vcs_provider.get_branch_sha(conn, owner, repo, branch)
    if not sha:
        raise InventoryResolutionFailed(f"branch {branch!r} was not found in {owner}/{repo}")
    return sha


def _operator_inventory_paths(root: Path) -> list[str]:
    """The `inventory` pathlist from the operator's own `ansible.cfg`, if any.

    Read rather than overridden so our `-i` cannot disagree with what ansible
    itself would do from the same repository. `inventory` is `type: pathlist`
    -- a comma-separated list, measured against ansible-core 2.18.3 -- and the
    order is precedence, so it is preserved exactly.

    Deliberately tolerant: a malformed or absent config is not an error here,
    it just means there is nothing to default from. The resolution then falls
    back to the config root, which is what happened before this existed.
    """
    import configparser

    cfg = root / "ansible.cfg"
    if not cfg.is_file():
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(cfg)
    except configparser.Error:
        return []
    raw = parser.get("defaults", "inventory", fallback="") or parser.get(
        "inventory", "inventory", fallback=""
    )
    return [part.strip() for part in raw.split(",") if part.strip()]


def _inventory_sources(ws, root: Path) -> list[Path]:
    """The `-i` arguments for a git source, in precedence order.

    Three tiers, and the order is the point:

    1. The workspace's `inventory_paths`, when set. Explicit beats inferred.
    2. Otherwise the operator's own `ansible.cfg` `inventory` pathlist, so the
       common case needs no configuration at all.
    3. Otherwise the config root itself, which is the behaviour that predates
       this and remains correct for a repository whose inventory is one
       directory.

    An entry may name a FILE, and that is why this is a list rather than a
    single root: ansible's own documented layout puts `production` and
    `staging` side by side, and pointing at the containing directory instead
    MERGES them -- measured -- so a staging workspace would resolve production
    hosts.

    An entry naming a path the repository does not have at this commit is
    refused rather than skipped: a silently-dropped source resolves a partial
    inventory, which is the failure mode this whole issue exists to remove.
    """
    declared = [e.strip() for e in (ws.inventory_paths or []) if e and e.strip()]
    entries = declared or _operator_inventory_paths(root)
    if not entries:
        return [root]

    resolved: list[Path] = []
    for entry in entries:
        candidate = (root / entry.lstrip("/")).resolve()
        # Refuse an entry that escapes the fetched root rather than reading
        # whatever happens to be there.
        if not str(candidate).startswith(str(root.resolve())):
            raise InventoryResolutionFailed(
                f"inventory path {entry!r} escapes the workspace's config directory"
            )
        if not candidate.exists():
            raise InventoryResolutionFailed(
                f"inventory path {entry!r} does not exist under the config directory "
                f"at this commit -- a source that cannot be read would resolve a "
                f"partial inventory rather than failing"
            )
        resolved.append(candidate)
    return resolved


async def _resolve_uncached(
    db,
    workspace_id: uuid.UUID,
    *,
    ws,
    include_platform: bool,
    git_sha: str,
    limit: str | None,
    config_version_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Render, materialise, run ansible, read the output. One scratch directory.

    `config_version_id` takes precedence over `git_sha` and is the RUN path
    (#2021): the git half comes from the configuration version the run is
    executing, which the poller already collected, rather than from a fetch of
    wherever the branch now points. `git_sha` remains the read path, where
    "as of the branch head" is the right answer because there may be no run.
    """
    workdir = Path(await asyncio.to_thread(make_tempdir))
    try:
        sources: list[Path] = []

        # The repository first, the declared rows second: a later `-i` wins a
        # conflicting variable, so the declared row overrides the committed
        # baseline. See the module docstring.
        # The cwd for the ansible run. `group_vars`/`host_vars` are found at the
        # working directory as well as inside the inventory directory, and the
        # operator's relative `ansible.cfg` paths resolve against it, so it has
        # to be the config root (#1929).
        cwd: Path | None = None

        # The CV first: a run resolves against the commit it is running, and
        # only a read with no run falls back to the branch head.
        if config_version_id is not None:
            config_root = await _config_root_from_cv(ws, config_version_id, workdir)
            cwd = config_root
            sources.extend(_inventory_sources(ws, config_root))
        elif git_sha:
            config_root = await _fetch_vcs_source(db, ws, git_sha, workdir)
            cwd = config_root
            sources.extend(_inventory_sources(ws, config_root))

        if include_platform:
            rendered = await _declared_sources(db, workspace_id)
            platform_file = workdir / "platform.yml"
            await asyncio.to_thread(platform_file.write_text, rendered)
            sources.append(platform_file)

        if not sources:
            # A defined empty result, not an error: a workspace that has turned
            # the declared rows off and bound no repository has an inventory of
            # no hosts, and saying so is the honest answer.
            return {"hosts": {}, "groups": {}, "children": {}}

        document = await run_ansible_inventory(sources, scratch=workdir, limit=limit, cwd=cwd)
        return normalise(document)
    finally:
        await asyncio.to_thread(shutil.rmtree, workdir, ignore_errors=True)


async def _fetch_vcs_source(db, ws, git_sha: str, workdir: Path) -> Path:
    """The repository's **config root**, fetched whole and made inert.

    Narrowed to `config_directory`, not to the inventory directory, and that
    distinction is the bug this replaced (#1929). The config root contains the
    inventory directory, `group_vars`, `host_vars`, `roles/` and their
    submodules; the inventory directory alone contains none of those, and
    fetching it gave the right hosts, the right groups, exit 0 and every
    variable missing -- silently, because nothing was unparsed. It also left
    `roles/` empty where roles are submodules, since `_submodules_under`
    initialises only submodules inside the sparse set, so the first
    `include_role` would fail.

    A monorepo still does not ship its whole tree: `config_directory` IS the
    narrowing, and it is the smallest subtree that is correct rather than the
    smallest one that parses.

    Everything fetched goes through `_refuse_plugin_sources` before ansible sees
    it: the exec bit comes off and a plugin configuration is refused by name.
    """
    from sqlalchemy import select

    from terrapod.db.models import VCSConnection
    from terrapod.services import vcs_provider
    from terrapod.services.vcs_archive_cache import VCSArchiveCache

    result = await db.execute(select(VCSConnection).where(VCSConnection.id == ws.vcs_connection_id))
    conn = result.scalar_one()
    owner, repo = vcs_provider.parse_repo_url(conn, ws.vcs_repo_url)  # type: ignore[misc]

    # `config_directory` is the fetch scope AND the cwd. `""` is legitimate and
    # means the configuration version's root, so an empty value fetches the
    # whole tree rather than being treated as "nothing to do" -- the caller's
    # `configure_enabled` gate is what decides whether we are here at all
    # (#1966).
    subdir = ws.config_directory.strip("/")
    paths = [subdir] if subdir else None

    # A fresh instance per resolution. Its own docstring says one per logical
    # work unit -- "one UI request" is named -- because `_known` grows without
    # bound and a stale entry can mask a cross-replica invalidation.
    cache = VCSArchiveCache()
    storage_key = await cache.get_or_fetch(conn, owner, repo, git_sha, paths)

    return await _materialise_config_root(
        storage_key, ws, workdir, origin=f"{owner}/{repo} at {git_sha[:8]}"
    )


async def _config_root_from_cv(ws, config_version_id, workdir: Path) -> Path:
    """The config root from a run's CONFIGURATION VERSION -- no fetch (#2021).

    This is the run path, and it exists because the Terraform shape is right:
    the poller collects code on change, so by the time anyone launches a
    configure the bytes are already in storage. `vcs_poller` passes
    `configure_enabled` and `config_directory` into the fetch and
    `git_fetch.workspace_fetch_paths` includes the config root (#2117), so the
    CV already contains the playbooks, `roles/`, `group_vars`, `host_vars` and
    every `inventory_paths` entry -- the config root is taken wholesale, so
    those need no term of their own.

    **It is also the only shape that keeps the inventory and the playbook on
    one commit.** `_fetch_vcs_source` resolves the branch head, so a push
    between this run's CV being created and the configure launching would give
    the run a playbook from one commit and an inventory from another, silently.
    A CV is immutable, so taking both halves from it makes them agree by
    construction -- and the run artifact is then pinned to the commit the run
    is running, not merely to the moment it was created.

    No `VCSConnection` is read and no credential is needed, which is the
    security half of the same point: resolving for a run touches no network.
    """
    return await _materialise_config_root(
        config_version_key(str(ws.id), str(config_version_id)),
        ws,
        workdir,
        origin=f"configuration version {config_version_id}",
    )


async def _materialise_config_root(storage_key: str, ws, workdir: Path, *, origin: str) -> Path:
    """Unpack a stored repository archive and make it inert.

    Shared by the fetch path and the CV path because the bytes are the same
    shape: the poller copies the archive-cache tarball to the CV key verbatim,
    so both are a provider archive wrapped in its commit-named directory and
    `_extract` unwraps either. Two call sites, one treatment -- a second copy
    of the chmod and the plugin refusal is how one of them comes to be missing
    it.

    `origin` appears only in the error, and naming it is the point: "not a
    directory in owner/repo at 0a4cf1cb" and "not a directory in configuration
    version <id>" send an operator to different places.
    """
    from terrapod.services.vcs_archive_cache import materialize_archive

    subdir = (ws.config_directory or "").strip("/")

    dest = workdir / "vcs"
    # `materialize_archive` yields the TARBALL, not a tree, so extracting is
    # ours to do -- and ours to do safely.
    async with materialize_archive(storage_key) as tarball:
        await asyncio.to_thread(_extract, tarball, dest)

    root = dest / subdir if subdir else dest
    if not root.is_dir():
        raise InventoryResolutionFailed(
            f"{subdir or '/'} is not a directory in {origin} -- "
            f"the workspace's config directory names a path the repository does not "
            f"have at that commit"
        )

    # BEFORE the plugin scan, not after: an ignored file should not be read at
    # all, and scanning one would refuse the resolution over a file the
    # operator had already taken out of the source.
    await asyncio.to_thread(_apply_ignore_paths, root, list(ws.inventory_ignore_paths or []))
    await asyncio.to_thread(_refuse_plugin_sources, root)
    return root


def _apply_ignore_paths(root: Path, patterns: list[str]) -> None:
    """Remove anything under `root` an ignore pattern matches.

    Ansible reads a directory as one source, taking every file in it, so this
    is the only way to leave a file out of an inventory whose source is a
    directory. Without it `ignore_paths` would be accepted, stored, serialised
    and honoured by nothing -- and the failure is in the direction that reads
    as harmless: the source stays WIDER than the operator asked for, so hosts
    they had excluded appear. Hosts appearing is as wrong as hosts
    disappearing, and far less alarming to look at.

    Patterns are globs against the path RELATIVE TO `root`, so they are
    relative to `config_directory` (#1929) -- the config root this fetched, not
    the Terraform `working_directory`. That is the only coherent reading: the
    two are configured together and one names the source the other narrows, and
    it means an operator never writes a prefix they did not choose. Same matcher
    as an autodiscovery rule's `ignore_patterns` (`_match_glob`), so the two
    mean the same thing.

    Note `rel` carries NO trailing slash even for a directory, so a pattern
    written `cache/` would match nothing at all. `validate_inventory_ignore_paths`
    refuses a trailing slash for exactly that reason -- it is a pattern that
    looks like it excludes a directory and silently excludes nothing.

    Synchronous, called through `asyncio.to_thread`: a directory walk and
    unlinks are blocking I/O over something that could realistically be large.
    """
    if not patterns:
        return

    from terrapod.services.workspace_autodiscovery_service import _match_glob

    cleaned = [p.strip() for p in patterns if p.strip()]
    if not cleaned:
        return

    # Deepest first, so removing a directory cannot invalidate a path already
    # queued beneath it.
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        rel = path.relative_to(root).as_posix()
        if not any(_match_glob(rel, pattern) for pattern in cleaned):
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def _extract(tarball: str, dest: Path) -> None:
    """Unpack a fetched archive, refusing anything that escapes `dest`.

    `filter="data"` refuses absolute paths, `..` traversal, device nodes, setuid
    bits and symlinks pointing outside the tree -- the same guard
    `runner/phases/platform_tool.py` applies to a fetched tool, and for the same
    reason: this is someone else's archive.

    Synchronous on purpose, called through `asyncio.to_thread` by the one
    caller: `tarfile` is blocking and a repository is exactly the
    "could realistically be large" case rule 13 is about.
    """
    import tarfile

    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tarball, mode="r:*") as tf:
        tf.extractall(dest, filter="data")

    # A VCS provider's archive is wrapped in a single commit-named directory
    # (`owner-repo-<sha>/`), so unwrap it -- otherwise every path an operator
    # configures would have to carry a prefix they never chose and cannot
    # predict.
    entries = list(dest.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        inner = entries[0]
        for child in list(inner.iterdir()):
            child.rename(dest / child.name)
        inner.rmdir()
