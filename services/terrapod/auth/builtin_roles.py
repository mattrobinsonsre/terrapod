"""Built-in roles that exist as code, not database rows.

Built-in roles are checked in application logic (dependencies, RBAC service)
rather than being stored in the roles table. The roles table only contains
custom roles created by admins.
"""

BUILTIN_ROLE_NAMES: frozenset[str] = frozenset({"admin", "audit", "everyone"})

BUILTIN_ROLES: dict[str, dict] = {
    "admin": {
        "description": "Unrestricted access to all resources and platform operations",
    },
    "audit": {
        "description": "Read-only access to all platform data",
    },
    "everyone": {
        "description": (
            "Implicit role for all authenticated users; "
            "grants access to resources labeled access=everyone"
        ),
        "allow_labels": {"access": ["everyone"]},
    },
}


PLATFORM_ROLE_NAMES: frozenset[str] = frozenset({"admin", "audit"})


def is_builtin_role(name: str) -> bool:
    """Check if a role name is a built-in role."""
    return name in BUILTIN_ROLE_NAMES


def is_platform_role(name: str) -> bool:
    """Check if a role name is a platform role (admin or audit)."""
    return name in PLATFORM_ROLE_NAMES
