"""Read-only cross-entity label aggregation.

Why this exists
---------------
Terrapod's organizational primitive is the label, not the team or
project. Workspaces, agent pools, registry modules and registry
providers all carry a `labels: dict[str, str]` JSONB column, all
feeding the same RBAC machinery.

This service answers three questions for the labels-browser UI:

* "What label keys exist across the things I can see?"  →
  `aggregate_keys(db, user)`
* "What values does this key have, and what's tagged with each?"  →
  `aggregate_values_for_key(db, user, key)`
* "What's tagged with this exact key=value?"  →
  `list_entities_for_label(db, user, key, value)`

Read-only by design. Editing labels stays on each entity's own edit
page; we don't want a labels-admin surface that becomes a back-door
for mass-mutating workspaces / pools / modules / providers.

RBAC
----
Each entity type has its own permission resolver
(`resolve_workspace_permission`, `resolve_pool_permission`,
`resolve_registry_permission`). We only surface labels on entities
the caller has at least `read` on. The four resolvers each support
`preloaded_roles=` to avoid an N+1 DB hit when iterating over many
entities; we pre-load roles once per entity type.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser
from terrapod.db.models import (
    AgentPool,
    RegistryModule,
    RegistryProvider,
    Workspace,
)
from terrapod.services import (
    pool_rbac_service,
    registry_rbac_service,
    workspace_rbac_service,
)

# Stable type identifiers used in the API and UI. Match the JSON:API
# `type` strings each entity uses elsewhere where possible, so frontend
# code can route consistently.
ENTITY_TYPES = ("workspaces", "agent-pools", "registry-modules", "registry-providers")


async def _readable_workspaces(db: AsyncSession, user: AuthenticatedUser) -> list[Workspace]:
    """Return workspaces the user has at least read access to."""
    result = await db.execute(select(Workspace).order_by(Workspace.name))
    workspaces = list(result.scalars().all())
    preloaded = await workspace_rbac_service.fetch_custom_roles(db, user.roles)
    token_preloaded = await workspace_rbac_service.fetch_custom_roles(db, user.pinned_roles or [])
    out: list[Workspace] = []
    for ws in workspaces:
        perm = await workspace_rbac_service.resolve_workspace_permission_for(
            db,
            user,
            ws,
            preloaded_roles=preloaded,
            token_preloaded_roles=token_preloaded,
        )
        if perm is not None:
            out.append(ws)
    return out


async def _readable_pools(db: AsyncSession, user: AuthenticatedUser) -> list[AgentPool]:
    result = await db.execute(select(AgentPool).order_by(AgentPool.name))
    pools = list(result.scalars().all())
    preloaded = await pool_rbac_service.fetch_custom_roles(db, user.roles)
    token_preloaded = await pool_rbac_service.fetch_custom_roles(db, user.pinned_roles or [])
    out: list[AgentPool] = []
    for p in pools:
        perm = await pool_rbac_service.resolve_pool_permission_for(
            db,
            user,
            pool_name=p.name,
            pool_labels=p.labels or {},
            owner_email=p.owner_email or "",
            preloaded_roles=preloaded,
            token_preloaded_roles=token_preloaded,
        )
        if perm is not None:
            out.append(p)
    return out


async def _readable_modules(db: AsyncSession, user: AuthenticatedUser) -> list[RegistryModule]:
    result = await db.execute(select(RegistryModule).order_by(RegistryModule.name))
    modules = list(result.scalars().all())
    preloaded = await registry_rbac_service.fetch_custom_roles(db, user.roles)
    token_preloaded = await registry_rbac_service.fetch_custom_roles(db, user.pinned_roles or [])
    out: list[RegistryModule] = []
    for m in modules:
        perm = await registry_rbac_service.resolve_registry_permission_for(
            db,
            user,
            resource_name=m.name,
            resource_labels=m.labels or {},
            owner_email=m.owner_email or "",
            preloaded_roles=preloaded,
            token_preloaded_roles=token_preloaded,
        )
        if perm is not None:
            out.append(m)
    return out


async def _readable_providers(db: AsyncSession, user: AuthenticatedUser) -> list[RegistryProvider]:
    result = await db.execute(select(RegistryProvider).order_by(RegistryProvider.name))
    providers = list(result.scalars().all())
    preloaded = await registry_rbac_service.fetch_custom_roles(db, user.roles)
    token_preloaded = await registry_rbac_service.fetch_custom_roles(db, user.pinned_roles or [])
    out: list[RegistryProvider] = []
    for p in providers:
        perm = await registry_rbac_service.resolve_registry_permission_for(
            db,
            user,
            resource_name=p.name,
            resource_labels=p.labels or {},
            owner_email=p.owner_email or "",
            preloaded_roles=preloaded,
            token_preloaded_roles=token_preloaded,
        )
        if perm is not None:
            out.append(p)
    return out


async def _readable_by_type(db: AsyncSession, user: AuthenticatedUser) -> dict[str, list]:
    """Return the readable entities of every type, keyed by ENTITY_TYPES."""
    return {
        "workspaces": await _readable_workspaces(db, user),
        "agent-pools": await _readable_pools(db, user),
        "registry-modules": await _readable_modules(db, user),
        "registry-providers": await _readable_providers(db, user),
    }


def _empty_counts() -> dict[str, int]:
    return dict.fromkeys(ENTITY_TYPES, 0)


async def aggregate_keys(db: AsyncSession, user: AuthenticatedUser) -> list[dict[str, Any]]:
    """Return distinct label keys across all readable entities, with counts.

    Shape:
        [
          {
            "key": "account",
            "value-count": 3,
            "entity-counts": {"workspaces": 12, "agent-pools": 1, ...},
          },
          ...
        ]

    Sorted alphabetically by key. An entity contributes to the count
    for a key once if it has that key in its labels dict, regardless
    of value.
    """
    by_type = await _readable_by_type(db, user)
    # Per-key: set of distinct values seen + per-type entity counts.
    key_values: dict[str, set[str]] = defaultdict(set)
    key_counts: dict[str, dict[str, int]] = defaultdict(_empty_counts)

    for entity_type, entities in by_type.items():
        for entity in entities:
            for key, value in (entity.labels or {}).items():
                key_values[key].add(value)
                key_counts[key][entity_type] += 1

    return [
        {
            "key": key,
            "value-count": len(key_values[key]),
            "entity-counts": dict(key_counts[key]),
        }
        for key in sorted(key_values)
    ]


async def aggregate_values_for_key(
    db: AsyncSession, user: AuthenticatedUser, key: str
) -> list[dict[str, Any]]:
    """Return distinct values for `key`, each with per-type entity counts.

    Shape:
        [
          {
            "value": "prod",
            "entity-counts": {"workspaces": 4, ...},
          },
          ...
        ]

    Sorted alphabetically by value. Returns [] if no entity carries
    the key.
    """
    by_type = await _readable_by_type(db, user)
    value_counts: dict[str, dict[str, int]] = defaultdict(_empty_counts)

    for entity_type, entities in by_type.items():
        for entity in entities:
            v = (entity.labels or {}).get(key)
            if v is None:
                continue
            value_counts[v][entity_type] += 1

    return [{"value": v, "entity-counts": dict(value_counts[v])} for v in sorted(value_counts)]


def _entity_to_json(entity_type: str, entity: Any) -> dict[str, Any]:
    """Minimal JSON shape for the entity-list view.

    Just enough for the UI to render a row + link to the entity's own
    page: id-prefix, name, and the entity's full labels dict so the
    UI can render the label badges.
    """
    if entity_type == "workspaces":
        return {
            "type": "workspaces",
            "id": f"ws-{entity.id}",
            "name": entity.name,
            "labels": entity.labels or {},
        }
    if entity_type == "agent-pools":
        return {
            "type": "agent-pools",
            "id": f"apool-{entity.id}",
            "name": entity.name,
            "labels": entity.labels or {},
        }
    if entity_type == "registry-modules":
        return {
            "type": "registry-modules",
            "id": f"mod-{entity.id}",
            "name": entity.name,
            "namespace": entity.namespace,
            "provider": entity.provider,
            "labels": entity.labels or {},
        }
    if entity_type == "registry-providers":
        return {
            "type": "registry-providers",
            "id": f"prov-{entity.id}",
            "name": entity.name,
            "namespace": entity.namespace,
            "labels": entity.labels or {},
        }
    raise ValueError(f"unknown entity type {entity_type!r}")


async def list_entities_for_label(
    db: AsyncSession, user: AuthenticatedUser, key: str, value: str
) -> dict[str, list[dict[str, Any]]]:
    """List the entities (across all four types) tagged exactly `key=value`.

    Shape:
        {
          "workspaces": [{...}, ...],
          "agent-pools": [...],
          "registry-modules": [...],
          "registry-providers": [...],
        }

    Each entity-type list is sorted by name. Empty lists are kept so
    the UI can render an empty section cleanly.
    """
    by_type = await _readable_by_type(db, user)
    out: dict[str, list[dict[str, Any]]] = {t: [] for t in ENTITY_TYPES}
    for entity_type, entities in by_type.items():
        for entity in entities:
            if (entity.labels or {}).get(key) == value:
                out[entity_type].append(_entity_to_json(entity_type, entity))
    return out
