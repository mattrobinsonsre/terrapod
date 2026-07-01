"""Agent-pool-specific RBAC capability resolution.

Resolves the capability set a principal holds on an agent pool using the
label-based, capability-based RBAC model (#585). Delegates to
``terrapod.services.capability_resolver``.
"""

from __future__ import annotations

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


async def resolve_pool_capabilities_for(
    db: AsyncSession,
    user: AuthenticatedUser,
    pool_name: str,
    pool_labels: dict,
    owner_email: str | None,
    *,
    preloaded_roles: list[Role] | None = None,
    token_preloaded_roles: list[Role] | None = None,
) -> frozenset[str]:
    """Capability set a principal holds on an agent pool (#585 enforcement).

    Pool-typed wrapper over ``capability_resolver`` (axis="pool")."""
    from terrapod.services.capability_resolver import resolve_capabilities_for

    return await resolve_capabilities_for(
        db,
        user,
        pool_name,
        pool_labels or {},
        owner_email,
        axis="pool",
        preloaded_roles=preloaded_roles,
        token_preloaded_roles=token_preloaded_roles,
    )
