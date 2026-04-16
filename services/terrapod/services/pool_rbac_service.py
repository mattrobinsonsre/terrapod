"""Agent-pool-specific RBAC permission resolution.

Resolves the highest permission level a user has on an agent pool
using the same label-based model as workspaces and registry resources
but with a simpler three-level hierarchy (no "plan" for pools).

Permission hierarchy: read < write < admin
Resolution order: platform admin > audit > owner > label RBAC > everyone > none
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.builtin_roles import BUILTIN_ROLE_NAMES
from terrapod.db.models import Role
from terrapod.logging_config import get_logger
from terrapod.services.rbac_service import matches_labels, merge_labels

logger = get_logger(__name__)

POOL_PERMISSION_HIERARCHY = {"read": 0, "write": 1, "admin": 2}


def has_pool_permission(effective: str | None, required: str) -> bool:
    """Check if effective permission meets the required level."""
    if effective is None:
        return False
    return POOL_PERMISSION_HIERARCHY.get(effective, -1) >= POOL_PERMISSION_HIERARCHY.get(
        required, 99
    )


async def fetch_custom_roles(
    db: AsyncSession,
    user_roles: list[str],
) -> list[Role]:
    """Fetch custom (non-builtin) Role objects for the given role names.

    Use this to pre-load roles once before calling resolve_pool_permission
    in a loop, passing the result as ``preloaded_roles`` to avoid N+1 queries.
    """
    custom_names = set(user_roles) - BUILTIN_ROLE_NAMES
    if not custom_names:
        return []
    result = await db.execute(select(Role).where(Role.name.in_(custom_names)))
    return list(result.scalars().all())


async def resolve_pool_permission(
    db: AsyncSession,
    user_email: str,
    user_roles: list[str],
    pool_name: str,
    pool_labels: dict,
    owner_email: str | None,
    *,
    preloaded_roles: list[Role] | None = None,
) -> str | None:
    """Returns highest permission level (read/write/admin), or None.

    Resolution order (highest wins):
    1. Platform admin → admin
    2. Platform audit → read
    3. Pool owner → admin
    4. Label-based RBAC (custom roles) → pool_permission field on role
    5. 'everyone' role with access: everyone label → read
    6. Default → None (no access)

    Pass ``preloaded_roles`` (from :func:`fetch_custom_roles`) to skip the
    per-call DB query — useful when resolving permissions for many pools.
    """
    role_set = set(user_roles)

    # 1. Platform admin
    if "admin" in role_set:
        return "admin"

    best: str | None = None

    # 2. Platform audit → read
    if "audit" in role_set:
        best = "read"

    # 3. Owner → admin
    if owner_email and owner_email == user_email:
        return "admin"

    # 4. Label-based RBAC from custom roles (uses pool_permission, not workspace_permission)
    custom_role_names = role_set - BUILTIN_ROLE_NAMES
    if custom_role_names:
        if preloaded_roles is not None:
            roles = preloaded_roles
        else:
            result = await db.execute(select(Role).where(Role.name.in_(custom_role_names)))
            roles = list(result.scalars().all())

        for role in roles:
            # Check deny first
            deny_labels: dict[str, set[str]] = {}
            merge_labels(deny_labels, role.deny_labels)
            deny_names = set(role.deny_names)

            if pool_name in deny_names:
                continue
            if matches_labels(pool_labels, deny_labels):
                continue

            # Check allow
            allow_labels: dict[str, set[str]] = {}
            merge_labels(allow_labels, role.allow_labels)
            allow_names = set(role.allow_names)

            matched = False
            if pool_name in allow_names:
                matched = True
            elif matches_labels(pool_labels, allow_labels):
                matched = True

            if matched:
                pool_perm = role.pool_permission
                if best is None or POOL_PERMISSION_HIERARCHY.get(
                    pool_perm, -1
                ) > POOL_PERMISSION_HIERARCHY.get(best, -1):
                    best = pool_perm

    # 5. 'everyone' role: if pool has label access=everyone, grant read
    if pool_labels.get("access") == "everyone":
        if best is None:
            best = "read"

    return best
