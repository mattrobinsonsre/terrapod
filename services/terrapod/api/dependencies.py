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

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.ids import strip_id_prefix
from terrapod.api.metrics import AUTH_FAILURES
from terrapod.auth.api_tokens import validate_api_token
from terrapod.auth.sessions import (
    Session,
    _should_refresh_session,
    get_session,
    refresh_session,
)
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger

logger = get_logger(__name__)
security = HTTPBearer(auto_error=False)

# ── Organization ─────────────────────────────────────────────────────────
# Single org, always "default". Organization paths use literal "default"
# in route patterns — no dynamic path parameter.

DEFAULT_ORG = "default"


# Redis cache TTL for API token role resolution (seconds)
_TOKEN_ROLES_CACHE_TTL = 60
_TOKEN_ROLES_PREFIX = "tp:token_roles:"

#: Token kind minted for the HA peer link (#960 phase 2, #1108). Its own class
#: precisely so peer visibility can never be inherited by a user or a runner.
PEER_KIND = "peer"


@dataclass
class AuthenticatedUser:
    """Unified user identity from either sessions or API tokens."""

    email: str
    display_name: str | None
    roles: list[str]  # the principal's UN-attenuated live roles (user_effective input)
    provider_name: str
    auth_method: str  # "session", "api_token", or "runner_token"
    run_id: str | None = None  # Set only for runner_token auth
    # Token kind (#495). For service tokens, `roles` stays the owner's live
    # roles and `pinned_roles` carries the token's own scope; the per-resource
    # min()/detached resolution happens in the resolve_*_for() wrappers, and
    # the kind-attenuated platform-role view is computed by effective_platform_roles().
    kind: str = "interactive"
    pinned_roles: list[str] | None = None
    # The IdP this principal authenticated with, matching
    # `RoleAssignment.provider_name`. NOT the same as `provider_name`, which for a
    # token is the literal "api_token" (the auth METHOD) -- reading that as an IdP
    # is how role resolution ended up provider-blind (GHSA-3m8x-ff8g-7x8c). None
    # for a runner token, and for a credential minted before the provider was
    # recorded; either way it resolves to no roles rather than to all of them.
    identity_provider: str | None = None


def effective_platform_roles(user: AuthenticatedUser) -> set[str]:
    """Kind-attenuated platform-role set for name-based admin/audit gates (#495).

    interactive -> the principal's live roles; service_bound -> live ∩ pinned;
    service_detached -> pinned only. This is a DERIVED view used only by
    platform gates (require_admin etc. + the inline "admin"/"audit" checks);
    it is NEVER substituted for `user.roles`, which the per-resource min()
    needs un-attenuated.
    """
    roles = set(user.roles)
    if user.kind == "service_detached":
        return set(user.pinned_roles or [])
    if user.kind == "service_bound":
        return roles & set(user.pinned_roles or [])
    return roles


def label_reach_roles(user: AuthenticatedUser) -> set[str]:
    """The narrowest defensible role set for a per-resource LABEL grant.

    Neither `user.roles` nor `effective_platform_roles(user)` is right on its own,
    and each is wrong in the opposite direction:

    - `user.roles` is the LIVE set, so for a `service_bound` token it includes roles
      the token was deliberately not pinned to — the pin is defeated.
    - `effective_platform_roles` returns PINNED-only for a `service_detached` token,
      so it includes roles the principal no longer holds.

    The intersection is narrower than both and escapes in neither direction, which is
    what a grant wants. For an interactive principal it is simply the live set.

    `admin` is dropped because a caller that needs the admin bypass asks for it
    explicitly, with the attenuated `effective_platform_roles` view. Leaving it in
    means `rbac_service.check_access` short-circuits to True on it, re-granting
    through the label path exactly the admin a pin had just removed. The other
    built-in names contribute nothing to `check_access`'s allow/deny sets — it
    subtracts them before loading roles — so they are harmless either way.
    """
    roles = set(user.roles)
    if user.kind in ("service_bound", "service_detached"):
        roles &= set(user.pinned_roles or [])
    roles.discard("admin")
    return roles


