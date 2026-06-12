"""API token management for terraform CLI and automation.

API tokens are long-lived Bearer tokens stored as SHA-256 hashes in PostgreSQL.
The raw token value is only available at creation time. Lookup is by hash
(indexed column) on every request.

Token format: {random_id}.tpod.{random_secret}

Three kinds (#495): interactive (terraform login / UI), service_bound
(user-bound automation; effective perms = min(pinned, owner live)),
service_detached (admin-managed M2M; absolute pinned perms). Expiry is
kind-aware (separate caps) and basis-aware (rotated_at or created_at);
user-bound tokens are subject to idle-login rejection.
"""

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.recent_users import user_seen_within_window
from terrapod.config import settings
from terrapod.db.models import APIToken, User, now_utc
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

# Minimum interval between last_used_at updates (seconds).
# Avoids a DB write on every single API request.
LAST_USED_UPDATE_INTERVAL = 60

# Service tokens ALWAYS expire — if the service cap is misconfigured to 0
# ("no limit"), fall back to this so a service token is never unbounded (#495).
_SERVICE_TTL_FALLBACK_HOURS = 8760

_USER_BOUND_KINDS = ("interactive", "service_bound")
_SERVICE_KINDS = ("service_bound", "service_detached")


def _generate_token_id() -> str:
    """Generate a token ID in the format 'at-{random}'."""
    return f"at-{secrets.token_hex(8)}"


def _generate_raw_token() -> str:
    """Generate a raw token in the format '{random_id}.tpod.{random_secret}'."""
    random_id = secrets.token_urlsafe(12)
    random_secret = secrets.token_urlsafe(32)
    return f"{random_id}.tpod.{random_secret}"


