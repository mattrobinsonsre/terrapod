"""OAuth2 endpoints for terraform/tofu CLI login flow.

Implements the terraform service discovery and OAuth2 Authorization Code + PKCE
flow that the terraform CLI uses for `terraform login`.

Endpoints:
    GET  /.well-known/terraform.json — service discovery
    GET  /oauth/authorize — start auth flow (terraform CLI sends user here)
    POST /oauth/token — exchange auth code for API token
"""

import hashlib
import hmac
import os

from fastapi import APIRouter, Depends, Form, HTTPException, Query, status
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.auth.api_tokens import create_api_token
from terrapod.auth.auth_state import (
    AuthState,
    consume_auth_code,
    generate_state,
    store_auth_state,
)
from terrapod.auth.pkce import s256_challenge
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.redis.client import get_redis_client

router = APIRouter(tags=["oauth"])


def _terrapod_version() -> str:
    """The running Terrapod version, injected by the release pipeline via the
    ``TERRAPOD_VERSION`` env var (``"dev"`` for local/untagged builds). Read at
    request time so tests and redeploys pick up the current value."""
    return os.environ.get("TERRAPOD_VERSION", "dev")


# Terrapod-only CLI login completion check. Terrapod-native: mounted
# only under /api/terrapod/v1 (the /api/v2 alias was removed in
# v0.24.0 — see #278).
extensions_router = APIRouter(tags=["oauth-extensions"])
logger = get_logger(__name__)


@router.get("/.well-known/terraform.json")
async def terraform_service_discovery() -> JSONResponse:
    """Terraform/OpenTofu service discovery endpoint.

    Returns the service discovery document that tells the CLI where to find
    the authorization, token, and API endpoints.
    """
    return JSONResponse(
        content={
            "login.v1": {
                "client": "terraform-cli",
                "grant_types": ["authz_code"],
                "authz": "/oauth/authorize",
                "token": "/oauth/token",
                "ports": [10000, 10010],
            },
            "modules.v1": "/api/v2/registry/modules/",
            "providers.v1": "/api/v2/registry/providers/",
            "tfe.v2": "/api/v2/",
            "tfe.v2.1": "/api/v2/",
            "tfe.v2.2": "/api/v2/",
            # The running Terrapod version, so the go-terrapod SDK / provider can
            # run a compatibility check (Client.VersionCheck) at startup. Not
            # consumed by terraform/tofu/tfci — they ignore unknown keys.
            "terrapod-version": _terrapod_version(),
        }
    )