async def _resolve_user_roles(
    db: AsyncSession, email: str, identity_provider: str | None
) -> list[str]:
    """Resolve a principal's roles from role_assignments + platform_role_assignments.

    **Both assignment tables are keyed (provider, email), and this must join on
    both.** It used to query on email alone, so a token minted after a login at
    one provider inherited every role assigned to that address under *any*
    provider -- up to platform admin -- which is the whole of
    GHSA-3m8x-ff8g-7x8c. An attacker needed only an account at the weakest
    configured provider, using a victim's address.

    ``identity_provider`` is the IdP the principal authenticated with.
    **None resolves to no roles beyond ``everyone``**: a token minted before the
    column existed cannot be attributed, and picking a provider for it would
    reinstate the hole. Such tokens must be re-minted.

    Cached in Redis for 60s. The cache key stays ``tp:token_roles:{email}`` and
    the *value* holds a per-provider map, deliberately: five call sites already
    invalidate by that exact key, and adding the provider to the key would mean
    finding and fixing every one of them -- missing one leaves a stale role set
    serving after a demotion, which is the failure this function exists to avoid.
    """
    from terrapod.db.models import PlatformRoleAssignment, RoleAssignment
    from terrapod.redis.client import get_redis_client

    if not email or not identity_provider:
        return ["everyone"] if email else []

    redis = get_redis_client()
    cache_key = _TOKEN_ROLES_PREFIX + email

    cached = await redis.get(cache_key)
    by_provider: dict[str, list[str]] = {}
    if cached is not None:
        try:
            loaded = json.loads(cached)
            # A list is the pre-provider-scoping shape. Discard it rather than
            # reading it: it is the union across providers, which is the bug.
            if isinstance(loaded, dict):
                by_provider = loaded
        except (TypeError, ValueError):
            by_provider = {}
        if identity_provider in by_provider:
            return by_provider[identity_provider]

    # Platform roles (admin, audit)
    result = await db.execute(
        select(PlatformRoleAssignment.role_name).where(
            PlatformRoleAssignment.email == email,
            PlatformRoleAssignment.provider_name == identity_provider,
        )
    )
    roles: set[str] = {row[0] for row in result.all()}

    # Custom role assignments
    result = await db.execute(
        select(RoleAssignment.role_name).where(
            RoleAssignment.email == email,
            RoleAssignment.provider_name == identity_provider,
        )
    )
    roles.update(row[0] for row in result.all())

    roles.add("everyone")
    role_list = _drop_roles_requiring_external_sso(sorted(roles), identity_provider, email)

    by_provider[identity_provider] = role_list
    await redis.set(cache_key, json.dumps(by_provider), ex=_TOKEN_ROLES_CACHE_TTL)

    return role_list


