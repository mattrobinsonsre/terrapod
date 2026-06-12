"""Terrapod-native API token CRUD + service-token management (#495).

JSON:API-shaped token management. Terrapod-native surface — mounted ONLY
under /api/terrapod/v1/ (the CLI mints tokens via the /oauth flow, never
here). Do NOT move any of this to /api/v2/ or tfe_v2.py.

UX CONTRACT: consumed by the web frontend
  - web/src/app/settings/tokens/page.tsx
Changes to response shapes, attribute names, or status codes here MUST be
matched by corresponding updates to that page.

Endpoints:
    POST   /users/:user_id/authentication-tokens                  — create token
    GET    /users/:user_id/authentication-tokens                  — list user tokens
    GET    /admin/authentication-tokens                           — list all (admin) [?kind=]
    POST   /admin/authentication-tokens/actions/revoke-all        — revoke all for a user (admin)
    GET    /authentication-tokens/expiring                        — caller-scoped expiring service tokens
    GET    /authentication-tokens/:id                             — show token (value null)
    PATCH  /authentication-tokens/:id                             — re-tag kind (interactive<->service_bound)
    POST   /authentication-tokens/:id/actions/rotate              — rotate secret + reset expiry
    DELETE /authentication-tokens/:id                             — revoke token
"""

from datetime import UTC

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, effective_platform_roles, get_current_user
from terrapod.auth.api_tokens import (
    create_api_token,
    get_token_by_id,
    list_all_tokens,
    list_expiring_service_tokens,
    list_user_tokens,
    revoke_all_for_user,
    revoke_token,
    rotate_token,
    token_expires_at,
)
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.redis.client import get_redis_client

router = APIRouter(tags=["tokens"])
logger = get_logger(__name__)

# Kinds anyone may create / re-tag bound to themselves. service_detached is
# admin-only and handled separately (unbound, admin-pinned absolute scope).
_CREATABLE_KINDS = {"interactive", "service_bound"}
_ALL_KINDS = {"interactive", "service_bound", "service_detached"}
_TOKEN_ROLES_PREFIX = "tp:token_roles:"


def _rfc3339(dt) -> str | None:  # type: ignore[no-untyped-def]
    """RFC3339 with a trailing Z (not +00:00) — go-tfe/contract compatible."""
    if dt is None:
        return None
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


class CreateTokenRequest(BaseModel):
    """JSON:API request for creating an authentication token."""

    class Data(BaseModel):
        class Attributes(BaseModel):
            description: str = ""
            lifespan_hours: int | None = None
            kind: str = "interactive"
            pinned_roles: list[str] | None = None

        type: str = "authentication-tokens"
        attributes: Attributes = Attributes()

    data: Data = Data()


class PatchTokenRequest(BaseModel):
    """JSON:API request for re-tagging a token's kind (and pinned roles on convert)."""

    class Data(BaseModel):
        class Attributes(BaseModel):
            kind: str
            pinned_roles: list[str] | None = None

        type: str = "authentication-tokens"
        attributes: Attributes

    data: Data


class RevokeAllRequest(BaseModel):
    email: str


def _token_to_jsonapi(token, raw_value: str | None = None) -> dict:  # type: ignore[no-untyped-def]
    """Convert an APIToken model to JSON:API format."""
    attributes: dict = {
        "description": token.description,
        "kind": token.kind,
        "bound-to": token.bound_to,
        "created-by": token.created_by,
        # The token's pinned scope (service tokens). Null for interactive
        # tokens. The tokens UI surfaces this so an operator can see exactly
        # what a service token is scoped to.
        "pinned-roles": token.pinned_roles,
        # token-type retained for response back-compat (superseded by kind).
        "token-type": token.token_type,
        "created-at": _rfc3339(token.created_at),
        "rotated-at": _rfc3339(token.rotated_at),
        "last-used-at": _rfc3339(token.last_used_at),
        "expires-at": _rfc3339(token_expires_at(token)),
        "lifespan-hours": token.lifespan_hours,
        # The raw token value is only included at creation/rotation time.
        "token": raw_value,
    }
    return {
        "id": token.id,
        "type": "authentication-tokens",
        "attributes": attributes,
    }


def _is_admin(user: AuthenticatedUser) -> bool:
    return "admin" in effective_platform_roles(user)


def _username(user: AuthenticatedUser) -> str:
    return user.email.split("@")[0] if user.email else ""