def hash_token(raw_token: str) -> str:
    """SHA-256 hash a raw token for storage."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def _max_ttl_hours_for_kind(kind: str) -> int:
    """Max-lifetime cap (hours) for a token kind.

    Service kinds use a separate (longer) cap and ALWAYS expire — a `0`
    ("no limit") is never honoured for them. Interactive keeps the historic
    `0 = no limit` behaviour.
    """
    if kind in _SERVICE_KINDS:
        cap = settings.auth.service_token_max_ttl_hours
        return cap if cap > 0 else _SERVICE_TTL_FALLBACK_HOURS
    return settings.auth.api_token_max_ttl_hours


def token_expires_at(token: APIToken) -> datetime | None:
    """When the token expires, or None if it never does.

    Expiry basis is ``rotated_at or created_at`` so rotation resets the
    clock. The per-token ``lifespan_hours`` takes precedence over the
    kind's global cap. None is only returned for interactive tokens when
    the global cap is 0 and no per-token lifespan is set.
    """
    cap = _max_ttl_hours_for_kind(token.kind)
    ttl = token.lifespan_hours if token.lifespan_hours is not None else cap
    if token.kind in _SERVICE_KINDS and ttl <= 0:
        # service tokens never go unbounded
        ttl = cap
    if ttl <= 0:
        return None
    basis = token.rotated_at or token.created_at
    return basis + timedelta(hours=ttl)


async def create_api_token(
    db: AsyncSession,
    *,
    bound_to: str | None,
    created_by: str,
    kind: str = "interactive",
    description: str = "",
    lifespan_hours: int | None = None,
    pinned_roles: list[str] | None = None,
) -> tuple[APIToken, str]:
    """Create an API token. Returns (model, raw_token_value).

    The raw token value is only available at creation time. ``bound_to`` is
    the owning identity (None for detached); ``created_by`` is the minter
    (audit). ``lifespan_hours`` is clamped to the kind's cap.
    """
    raw_token = _generate_raw_token()
    token_id = _generate_token_id()

    cap = _max_ttl_hours_for_kind(kind)
    if lifespan_hours is not None and cap > 0:
        lifespan_hours = min(lifespan_hours, cap)

    api_token = APIToken(
        id=token_id,
        token_hash=hash_token(raw_token),
        description=description,
        kind=kind,
        bound_to=bound_to,
        created_by=created_by,
        pinned_roles=pinned_roles,
        lifespan_hours=lifespan_hours,
    )

    db.add(api_token)
    await db.flush()

    logger.info(
        "API token created",
        token_id=token_id,
        kind=kind,
        bound_to=bound_to,
        created_by=created_by,
        lifespan_hours=lifespan_hours,
    )

    return api_token, raw_token


async def _bound_token_owner_active(db: AsyncSession, email: str | None) -> bool:
    """Whether a user-bound token's owner is still valid (#495).

    Rejected when: no owner; a local ``users`` row exists and is inactive;
    or the idle-login window has lapsed (no ``tp:user_seen`` marker). SSO
    identities have no ``users`` row, so an absent row is NOT a rejection —
    the idle window is what offboards them.
    """
    if not email:
        return False

    # Local-account deactivation (only local users have a row + is_active).
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if user is not None and not user.is_active:
        return False

    # Idle-login window (disabled when bound_token_idle_days == 0).
    if settings.auth.bound_token_idle_days > 0:
        if not await user_seen_within_window(email):
            return False

    return True


async def validate_api_token(db: AsyncSession, raw_token: str) -> APIToken | None:
    """Validate a Bearer token against the database.

    SHA-256 hash the token, look up by hash. Check kind-aware expiry, then
    idle-login rejection for user-bound tokens (detached are exempt). Update
    last_used_at (rate-limited to once per minute).
    """
    token_hash = hash_token(raw_token)

    result = await db.execute(select(APIToken).where(APIToken.token_hash == token_hash))
    api_token = result.scalar_one_or_none()

    if api_token is None:
        return None

    now = now_utc()
    expiry = token_expires_at(api_token)
    if expiry is not None and now > expiry:
        logger.debug("API token expired", token_id=api_token.id)
        return None

    # Idle-login rejection — user-bound tokens only; detached are exempt.
    if api_token.kind in _USER_BOUND_KINDS:
        if not await _bound_token_owner_active(db, api_token.bound_to):
            logger.debug("API token rejected: owner idle or inactive", token_id=api_token.id)
            return None

    # Update last_used_at (rate-limited). Driven off last_used_at, never rotated_at.
    should_update = (
        api_token.last_used_at is None
        or (now - api_token.last_used_at).total_seconds() > LAST_USED_UPDATE_INTERVAL
    )
    if should_update:
        await db.execute(
            update(APIToken).where(APIToken.id == api_token.id).values(last_used_at=now)
        )

    return api_token


async def list_user_tokens(db: AsyncSession, email: str) -> list[APIToken]:
    """List a user's own tokens.

    Filters on ``bound_to == email``, so detached tokens (bound_to NULL) are
    naturally excluded from any per-user view.
    """
    result = await db.execute(
        select(APIToken).where(APIToken.bound_to == email).order_by(APIToken.created_at.desc())
    )
    return list(result.scalars().all())


async def list_all_tokens(db: AsyncSession, kind: str | None = None) -> list[APIToken]:
    """List all API tokens (admin use), optionally filtered by kind."""
    stmt = select(APIToken).order_by(APIToken.created_at.desc())
    if kind:
        stmt = stmt.where(APIToken.kind == kind)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def list_expiring_service_tokens(
    db: AsyncSession,
    caller_email: str,
    is_admin: bool,
    within_days: int,
) -> list[APIToken]:
    """Service tokens nearing expiry, scoped to the caller (#495).

    Every caller sees their own ``service_bound`` tokens; admins additionally
    see all ``service_detached`` tokens. Single query for the in-scope
    candidates, then the kind-aware expiry filter is applied in Python (no
    per-token query).
    """
    conds = [(APIToken.bound_to == caller_email) & (APIToken.kind == "service_bound")]
    if is_admin:
        conds.append(APIToken.kind == "service_detached")

    result = await db.execute(select(APIToken).where(or_(*conds)))
    candidates = result.scalars().all()

    threshold = now_utc() + timedelta(days=within_days)
    expiring = []
    for token in candidates:
        expiry = token_expires_at(token)
        if expiry is not None and expiry <= threshold:
            expiring.append(token)
    expiring.sort(key=lambda t: token_expires_at(t) or now_utc())
    return expiring


async def get_token_by_id(db: AsyncSession, token_id: str) -> APIToken | None:
    """Get a token by its public ID."""
    result = await db.execute(select(APIToken).where(APIToken.id == token_id))
    return result.scalar_one_or_none()


async def rotate_token(db: AsyncSession, token_id: str) -> tuple[APIToken, str] | None:
    """Roll a token's secret and reset its expiry clock (#495).

    Single-row UPDATE: new hash + ``rotated_at = now`` (the expiry basis),
    keeping the same kind/binding/scope/description. Returns (model,
    raw_token) or None if the token doesn't exist. The old secret is invalid
    immediately. Authorization is enforced by the caller.
    """
    token = await get_token_by_id(db, token_id)
    if token is None:
        return None

    raw_token = _generate_raw_token()
    token.token_hash = hash_token(raw_token)
    token.rotated_at = now_utc()
    await db.flush()

    logger.info("API token rotated", token_id=token_id, kind=token.kind)
    return token, raw_token


async def revoke_token(db: AsyncSession, token_id: str) -> bool:
    """Revoke (delete) an API token by ID.

    Returns True if the token existed, False if not found.
    """
    result = await db.execute(select(APIToken).where(APIToken.id == token_id))
    api_token = result.scalar_one_or_none()

    if api_token is None:
        return False

    await db.delete(api_token)
    await db.flush()

    logger.info("API token revoked", token_id=token_id)
    return True


async def revoke_all_for_user(db: AsyncSession, email: str) -> int:
    """Revoke (delete) every token bound to an identity (urgent offboarding, #495).

    Single DELETE over ``bound_to == email`` (detached tokens, bound_to NULL,
    are untouched). Returns the number of tokens removed. The caller is
    responsible for invalidating the cached roles for the email after commit.
    """
    result = await db.execute(delete(APIToken).where(APIToken.bound_to == email))
    await db.flush()
    count = result.rowcount or 0
    logger.info("Revoked all tokens for user", email=email, count=count)
    return count
