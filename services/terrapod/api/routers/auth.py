"""Authentication router.

All authentication — local password, OIDC, SAML — goes through the same
/authorize -> login -> /callback -> /token pipeline. The API server is the
single gateway for all IDP communication.

Consumers:
    Web UI:
        GET  /api/v2/auth/providers         — login page provider list
        POST /api/v2/auth/local/authorize   — direct JSON login (no redirect)
        POST /api/v2/auth/token             — exchange code for session
        POST /api/v2/auth/logout            — revoke current session
        GET  /api/v2/auth/sessions          — sessions management page
        GET  /api/v2/auth/sessions/all      — admin sessions view
        DELETE /api/v2/auth/sessions/user/{email} — admin revoke
"""

import base64
import hashlib
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.auth_state import (
    AuthCode,
    AuthState,
    consume_auth_code,
    consume_auth_state,
    generate_code,
    generate_state,
    store_auth_code,
    store_auth_state,
)
from terrapod.auth.connectors import get_connector, get_default_connector, list_connectors
from terrapod.auth.sessions import (
    create_session,
    get_session,
    list_all_sessions,
    list_user_sessions,
    revoke_all_user_sessions,
    revoke_session,
)
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.sso_service import process_login

router = APIRouter(prefix="/auth", tags=["auth"])
logger = get_logger(__name__)


# --- Pydantic models ---


class ProviderInfo(BaseModel):
    name: str
    type: str
    display_name: str


class ProvidersResponse(BaseModel):
    providers: list[ProviderInfo]
    default_provider: str | None = None


class LocalAuthorizeRequest(BaseModel):
    email: str
    password: str
    code_challenge: str
    code_challenge_method: str = "S256"
    state: str = ""


class LocalAuthorizeResponse(BaseModel):
    code: str
    state: str


class TokenExchangeResponse(BaseModel):
    session_token: str
    expires_at: str
    email: str
    roles: list[str]


class SessionInfo(BaseModel):
    email: str
    roles: list[str]
    provider_name: str
    created_at: str
    expires_at: str
    last_active_at: str
    token_hint: str
    is_current: bool = False


# --- Endpoints ---


@router.get("/providers", response_model=ProvidersResponse)
async def list_providers() -> ProvidersResponse:
    """List configured auth providers."""
    providers = [ProviderInfo(**p) for p in list_connectors()]
    return ProvidersResponse(
        providers=providers,
        default_provider=settings.auth.sso.default_provider or None,
    )


@router.get("/authorize")
async def authorize(
    redirect_uri: str = Query(..., description="Client callback URL"),
    code_challenge: str = Query(..., description="PKCE S256 code challenge"),
    state: str = Query(..., description="Client-generated state parameter"),
    response_type: str = Query("code"),
    provider: str = Query("", description="Provider name (default if empty)"),
    code_challenge_method: str = Query("S256"),
) -> RedirectResponse:
    """Start the auth flow.

    Stores auth state in Redis, then redirects (302) to the provider's login.
    """
    if response_type != "code":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only response_type=code is supported",
        )

    if code_challenge_method != "S256":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only S256 code_challenge_method is supported",
        )

    # Resolve provider
    if provider:
        connector = get_connector(provider)
    else:
        connector = get_default_connector()

    if connector is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Provider not found: {provider or '(default)'}",
        )

    # Generate IDP-facing state
    idp_state = generate_state()

    # Build callback URL for the IDP
    callback_url = f"{settings.auth.callback_base_url}{settings.api_prefix}/auth/callback"

    # Build authorization request to the provider
    auth_request = await connector.build_authorization_request(
        callback_url=callback_url,
        state=idp_state,
    )

    # Store auth state in Redis
    auth_state = AuthState(
        provider_name=connector.name,
        client_redirect_uri=redirect_uri,
        client_state=state,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        idp_state=idp_state,
        nonce=auth_request.nonce,
    )
    await store_auth_state(auth_state)

    logger.info(
        "Authorize: redirecting to provider",
        provider=connector.name,
        client_redirect_uri=redirect_uri,
    )

    return RedirectResponse(url=auth_request.authorize_url, status_code=302)