@router.get("/oauth/authorize")
async def oauth_authorize(
    response_type: str = Query("code"),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    state: str = Query(""),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query("S256"),
) -> RedirectResponse:
    """Start the OAuth2 authorization flow for terraform CLI.

    The terraform CLI sends the user's browser here with PKCE params.
    We store auth state in Redis and redirect to the SSO provider
    (or local login form). The callback is shared with the web UI flow.
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

    # Generate IDP-facing state
    idp_state = generate_state()

    # Store auth state without a provider — user will choose on the login page
    auth_state = AuthState(
        provider_name="pending",
        client_redirect_uri=redirect_uri,
        client_state=state,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        idp_state=idp_state,
        credential_type="api_token",
    )
    await store_auth_state(auth_state)

    logger.info(
        "OAuth authorize: redirecting to login page for provider selection",
        redirect_uri=redirect_uri,
    )

    return RedirectResponse(url=f"/login?cli_state={idp_state}", status_code=302)


def _hash_client_secret(secret: str) -> str:
    """SHA-256, matching how API tokens are hashed at rest."""
    return hashlib.sha256(secret.encode()).hexdigest()


async def _client_credentials_grant(
    db: AsyncSession, client_id: str, client_secret: str
) -> JSONResponse:
    """RFC 6749 client-credentials grant, for the HA peer link (#1108).

    The two nodes authenticate with a standard grant rather than a bespoke
    handshake, so a reviewer can read the RFC and know what it guarantees.

    **The expected credential is config, not a database row.** Both halves are
    already declared in the chart, and persisting a hash of what config states
    would make the database a second copy of the same fact — one that can then
    disagree with it. The CA is persisted because the node *generates* the
    keypair and it must survive a restart; nothing is generated here, so there
    is nothing to preserve. Dropping the row also drops a startup reconcile, its
    advisory lock, and the whole class of "rotating by editing the chart
    silently does nothing" (#1171).

    The issued token carries `kind="peer"`, its OWN identity class rather than a
    reuse of the runner-token path. A peer is entitled to see resolved sensitive
    variables, and granting that must not widen what a runner can reach, nor
    leave an audit unable to tell the two apart.
    """
    if not client_id or not client_secret:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="grant_type=client_credentials requires client_id and client_secret",
        )

    # One message and one code for every failure mode — unknown client id,
    # peering not configured, wrong secret — so the response cannot be used to
    # enumerate anything.
    invalid = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid client credentials",
    )

    expected = settings.ha.peer.inbound
    # Both must be non-empty. Without this an unpaired node — where both are ""
    # — would accept an empty client_id and secret, which compare_digest would
    # happily call a match.
    if not expected.client_id or not expected.client_secret:
        _hash_client_secret(client_secret)
        raise invalid

    # Both compared in constant time, and both always compared, so a wrong
    # client id is not measurably faster to reject than a wrong secret.
    id_ok = hmac.compare_digest(expected.client_id, client_id)
    secret_ok = hmac.compare_digest(
        _hash_client_secret(expected.client_secret), _hash_client_secret(client_secret)
    )
    if not (id_ok and secret_ok):
        raise invalid

    api_token, raw_token = await create_api_token(
        db=db,
        bound_to=None,
        created_by=f"oauth-client:{client_id}",
        kind="peer",
        description=f"client_credentials grant for {expected.name or client_id}",
        lifespan_hours=settings.auth.peer_token_ttl_hours,
    )
    await db.commit()

    logger.info(
        "Issued peer token via client_credentials",
        client_id=client_id,
        token_id=str(api_token.id),
    )
    return JSONResponse(
        content={
            "access_token": raw_token,
            "token_type": "bearer",
            "expires_in": settings.auth.peer_token_ttl_hours * 3600,
        }
    )


@router.post("/oauth/token")
async def oauth_token(
    grant_type: str = Form(...),
    # Optional because two grants share this endpoint now. The
    # authorization_code path still requires both and says so explicitly
    # below, so an existing caller that omits them gets the same 4xx it
    # always did — just from a check rather than from FastAPI's validator.
    code: str = Form(""),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    redirect_uri: str = Form(""),
    code_verifier: str = Form(""),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Exchange authorization code for API token (terraform CLI flow).

    The terraform CLI calls this after the browser redirect completes.
    Validates PKCE, creates a long-lived API token in PostgreSQL, and
    returns it. No refresh_token, no expires_in — terraform stores it
    permanently in .terraformrc.
    """
    if grant_type == "client_credentials":
        return await _client_credentials_grant(db, client_id, client_secret)

    if grant_type != "authorization_code":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported grant_type: expected authorization_code or client_credentials",
        )

    if not code or not code_verifier:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="grant_type=authorization_code requires code and code_verifier",
        )

    # Consume the one-time auth code
    auth_code = await consume_auth_code(code)
    if auth_code is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authorization code",
        )

    # Verify PKCE
    if not _verify_pkce(code_verifier, auth_code.code_challenge, auth_code.code_challenge_method):
        logger.warning("PKCE verification failed for terraform login", email=auth_code.email)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="PKCE verification failed",
        )

    # Create a short-lived interactive API token (terraform login). Bound to
    # the authenticating identity; created_by is the same identity. The
    # lifespan comes from auth.login_token_ttl_hours (default 12h) — these are
    # per-session CLI credentials, not max-lifetime tokens. 0 falls back to the
    # api_token_max_ttl_hours cap. create_api_token clamps to that cap either way.
    api_token, raw_token = await create_api_token(
        db=db,
        bound_to=auth_code.email,
        created_by=auth_code.email,
        kind="interactive",
        description=f"terraform login ({auth_code.provider_name})",
        lifespan_hours=settings.auth.login_token_ttl_hours or None,
    )
    await db.commit()

    # Set completion flag so the CLI success page can confirm the round-trip
    redis = get_redis_client()
    await redis.set(f"tp:cli_complete:{code}", "1", ex=300)

    logger.info(
        "API token created via terraform login",
        email=auth_code.email,
        token_id=api_token.id,
    )

    # Return in OAuth2 format that terraform CLI expects
    return JSONResponse(
        content={
            "access_token": raw_token,
            "token_type": "bearer",
        }
    )


@extensions_router.get("/auth/cli-login-status")
async def cli_login_status(code: str = Query(...)) -> JSONResponse:
    """Check if a CLI login flow completed (token was created)."""
    redis = get_redis_client()
    result = await redis.get(f"tp:cli_complete:{code}")
    return JSONResponse(content={"complete": result is not None})


def _verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    """Verify PKCE code_verifier against stored code_challenge."""
    if method != "S256":
        return False

    computed_challenge = s256_challenge(code_verifier)
    # Timing-safe — the PKCE challenge is attacker-influenced in the token
    # exchange; match every other secret comparison in the codebase.
    return hmac.compare_digest(computed_challenge, code_challenge)