@router.post("/users/{user_id}/authentication-tokens")
async def create_user_token(
    user_id: str,
    body: CreateTokenRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create an authentication token for a user.

    The user_id in the path must match the authenticated user (or admin).
    Anyone may create `interactive` or `service_bound` tokens (bound to
    themselves). `service_detached` is admin-only and unbound — the admin
    pins its absolute scope.
    """
    if user_id != _username(user) and not _is_admin(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot create tokens for other users",
        )

    kind = body.data.attributes.kind
    if kind not in _ALL_KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid token kind: {kind}",
        )

    if kind == "service_detached":
        # Detached tokens are admin-only and unbound (no owner); the admin
        # pins the absolute scope. Effective-admin is kind-attenuated.
        if "admin" not in effective_platform_roles(user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Detached service tokens are admin-only",
            )
        bound_to = None
        pinned = body.data.attributes.pinned_roles or []
    elif kind == "service_bound":
        bound_to = user.email
        pinned = body.data.attributes.pinned_roles
    else:  # interactive
        bound_to = user.email
        pinned = None

    api_token, raw_token = await create_api_token(
        db=db,
        bound_to=bound_to,
        created_by=user.email,
        kind=kind,
        description=body.data.attributes.description,
        lifespan_hours=body.data.attributes.lifespan_hours,
        pinned_roles=pinned,
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
    """List a user's own tokens (never includes detached tokens)."""
    if user_id != _username(user) and not _is_admin(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot list tokens for other users",
        )

    tokens = await list_user_tokens(db, user.email)
    return JSONResponse(content={"data": [_token_to_jsonapi(t) for t in tokens]})


@router.get("/admin/authentication-tokens")
async def list_all_tokens_endpoint(
    kind: str | None = Query(default=None),
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all authentication tokens across all users (admin only).

    Optional ``?kind=`` filter. A valid-but-unpopulated kind (e.g.
    service_detached before it exists) returns an empty set, not an error.
    """
    if not _is_admin(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    if kind is not None and kind not in _ALL_KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid token kind: {kind}",
        )

    tokens = await list_all_tokens(db, kind=kind)
    return JSONResponse(content={"data": [_token_to_jsonapi(t) for t in tokens]})


@router.post("/admin/authentication-tokens/actions/revoke-all")
async def revoke_all_tokens_for_user(
    body: RevokeAllRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Revoke every token bound to an identity — urgent offboarding (admin only)."""
    if not _is_admin(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    count = await revoke_all_for_user(db, body.email)
    await db.commit()
    # Bust the cached role resolution for the now-token-less identity.
    await get_redis_client().delete(f"{_TOKEN_ROLES_PREFIX}{body.email}")

    return JSONResponse(content={"data": {"email": body.email, "revoked": count}})


@router.get("/authentication-tokens/expiring")
async def list_expiring_tokens(
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Service tokens nearing expiry, scoped to the caller (#495).

    Own ``service_bound`` for everyone; plus all ``service_detached`` for
    admins. Drives the in-app nav-bar/login warnings.
    """
    tokens = await list_expiring_service_tokens(
        db,
        caller_email=user.email,
        is_admin=_is_admin(user),
        within_days=settings.auth.token_expiry_warning_days,
    )
    return JSONResponse(content={"data": [_token_to_jsonapi(t) for t in tokens]})


@router.get("/authentication-tokens/{token_id}")
async def show_token(
    token_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show an authentication token (value is null — only available at creation)."""
    api_token = await get_token_by_id(db, token_id)
    if api_token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")

    if api_token.bound_to != user.email and not _is_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    return JSONResponse(content={"data": _token_to_jsonapi(api_token)})


@router.patch("/authentication-tokens/{token_id}")
async def retag_token(
    token_id: str,
    body: PatchTokenRequest,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Re-tag a token's kind.

    interactive <-> service_bound is owner-or-admin. Converting to/from
    service_detached is admin-only: TO detached unbinds the token and pins
    the supplied absolute scope; FROM detached binds it to the acting admin.
    """
    api_token = await get_token_by_id(db, token_id)
    if api_token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")

    if api_token.bound_to != user.email and not _is_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    new_kind = body.data.attributes.kind
    if new_kind not in _ALL_KINDS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid token kind: {new_kind}",
        )

    if (new_kind == "service_detached" or api_token.kind == "service_detached") and (
        "admin" not in effective_platform_roles(user)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Converting to/from detached is admin-only",
        )

    affected = {api_token.bound_to}
    if new_kind == "service_detached":
        api_token.kind = "service_detached"
        api_token.bound_to = None
        api_token.pinned_roles = body.data.attributes.pinned_roles or []
    else:
        api_token.kind = new_kind
        if api_token.bound_to is None:  # coming from detached → bind to the acting admin
            api_token.bound_to = user.email
        if new_kind == "interactive":
            api_token.pinned_roles = None
        elif body.data.attributes.pinned_roles is not None:
            api_token.pinned_roles = body.data.attributes.pinned_roles
    affected.add(api_token.bound_to)

    await db.commit()
    await db.refresh(api_token)
    # Bust cached role resolution for any affected identity (#495 B3).
    redis = get_redis_client()
    for email in affected:
        if email:
            await redis.delete(f"{_TOKEN_ROLES_PREFIX}{email}")
    return JSONResponse(content={"data": _token_to_jsonapi(api_token)})


@router.post("/authentication-tokens/{token_id}/actions/rotate")
async def rotate_token_endpoint(
    token_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Rotate a token's secret + reset its expiry clock (owner or admin)."""
    api_token = await get_token_by_id(db, token_id)
    if api_token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")

    if api_token.bound_to != user.email and not _is_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    rotated = await rotate_token(db, token_id)
    assert rotated is not None  # just fetched it above
    token, raw_token = rotated
    await db.commit()

    return JSONResponse(content={"data": _token_to_jsonapi(token, raw_value=raw_token)})


@router.delete("/authentication-tokens/{token_id}")
async def delete_token(
    token_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Revoke (delete) an authentication token (owner or admin)."""
    api_token = await get_token_by_id(db, token_id)
    if api_token is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Token not found")

    if api_token.bound_to != user.email and not _is_admin(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Access denied")

    await revoke_token(db, token_id)

    return JSONResponse(status_code=status.HTTP_204_NO_CONTENT, content=None)
