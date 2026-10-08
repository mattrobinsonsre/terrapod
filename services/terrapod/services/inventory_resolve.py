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
eight structures' writers, and missing one is a silent stale read. A TTL still
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
import stat
import tempfile
import uuid
from pathlib import Path
from typing import Any

import structlog
import yaml

from terrapod.config import settings
from terrapod.services.api_ansible import ansible_inventory_binary

logger = structlog.get_logger(__name__)

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


def render_inventory_yaml(
    *,
    hosts: list[tuple[str, dict[str, Any]]],
    groups: list[tuple[str, dict[str, Any]]],
    memberships: list[tuple[str, str]],
    children: list[tuple[str, str]],
    global_vars: dict[str, Any],
) -> str:
    """The declared rows as one YAML inventory file.

    Rooted at `all:`, which is YAML inventory's own structure -- and exactly why
    `all` cannot also be a declared group name. The `group_vars/all` rows land
    at that root's `vars:`, so they apply to every host without a group row to
    collide with.

    Group membership and nesting are emitted as ansible's `hosts:` and
    `children:` maps and nothing more. Precedence among `all`, a more specific
    group and a host variable is ansible's to apply; emitting the structure
    faithfully is the whole job here.
    """
    members: dict[str, list[str]] = {}
    for group_name, host_name in memberships:
        members.setdefault(group_name, []).append(host_name)

    nested: dict[str, list[str]] = {}
    for parent, child in children:
        nested.setdefault(parent, []).append(child)

    group_block: dict[str, Any] = {}
    for name, variables in groups:
        entry: dict[str, Any] = {}
        if name in members:
            # A mapping with null values, not a list: that is the shape
            # ansible's YAML plugin expects for a group's hosts.
            entry["hosts"] = dict.fromkeys(sorted(members[name]))
        if name in nested:
            entry["children"] = dict.fromkeys(sorted(nested[name]))
        if variables:
            entry["vars"] = variables
        # An empty group is still a group -- ansible will report it, and a
        # reader asking "did my group get created" needs to see it.
        group_block[name] = entry or None

    root: dict[str, Any] = {}
    if global_vars:
        root["vars"] = global_vars
    # Every host at the root too, so a host in no group is still in the
    # inventory. Without this a group-less host would exist only if some group
    # happened to name it.
    if hosts:
        root["hosts"] = {name: (variables or None) for name, variables in sorted(hosts)}
    if group_block:
        root["children"] = group_block

    # `default_flow_style=False` and `sort_keys=True` so the same rows render
    # the same bytes -- which is what makes a rendered file comparable between
    # two reads when something looks wrong.
    return yaml.safe_dump({"all": root or None}, default_flow_style=False, sort_keys=True)


# ── Refusing a plugin source ─────────────────────────────────────────────────