@router.post("/local/authorize", response_model=LocalAuthorizeResponse)
async def local_authorize(
    body: LocalAuthorizeRequest,
    db: AsyncSession = Depends(get_db),
) -> LocalAuthorizeResponse:
    """Authenticate with local credentials + PKCE in a single JSON call.

    Used by the Web UI to avoid the redirect dance.
    """
    if body.code_challenge_method != "S256":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only S256 code_challenge_method is supported",
        )

    connector = get_connector("local")
    if connector is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Local authentication is not enabled",
        )

    try:
        identity = await connector.handle_callback(
            callback_url="",
            db=db,
            email=body.email,
            password=body.password,
        )
    except ValueError as e:
        logger.info("Local login failed", email=body.email, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        ) from e

    claims_rules = _get_claims_rules("local")
    login = await process_login(db, identity, claims_rules)

    _enforce_external_sso_requirement("local", login.roles)

    code = generate_code()
    auth_code = AuthCode(
        email=login.email,
        roles=login.roles,
        provider_name="local",
        code_challenge=body.code_challenge,
        code_challenge_method=body.code_challenge_method,
        display_name=login.display_name,
    )
    await store_auth_code(code, auth_code)

    logger.info("Local authorize: issued code", email=login.email)
    return LocalAuthorizeResponse(code=code, state=body.state)


@router.post("/local/login")
async def local_login_submit(
    state: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Handle local login form submission."""
    auth_state = await consume_auth_state(state)
    if auth_state is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired auth state",
        )

    if auth_state.provider_name != "local":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Auth state is not for local provider",
        )

    connector = get_connector("local")
    if connector is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Local provider not configured",
        )

    try:
        identity = await connector.handle_callback(
            callback_url="",
            db=db,
            email=email,
            password=password,
        )
    except ValueError as e:
        logger.info("Local login failed", email=email, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
        ) from e

    claims_rules = _get_claims_rules(auth_state.provider_name)
    login = await process_login(db, identity, claims_rules)

    _enforce_external_sso_requirement("local", login.roles)

    code = generate_code()
    auth_code = AuthCode(
        email=login.email,
        roles=login.roles,
        provider_name="local",
        code_challenge=auth_state.code_challenge,
        code_challenge_method=auth_state.code_challenge_method,
        display_name=login.display_name,
    )
    await store_auth_code(code, auth_code)

    separator = "&" if "?" in auth_state.client_redirect_uri else "?"
    redirect_url = (
        f"{auth_state.client_redirect_uri}{separator}"
        f"code={code}&state={auth_state.client_state}"
    )

    logger.info("Local login: redirecting to client", email=login.email)
    return RedirectResponse(url=redirect_url, status_code=302)


@router.get("/callback")
async def callback(
    code: str = Query(..., description="Authorization code from IDP"),
    state: str = Query(..., description="State parameter from IDP"),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Handle the OIDC IDP callback."""
    auth_state = await consume_auth_state(state)
    if auth_state is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired auth state",
        )

    connector = get_connector(auth_state.provider_name)
    if connector is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Provider {auth_state.provider_name} no longer configured",
        )

    callback_url = f"{settings.auth.callback_base_url}{settings.api_prefix}/auth/callback"

    try:
        identity = await connector.handle_callback(
            callback_url=callback_url,
            code=code,
            state=state,
        )
    except ValueError as e:
        logger.error("SSO callback failed", provider=auth_state.provider_name, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Authentication failed: {e}",
        ) from e

    claims_rules = _get_claims_rules(auth_state.provider_name)
    login = await process_login(db, identity, claims_rules)

    _enforce_external_sso_requirement(auth_state.provider_name, login.roles)

    max_session_ttl = _compute_max_session_ttl(identity.id_token_expires_at)

    terrapod_code = generate_code()
    auth_code = AuthCode(
        email=login.email,
        roles=login.roles,
        provider_name=auth_state.provider_name,
        code_challenge=auth_state.code_challenge,
        code_challenge_method=auth_state.code_challenge_method,
        display_name=login.display_name,
        max_session_ttl=max_session_ttl,
        credential_type=auth_state.credential_type,
    )
    await store_auth_code(terrapod_code, auth_code)

    separator = "&" if "?" in auth_state.client_redirect_uri else "?"
    redirect_url = (
        f"{auth_state.client_redirect_uri}{separator}"
        f"code={terrapod_code}&state={auth_state.client_state}"
    )

    logger.info(
        "Callback: redirecting to client",
        provider=auth_state.provider_name,
        email=login.email,
    )

    return RedirectResponse(url=redirect_url, status_code=302)


