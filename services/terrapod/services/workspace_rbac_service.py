"""Workspace-specific RBAC permission resolution.

Resolves the highest permission level a user has on a workspace
using the label-based RBAC model with hierarchical permission levels.

Permission hierarchy: read < plan < write < admin
Resolution order: platform admin > audit > owner > label RBAC > everyone > none
"""

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.builtin_roles import BUILTIN_ROLE_NAMES
from terrapod.db.models import Role, Workspace
from terrapod.logging_config import get_logger
from terrapod.services.rbac_service import matches_labels, merge_labels

if TYPE_CHECKING:
    from terrapod.api.dependencies import AuthenticatedUser

logger = get_logger(__name__)

PERMISSION_HIERARCHY = {"read": 0, "plan": 1, "write": 2, "admin": 3}


def has_permission(effective: str | None, required: str) -> bool:
    """Check if effective permission meets the required level."""
    if effective is None:
        return False
    return PERMISSION_HIERARCHY.get(effective, -1) >= PERMISSION_HIERARCHY.get(required, 99)


async def fetch_custom_roles(
    db: AsyncSession,
    user_roles: list[str],
) -> list[Role]:
    """Fetch custom (non-builtin) Role objects for the given role names.

    Use this to pre-load roles once before calling resolve_workspace_permission
    in a loop, passing the result as ``preloaded_roles`` to avoid N+1 queries.
    """
    custom_names = set(user_roles) - BUILTIN_ROLE_NAMES
    if not custom_names:
        return []
    result = await db.execute(select(Role).where(Role.name.in_(custom_names)))
    return list(result.scalars().all())


async def resolve_workspace_permission(
    db: AsyncSession,
    user_email: str,
    user_roles: list[str],
    workspace: Workspace,
    *,
    preloaded_roles: list[Role] | None = None,
    apply_everyone_floor: bool = True,
) -> str | None:
    """Returns the highest permission level for a user on a workspace, or None.

    Resolution order (highest wins):
    1. Platform admin → admin
    2. Platform audit → read
    3. Workspace owner → admin
    4. Label-based RBAC (custom roles) → role's workspace_permission (highest wins)
    5. 'everyone' role with access: everyone label → read
    6. Default → None (no access)

    Pass ``preloaded_roles`` (from :func:`fetch_custom_roles`) to skip the
    per-call DB query — useful when resolving permissions for many workspaces.
    """
    role_set = set(user_roles)

    # 1. Platform admin bypasses all
    if "admin" in role_set:
        return "admin"

    best: str | None = None

    # 2. Platform audit → read
    if "audit" in role_set:
        best = "read"

    # 3. Workspace owner → admin
    if workspace.owner_email and workspace.owner_email == user_email:
        return "admin"

    # 4. Label-based RBAC from custom roles
    custom_role_names = role_set - BUILTIN_ROLE_NAMES
    if custom_role_names:
        if preloaded_roles is not None:
            roles = [r for r in preloaded_roles if r.name in custom_role_names]
        else:
            result = await db.execute(select(Role).where(Role.name.in_(custom_role_names)))
            roles = list(result.scalars().all())

        resource_labels = workspace.labels or {}
        resource_name = workspace.name

        for role in roles:
            # Check deny first — deny wins, skip this role entirely
            deny_labels: dict[str, set[str]] = {}
            merge_labels(deny_labels, role.deny_labels)
            deny_names = set(role.deny_names)

            if resource_name in deny_names:
                continue
            if matches_labels(resource_labels, deny_labels):
                continue

            # Check allow
            allow_labels: dict[str, set[str]] = {}
            merge_labels(allow_labels, role.allow_labels)
            allow_names = set(role.allow_names)

            matched = False
            if resource_name in allow_names:
                matched = True
            elif matches_labels(resource_labels, allow_labels):
                matched = True

            if matched:
                perm = role.workspace_permission
                if best is None or PERMISSION_HIERARCHY.get(perm, -1) > PERMISSION_HIERARCHY.get(
                    best, -1
                ):
                    best = perm

    # 5. 'everyone' role: if workspace has label access=everyone, grant read.
    # Suppressed (apply_everyone_floor=False) when resolving a token's own
    # pinned scope, so a zero-scope service token does not inherit read on
    # every access:everyone resource (#495 — the floor is label-gated, so
    # dropping `everyone` from the role list alone would not suppress it).
    resource_labels = workspace.labels or {}
    if apply_everyone_floor and resource_labels.get("access") == "everyone":
        if best is None:
            best = "read"

    return best


def min_workspace_permission(a: str | None, b: str | None) -> str | None:
    """Lower of two workspace permission levels (None = no access; None dominates)."""
    if a is None or b is None:
        return None
    return a if PERMISSION_HIERARCHY[a] <= PERMISSION_HIERARCHY[b] else b


async def resolve_workspace_permission_for(
    db: AsyncSession,
    user: "AuthenticatedUser",
    workspace: Workspace,
    *,
    preloaded_roles: list[Role] | None = None,
    token_preloaded_roles: list[Role] | None = None,
) -> str | None:
    """Kind-aware workspace permission for an authenticated principal (#495).

    - interactive       -> resolve(user's live roles)
    - service_bound     -> min(user_effective, token_effective)
    - service_detached  -> token_effective only

    ``token_effective`` resolves the token's pinned roles with no owner
    identity (empty email) and the everyone-floor suppressed. Pass the per-side
    ``fetch_custom_roles`` results as ``preloaded_roles`` (owner) and
    ``token_preloaded_roles`` (pinned) for loop callers; None for a single
    resolve.
    """
    if user.kind == "service_detached":
        return await resolve_workspace_permission(
            db,
            "",
            user.pinned_roles or [],
            workspace,
            preloaded_roles=token_preloaded_roles,
            apply_everyone_floor=False,
        )

    user_eff = await resolve_workspace_permission(
        db, user.email, user.roles, workspace, preloaded_roles=preloaded_roles
    )
    if user.kind == "service_bound":
        token_eff = await resolve_workspace_permission(
            db,
            "",
            user.pinned_roles or [],
            workspace,
            preloaded_roles=token_preloaded_roles,
            apply_everyone_floor=False,
        )
        return min_workspace_permission(user_eff, token_eff)
    return user_eff
