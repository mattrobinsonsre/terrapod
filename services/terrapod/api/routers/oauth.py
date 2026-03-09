"""OAuth2 endpoints for terraform/tofu CLI login flow.

Implements the terraform service discovery and OAuth2 Authorization Code + PKCE
flow that the terraform CLI uses for `terraform login`.

Endpoints:
    GET  /.well-known/terraform.json — service discovery
    GET  /oauth/authorize — start auth flow (terraform CLI sends user here)
    POST /oauth/token — exchange auth code for API token
"""

import base64
import hashlib

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
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.redis.client import get_redis_client

router = APIRouter(tags=["oauth"])
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


@router.post("/oauth/token")
async def oauth_token(
    grant_type: str = Form(...),
    code: str = Form(...),
    client_id: str = Form(""),
    redirect_uri: str = Form(""),
    code_verifier: str = Form(...),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Exchange authorization code for API token (terraform CLI flow).

    The terraform CLI calls this after the browser redirect completes.
    Validates PKCE, creates a long-lived API token in PostgreSQL, and
    returns it. No refresh_token, no expires_in — terraform stores it
    permanently in .terraformrc.
    """
    if grant_type != "authorization_code":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only grant_type=authorization_code is supported",
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

    # Create a long-lived API token (no expiry by default)
    api_token, raw_token = await create_api_token(
        db=db,
        user_email=auth_code.email,
        description=f"terraform login ({auth_code.provider_name})",
        token_type="user",
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


@router.get("/api/v2/auth/cli-login-status")
async def cli_login_status(code: str = Query(...)) -> JSONResponse:
    """Check if a CLI login flow completed (token was created)."""
    redis = get_redis_client()
    result = await redis.get(f"tp:cli_complete:{code}")
    return JSONResponse(content={"complete": result is not None})


def _verify_pkce(code_verifier: str, code_challenge: str, method: str) -> bool:
    """Verify PKCE code_verifier against stored code_challenge."""
    if method != "S256":
        return False

    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    computed_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return computed_challenge == code_challenge