def _drop_roles_requiring_external_sso(
    roles: list[str], identity_provider: str, email: str
) -> list[str]:
    """Apply ``require_external_sso_for_roles`` to a non-login principal.

    The login path refuses outright (``_enforce_external_sso_requirement`` in
    routers/auth.py), but it only ever saw the session's role set -- so a token
    minted by a local account carried the restricted roles anyway and the policy
    was advisory in practice (GHSA-3m8x-ff8g-7x8c).

    Here the roles are ATTENUATED rather than the request refused. A token is used
    by automation that cannot be prompted to go and log in via SSO, so a blanket
    403 on every call would convert a policy violation into an outage; dropping
    the restricted roles leaves the token doing exactly what it is still entitled
    to. Acting with fewer roles than minted is survivable, acting with more is the
    vulnerability.
    """
    restricted = settings.auth.require_external_sso_for_roles
    if not restricted or identity_provider != "local":
        return roles

    kept = [r for r in roles if r not in restricted]
    if len(kept) != len(roles):
        logger.warning(
            "Dropped roles requiring external SSO from a local principal",
            email=email,
            dropped=sorted(set(roles) - set(kept)),
        )
    return kept


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> AuthenticatedUser:
    """Unified auth dependency — checks API tokens, then sessions.

    Priority order:
    1. Bearer token → API token (SHA-256 hash + DB lookup)
    2. Bearer token → Redis session
    3. 401

    Returns AuthenticatedUser with email, roles, auth_method.
    """
    if credentials is not None:
        token = credentials.credentials

        # Try runner token first (fast HMAC check, no DB/Redis)
        if token.startswith("runtok:"):
            from terrapod.auth.runner_tokens import verify_runner_token

            run_id = verify_runner_token(token)
            if run_id is not None:
                request.state.user_email = "runner"  # for audit middleware
                return AuthenticatedUser(
                    email="runner",
                    display_name="Runner Job",
                    roles=["everyone"],
                    provider_name="runner_token",
                    auth_method="runner_token",
                    run_id=run_id,
                )

        # Try API token (fast hash + indexed DB lookup)
        api_token = await validate_api_token(db, token)
        if api_token is not None:
            # A peer token is NOT a user (#960 phase 3, #1110). It is unbound,
            # so it resolves to no roles and fails every RBAC check — but a
            # handful of endpoints require only "some authenticated principal"
            # (creating a workspace, for one), and a peer would satisfy those.
            # Peer credentials are accepted by `get_peer_identity` and nowhere
            # else; refusing here is what makes that true.
            if api_token.kind == PEER_KIND:
                raise HTTPException(status_code=401, detail="Not authenticated")

            # Resolve roles from DB (cached in Redis for 60s)
            email = api_token.bound_to or ""
            if email and api_token.identity_provider is None:
                # Fails closed to no roles, which is right but undiagnosable from
                # the outside: the caller just starts getting 403s. Name the token
                # so an operator can find and re-mint it. Only reachable for a
                # token minted before the provider was recorded.
                logger.warning(
                    "API token has no recorded identity provider; it resolves to no roles",
                    token_id=api_token.id,
                    bound_to=email,
                )
            roles = (
                await _resolve_user_roles(db, email, api_token.identity_provider) if email else []
            )

            request.state.user_email = email  # for audit middleware
            return AuthenticatedUser(
                email=email,
                display_name=None,
                roles=roles,
                provider_name="api_token",
                auth_method="api_token",
                identity_provider=api_token.identity_provider,
                kind=api_token.kind,
                pinned_roles=api_token.pinned_roles,
            )

        # Try session (Redis lookup)
        session = await get_session(token)
        if session is not None:
            # Sliding window: refresh TTL on activity (rate-limited to every 5 min)
            if _should_refresh_session(session):
                new_expires = await refresh_session(token, session)
                request.state.session_expires_at = new_expires

            request.state.user_email = session.email  # for audit middleware
            return AuthenticatedUser(
                email=session.email,
                display_name=session.display_name,
                roles=session.roles,
                provider_name=session.provider_name,
                auth_method="session",
                identity_provider=session.provider_name,
            )

    if credentials is not None:
        _tok = credentials.credentials
        if _tok.startswith("runtok:"):
            AUTH_FAILURES.labels(method="runner_token", reason="invalid_or_expired").inc()
        else:
            AUTH_FAILURES.labels(method="bearer", reason="invalid_or_expired").inc()
    else:
        AUTH_FAILURES.labels(method="none", reason="missing").inc()

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def authenticate_request(request: Request) -> AuthenticatedUser:
    """Authenticate a request using a short-lived DB session.

    Unlike get_current_user (which uses Depends(get_db) and holds the session
    for the request lifetime), this function creates and closes its own session.
    Use this in SSE endpoints to avoid holding DB connections for the entire
    stream duration.
    """
    from terrapod.db.session import get_db_session

    # Extract bearer token from Authorization header
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = auth_header[7:]  # strip "Bearer "

    # Try runner token first (no DB needed)
    if token.startswith("runtok:"):
        from terrapod.auth.runner_tokens import verify_runner_token

        run_id = verify_runner_token(token)
        if run_id is not None:
            request.state.user_email = "runner"
            return AuthenticatedUser(
                email="runner",
                display_name="Runner Job",
                roles=["everyone"],
                provider_name="runner_token",
                auth_method="runner_token",
                run_id=run_id,
            )

    # Try API token and session with a short-lived DB session
    async with get_db_session() as db:
        api_token = await validate_api_token(db, token)
        if api_token is not None:
            # A peer is not a user — see the note in get_current_user.
            if api_token.kind == PEER_KIND:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or expired token",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            email = api_token.bound_to or ""
            roles = (
                await _resolve_user_roles(db, email, api_token.identity_provider) if email else []
            )
            request.state.user_email = email
            return AuthenticatedUser(
                email=email,
                display_name=None,
                roles=roles,
                provider_name="api_token",
                auth_method="api_token",
                identity_provider=api_token.identity_provider,
                kind=api_token.kind,
                pinned_roles=api_token.pinned_roles,
            )

    # Try session (Redis only — no DB needed)
    session = await get_session(token)
    if session is not None:
        if _should_refresh_session(session):
            new_expires = await refresh_session(token, session)
            request.state.session_expires_at = new_expires
        request.state.user_email = session.email
        return AuthenticatedUser(
            email=session.email,
            display_name=session.display_name,
            roles=session.roles,
            provider_name=session.provider_name,
            auth_method="session",
            identity_provider=session.provider_name,
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _enforce_listener_pop(request: "Request", cert) -> None:
    """Require the caller to hold the private key behind `cert`.

    Called from BOTH listener auth paths — `authenticate_listener` (the SSE one,
    which cannot use yield-dependencies) and `get_listener_identity` (everything
    else). It is one function on purpose: every check those two perform before
    this point is satisfied by a COPY of the certificate, which is public and is
    sent on every request, so a path that skipped this would accept a replayed
    header indefinitely while looking fully authenticated. A guard that lives in
    one entry point and not its sibling is enforced only where someone happens to
    be looking.
    """
    from terrapod.auth.listener_pop import (
        NONCE_HEADER,
        SIGNATURE_HEADER,
        TIMESTAMP_HEADER,
        ProofOfPossessionError,
        verify_request,
    )
    from terrapod.config import settings

    if not settings.agent_pools.require_listener_proof_of_possession:
        return
    h = request.headers
    try:
        await verify_request(
            cert,
            method=request.method,
            path=request.url.path,
            timestamp=h.get(TIMESTAMP_HEADER.lower(), ""),
            nonce=h.get(NONCE_HEADER.lower(), ""),
            signature=h.get(SIGNATURE_HEADER.lower(), ""),
        )
    except ProofOfPossessionError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Listener proof of possession failed: {exc}",
        ) from None


