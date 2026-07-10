"""Derive a single-workspace state resource graph (#765).

The third WebGL surface in the #736 visualisation initiative, after plan
blast-radius (#762) and estate topology (#763). Estate topology shows how
*workspaces* relate; this is the complement — how the *resources inside one
workspace* relate, as the resource-level dependency DAG that Terraform records
in the workspace's stored state.

For a given `StateVersion` (the workspace's current version by default, or any
older one the caller picks), we load the state blob from object storage,
decrypt it if app-layer state encryption was on when it was written (#635), and
parse the Terraform state v4 JSON:

  - a **node** per resource address (managed `type.name` / data
    `data.type.name`, module-prefixed when nested), collapsing the per-instance
    fan-out of `count`/`for_each` back to one node;
  - a **depends-on edge** per entry in each instance's `dependencies` array
    (unioned across instances), kept only when the target is another node in
    this state.

Nodes carry `type` / `mode` / `module` / `provider` so the client can pick the
grouping axis at view time (the same label-agnostic pivot approach estate uses)
and `indeg` (how many resources depend on this one) so the UI can size hubs
like a VPC or a shared security group.

**RBAC (hard):** gated on ``state:read`` — the *same* capability as downloading
the raw state — because the graph is derived from the secret-bearing state
blob. Seeing the resource graph therefore requires the same trust as reading
the state it comes from.

**Rules 13/14:** the state blob can be tens of MB; ``json.loads`` + the graph
build is CPU work, so the whole parse+assemble step runs in a worker thread
(``asyncio.to_thread``), never on the event loop. We only ever hold the parsed
structure the existing raw-state download endpoint already holds — no new
tempfile, so rule 14 doesn't apply here.
"""

from __future__ import annotations

import asyncio

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import has_capability
from terrapod.db.models import StateVersion, Workspace
from terrapod.logging_config import get_logger
from terrapod.services.workspace_rbac_service import resolve_workspace_capabilities_for
from terrapod.storage import get_storage
from terrapod.storage.keys import state_key

logger = get_logger(__name__)

# Guardrail: a state with tens of thousands of resources would make the WebGL
# graph unusable and the payload huge. Cap the node count and report the
# truncation honestly in `meta` rather than silently dropping resources.
MAX_NODES = 2000


def _provider_short(provider: str) -> str:
    """`provider["registry.terraform.io/hashicorp/aws"]` -> `aws`."""
    if not provider:
        return ""
    inner = provider
    if "[" in inner:
        inner = inner[inner.find("[") + 1 : inner.rfind("]")].strip().strip('"')
    # inner is now e.g. registry.terraform.io/hashicorp/aws (or just a name)
    return inner.rsplit("/", 1)[-1] if inner else ""


def _resource_address(module: str, mode: str, rtype: str, name: str) -> str:
    """Build the canonical resource address used as the node id.

    Matches the form Terraform records in `dependencies`, so edges resolve:
      managed, root:   aws_instance.web
      data,    root:   data.aws_ami.ubuntu
      managed, module: module.vpc.aws_subnet.private
    """
    base = f"{rtype}.{name}" if mode != "data" else f"data.{rtype}.{name}"
    return f"{module}.{base}" if module else base


