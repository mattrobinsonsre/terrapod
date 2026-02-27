"""Workspace-specific RBAC permission resolution.

Resolves the highest permission level a user has on a workspace
using the label-based RBAC model with hierarchical permission levels.

Permission hierarchy: read < plan < write < admin
Resolution order: platform admin > audit > owner > label RBAC > everyone > none
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.builtin_roles import BUILTIN_ROLE_NAMES
from terrapod.db.models import Role, Workspace
from terrapod.logging_config import get_logger
from terrapod.services.rbac_service import matches_labels, merge_labels

logger = get_logger(__name__)

PERMISSION_HIERARCHY = {"read": 0, "plan": 1, "write": 2, "admin": 3}


def has_permission(effective: str | None, required: str) -> bool:
    """Check if effective permission meets the required level."""
    if effective is None:
        return False
    return PERMISSION_HIERARCHY.get(effective, -1) >= PERMISSION_HIERARCHY.get(required, 99)


async def resolve_workspace_permission(
    db: AsyncSession,
    user_email: str,
    user_roles: list[str],
    workspace: Workspace,
) -> str | None:
    """Returns the highest permission level for a user on a workspace, or None.

    Resolution order (highest wins):
    1. Platform admin → admin
    2. Platform audit → read
    3. Workspace owner → admin
    4. Label-based RBAC (custom roles) → role's workspace_permission (highest wins)
    5. 'everyone' role with access: everyone label → read
    6. Default → None (no access)
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
        result = await db.execute(
            select(Role).where(Role.name.in_(custom_role_names))
        )
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
                if best is None or PERMISSION_HIERARCHY.get(perm, -1) > PERMISSION_HIERARCHY.get(best, -1):
                    best = perm

    # 5. 'everyone' role: if workspace has label access=everyone, grant read
    resource_labels = workspace.labels or {}
    if resource_labels.get("access") == "everyone":
        if best is None:
            best = "read"

    return best
