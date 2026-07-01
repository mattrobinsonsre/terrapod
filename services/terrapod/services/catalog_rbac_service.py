"""Service-catalog RBAC capability resolution (#535, #585).

Resolves the capability set a principal holds on a catalog item using the
label-based, capability-based RBAC model on a dedicated axis that is
**opt-in** — existing roles grant nothing on the catalog.

There is deliberately **no 'everyone' floor** — the catalog is gated behind an
explicit grant (the user described catalog access as a distinct RBAC extension),
so a resource label can never hand out catalog access implicitly.

Delegates to ``terrapod.services.capability_resolver``.
"""

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.builtin_roles import BUILTIN_ROLE_NAMES
from terrapod.db.models import Role
from terrapod.logging_config import get_logger

if TYPE_CHECKING:
    from terrapod.api.dependencies import AuthenticatedUser

logger = get_logger(__name__)


async def fetch_custom_roles(
    db: AsyncSession,
    user_roles: list[str],
) -> list[Role]:
    """Fetch custom (non-builtin) Role objects for the given role names.

    Use this to pre-load roles once before calling the capability resolver
    in a loop, passing the result as ``preloaded_roles`` to avoid N+1 queries.
    """
    custom_names = set(user_roles) - BUILTIN_ROLE_NAMES
    if not custom_names:
        return []
    result = await db.execute(select(Role).where(Role.name.in_(custom_names)))
    return list(result.scalars().all())


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
    everyone-floor, opt-in)."""
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
