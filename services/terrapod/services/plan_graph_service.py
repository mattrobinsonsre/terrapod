"""Derive a compact dependency graph from a run's stored plan JSON (#761).

Powers the run-page "Impact graph" — nodes are the resources in the plan
(coloured by their planned action), edges are dependencies. Terraform/OpenTofu
plan JSON has NO explicit instance-level edge list, so edges are DERIVED from
the `configuration` block's per-resource `expressions.*.references` (plus
`for_each`/`count` references), then expanded across `for_each` keys with a
same-key heuristic (source["api"] -> target["api"]; singletons fan out). This
is the standard way graph tools reconstruct the DAG.

Real infrastructure is almost always MODULAR, so the derivation walks the whole
`root_module` → `module_calls[].module` tree, not just root resources. A
reference is resolved in its own module scope: a sibling `type.name` becomes a
block address qualified with the module prefix (`module.vpc.aws_subnet.this`); a
`var.x` binds to the parent `module_call`'s input expression (resolved in the
PARENT scope); a `module.child.out` binds to the child module's output
expression (resolved in the CHILD scope). That cross-module binding is what
makes edges span module boundaries — without it a modular plan shows zero edges.

Deriving server-side (rather than shipping the raw, possibly multi-MB plan JSON
to the browser) keeps the payload small and works uniformly through the BFF in
every storage backend — the `json-output` endpoint's presigned redirect isn't
browser-reachable with the filesystem backend. The parse + derivation is CPU
work over a large buffer, so it runs in a thread (hard rule 13).
"""

from __future__ import annotations

import asyncio
import json
import re

from terrapod.db.models import Run
from terrapod.storage import get_storage
from terrapod.storage.keys import plan_json_output_key

# Normalised planned action per resource_change.
_ACTION = {
    ("create",): "create",
    ("delete",): "delete",
    ("update",): "update",
    ("no-op",): "noop",
    ("read",): "noop",
    ("delete", "create"): "replace",
    ("create", "delete"): "replace",
}

# Node-level colour when a block's instances have mixed actions: the most
# severe wins (a block with any replace reads as replace, etc.). The nucleus
# still shows every instance's true action per-pearl.
_SEVERITY = {"replace": 0, "delete": 1, "create": 2, "update": 3, "noop": 4}

_INDEX_RE = re.compile(r"\[[^\]]*\]$")


def _block_of(addr: str) -> str:
    # strip a trailing for_each/count index: foo.bar["api"] -> foo.bar
    return _INDEX_RE.sub("", addr)


def _module_of(addr: str) -> str:
    """The module path a resource lives in, from its address — so the UI can
    cluster + label by module on big estates. `module.vpc.aws_x.y` -> "vpc";
    `module.eks.module.ng.aws_x.y` -> "eks.ng"; a root resource -> "" (#761)."""
    parts = addr.split(".")
    mods: list[str] = []
    i = 0
    while i + 1 < len(parts) and parts[i] == "module":
        mods.append(_INDEX_RE.sub("", parts[i + 1]))
        i += 2
    return ".".join(mods)


def _collect_refs(expr: object, out: set[str]) -> None:
    if isinstance(expr, dict):
        refs = expr.get("references")
        if isinstance(refs, list):
            out.update(r for r in refs if isinstance(r, str))
        for v in expr.values():
            _collect_refs(v, out)
    elif isinstance(expr, list):
        for v in expr:
            _collect_refs(v, out)


def _qualify(path: tuple[str, ...], local_key: str) -> str:
    """Fully-qualified block address for a module-local `type.name` key, matching
    the `resource_changes[].address` shape: root → `type.name`, a module at
    path ("vpc",) → `module.vpc.type.name`, nested → `module.a.module.b.type.name`."""
    if not path:
        return local_key
    return ".".join(f"module.{m}" for m in path) + "." + local_key


