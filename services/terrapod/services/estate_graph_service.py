"""Derive the whole-estate topology graph (#763, productizing the #736 spike).

Powers the Estate topology page — a graph of every workspace the caller can see
plus the registry modules they use, wired by the cross-workspace structure
Terrapod already holds centrally:

  - remote-state (consumer -> producer, "reads state of")
  - run-trigger  (source   -> dest,     "apply triggers")
  - uses-module  (module   -> workspace, from module_workspace_links)

The platform enforces NO labelling convention, so this service is deliberately
label-agnostic: it ships each workspace's raw labels / pool / name and lets the
client pick the grouping axis at view time. Nodes carry `indeg` (how many things
depend on them) so the UI can size hubs.

**RBAC (hard):** the graph is filtered to the workspaces the caller can read —
exactly the same per-workspace capability check the workspace list uses. An edge
or module is only included when it touches a visible workspace, so a user can
never learn about a workspace (or a dependency on one) they can't otherwise see.

Deriving server-side keeps the payload small and works through the BFF like every
other feature. It's async DB work (bulk selects + light assembly), not CPU-bound,
so no thread offload is needed (rule 13).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.db.models import (
    AgentPool,
    ModuleWorkspaceLink,
    RegistryModule,
    RunTrigger,
    Workspace,
    WorkspaceRemoteStateConsumer,
)
from terrapod.services.workspace_rbac_service import resolve_workspace_capabilities_for


async def derive_estate_graph(db: AsyncSession, user: AuthenticatedUser) -> dict:
    """Build the estate graph visible to `user`. Never raises on an empty estate."""
    workspaces = (await db.execute(select(Workspace))).scalars().all()

    # RBAC: keep only workspaces the caller can read (non-empty capability set).
    visible: dict = {}
    for ws in workspaces:
        caps = await resolve_workspace_capabilities_for(db, user, ws)
        if caps:
            visible[ws.id] = ws
    vids = set(visible)

    pool_name = {p.id: p.name for p in (await db.execute(select(AgentPool))).scalars().all()}

    nodes: list[dict] = []
    for ws in visible.values():
        if ws.agent_pool_id:
            pool = pool_name.get(ws.agent_pool_id, "(pool)")
        else:
            pool = "(local)" if ws.execution_mode == "local" else "(no pool)"
        nodes.append(
            {
                "id": f"ws-{ws.id}",
                "kind": "workspace",
                "name": ws.name,
                "labels": ws.labels or {},
                "pool": pool,
                "indeg": 0,
            }
        )

    edges: list[dict] = []

    # run-triggers: source apply -> destination run. Both ends must be visible.
    for rt in (await db.execute(select(RunTrigger))).scalars().all():
        if rt.source_workspace_id in vids and rt.workspace_id in vids:
            edges.append(
                {
                    "source": f"ws-{rt.source_workspace_id}",
                    "target": f"ws-{rt.workspace_id}",
                    "kind": "run-trigger",
                }
            )

    # remote-state: consumer reads producer's state (edge consumer -> producer).
    for rs in (await db.execute(select(WorkspaceRemoteStateConsumer))).scalars().all():
        if rs.producer_workspace_id in vids and rs.consumer_workspace_id in vids:
            edges.append(
                {
                    "source": f"ws-{rs.consumer_workspace_id}",
                    "target": f"ws-{rs.producer_workspace_id}",
                    "kind": "remote-state",
                }
            )

    # modules: include a module node when it links to a visible workspace.
    links = (await db.execute(select(ModuleWorkspaceLink))).scalars().all()
    used_module_ids = {lk.module_id for lk in links if lk.workspace_id in vids}
    module_node_id: dict = {}
    if used_module_ids:
        mods = (
            (await db.execute(select(RegistryModule).where(RegistryModule.id.in_(used_module_ids))))
            .scalars()
            .all()
        )
        for m in mods:
            mid = f"mod-{m.id}"
            module_node_id[m.id] = mid
            nodes.append(
                {
                    "id": mid,
                    "kind": "module",
                    "name": f"{m.name}/{m.provider}",
                    "labels": {},
                    "pool": "",
                    "indeg": 0,
                }
            )
    for lk in links:
        if lk.workspace_id in vids and lk.module_id in module_node_id:
            edges.append(
                {
                    "source": module_node_id[lk.module_id],
                    "target": f"ws-{lk.workspace_id}",
                    "kind": "uses-module",
                }
            )

    by_id = {n["id"]: n for n in nodes}
    for e in edges:
        if e["target"] in by_id:
            by_id[e["target"]]["indeg"] += 1

    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "counts": {
                "workspaces": sum(1 for n in nodes if n["kind"] == "workspace"),
                "modules": sum(1 for n in nodes if n["kind"] == "module"),
                "edges": len(edges),
            }
        },
    }
