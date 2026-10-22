"""Narrow a derived configure to the hosts an inventory change affected (#2229).

**Narrowing is the DEFAULT, conditioned on the ansible code being unchanged.**
The user's rule:

    a composite or autoconfigure run targets only changed hosts IF there is no
    change to the ansible code. If there is a change to the ansible code it
    targets all hosts. Override with a `configure_all_hosts` option

That condition is the thing that actually justifies breadth. New playbook or role
code must reach every host; a changed HOST is no reason at all to touch the
others. And the override widens, which is the safe direction for a flag someone
forgets: forgetting it configures more, never less.

## The diff is over the RESOLVED inventory, never the declared rows

This is load-bearing. A row-level diff would say "this group variable changed"
and leave us to work out which hosts inherit it -- walking the group DAG,
including `[group:children]` nesting, which is exactly what the inventory model
forbids:

    Ansible performs the merge, the precedence, the group DAG, the derivation of
    `all`/`ungrouped` and the expansion of `--limit`. Terrapod renders the input
    and reads the output, and there is NO SECOND IMPLEMENTATION of any of it.

With the resolved form ansible has already done the attribution, so a host
variable, a group variable and a `group_vars/all` variable all come out as "these
hosts differ" with no cases of ours. A global change naturally returns every
host, so the run is not narrowed -- correct, and automatic.

## Four reasons to go wide, and the empty-set case

1. `configure_all_hosts` -- the operator's override.
2. No baseline: the first configure ever, or the last one failed. Hosts may be
   unconfigured for reasons no diff can see (#1930 owns the failure case).
3. The ansible code under `config_directory` moved since the baseline.
4. The group NESTING changed. A `children` edit alters what a pattern resolves
   to without necessarily changing any host's direct membership, and computing
   the affected subtree is the DAG walk above. Rare, broad in effect, so wide.

An **empty** changed set also returns the workspace's own limit rather than a
limit matching nothing: `--limit` matching nothing exits 1 (measured), so
emitting one would fail a run that had nothing to do. Suppressing such a run
entirely is better and is #2229's remaining open decision -- it changes the run
lifecycle, so it is not settled here.

## The intersection is computed, not expressed as a pattern

`changed:&<default-limit>` works for a single-term limit (measured:
`h1:h3:&canary` -> `h1`), but ansible applies pattern terms left to right with no
grouping, so it cannot express `changed ∩ (A ∪ B)` for a multi-term limit --
`h1:h2:&canary:prod` parses as `({h1,h2} ∩ canary) ∪ prod`, which is WIDER than
either input. So ansible expands the workspace's limit, and the intersection is
taken here over host names.

The emitted limit is therefore a plain `:`-joined host list, which is safe
because `validate_host_name` refuses `, : ! & ~` and whitespace -- those being
`--limit`'s own operators. A host cannot carry a character that would re-parse.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.db.models import Run, Workspace
from terrapod.logging_config import get_logger
from terrapod.storage.keys import config_version_key, inventory_baseline_key

logger = get_logger(__name__)

#: The sources that mean "the PLATFORM decided this run", as against somebody
#: asking for one. Narrowing applies to a derived configure only: a configure
#: a person queued with no limit means "the workspace's default", so narrowing
#: it would be overriding a request rather than bounding a side effect.
#:
#: A COMPOSITE is derived whatever its source -- the apply decided it -- and is
#: narrowed at the hand-over instead of here, because its inventory is only
#: knowable once the apply has finished moving the estate (#2230).
DERIVED_SOURCES = frozenset({"auto-configure", "vcs"})


def is_derived_configure(run: Run) -> bool:
    """A standalone configure the platform started by itself."""
    return (
        bool(run.is_configure)
        and not bool(run.configure_after_apply)
        and run.source in DERIVED_SOURCES
    )


#: Why a run went wide, for the log line and for the tests to assert on.
WIDE_OVERRIDE = "configure_all_hosts is set"
WIDE_NO_BASELINE = "no previous successful configure to compare against"
WIDE_CODE_CHANGED = "the ansible code changed since the last successful configure"
WIDE_NESTING = "the group nesting changed"
WIDE_NOTHING_CHANGED = "no host's resolved configuration changed"


async def load_baseline(workspace_id: str) -> dict[str, Any] | None:
    """The stored baseline, or None when there is not one yet."""
    from terrapod.storage import get_storage
    from terrapod.storage.protocol import ObjectNotFoundError

    try:
        raw = await get_storage().get(inventory_baseline_key(workspace_id))
    except ObjectNotFoundError:
        return None
    except Exception:  # noqa: BLE001
        # A baseline we cannot read is a baseline we do not have, and the
        # consequence of that is a WIDE run -- safe. Erroring here would fail a
        # configure over an optimisation.
        logger.warning("Could not read the inventory baseline", workspace_id=workspace_id)
        return None
    try:
        return json.loads(raw)
    except ValueError:
        logger.warning("The inventory baseline is not JSON", workspace_id=workspace_id)
        return None


async def store_baseline(
    workspace_id: str, *, document: dict, config_version_id: str | None
) -> None:
    """Record what this configure targeted, as the next one's baseline.

    Carries the configuration version alongside the document so "did the ansible
    code change" needs no extra column: the comparison is this run's CV against
    the one the baseline was taken at.
    """
    from terrapod.storage import get_storage

    await get_storage().put(
        inventory_baseline_key(workspace_id),
        json.dumps({"configuration-version-id": config_version_id, "document": document}).encode(),
        content_type="application/json",
    )


def changed_hosts(baseline: dict, current: dict) -> tuple[set[str], str | None]:
    """Hosts whose resolved configuration moved, or a reason to go wide.

    A host's resolved state here is its effective variables AND the groups it
    belongs to. Variables alone would miss a host moved into a group that
    carries no `group_vars`, which is an ordinary way to bring one into scope --
    the play reaching it is `hosts: webservers`, not a variable.
    """
    if (baseline.get("children") or {}) != (current.get("children") or {}):
        return set(), WIDE_NESTING

    b_hosts: dict[str, Any] = baseline.get("hosts") or {}
    c_hosts: dict[str, Any] = current.get("hosts") or {}

    def groups_of(doc: dict) -> dict[str, set[str]]:
        out: dict[str, set[str]] = {}
        for group, members in (doc.get("groups") or {}).items():
            for host in members or []:
                out.setdefault(host, set()).add(group)
        return out

    b_groups, c_groups = groups_of(baseline), groups_of(current)

    changed = set()
    for host, vars_now in c_hosts.items():
        if host not in b_hosts:
            changed.add(host)  # new host: nothing has ever been applied to it
            continue
        if vars_now != b_hosts[host]:
            changed.add(host)
            continue
        if c_groups.get(host, set()) != b_groups.get(host, set()):
            changed.add(host)
    # A host in the baseline and absent now is gone. There is nothing to
    # configure on a host that no longer exists, so it is deliberately not here.
    return changed, None


async def _code_changed(db: AsyncSession, run: Run, ws: Workspace, baseline_cv: str | None) -> bool:
    """Did the ansible code under `config_directory` move since the baseline?

    Scoped to that directory rather than "is this a different configuration
    version", which decides whether the feature is useful at all: a composite's
    CV almost always differs because it came from a push, so the coarse test
    would send every run wide. A Terraform-only push leaves the playbooks
    identical and should narrow; a push touching the ansible root should not.
    """
    this_cv = str(run.configuration_version_id) if run.configuration_version_id else None
    if this_cv == baseline_cv:
        return False
    if this_cv is None or baseline_cv is None:
        # One side has no code to compare. Treat as changed: we cannot show the
        # playbooks are identical, and the whole condition is "narrow only when
        # the code provably did not move".
        return True

    from terrapod.services import cv_diff_service

    ws_id = str(ws.id)
    try:
        diff = await cv_diff_service.diff_tarballs(
            config_version_key(ws_id, baseline_cv), config_version_key(ws_id, this_cv)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Could not diff the configuration versions; treating the code as changed",
            workspace_id=ws_id,
            err=str(exc),
        )
        return True

    root = (ws.config_directory or "").strip("/")
    prefix = f"{root}/" if root else ""
    for entry in diff.get("files") or []:
        path = str(entry.get("path") or "")
        if path.startswith(prefix):
            return True

    # A file over the per-file cap is EXCLUDED from `files` and listed in
    # `oversized` instead, so reading `files` alone says "unchanged" about a
    # large playbook or role that may well have moved -- a fail-open in the one
    # direction this module promises never to fail in. An oversized path under
    # the config root means we cannot tell, which is the same answer as changed.
    for path in diff.get("oversized") or []:
        if str(path).startswith(prefix):
            logger.info(
                "Treating the ansible code as changed: a file under the config root "
                "was too large to diff",
                workspace_id=str(ws.id),
                path=str(path),
            )
            return True
    return False


async def limit_for(db: AsyncSession, run: Run, ws: Workspace, *, current: dict) -> str:
    """The `--limit` a derived configure should carry.

    Returns `""` whenever it cannot prove narrowing is safe, so every failure
    mode is "configure more".

    **Empty rather than the workspace's `default_limit`, though the two dispatch
    identically.** `build_job_spec` already falls back to `default-limit` when
    the run carries none, so an empty value means exactly "this run adds no
    narrowing of its own" -- which leaves a wide derived run recorded, served
    and dispatched precisely as it is today. Writing the default onto the run
    would start serialising an `ansible-limit` attribute that is absent now,
    for no change in what runs.
    """
    ws_id = str(ws.id)
    default = ws.default_limit or ""

    def wide(reason: str) -> str:
        logger.info(
            "Configure targets every host in scope",
            workspace_id=ws_id,
            run_id=str(run.id),
            reason=reason,
        )
        return ""

    if ws.configure_all_hosts:
        return wide(WIDE_OVERRIDE)

    baseline = await load_baseline(ws_id)
    if baseline is None:
        return wide(WIDE_NO_BASELINE)

    if await _code_changed(db, run, ws, baseline.get("configuration-version-id")):
        return wide(WIDE_CODE_CHANGED)

    changed, reason = changed_hosts(baseline.get("document") or {}, current)
    if reason is not None:
        return wide(reason)
    if not changed:
        return wide(WIDE_NOTHING_CHANGED)

    if default:
        # Ansible expands the workspace's own limit; the intersection is taken
        # here. See the module docstring on why this is not a pattern.
        from terrapod.services import inventory_resolve

        allowed_doc = await inventory_resolve.resolve_for_run(db, run, limit=default)
        allowed = set((allowed_doc.get("hosts") or {}).keys())
    else:
        allowed = set((current.get("hosts") or {}).keys())

    target = sorted(changed & allowed)
    if not target:
        # The changed hosts are all outside the workspace's limit. Narrowing to
        # nothing would exit 1, so this is the same case as "nothing changed".
        return wide(WIDE_NOTHING_CHANGED)

    logger.info(
        "Configure narrowed to the hosts whose resolved configuration changed",
        workspace_id=ws_id,
        run_id=str(run.id),
        hosts=len(target),
    )
    return ":".join(target)
