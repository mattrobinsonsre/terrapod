"""Service-catalog RBAC permission resolution (#535).

Resolves the highest catalog permission a user has on a catalog item using the
same label-based model as workspaces and the registry, but on a dedicated axis
(``Role.catalog_permission``) that is **opt-in** — existing roles carry
``"none"`` and grant nothing on the catalog.

Permission hierarchy: read < use < admin
  - ``read``  — browse/view catalog items and their inputs
  - ``use``   — provision instances from a catalog item
  - ``admin`` — manage catalog items and provider templates

Resolution order: platform admin > audit > owner > label RBAC > none

There is deliberately **no 'everyone' floor** — the catalog is gated behind an
explicit grant (the user described catalog access as a distinct RBAC extension),
so a resource label can never hand out catalog access implicitly.
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

CATALOG_PERMISSION_HIERARCHY = {"read": 0, "use": 1, "admin": 2}


def has_catalog_permission(effective: str | None, required: str) -> bool:
    """Check if effective permission meets the required level."""
    if effective is None:
        return False
    return CATALOG_PERMISSION_HIERARCHY.get(effective, -1) >= CATALOG_PERMISSION_HIERARCHY.get(
        required, 99
    )


async def fetch_custom_roles(
    db: AsyncSession,
    user_roles: list[str],
) -> list[Role]:
    """Fetch custom (non-builtin) Role objects for the given role names.

    Use this to pre-load roles once before calling resolve_catalog_permission
    in a loop, passing the result as ``preloaded_roles`` to avoid N+1 queries.
    """
    custom_names = set(user_roles) - BUILTIN_ROLE_NAMES
    if not custom_names:
        return []
    result = await db.execute(select(Role).where(Role.name.in_(custom_names)))
    return list(result.scalars().all())


async def resolve_catalog_permission(
    db: AsyncSession,
    user_email: str,
    user_roles: list[str],
    resource_name: str,
    resource_labels: dict,
    owner_email: str,
    *,
    preloaded_roles: list[Role] | None = None,
) -> str | None:
    """Returns highest catalog permission level (read/use/admin), or None.

    Resolution order (highest wins):
    1. Platform admin → admin
    2. Platform audit → read
    3. Catalog-item owner → admin
    4. Label-based RBAC (custom roles) → role.catalog_permission (if not "none")
    5. Default → None (no access)

    Pass ``preloaded_roles`` (from :func:`fetch_custom_roles`) to skip the
    per-call DB query — useful when resolving permissions for many items.
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
        if preloaded_roles is not None:
            roles = [r for r in preloaded_roles if r.name in custom_role_names]
        else:
            result = await db.execute(select(Role).where(Role.name.in_(custom_role_names)))
            roles = list(result.scalars().all())

        for role in roles:
            # An opt-out role grants nothing on the catalog axis.
            perm = role.catalog_permission
            if perm == "none" or perm not in CATALOG_PERMISSION_HIERARCHY:
                continue

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
                if best is None or CATALOG_PERMISSION_HIERARCHY.get(
                    perm, -1
                ) > CATALOG_PERMISSION_HIERARCHY.get(best, -1):
                    best = perm

    return best


async def resolve_catalog_capabilities_for(
    db: AsyncSession,
    user: "AuthenticatedUser",
    resource_name: str,
    resource_labels: dict,
    owner_email: str,
    *,
    preloaded_roles: list[Role] | None = None,
    token_preloaded_roles: list[Role] | None = None,
) -> frozenset[str]:
    """Capability set a principal holds on a catalog item (#585).

    Catalog-typed wrapper over ``capability_resolver`` (axis="catalog"; no
    everyone-floor, opt-in). Faithful to :func:`resolve_catalog_permission_for`
    for every preset role."""
    from terrapod.services.capability_resolver import resolve_capabilities_for

    return await resolve_capabilities_for(
        db,
        user,
        resource_name,
        resource_labels or {},
        owner_email,
        axis="catalog",
        preloaded_roles=preloaded_roles,
        token_preloaded_roles=token_preloaded_roles,
    )


async def resolve_catalog_permission_for(
    db: AsyncSession,
    user: "AuthenticatedUser",
    resource_name: str,
    resource_labels: dict,
    owner_email: str,
    *,
    preloaded_roles: list[Role] | None = None,
    token_preloaded_roles: list[Role] | None = None,
) -> str | None:
    """Kind-aware catalog permission (#495 service-token model).

    interactive -> user's live roles; service_bound -> min(user, token);
    service_detached -> token scope only. Token-side resolution uses no owner
    identity (catalog provisioning via an automation token is gated by the
    token's own pinned roles).
    """
    if user.kind == "service_detached":
        return await resolve_catalog_permission(
            db,
            "",
            user.pinned_roles or [],
            resource_name,
            resource_labels,
            owner_email,
            preloaded_roles=token_preloaded_roles,
        )

    user_eff = await resolve_catalog_permission(
        db,
        user.email,
        user.roles,
        resource_name,
        resource_labels,
        owner_email,
        preloaded_roles=preloaded_roles,
    )
    if user.kind == "service_bound":
        token_eff = await resolve_catalog_permission(
            db,
            "",
            user.pinned_roles or [],
            resource_name,
            resource_labels,
            owner_email,
            preloaded_roles=token_preloaded_roles,
        )
        return _min_catalog_permission(user_eff, token_eff)
    return user_eff


def _min_catalog_permission(a: str | None, b: str | None) -> str | None:
    """Lower of two catalog permission levels (None = no access; None dominates)."""
    if a is None or b is None:
        return None
    return a if CATALOG_PERMISSION_HIERARCHY[a] <= CATALOG_PERMISSION_HIERARCHY[b] else b