def build_graph_from_state(state: dict) -> dict:
    """Pure transform: Terraform state v4 dict -> {nodes, edges, meta}.

    Separated from I/O so it's trivially unit-testable and runs in a thread.
    """
    resources = state.get("resources") or []
    total_resources = len(resources)

    nodes: list[dict] = []
    node_ids: set[str] = set()
    # source address -> set of dependency addresses (unioned across instances)
    deps: dict[str, set[str]] = {}
    truncated = False

    for res in resources:
        if not isinstance(res, dict):
            continue
        module = res.get("module") or ""
        mode = res.get("mode") or "managed"
        rtype = res.get("type") or ""
        name = res.get("name") or ""
        if not rtype or not name:
            continue
        addr = _resource_address(module, mode, rtype, name)
        # instances count drives the nucleus (a count/for_each resource is ONE
        # node drawn as a clump of `instances` pearls). A resource with no
        # instances (count = 0) is still a single node.
        n_instances = len(res.get("instances") or [])
        if addr in node_ids:
            # Same address seen twice (shouldn't happen in valid state) — merge.
            by_id_local = next((nd for nd in nodes if nd["id"] == addr), None)
            if by_id_local is not None:
                by_id_local["instances"] = by_id_local.get("instances", 1) + n_instances
        elif len(node_ids) >= MAX_NODES:
            truncated = True
            continue
        else:
            node_ids.add(addr)
            nodes.append(
                {
                    "id": addr,
                    "kind": "resource",
                    "name": addr,
                    "type": rtype,
                    "mode": mode,
                    # "" for root-module resources (matches the impact graph's
                    # convention so the shared renderer's module-cluster force
                    # skips the root module rather than boxing it).
                    "module": module,
                    "provider": _provider_short(res.get("provider") or ""),
                    "instances": max(n_instances, 1),
                    "indeg": 0,
                }
            )
        bucket = deps.setdefault(addr, set())
        for inst in res.get("instances") or []:
            if not isinstance(inst, dict):
                continue
            for dep in inst.get("dependencies") or []:
                if isinstance(dep, str) and dep != addr:
                    bucket.add(dep)

    edges: list[dict] = []
    for src, targets in deps.items():
        if src not in node_ids:
            continue
        for tgt in sorted(targets):
            if tgt in node_ids:
                edges.append({"source": src, "target": tgt, "kind": "depends-on"})

    by_id = {n["id"]: n for n in nodes}
    for e in edges:
        by_id[e["target"]]["indeg"] += 1

    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "counts": {"resources": len(nodes), "edges": len(edges)},
            "truncated": truncated,
            "total_resources": total_resources,
            "max_nodes": MAX_NODES,
        },
    }


async def derive_state_graph(
    db: AsyncSession,
    user: AuthenticatedUser,
    workspace_id: str,
    state_version_id: str | None = None,
) -> dict:
    """Build the resource graph for a workspace's state version.

    `state_version_id` (accepts the `sv-` prefix) selects a specific version;
    when omitted, the current (highest-serial) version is used. Returns a graph
    plus `meta.versions` (the picker list) and `meta.state_version` (the one
    rendered). Never raises on a workspace that has no state yet — returns an
    empty graph with an empty version list.
    """
    from terrapod.api.routers.tfe_v2 import _get_workspace_by_id

    ws: Workspace = await _get_workspace_by_id(workspace_id, db)

    caps = await resolve_workspace_capabilities_for(db, user, ws)
    if not has_capability(caps, cap.STATE_READ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires state:read permission on workspace",
        )

    versions = (
        (
            await db.execute(
                select(StateVersion)
                .where(StateVersion.workspace_id == ws.id)
                .order_by(StateVersion.serial.desc())
            )
        )
        .scalars()
        .all()
    )

    version_list = [
        {
            "id": f"sv-{sv.id}",
            "serial": sv.serial,
            "created_at": sv.created_at.isoformat().replace("+00:00", "Z"),
            "is_current": i == 0,
        }
        for i, sv in enumerate(versions)
    ]

    def _empty(sv_meta: dict | None) -> dict:
        return {
            "nodes": [],
            "edges": [],
            "meta": {
                "counts": {"resources": 0, "edges": 0},
                "truncated": False,
                "total_resources": 0,
                "max_nodes": MAX_NODES,
                "versions": version_list,
                "state_version": sv_meta,
            },
        }

    if not versions:
        return _empty(None)

    # Resolve which version to render.
    if state_version_id:
        wanted = state_version_id.removeprefix("sv-")
        target = next((sv for sv in versions if str(sv.id) == wanted), None)
        if target is None:
            raise HTTPException(status_code=404, detail="State version not found")
    else:
        target = versions[0]

    sv_meta = next(v for v in version_list if v["id"] == f"sv-{target.id}")

    # A version row can exist before its /content PUT landed (state_size == 0);
    # treat that as an empty graph rather than a 404.
    if target.state_size == 0:
        return _empty(sv_meta)

    storage = get_storage()
    key = state_key(str(target.workspace_id), str(target.id))
    try:
        data = await storage.get(key)
    except Exception:
        # Metadata row without a backing object — surface as empty, not a 500.
        logger.warning("state_graph_blob_missing", state_version_id=str(target.id))
        return _empty(sv_meta)

    from terrapod.crypto.state import decrypt_state_bytes

    data = await decrypt_state_bytes(data)

    def _parse_and_build(raw: bytes) -> dict:
        import json

        state = json.loads(raw)
        return build_graph_from_state(state)

    try:
        graph = await asyncio.to_thread(_parse_and_build, data)
    except (ValueError, TypeError):
        logger.warning("state_graph_parse_failed", state_version_id=str(target.id))
        return _empty(sv_meta)

    graph["meta"]["versions"] = version_list
    graph["meta"]["state_version"] = sv_meta
    return graph
