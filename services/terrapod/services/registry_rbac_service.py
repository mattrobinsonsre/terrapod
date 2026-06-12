"""Registry-specific RBAC permission resolution.

Resolves the highest permission level a user has on a registry resource
(module or provider) using the same label-based model as workspaces but
with a simpler three-level hierarchy (no "plan" for registry resources).

Permission hierarchy: read < write < admin
Resolution order: platform admin > audit > owner > label RBAC > everyone > none
"""

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.builtin_roles import BUILTIN_ROLE_NAMES
from terrapod.db.models import Role
from terrapod.logging_config import get_logger
from terrapod.services.rbac_service import matches_labels, merge_labels

if TYPE_CHECKING:
    from terrapod.api.dependencies import AuthenticatedUser

logger = get_logger(__name__)

REGISTRY_PERMISSION_HIERARCHY = {"read": 0, "write": 1, "admin": 2}

# Map workspace_permission values to registry permission levels.
# "plan" has no meaning for registry resources → maps to "read".
_WS_PERM_TO_REGISTRY = {
    "read": "read",
    "plan": "read",
    "write": "write",
    "admin": "admin",
}


def has_registry_permission(effective: str | None, required: str) -> bool:
    """Check if effective permission meets the required level."""
    if effective is None:
        return False
    return REGISTRY_PERMISSION_HIERARCHY.get(effective, -1) >= REGISTRY_PERMISSION_HIERARCHY.get(
        required, 99
    )


async def fetch_custom_roles(
    db: AsyncSession,
    user_roles: list[str],
) -> list[Role]:
    """Fetch custom (non-builtin) Role objects for the given role names.

    Use this to pre-load roles once before calling resolve_registry_permission
    in a loop, passing the result as ``preloaded_roles`` to avoid N+1 queries.
    """
    custom_names = set(user_roles) - BUILTIN_ROLE_NAMES
    if not custom_names:
        return []
    result = await db.execute(select(Role).where(Role.name.in_(custom_names)))
    return list(result.scalars().all())


async def resolve_registry_permission(
    db: AsyncSession,
    user_email: str,
    user_roles: list[str],
    resource_name: str,
    resource_labels: dict,
    owner_email: str,
    auth_method: str = "",
    *,
    preloaded_roles: list[Role] | None = None,
    apply_everyone_floor: bool = True,
) -> str | None:
    """Returns highest permission level (read/write/admin), or None.

    Resolution order (highest wins):
    1. Platform admin → admin
    2. Platform audit → read
    3. Runner token → read (runners must download modules/providers to execute)
    4. Resource owner → admin
    5. Label-based RBAC (custom roles) → mapped workspace_permission
    6. 'everyone' role with access: everyone label → read
    7. Default → None (no access)

    Pass ``preloaded_roles`` (from :func:`fetch_custom_roles`) to skip the
    per-call DB query — useful when resolving permissions for many resources.
    """
    role_set = set(user_roles)

    # 1. Platform admin
    if "admin" in role_set:
        return "admin"

    best: str | None = None

    # 2. Platform audit → read
    if "audit" in role_set:
        best = "read"

    # 3. Runner tokens → read (runners must download modules/providers to execute)
    if auth_method == "runner_token":
        if best is None:
            best = "read"

    # 4. Owner → admin
    if owner_email and owner_email == user_email:
        return "admin"

    # 5. Label-based RBAC from custom roles
    custom_role_names = role_set - BUILTIN_ROLE_NAMES
    if custom_role_names:
        if preloaded_roles is not None:
            roles = [r for r in preloaded_roles if r.name in custom_role_names]
        else:
            result = await db.execute(select(Role).where(Role.name.in_(custom_role_names)))
            roles = list(result.scalars().all())

        for role in roles:
            # Check deny first
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
                registry_perm = _WS_PERM_TO_REGISTRY.get(role.workspace_permission, "read")
                if best is None or REGISTRY_PERMISSION_HIERARCHY.get(
                    registry_perm, -1
                ) > REGISTRY_PERMISSION_HIERARCHY.get(best, -1):
                    best = registry_perm

    # 6. 'everyone' role: if resource has label access=everyone, grant read.
    # Suppressed for token-scope resolution (apply_everyone_floor=False, #495).
    if apply_everyone_floor and resource_labels.get("access") == "everyone":
        if best is None:
            best = "read"

    return best


def min_registry_permission(a: str | None, b: str | None) -> str | None:
    """Lower of two registry permission levels (None = no access; None dominates)."""
    if a is None or b is None:
        return None
    return a if REGISTRY_PERMISSION_HIERARCHY[a] <= REGISTRY_PERMISSION_HIERARCHY[b] else b


async def resolve_registry_permission_for(
    db: AsyncSession,
    user: "AuthenticatedUser",
    resource_name: str,
    resource_labels: dict,
    owner_email: str,
    *,
    preloaded_roles: list[Role] | None = None,
    token_preloaded_roles: list[Role] | None = None,
) -> str | None:
    """Kind-aware registry permission (#495).

    interactive -> resolve(user roles, real auth_method); service_bound ->
    min(user, token); service_detached -> token scope only. Token-side
    resolution uses no owner identity, no runner floor, and the everyone-floor
    suppressed.
    """
    if user.kind == "service_detached":
        return await resolve_registry_permission(
            db,
            "",
            user.pinned_roles or [],
            resource_name,
            resource_labels,
            owner_email,
            auth_method="",
            preloaded_roles=token_preloaded_roles,
            apply_everyone_floor=False,
        )

    user_eff = await resolve_registry_permission(
        db,
        user.email,
        user.roles,
        resource_name,
        resource_labels,
        owner_email,
        auth_method=user.auth_method,
        preloaded_roles=preloaded_roles,
    )
    if user.kind == "service_bound":
        token_eff = await resolve_registry_permission(
            db,
            "",
            user.pinned_roles or [],
            resource_name,
            resource_labels,
            owner_email,
            auth_method="",
            preloaded_roles=token_preloaded_roles,
            apply_everyone_floor=False,
        )
        return min_registry_permission(user_eff, token_eff)
    return user_eff