async def authenticate_listener(request: Request) -> "ListenerIdentity":
    """Authenticate a listener via certificate, looking up identity in Redis.

    Like authenticate_request but for certificate-based listener auth.
    Used in SSE endpoints to avoid holding DB connections.
    """
    import base64
    import datetime as dt

    from cryptography import x509
    from cryptography.exceptions import InvalidSignature

    from terrapod.auth.ca import get_ca, get_certificate_fingerprint
    from terrapod.services import agent_pool_service

    cert_header = request.headers.get("x-terrapod-client-cert")
    if not cert_header:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Terrapod-Client-Cert header required",
        )

    try:
        cert_pem = base64.b64decode(cert_header)
        cert = x509.load_pem_x509_certificate(cert_pem)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid certificate encoding",
        ) from None

    # Verify CA signature
    ca = get_ca()
    try:
        ca.ca_cert.public_key().verify(cert.signature, cert.tbs_certificate_bytes)
    except (InvalidSignature, Exception):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Certificate not signed by this CA",
        ) from None

    # Check expiry
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

    # Redis lookup (no DB session needed)
    listener = await agent_pool_service.get_listener_by_name(listener_name)
    if listener is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"No listener registered with name '{listener_name}'",
        )

    fingerprint = get_certificate_fingerprint(cert)
    if not await agent_pool_service.is_fingerprint_valid(
        listener["id"], fingerprint, listener=listener
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Certificate fingerprint not registered",
        )

    await _enforce_listener_pop(request, cert)

    return ListenerIdentity(
        listener_id=uuid.UUID(listener["id"]),
        name=listener.get("name", listener_name),
        pool_id=uuid.UUID(listener["pool_id"]),
        certificate_fingerprint=fingerprint,
        certificate_expires_at=None,  # not needed for auth
    )


def require_non_runner(
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """Reject runner tokens — use on endpoints runners must not access.

    Runner tokens are scoped to artifact/cache operations only. This
    dependency blocks them from resource creation and management endpoints.
    """
    if user.auth_method == "runner_token":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Runner tokens cannot access this endpoint",
        )
    return user


