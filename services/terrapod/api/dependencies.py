"""FastAPI dependencies for authentication and authorization.

Two credential types, one Bearer header:
- API tokens (PostgreSQL) — long-lived, for terraform CLI and automation
- Sessions (Redis) — short-lived (sliding 12h), for web UI

The auth dependency tries API token lookup first (fast SHA-256 hash + DB query),
then Redis session lookup. Both return the same AuthenticatedUser shape.

Additionally, runner listeners authenticate via X-Terrapod-Client-Cert header
with Ed25519 certificates signed by the Terrapod CA.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.api_tokens import validate_api_token
from terrapod.auth.sessions import (
    Session,
    _should_refresh_session,
    get_session,
    refresh_session,
)
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger

logger = get_logger(__name__)
security = HTTPBearer()

# Redis cache TTL for API token role resolution (seconds)
_TOKEN_ROLES_CACHE_TTL = 60
_TOKEN_ROLES_PREFIX = "tp:token_roles:"


@dataclass
class AuthenticatedUser:
    """Unified user identity from either sessions or API tokens."""

    email: str
    display_name: str | None
    roles: list[str]
    provider_name: str
    auth_method: str  # "session" or "api_token"


async def _resolve_user_roles(db: AsyncSession, email: str) -> list[str]:
    """Resolve a user's roles from role_assignments + platform_role_assignments.

    Checks Redis cache first (60s TTL). On miss, queries both tables and
    caches the result.
    """
    from terrapod.db.models import PlatformRoleAssignment, RoleAssignment
    from terrapod.redis.client import get_redis_client

    redis = get_redis_client()
    cache_key = _TOKEN_ROLES_PREFIX + email

    # Check cache
    cached = await redis.get(cache_key)
    if cached is not None:
        return json.loads(cached)

    # Query platform roles (admin, audit)
    result = await db.execute(
        select(PlatformRoleAssignment.role_name).where(PlatformRoleAssignment.email == email)
    )
    roles: set[str] = {row[0] for row in result.all()}

    # Query custom role assignments
    result = await db.execute(select(RoleAssignment.role_name).where(RoleAssignment.email == email))
    roles.update(row[0] for row in result.all())

    # Always include 'everyone'
    roles.add("everyone")

    role_list = sorted(roles)

    # Cache for 60s
    await redis.set(cache_key, json.dumps(role_list), ex=_TOKEN_ROLES_CACHE_TTL)

    return role_list


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> AuthenticatedUser:
    """Unified auth dependency — checks API tokens then sessions.

    1. Hash the bearer token, query api_tokens table
    2. If not found, check Redis sessions
    3. If neither matches, 401

    Returns AuthenticatedUser with email, roles, auth_method.
    """
    token = credentials.credentials

    # Try API token first (fast hash + indexed DB lookup)
    api_token = await validate_api_token(db, token)
    if api_token is not None:
        # Resolve roles from DB (cached in Redis for 60s)
        email = api_token.user_email or ""
        roles = await _resolve_user_roles(db, email) if email else []

        return AuthenticatedUser(
            email=email,
            display_name=None,
            roles=roles,
            provider_name="api_token",
            auth_method="api_token",
        )

    # Try session (Redis lookup)
    session = await get_session(token)
    if session is not None:
        # Sliding window: refresh TTL on activity (rate-limited to every 5 min)
        if _should_refresh_session(session):
            await refresh_session(token, session)

        return AuthenticatedUser(
            email=session.email,
            display_name=session.display_name,
            roles=session.roles,
            provider_name=session.provider_name,
            auth_method="session",
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_current_session(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> Session:
    """Dependency to get the current authenticated session (sessions only).

    Does NOT check API tokens — use get_current_user for unified auth.
    """
    token = credentials.credentials
    session = await get_session(token)

    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Sliding window: refresh TTL on activity (rate-limited to every 5 min)
    if _should_refresh_session(session):
        await refresh_session(token, session)

    return session


async def require_admin(
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """Dependency to require admin role."""
    if "admin" not in user.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user


async def require_admin_or_audit(
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """Dependency to require admin or audit role."""
    if not ({"admin", "audit"} & set(user.roles)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin or audit access required",
        )
    return user


# ── Listener Certificate Auth ────────────────────────────────────────────


@dataclass
class ListenerIdentity:
    """Authenticated listener identity from certificate auth."""

    listener_id: uuid.UUID
    name: str
    pool_id: uuid.UUID
    certificate_fingerprint: str
    certificate_expires_at: datetime | None


async def get_listener_identity(
    x_terrapod_client_cert: str = Header(None),
    db: AsyncSession = Depends(get_db),
) -> ListenerIdentity:
    """Authenticate a runner listener via X-Terrapod-Client-Cert header.

    The header contains a base64-encoded PEM certificate. We:
    1. Decode and parse the certificate
    2. Verify it was signed by our CA
    3. Check it hasn't expired
    4. Extract the CN and look up the RunnerListener in the DB
    5. Verify the certificate fingerprint matches
    """
    if not x_terrapod_client_cert:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Terrapod-Client-Cert header required",
        )

    import base64

    from cryptography import x509
    from cryptography.exceptions import InvalidSignature

    from terrapod.auth.ca import get_ca, get_certificate_fingerprint
    from terrapod.db.models import RunnerListener

    try:
        cert_pem = base64.b64decode(x_terrapod_client_cert)
        cert = x509.load_pem_x509_certificate(cert_pem)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid certificate encoding",
        ) from None

    # Verify CA signature
    ca = get_ca()
    try:
        ca.ca_cert.public_key().verify(
            cert.signature,
            cert.tbs_certificate_bytes,
        )
    except (InvalidSignature, Exception):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Certificate not signed by this CA",
        ) from None

    # Check expiry
    import datetime as dt

    now = dt.datetime.now(dt.UTC)
    if now > cert.not_valid_after_utc or now < cert.not_valid_before_utc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Certificate expired or not yet valid",
        )

    # Extract CN
    cn = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
    if not cn:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Certificate has no Common Name",
        )
    listener_name = cn[0].value

    # Look up listener by name
    result = await db.execute(select(RunnerListener).where(RunnerListener.name == listener_name))
    listener = result.scalar_one_or_none()
    if listener is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"No listener registered with name '{listener_name}'",
        )

    # Verify fingerprint match
    fingerprint = get_certificate_fingerprint(cert)
    if listener.certificate_fingerprint and fingerprint != listener.certificate_fingerprint:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Certificate fingerprint mismatch",
        )

    return ListenerIdentity(
        listener_id=listener.id,
        name=listener.name,
        pool_id=listener.pool_id,
        certificate_fingerprint=fingerprint,
        certificate_expires_at=listener.certificate_expires_at,
    )