@router.post("/saml/acs")
async def saml_acs(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """SAML Assertion Consumer Service endpoint."""
    form = await request.form()
    saml_response = form.get("SAMLResponse")
    relay_state = form.get("RelayState", "")

    if not saml_response:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing SAMLResponse",
        )

    auth_state = await consume_auth_state(str(relay_state))
    if auth_state is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired auth state",
        )

    connector = get_connector(auth_state.provider_name)
    if connector is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Provider {auth_state.provider_name} no longer configured",
        )

    acs_url = f"{settings.auth.callback_base_url}{settings.api_prefix}/auth/saml/acs"

    try:
        identity = await connector.handle_callback(
            callback_url=acs_url,
            saml_response=str(saml_response),
            relay_state=str(relay_state),
        )
    except ValueError as e:
        logger.error("SAML callback failed", provider=auth_state.provider_name, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"SAML authentication failed: {e}",
        ) from e

    claims_rules = _get_claims_rules(auth_state.provider_name)
    login = await process_login(db, identity, claims_rules)

    _enforce_external_sso_requirement(auth_state.provider_name, login.roles)

    terrapod_code = generate_code()
    auth_code = AuthCode(
        email=login.email,
        roles=login.roles,
        provider_name=auth_state.provider_name,
        code_challenge=auth_state.code_challenge,
        code_challenge_method=auth_state.code_challenge_method,
        display_name=login.display_name,
        credential_type=auth_state.credential_type,
    )
    await store_auth_code(terrapod_code, auth_code)

    separator = "&" if "?" in auth_state.client_redirect_uri else "?"
    redirect_url = (
        f"{auth_state.client_redirect_uri}{separator}"
        f"code={terrapod_code}&state={auth_state.client_state}"
    )

    return RedirectResponse(url=redirect_url, status_code=302)


@router.post("/token", response_model=TokenExchangeResponse)
async def exchange_token(
    grant_type: str = Form(...),
    code: str = Form(...),
    code_verifier: str = Form(...),
) -> JSONResponse:
    """Exchange a code + PKCE verifier for a session.

    This is the final step of the web UI auth flow.
    """
    if grant_type != "authorization_code":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only grant_type=authorization_code is supported",
        )

    auth_code = await consume_auth_code(code)
    if auth_code is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authorization code",
        )

    if not _verify_pkce(code_verifier, auth_code.code_challenge, auth_code.code_challenge_method):
        logger.warning("PKCE verification failed", email=auth_code.email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="PKCE verification failed",
        )

    session = await create_session(
        email=auth_code.email,
        display_name=auth_code.display_name,
        roles=auth_code.roles,
        provider_name=auth_code.provider_name,
        max_ttl=auth_code.max_session_ttl,
    )

    logger.info(
        "Session created via auth flow",
        email=auth_code.email,
        provider=auth_code.provider_name,
    )

    body = TokenExchangeResponse(
        session_token=session.token,
        expires_at=session.expires_at,
        email=auth_code.email,
        roles=auth_code.roles,
    )
    return JSONResponse(content=body.model_dump(mode="json"))