def _index_modules(
    root: dict,
) -> tuple[dict[tuple[str, ...], dict], dict[tuple[str, ...], dict]]:
    """Flatten the configuration module tree into path-keyed lookups: `modules`
    maps a module path → its config dict; `calls` maps a module path → its
    `module_calls` dict (child name → call, whose `expressions` are the child's
    inputs evaluated in THIS scope)."""
    modules: dict[tuple[str, ...], dict] = {(): root}
    calls: dict[tuple[str, ...], dict] = {}

    def walk(path: tuple[str, ...], mod: dict) -> None:
        mc = mod.get("module_calls", {}) or {}
        calls[path] = mc
        for name, call in mc.items():
            child = (call or {}).get("module", {}) or {}
            cpath = path + (name,)
            modules[cpath] = child
            walk(cpath, child)

    walk((), root)
    return modules, calls


def _make_resolver(
    modules: dict[tuple[str, ...], dict],
    calls: dict[tuple[str, ...], dict],
    resource_types: set[str],
):
    """Build a `resolve_ref(path, ref) -> set[block_addr]` closure that resolves a
    single reference string, in the scope of module `path`, to the set of
    fully-qualified resource *block* addresses it ultimately depends on —
    following `var.*` up into the parent's input expression and `module.child.out`
    down into the child's output expression. Memoised, with a cycle guard.

    A plain `type.name` reference is a resource dependency iff `type` is a known
    managed-resource type (from the plan's resource_changes); an unqualified HCL
    resource reference always targets a resource in the SAME module, so it's
    qualified with the current scope's prefix. This gate (rather than checking the
    config's `resources` list) tolerates plans that omit no-expression resources
    from a module's configuration block."""
    memo_in: dict[tuple[tuple[str, ...], str], set[str]] = {}
    memo_out: dict[tuple[tuple[str, ...], str], set[str]] = {}
    in_progress: set[tuple[str, tuple[str, ...], str]] = set()

    def refs_of(expr: object) -> set[str]:
        s: set[str] = set()
        _collect_refs(expr, s)
        return s

    def resolve_ref(path: tuple[str, ...], ref: str) -> set[str]:
        parts = ref.split(".")
        head = parts[0]
        if head == "var" and len(parts) >= 2:
            return resolve_input(path, parts[1])
        if head == "module":
            # `module.child.out.…` binds to the child's output; a bare `module.child`
            # (no output name) always rides alongside a specific `.out` ref, so skip it.
            if len(parts) >= 3 and (path + (parts[1],)) in modules:
                return resolve_output(path + (parts[1],), parts[2])
            return set()
        if len(parts) >= 2 and parts[0] in resource_types:
            return {_qualify(path, f"{parts[0]}.{parts[1]}")}
        return set()

    def resolve_input(path: tuple[str, ...], varname: str) -> set[str]:
        if not path:
            return set()
        k = (path, varname)
        if k in memo_in:
            return memo_in[k]
        guard = ("in", path, varname)
        if guard in in_progress:
            return set()
        in_progress.add(guard)
        parent, name = path[:-1], path[-1]
        call = (calls.get(parent, {}) or {}).get(name, {}) or {}
        expr = (call.get("expressions", {}) or {}).get(varname)
        out: set[str] = set()
        for r in refs_of(expr):
            out |= resolve_ref(parent, r)
        in_progress.discard(guard)
        memo_in[k] = out
        return out

    def resolve_output(path: tuple[str, ...], outname: str) -> set[str]:
        k = (path, outname)
        if k in memo_out:
            return memo_out[k]
        guard = ("out", path, outname)
        if guard in in_progress:
            return set()
        in_progress.add(guard)
        o = ((modules.get(path, {}) or {}).get("outputs", {}) or {}).get(outname, {}) or {}
        out: set[str] = set()
        for r in refs_of(o.get("expression")):
            out |= resolve_ref(path, r)
        in_progress.discard(guard)
        memo_out[k] = out
        return out

    return resolve_ref


