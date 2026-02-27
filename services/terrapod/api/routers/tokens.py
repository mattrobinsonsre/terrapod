"""TFE V2 API token CRUD endpoints.

Implements the authentication-tokens endpoints in JSON:API format
compatible with the TFE V2 API.

Endpoints:
    POST   /api/v2/users/:user_id/authentication-tokens — create user token
    GET    /api/v2/users/:user_id/authentication-tokens — list user tokens
    GET    /api/v2/authentication-tokens/:id — show token (value is null)
    DELETE /api/v2/authentication-tokens/:id — revoke token
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.auth.api_tokens import (
    create_api_token,
    get_token_by_id,
    list_user_tokens,
    revoke_token,
)
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger

router = APIRouter(prefix="/api/v2", tags=["tokens"])
logger = get_logger(__name__)


class CreateTokenRequest(BaseModel):
    """JSON:API request for creating an authentication token."""

    class Data(BaseModel):
        class Attributes(BaseModel):
            description: str = ""

        type: str = "authentication-tokens"
        attributes: Attributes = Attributes()

    data: Data = Data()


def _token_to_jsonapi(token, raw_value: str | None = None) -> dict:  # type: ignore[no-untyped-def]
    """Convert an APIToken model to JSON:API format."""
    attributes: dict = {
        "description": token.description,
        "token-type": token.token_type,
        "created-at": token.created_at.isoformat() if token.created_at else None,
        "last-used-at": token.last_used_at.isoformat() if token.last_used_at else None,
        # The raw token value is only included at creation time
        "token": raw_value,
    }
    return {
        "id": token.id,
        "type": "authentication-tokens",
        "attributes": attributes,
    }


@router.post("/users/{user_id}/authentication-tokens")
async def create_user_token(
    user_id: str,
    body: CreateTokenRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create an authentication token for a user.

    The user_id in the path must match the authenticated user (or admin).
    """
    # user_id is the username (email prefix) — match against current user
    username = user.email.split("@")[0] if user.email else ""
    if user_id != username and "admin" not in user.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot create tokens for other users",
        )

    api_token, raw_token = await create_api_token(
        db=db,
        user_email=user.email,
        description=body.data.attributes.description,
        token_type="user",
    )

    return JSONResponse(
        status_code=status.HTTP_201_CREATED,
        content={"data": _token_to_jsonapi(api_token, raw_value=raw_token)},
    )


@router.get("/users/{user_id}/authentication-tokens")
async def list_user_tokens_endpoint(
    user_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List authentication tokens for a user."""
    username = user.email.split("@")[0] if user.email else ""
    if user_id != username and "admin" not in user.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot list tokens for other users",
        )

    tokens = await list_user_tokens(db, user.email)
    return JSONResponse(
        content={"data": [_token_to_jsonapi(t) for t in tokens]},
    )


@router.get("/authentication-tokens/{token_id}")
async def show_token(
    token_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show an authentication token (value is null — only available at creation)."""
    api_token = await get_token_by_id(db, token_id)
    if api_token is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Token not found",
        )

    # Only the token owner or admin can view
    if api_token.user_email != user.email and "admin" not in user.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    return JSONResponse(
        content={"data": _token_to_jsonapi(api_token)},
    )


@router.delete("/authentication-tokens/{token_id}")
async def delete_token(
    token_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Revoke (delete) an authentication token."""
    api_token = await get_token_by_id(db, token_id)
    if api_token is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Token not found",
        )

    # Only the token owner or admin can revoke
    if api_token.user_email != user.email and "admin" not in user.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
        )

    await revoke_token(db, token_id)

    return JSONResponse(
        status_code=status.HTTP_204_NO_CONTENT,
        content=None,
    )