# --- Session management endpoints ---


@router.get("/sessions", response_model=list[SessionInfo])
async def list_sessions_endpoint(
    request: Request,
) -> list[SessionInfo]:
    """List active sessions for the current user."""
    session = await _require_session(request)
    sessions = await list_user_sessions(session.email)
    return [
        SessionInfo(
            email=s.email,
            roles=s.roles,
            provider_name=s.provider_name,
            created_at=s.created_at,
            expires_at=s.expires_at,
            last_active_at=s.last_active_at,
            token_hint=s.token[-8:],
            is_current=(s.token == session.token),
        )
        for s in sessions
    ]


@router.get("/sessions/all", response_model=list[SessionInfo])
async def list_all_sessions_endpoint(
    request: Request,
) -> list[SessionInfo]:
    """List all active sessions across the platform (admin only)."""
    session = await _require_session(request)
    if "admin" not in session.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    sessions = await list_all_sessions()
    return [
        SessionInfo(
            email=s.email,
            roles=s.roles,
            provider_name=s.provider_name,
            created_at=s.created_at,
            expires_at=s.expires_at,
            last_active_at=s.last_active_at,
            token_hint=s.token[-8:],
            is_current=(s.token == session.token),
        )
        for s in sessions
    ]


@router.delete("/sessions/user/{email}")
async def revoke_user_sessions(
    email: str,
    request: Request,
) -> dict[str, int]:
    """Revoke all sessions for a specific user (admin only)."""
    session = await _require_session(request)
    if "admin" not in session.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    count = await revoke_all_user_sessions(email)
    logger.info("Admin revoked user sessions", admin=session.email, target=email, count=count)
    return {"revoked": count}


@router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """Revoke the current session."""
    session = await _require_session(request)
    await revoke_session(session.token)
    logger.info("Session revoked via logout", email=session.email)
    return JSONResponse(content={"status": "logged_out"})


@router.post("/logout/all")
async def logout_all(request: Request) -> JSONResponse:
    """Revoke all sessions for the current user."""
    session = await _require_session(request)
    count = await revoke_all_user_sessions(session.email)
    logger.info("All sessions revoked", email=session.email, count=count)
    return JSONResponse(content={"revoked": count})


# --- Helpers ---


async def _require_session(request: Request):  # type: ignore[no-untyped-def]
    """Extract and validate the session token from the Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = auth_header[7:]
    session = await get_session(token)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return session


def _verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    """Verify PKCE code_verifier against stored code_challenge."""
    if method != "S256":
        return False

    # S256: BASE64URL(SHA256(code_verifier)) == code_challenge
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return computed_challenge == code_challenge


def _enforce_external_sso_requirement(provider_name: str, roles: list[str]) -> None:
    """Enforce require_external_sso_for_roles policy."""
    restricted_roles = settings.auth.require_external_sso_for_roles
    if not restricted_roles or provider_name != "local":
        return

    violations = [r for r in roles if r in restricted_roles]
    if violations:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Roles {violations} require external SSO login. "
                "Local authentication is not permitted for these roles."
            ),
        )


def _compute_max_session_ttl(id_token_expires_at: datetime | None) -> int | None:
    """Compute max session TTL from id_token expiry."""
    if id_token_expires_at is None:
        return None

    from terrapod.db.models import utc_now

    remaining = (id_token_expires_at - utc_now()).total_seconds()
    configured = settings.auth.session_ttl_hours * 3600

    if 0 < remaining < configured:
        return int(remaining)
    return None


def _get_claims_rules(provider_name: str) -> list:
    """Get claims_to_roles rules for a provider from config."""
    for oidc in settings.auth.sso.oidc:
        if oidc.name == provider_name:
            return oidc.claims_to_roles
    for saml in settings.auth.sso.saml:
        if saml.name == provider_name:
            return saml.claims_to_roles
    return []
