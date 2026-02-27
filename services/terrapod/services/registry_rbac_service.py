"""Registry-specific RBAC permission resolution.

Resolves the highest permission level a user has on a registry resource
(module or provider) using the same label-based model as workspaces but
with a simpler three-level hierarchy (no "plan" for registry resources).

Permission hierarchy: read < write < admin
Resolution order: platform admin > audit > owner > label RBAC > everyone > none
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.builtin_roles import BUILTIN_ROLE_NAMES
from terrapod.db.models import Role
from terrapod.logging_config import get_logger
from terrapod.services.rbac_service import matches_labels, merge_labels

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
    return REGISTRY_PERMISSION_HIERARCHY.get(effective, -1) >= REGISTRY_PERMISSION_HIERARCHY.get(required, 99)


async def resolve_registry_permission(
    db: AsyncSession,
    user_email: str,
    user_roles: list[str],
    resource_name: str,
    resource_labels: dict,
    owner_email: str,
) -> str | None:
    """Returns highest permission level (read/write/admin), or None.

    Resolution order (highest wins):
    1. Platform admin → admin
    2. Platform audit → read
    3. Resource owner → admin
    4. Label-based RBAC (custom roles) → mapped workspace_permission
    5. 'everyone' role with access: everyone label → read
    6. Default → None (no access)
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

    # 4. Label-based RBAC from custom roles
    custom_role_names = role_set - BUILTIN_ROLE_NAMES
    if custom_role_names:
        result = await db.execute(
            select(Role).where(Role.name.in_(custom_role_names))
        )
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
                if best is None or REGISTRY_PERMISSION_HIERARCHY.get(registry_perm, -1) > REGISTRY_PERMISSION_HIERARCHY.get(best, -1):
                    best = registry_perm

    # 5. 'everyone' role: if resource has label access=everyone, grant read
    if resource_labels.get("access") == "everyone":
        if best is None:
            best = "read"

    return best
