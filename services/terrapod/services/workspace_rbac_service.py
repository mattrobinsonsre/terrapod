"""Workspace-specific RBAC capability resolution.

Resolves the capability set a principal holds on a workspace using the
label-based, capability-based RBAC model (#585). Delegates to
``terrapod.services.capability_resolver``.
"""

from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.builtin_roles import BUILTIN_ROLE_NAMES
from terrapod.db.models import Role, Workspace
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


async def resolve_workspace_capabilities_for(
    db: AsyncSession,
    user: "AuthenticatedUser",
    workspace: Workspace,
    *,
    preloaded_roles: list[Role] | None = None,
    token_preloaded_roles: list[Role] | None = None,
) -> frozenset[str]:
    """Capability set a principal holds on a workspace (#585 enforcement).

    Workspace-typed wrapper over ``capability_resolver`` — extracts the resource
    fields and delegates. Gates check ``has_capability(caps, RUN_PLAN)`` instead
    of a level threshold."""
    from terrapod.services.capability_resolver import resolve_capabilities_for

    return await resolve_capabilities_for(
        db,
        user,
        workspace.name,
        workspace.labels or {},
        workspace.owner_email,
        axis="workspace",
        preloaded_roles=preloaded_roles,
        token_preloaded_roles=token_preloaded_roles,
        is_catalog_managed=workspace.catalog_item_id is not None,
    )
