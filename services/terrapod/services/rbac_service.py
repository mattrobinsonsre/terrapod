"""RBAC (Role-Based Access Control) service.

Label-based RBAC (allow/deny labels and names) is the permanent permission
model for Terrapod. Resources are matched by labels and explicit names;
deny rules take precedence over allow rules; the admin role bypasses all checks.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.builtin_roles import BUILTIN_ROLE_NAMES
from terrapod.db.models import Role
from terrapod.logging_config import get_logger

logger = get_logger(__name__)


async def check_access(
    db: AsyncSession,
    user_email: str,
    resource_name: str,
    resource_labels: dict,
    role_names: list[str],
) -> bool:
    """
    Check if a user has access to a resource.

    RBAC evaluation:
    1. If user has 'admin' role -> ALLOW (bypasses all checks)
    2. Compute effective_allow = union(role_allows, everyone_allows)
    3. Compute effective_deny = union(role_denies)
    4. can_access = matches(effective_allow) AND NOT matches(effective_deny)

    Args:
        db: Database session
        user_email: User's email address
        resource_name: The resource being accessed
        resource_labels: Labels on the resource
        role_names: Role names from the user's session (resolved at login)

    Returns:
        True if access is granted, False otherwise
    """
    role_name_set = set(role_names)

    # Check if admin (bypasses all checks)
    if "admin" in role_name_set:
        logger.debug("Access granted: admin role", user=user_email, resource=resource_name)
        return True

    # Load custom Role objects by name (built-in roles have no DB rows)
    custom_role_names = role_name_set - BUILTIN_ROLE_NAMES
    roles: list[Role] = []
    if custom_role_names:
        result = await db.execute(select(Role).where(Role.name.in_(custom_role_names)))
        roles = list(result.scalars().all())

    # Build effective allow/deny sets
    effective_allow_labels: dict[str, set[str]] = {}
    effective_allow_names: set[str] = set()
    effective_deny_labels: dict[str, set[str]] = {}
    effective_deny_names: set[str] = set()

    # Built-in: everyone role grants access to resources labeled access=everyone
    _merge_labels(effective_allow_labels, {"access": ["everyone"]})

    # Add from custom roles
    for role in roles:
        _merge_labels(effective_allow_labels, role.allow_labels)
        effective_allow_names.update(role.allow_names)
        _merge_labels(effective_deny_labels, role.deny_labels)
        effective_deny_names.update(role.deny_names)

    # Check deny first (deny wins)
    if resource_name in effective_deny_names:
        logger.debug(
            "Access denied: resource name in deny list",
            user=user_email,
            resource=resource_name,
        )
        return False

    if _matches_labels(resource_labels, effective_deny_labels):
        logger.debug(
            "Access denied: resource labels match deny",
            user=user_email,
            resource=resource_name,
        )
        return False

    # Check allow
    if resource_name in effective_allow_names:
        logger.debug(
            "Access granted: resource name in allow list",
            user=user_email,
            resource=resource_name,
        )
        return True

    if _matches_labels(resource_labels, effective_allow_labels):
        logger.debug(
            "Access granted: resource labels match allow",
            user=user_email,
            resource=resource_name,
        )
        return True

    # Default deny
    logger.debug(
        "Access denied: no matching allow rule",
        user=user_email,
        resource=resource_name,
    )
    return False


def merge_labels(target: dict[str, set[str]], source: dict) -> None:
    """Merge label permissions from source into target."""
    for key, values in source.items():
        if key not in target:
            target[key] = set()
        if isinstance(values, list):
            target[key].update(values)
        else:
            target[key].add(values)


def first_matching_label(
    resource_labels: dict, permission_labels: dict[str, set[str]]
) -> tuple[str, str] | None:
    """The first (key, value) pair by which a resource matches a label rule,
    or None if it does not match.

    Exists so a caller that must EXPLAIN a match (the role-reach preview) reads
    the same pass/fail as the caller that merely enforces it. A second matcher
    written for the explanation would be free to drift from this one, and a
    permissions view that disagrees with enforcement is worse than no view.
    """
    for perm_key, perm_values in permission_labels.items():
        if perm_key in resource_labels:
            resource_value = resource_labels[perm_key]
            if resource_value in perm_values:
                return perm_key, resource_value
    return None


def matches_labels(resource_labels: dict, permission_labels: dict[str, set[str]]) -> bool:
    """
    Check if resource labels match any permission label pattern.

    A match occurs when any permission label key exists in resource labels
    and the resource's value for that key is in the permission's allowed values.
    """
    return first_matching_label(resource_labels, permission_labels) is not None


# Keep underscore aliases for backwards compatibility within this module
_merge_labels = merge_labels
_matches_labels = matches_labels
