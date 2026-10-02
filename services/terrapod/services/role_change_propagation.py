"""Make a role change reach the credentials that were issued before it.

Two caches hold a resolved authorization decision, and neither notices a write
to the thing it was resolved from (GHSA-pwrq-j4cv-w7qg):

* a **web session** carries the roles login resolved, in its Redis record, for up
  to the session lifetime;
* the **API-token role cache** (`tp:token_roles:`) holds the same answer for 60
  seconds.

The 60-second one is short enough to live with, and was already being busted on
the assignment paths. The session is not: a demoted user kept `admin` in the web
UI for the rest of the session — long enough to undo the demotion, including
revoking the session of the admin who made it.

**Reduction revokes, widening refreshes.** That asymmetry is deliberate rather
than tidy. Removing access has to take effect at once, and a session's role list
*is* the access, so nothing short of ending the session is honest. Granting
access does not need anyone logged out, so the new roles are added to the live
sessions instead — see `grant_roles_to_user_sessions` for why that is a union and
not a re-resolution.

**Nothing here swallows an error.** A caller that cannot reach Redis should
surface that rather than report a demotion as complete; the session ceiling
(`auth.session_absolute_ttl_hours`) and the 60-second token cache are the
eventual-convergence backstop, not a reason to ignore a failure now.

Callers run this on whichever side of their commit fails safe. The assignment
routes call it **after** committing, because the grant has to be durable before
anyone is signed out on the strength of it. The password reset calls it
**before**, because a crash between the commit and the revocation would leave the
old session alive — a rollback that signs someone out unnecessarily is the
cheaper mistake.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# The one place this prefix is spelled. Importing it rather than re-typing
# "tp:token_roles:" is the point: a fourth hand-written copy is a cache that
# silently stops being invalidated.
from terrapod.api.dependencies import _TOKEN_ROLES_PREFIX
from terrapod.auth.sessions import grant_roles_to_user_sessions, revoke_user_sessions
from terrapod.db.models import RoleAssignment
from terrapod.logging_config import get_logger
from terrapod.redis.client import get_redis_client

logger = get_logger(__name__)


async def invalidate_token_roles(email: str) -> None:
    """Drop the cached API-token role set for one identity."""
    await get_redis_client().delete(_TOKEN_ROLES_PREFIX + email)


async def propagate_identity_role_change(
    provider_name: str,
    email: str,
    *,
    previous: set[str],
    current: set[str],
) -> str:
    """Carry an assignment change for one (provider, email) to its credentials.

    Returns what was done to the sessions — ``"revoked"``, ``"refreshed"`` or
    ``"unchanged"`` — so a caller can log it. The token-role cache is dropped
    either way, because it is keyed on email alone and cannot distinguish a
    widening from a reduction.

    `previous` and `current` are the roles stored for this identity before and
    after the write. A role that disappeared is a reduction and outranks any
    additions in the same call: a write that both grants and removes is still a
    removal, and the session has to end.
    """
    await invalidate_token_roles(email)

    removed = previous - current
    added = current - previous

    if removed:
        count = await revoke_user_sessions(email, provider_name=provider_name)
        logger.info(
            "Revoked sessions after a role reduction",
            provider=provider_name,
            email=email,
            removed=sorted(removed),
            sessions=count,
        )
        return "revoked"

    if added:
        await grant_roles_to_user_sessions(email, added, provider_name=provider_name)
        return "refreshed"

    return "unchanged"


async def propagate_role_grant_reduction(db: AsyncSession, role_name: str) -> int:
    """End the sessions of everyone holding a role whose grant just narrowed.

    Returns the number of identities acted on.

    Capabilities are resolved from the `roles` row on every request, so narrowing
    one already bites immediately and this is defence in depth rather than the
    load-bearing fix. It earns its place on the *deletion* path, where it closes a
    real gap: a deleted role's assignments cascade away while the role NAME stays
    in every live session, so recreating a role under that name later hands those
    sessions its new grant without anyone logging in again.

    Only `role_assignments` is consulted. Platform roles (`admin`, `audit`) are
    built-in, and the role endpoints refuse to modify or delete a built-in role,
    so a narrowing here can never be a platform grant.
    """
    rows = await db.execute(
        select(RoleAssignment.provider_name, RoleAssignment.email).where(
            RoleAssignment.role_name == role_name
        )
    )
    holders = {(provider, email) for provider, email in rows.all()}

    for provider_name, email in sorted(holders):
        await revoke_user_sessions(email, provider_name=provider_name)
        await invalidate_token_roles(email)

    if holders:
        logger.info(
            "Revoked sessions after a role grant narrowed",
            role=role_name,
            identities=len(holders),
        )
    return len(holders)