async def get_current_session(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
) -> Session:
    """Dependency to get the current authenticated session (sessions only).

    Does NOT check API tokens — use get_current_user for unified auth.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session",
            headers={"WWW-Authenticate": "Bearer"},
        )

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
    """Dependency to require admin role (kind-attenuated for service tokens)."""
    if "admin" not in effective_platform_roles(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return user


async def require_admin_or_audit(
    user: AuthenticatedUser = Depends(get_current_user),
) -> AuthenticatedUser:
    """Dependency to require admin or audit role (kind-attenuated for service tokens)."""
    if not ({"admin", "audit"} & effective_platform_roles(user)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin or audit access required",
        )
    return user


def require_runner_for_run(user: AuthenticatedUser, run_id: str) -> None:
    """Reject the request unless the caller is a runner-token authenticated
    for this exact run.

    Used by runner-protocol endpoints (artifact upload/download, OPA
    policy bundle/results) so a leaked token from one run can't drive
    actions on another. Not a FastAPI dependency — call it inside the
    handler with the path's ``run_id`` after resolving the user.
    """
    if user.auth_method != "runner_token":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Runner token required",
        )
    # Compare the two ids in the same spelling. The token carries a bare uuid
    # (`runner_tokens.generate_runner_token` stores `str(run_id)`), while the
    # path may carry `run-{uuid}` -- every other run endpoint accepts both, so
    # a prefixed id reaching here used to be rejected as "not scoped to this
    # run", which reads as a security failure rather than a spelling one
    # (#1699). Normalising both sides only widens what is accepted: the token
    # must still name the same run.
    if strip_id_prefix(user.run_id or "", "run-") != strip_id_prefix(run_id, "run-"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Token not scoped to this run",
        )


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
    request: Request,
    x_terrapod_client_cert: str = Header(None),
) -> ListenerIdentity:
    """Authenticate a runner listener via X-Terrapod-Client-Cert header.

    The header contains a base64-encoded PEM certificate. We:
    1. Decode and parse the certificate
    2. Verify it was signed by our CA
    3. Check it hasn't expired
    4. Extract the CN and look up the listener in Redis
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
    from terrapod.services import agent_pool_service

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

    # Redis lookup (no DB session needed)
    listener = await agent_pool_service.get_listener_by_name(listener_name)
    if listener is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"No listener registered with name '{listener_name}'",
        )

    # Verify fingerprint match
    fingerprint = get_certificate_fingerprint(cert)
    if not await agent_pool_service.is_fingerprint_valid(
        listener["id"], fingerprint, listener=listener
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Certificate fingerprint not registered",
        )

    await _enforce_listener_pop(request, cert)

    return ListenerIdentity(
        listener_id=uuid.UUID(listener["id"]),
        name=listener.get("name", listener_name),
        pool_id=uuid.UUID(listener["pool_id"]),
        certificate_fingerprint=fingerprint,
        certificate_expires_at=None,  # not needed for auth
    )


@dataclass
class PeerIdentity:
    """The other node in an HA pair, authenticated over the peer link."""

    client_id: str
    token_id: str


async def get_peer_identity(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PeerIdentity:
    """Authenticate the peer node, and only the peer node (#960 phase 3, #1110).

    The replication endpoints are the one place a ``peer`` token is accepted.
    Everything else refuses it outright — a peer may read entities an ordinary
    user could not (resolved sensitive variables among them), so the identity is
    deliberately not expressible in terms of roles that could be granted to a
    person by accident.
    """
    # A deployment that has not declared peering accepts no peer identity, even
    # if a credential row survives from one that was torn down (#1169). Config
    # expresses intent; withdrawing it withdraws the capability, rather than
    # leaving a forgotten credential with full read of decrypted variables.
    if not settings.ha.peering_configured:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    api_token = await validate_api_token(db, auth_header[7:])
    if api_token is None or api_token.kind != PEER_KIND:
        # Deliberately the same response either way: an unknown token and a
        # valid-but-non-peer token are indistinguishable to the caller.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    request.state.user_email = "peer"  # for audit middleware
    # `created_by` is "oauth-client:<client_id>", set by the client_credentials
    # grant — the only thing that mints a peer token.
    created_by = api_token.created_by or ""
    client_id = created_by.removeprefix("oauth-client:")
    return PeerIdentity(client_id=client_id, token_id=str(api_token.id))
