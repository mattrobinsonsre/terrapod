"""Login service for role resolution.

Handles the business logic of processing any login (local or SSO):
1. For local: verify user exists and is active
2. Resolve roles from three sources (read-only — no writes to role_assignments)
3. Record recent user in Redis for admin UX

SSO users are NOT stored in the users table. The users table is exclusively
for locally-managed users. SSO identity comes from the IdP token; we cache
recent logins in Redis and look up role assignments by (provider, email).
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.claims_mapper import map_claims_to_roles
from terrapod.auth.recent_users import record_recent_user
from terrapod.auth.sso import AuthenticatedIdentity
from terrapod.config import ClaimsToRolesMapping
from terrapod.db.models import PlatformRoleAssignment, RoleAssignment, User
from terrapod.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class LoginResult:
    """Result of processing a login."""

    email: str
    display_name: str | None
    roles: list[str]
    provider_name: str


async def process_login(
    db: AsyncSession,
    identity: AuthenticatedIdentity,
    claims_rules: list[ClaimsToRolesMapping],
) -> LoginResult:
    """Process a login and return email + role names.

    This function is read-only with respect to roles — it never writes to
    the role_assignments table. Role resolution merges three sources:
    1. IDP groups from connector (already prefix-stripped by the connector)
    2. claims_to_roles config mapping
    3. Internal role_assignments table query by (provider_name, email)

    For local logins, verifies the user exists in the users table and is
    active. For SSO logins, does NOT touch the users table — SSO users
    are not stored there.

    Args:
        db: Database session.
        identity: Authenticated identity from any connector.
        claims_rules: Claims-to-roles mapping rules for this provider.

    Returns:
        LoginResult with email, display_name, roles, and provider_name.
    """
    # For local logins, verify user exists and is active
    if identity.provider_name == "local":
        result = await db.execute(select(User).where(User.email == identity.email))
        user = result.scalar_one_or_none()
        if not user:
            raise ValueError("User not found")
        if not user.is_active:
            raise ValueError("User account is disabled")
        # Update last_login_at
        from terrapod.db.models import utc_now

        user.last_login_at = utc_now()
        if identity.display_name and not user.display_name:
            user.display_name = identity.display_name

    logger.info(
        "Login: resolving roles",
        provider=identity.provider_name,
        email=identity.email,
    )

    # Resolve roles from three sources (read-only — no writes)
    roles: set[str] = set()

    # Source 1: IDP groups from connector
    roles.update(identity.groups)

    # Source 2: claims_to_roles config mapping
    if claims_rules:
        mapped = map_claims_to_roles(identity.raw_claims, claims_rules)
        roles.update(mapped)

    # Source 3: Internal role assignments from DB
    internal = await _load_internal_assignments(db, identity.provider_name, identity.email)
    roles.update(internal)

    all_roles = sorted(roles)

    await db.commit()

    # Record recent user in Redis (fire-and-forget, don't block login)
    try:
        await record_recent_user(identity.provider_name, identity.email, identity.display_name)
    except Exception:
        logger.debug("Failed to record recent user", exc_info=True)

    return LoginResult(
        email=identity.email,
        display_name=identity.display_name,
        roles=all_roles,
        provider_name=identity.provider_name,
    )


async def _load_internal_assignments(
    db: AsyncSession,
    provider_name: str,
    email: str,
) -> list[str]:
    """Load internally-assigned role names from both role tables."""
    # Custom role assignments (FK to roles table)
    result = await db.execute(
        select(RoleAssignment.role_name).where(
            RoleAssignment.provider_name == provider_name,
            RoleAssignment.email == email,
        )
    )
    roles = list(result.scalars().all())

    # Platform role assignments (admin, audit)
    result = await db.execute(
        select(PlatformRoleAssignment.role_name).where(
            PlatformRoleAssignment.provider_name == provider_name,
            PlatformRoleAssignment.email == email,
        )
    )
    roles.extend(result.scalars().all())
    return roles