def derive_graph(plan_bytes: bytes) -> dict:
    """Pure, synchronous derivation (runs in a thread). Never raises on a
    well-formed-but-empty plan — returns an empty graph."""
    plan = json.loads(plan_bytes)
    rcs = plan.get("resource_changes") or []

    # ONE node per resource BLOCK (a count/for_each resource collapses to a
    # single node, drawn as a "nucleus" of per-instance pearls — #770). Each
    # node carries `instances` and the per-instance `instance_actions` so the
    # renderer can colour each pearl by its own planned action (a single count
    # can be [0] noop / [1] create / [2] destroy). The node-level `action` is the
    # most-severe action, used for the collapsed dot / legend swatch.
    nodes: list[dict] = []
    node_by_block: dict[str, dict] = {}
    for rc in rcs:
        addr = rc.get("address")
        if not addr:
            continue
        block = _block_of(addr)
        action = _ACTION.get(tuple(rc.get("change", {}).get("actions", [])), "update")
        node = node_by_block.get(block)
        if node is None:
            node = {
                "id": block,
                "type": rc.get("type", ""),
                "name": rc.get("name", ""),
                "provider": (rc.get("provider_name", "") or "").split("/")[-1],
                "module": _module_of(block),
                "instance_actions": [],
            }
            node_by_block[block] = node
            nodes.append(node)
        node["instance_actions"].append(action)

    for node in nodes:
        acts = node["instance_actions"]
        node["instances"] = len(acts)
        # most-severe action drives the node-level colour (replace>delete>create>update>noop)
        node["action"] = min(acts, key=lambda a: _SEVERITY.get(a, 9)) if acts else "noop"

    resource_types = {n["type"] for n in nodes}
    node_ids = {n["id"] for n in nodes}

    # Block-level reference map from the configuration, walked across the whole
    # module tree (root + every module_call) with cross-module var/output binding.
    root_cfg = plan.get("configuration", {}).get("root_module", {}) or {}
    modules, calls = _index_modules(root_cfg)
    resolve_ref = _make_resolver(modules, calls, resource_types)
    block_refs: dict[str, set[str]] = {}
    for path, mod in modules.items():
        for cr in mod.get("resources", []) or []:
            local = cr.get("address")
            if not local:
                continue
            src_block = _qualify(path, local)
            refs: set[str] = set()
            _collect_refs(cr.get("expressions", {}), refs)
            # for_each / count references are real dependencies too (e.g. a
            # per-instance resource keyed off another module's output map).
            _collect_refs(cr.get("for_each_expression"), refs)
            _collect_refs(cr.get("count_expression"), refs)
            targets: set[str] = set()
            for r in refs:
                targets |= resolve_ref(path, r)
            targets.discard(src_block)
            block_refs[src_block] = block_refs.get(src_block, set()) | targets

    # Block-level edges (source block depends-on target block). Nodes are now
    # per-block, so no instance expansion is needed.
    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for src_block, targets in block_refs.items():
        if src_block not in node_ids:
            continue
        for tb in targets:
            if tb in node_ids and tb != src_block and (src_block, tb) not in seen:
                seen.add((src_block, tb))
                edges.append({"source": src_block, "target": tb})

    # Legend counts stay per-INSTANCE (the plan really does create/destroy N
    # things) even though nodes are per-block.
    counts = dict.fromkeys(("create", "update", "replace", "delete", "noop"), 0)
    for node in nodes:
        for a in node["instance_actions"]:
            counts[a] = counts.get(a, 0) + 1
    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "terraform_version": plan.get("terraform_version"),
            "counts": counts,
        },
    }


async def get_impact_graph(run: Run) -> dict | None:
    """Read the run's plan JSON from storage and derive its graph.

    Returns None when the run has no stored JSON plan output (caller → 404).
    """
    if not getattr(run, "has_json_output", False):
        return None
    storage = get_storage()
    key = plan_json_output_key(str(run.workspace_id), str(run.id))
    if not await storage.exists(key):
        return None
    raw = await storage.get(key)
    # Parse + derive off the event loop (large buffer, CPU-bound) — rule 13.
    return await asyncio.to_thread(derive_graph, raw)