def _refuse_plugin_sources(root: Path) -> None:
    """Refuse any fetched YAML that is a plugin config, and strip the exec bit.

    Three layers close dynamic inventory (#1970, closed NOT_PLANNED), and this
    is the one that matters:

    1. **The plugin is not installed.** `ansible-core` ships no collections, so
       `amazon.aws.aws_ec2` does not exist here. Decisive but silent.
    2. **`enable_plugins = yaml, ini`** in the generated `ansible.cfg` -- no
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


def _write_ansible_cfg(directory: Path) -> Path:
    """The `ansible.cfg` the resolution runs under.

    `enable_plugins = yaml, ini` is layer 2 of the plugin refusal above: no
    `auto` (the dispatcher) and no `script` (an executable source).

    `any_unparsed_is_failed = True` is adopted from a production repository and
    is exactly #1967's requirement -- a source that cannot be read must not
    silently resolve to a partial host set. Without it an unreadable file is a
    warning and the answer is a target set quietly missing whatever it held.
    """
    cfg = directory / "ansible.cfg"
    cfg.write_text(
        "[defaults]\n"
        "# Nothing here should ever reach a remote host: this is a parse.\n"
        "host_key_checking = False\n"
        "retry_files_enabled = False\n"
        "\n"
        "[inventory]\n"
        "# No `auto` and no `script`: see inventory_resolve._refuse_plugin_sources.\n"
        "enable_plugins = yaml, ini\n"
        "# A source that cannot be parsed fails the resolution rather than\n"
        "# silently contributing nothing (#1967).\n"
        "any_unparsed_is_failed = True\n"
    )
    return cfg


# ── Running ansible ──────────────────────────────────────────────────────────


async def run_ansible_inventory(
    sources: list[Path], *, cfg: Path, limit: str | None = None
) -> dict[str, Any]:
    """`ansible-inventory --list` over `sources`, in `-i` order.

    The sources are passed in the order given, which is the order that decides a
    conflicting variable -- see the module docstring. `--limit` is passed
    straight through, so the expansion is ansible's own, `~regex` included.
    """
    binary = await ansible_inventory_binary()

    argv = [binary, "--list"]
    if limit:
        argv += ["--limit", limit]
    for source in sources:
        argv += ["-i", str(source)]

    proc = await asyncio.create_subprocess_exec(
        *argv,
        env={
            **os.environ,
            "ANSIBLE_CONFIG": str(cfg),
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
            "ANSIBLE_HOME": str(cfg.parent / ".ansible"),
            "ANSIBLE_LOCAL_TEMP": str(cfg.parent / ".ansible-tmp"),
            # Nothing is being connected to, so a missing interpreter on a
            # host is not a thing that can happen -- and the warning would
            # otherwise land in stderr and look like a failure.
            "ANSIBLE_DEPRECATION_WARNINGS": "False",
        },
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
        InventorySettings,
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

    # The settings too: flipping `include_platform` changes the resolution
    # without touching a single row.
    result = await db.execute(
        select(InventorySettings.include_platform, InventorySettings.updated_at).where(
            InventorySettings.workspace_id == workspace_id
        )
    )
    row = result.first()
    parts.append(f"s{int(bool(row[0]))}@{row[1].isoformat()}" if row else "s-")

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


async def resolve(db, workspace_id: uuid.UUID, *, limit: str | None = None) -> dict[str, Any]:
    """The workspace's inventory, resolved by ansible, cached in Redis.

    Reads nothing from, and writes nothing to, any inventory table: the
    resolution is a function of the rows, so there is no record of it to keep
    and no staleness for a reader to reason about.
    """
    from terrapod.redis.client import get_redis_client
    from terrapod.services import inventory_service as inv

    settings_row = await inv.get_settings(db, workspace_id)
    include_platform = settings_row.include_platform if settings_row else True
    git_sha = ""
    if settings_row is not None and settings_row.vcs_connection_id and settings_row.repo_url:
        git_sha = await _resolve_git_sha(db, settings_row)

    rev = await platform_rev(db, workspace_id)
    key = cache_key(workspace_id, rev, git_sha, limit)

    redis = get_redis_client()
    cached = await redis.get(key)
    if cached:
        return json.loads(cached)

    resolved = await _resolve_uncached(
        db,
        workspace_id,
        settings_row=settings_row,
        include_platform=include_platform,
        git_sha=git_sha,
        limit=limit,
    )
    await redis.setex(key, CACHE_TTL_SECONDS, json.dumps(resolved))
    return resolved


async def _resolve_git_sha(db, settings_row) -> str:  # type: ignore[no-untyped-def]
    """The commit the bound branch currently points at.

    The one input that cannot be derived from rows we hold, which is why the
    cache entry needs a TTL at all.
    """
    from sqlalchemy import select

    from terrapod.db.models import VCSConnection
    from terrapod.services import vcs_provider

    result = await db.execute(
        select(VCSConnection).where(VCSConnection.id == settings_row.vcs_connection_id)
    )
    conn = result.scalar_one_or_none()
    if conn is None:
        raise InventoryResolutionFailed(
            "the VCS connection this inventory is bound to no longer exists, so the "
            "repository half cannot be fetched. Rebind it or clear the binding."
        )

    parsed = vcs_provider.parse_repo_url(conn, settings_row.repo_url)
    if parsed is None:
        raise InventoryResolutionFailed(
            f"could not parse {settings_row.repo_url!r} as a repository for this "
            f"{conn.provider} connection"
        )
    owner, repo = parsed
    branch = settings_row.branch or await vcs_provider.get_default_branch(conn, owner, repo)
    if not branch:
        raise InventoryResolutionFailed(
            f"could not determine a branch for {owner}/{repo}: the inventory sets none "
            f"and the repository's default branch could not be read"
        )
    sha = await vcs_provider.get_branch_sha(conn, owner, repo, branch)
    if not sha:
        raise InventoryResolutionFailed(f"branch {branch!r} was not found in {owner}/{repo}")
    return sha


async def _resolve_uncached(
    db,
    workspace_id: uuid.UUID,
    *,
    settings_row,
    include_platform: bool,
    git_sha: str,
    limit: str | None,
) -> dict[str, Any]:
    """Render, fetch, run ansible, read the output. One scratch directory."""
    import shutil

    workdir = Path(await asyncio.to_thread(make_tempdir))
    try:
        cfg = await asyncio.to_thread(_write_ansible_cfg, workdir)
        sources: list[Path] = []

        # The repository first, the declared rows second: a later `-i` wins a
        # conflicting variable, so the declared row overrides the committed
        # baseline. See the module docstring.
        if git_sha:
            sources.append(await _fetch_vcs_source(db, settings_row, git_sha, workdir))

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

        document = await run_ansible_inventory(sources, cfg=cfg, limit=limit)
        return normalise(document)
    finally:
        await asyncio.to_thread(shutil.rmtree, workdir, ignore_errors=True)


async def _fetch_vcs_source(db, settings_row, git_sha: str, workdir: Path) -> Path:
    """The repository's inventory directory, fetched and made inert.

    Narrowed to `working_directory` where one is set, so a monorepo does not
    ship its whole tree to resolve an inventory -- the archive cache already
    supports that and two callers agreeing on the path set share an entry.

    Everything fetched goes through `_refuse_plugin_sources` before ansible sees
    it: the exec bit comes off and a plugin configuration is refused by name.
    """
    from sqlalchemy import select

    from terrapod.db.models import VCSConnection
    from terrapod.services import vcs_provider
    from terrapod.services.vcs_archive_cache import VCSArchiveCache, materialize_archive

    result = await db.execute(
        select(VCSConnection).where(VCSConnection.id == settings_row.vcs_connection_id)
    )
    conn = result.scalar_one()
    owner, repo = vcs_provider.parse_repo_url(conn, settings_row.repo_url)  # type: ignore[misc]

    subdir = (settings_row.working_directory or "").strip("/")
    paths = [subdir] if subdir else None

    # A fresh instance per resolution. Its own docstring says one per logical
    # work unit -- "one UI request" is named -- because `_known` grows without
    # bound and a stale entry can mask a cross-replica invalidation.
    cache = VCSArchiveCache()
    storage_key = await cache.get_or_fetch(conn, owner, repo, git_sha, paths)

    dest = workdir / "vcs"
    # `materialize_archive` yields the TARBALL, not a tree, so extracting is
    # ours to do -- and ours to do safely.
    async with materialize_archive(storage_key) as tarball:
        await asyncio.to_thread(_extract, tarball, dest)

    root = dest / subdir if subdir else dest
    if not root.is_dir():
        raise InventoryResolutionFailed(
            f"{subdir or '/'} is not a directory in {owner}/{repo} at {git_sha[:8]} -- "
            f"the inventory's working directory names a path the repository does not "
            f"have at that commit"
        )

    await asyncio.to_thread(_refuse_plugin_sources, root)
    return root


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
