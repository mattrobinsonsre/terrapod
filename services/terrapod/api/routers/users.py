"""User management endpoints (admin or audit role required).

UX CONTRACT: User management endpoints are consumed by the web frontend:
  - web/src/app/admin/users/page.tsx (user list, create, edit, deactivate, password reset)
  Changes to response shapes, attribute names, or status codes here MUST be
  matched by corresponding updates to that frontend page.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, require_admin, require_admin_or_audit
from terrapod.auth.passwords import hash_password, validate_password_strength
from terrapod.db.models import (
    PlatformRoleAssignment,
    RoleAssignment,
    User,
)
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v2", tags=["users"])


def _format_timestamp(dt) -> str | None:  # type: ignore[no-untyped-def]
    """Format datetime as RFC3339 with Z suffix."""
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _user_to_jsonapi(user: User) -> dict:
    """Serialize a User to JSON:API format."""
    return {
        "id": user.email,
        "type": "users",
        "attributes": {
            "email": user.email,
            "display-name": user.display_name,
            "is-active": user.is_active,
            "has-password": user.password_hash is not None,
            "last-login-at": _format_timestamp(user.last_login_at),
            "created-at": _format_timestamp(user.created_at),
            "updated-at": _format_timestamp(user.updated_at),
        },
    }


class UserCreateAttributes(BaseModel):
    """Attributes for creating a user."""

    model_config = ConfigDict(populate_by_name=True)

    email: str
    password: str | None = None
    display_name: str | None = Field(None, alias="display-name")


class UserCreateData(BaseModel):
    type: str = "users"
    attributes: UserCreateAttributes


class UserCreateRequest(BaseModel):
    data: UserCreateData


@router.post("/organizations/default/users", status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreateRequest,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Create a new local user (admin only)."""
    attrs = body.data.attributes
    email = attrs.email.strip().lower()

    if not email or "@" not in email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A valid email address is required",
        )

    # Check duplicate
    existing = await db.execute(select(User).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User with email '{email}' already exists",
        )

    # Validate and hash password if provided
    pw_hash = None
    if attrs.password:
        try:
            validate_password_strength(attrs.password, [email])
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(e),
            ) from None
        pw_hash = hash_password(attrs.password)

    new_user = User(
        email=email,
        display_name=attrs.display_name,
        password_hash=pw_hash,
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    logger.info("Created user", target_email=email, by=user.email)
    return {"data": _user_to_jsonapi(new_user)}


@router.get("/organizations/default/users")
async def list_users(
    user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
    filter_email: str | None = Query(None, alias="filter[email]"),
    page_number: int = Query(1, alias="page[number]", ge=1),
    page_size: int = Query(20, alias="page[size]", ge=1, le=100),
) -> dict:
    """List users."""
    stmt = select(User)
    count_stmt = select(func.count()).select_from(User)

    if filter_email:
        stmt = stmt.where(User.email.ilike(f"%{filter_email}%"))
        count_stmt = count_stmt.where(User.email.ilike(f"%{filter_email}%"))

    # Count
    total = (await db.execute(count_stmt)).scalar() or 0

    # Page
    offset = (page_number - 1) * page_size
    stmt = stmt.order_by(User.created_at.desc()).offset(offset).limit(page_size)

    result = await db.execute(stmt)
    users = list(result.scalars().all())

    return {
        "data": [_user_to_jsonapi(u) for u in users],
        "meta": {
            "pagination": {
                "current-page": page_number,
                "page-size": page_size,
                "total-count": total,
                "total-pages": (total + page_size - 1) // page_size if total > 0 else 0,
            }
        },
    }


@router.get("/users/{email}")
async def show_user(
    email: str,
    user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Show a single user."""
    result = await db.execute(select(User).where(User.email == email))
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return {"data": _user_to_jsonapi(target)}


class UserUpdateAttributes(BaseModel):
    """Updatable user attributes."""

    model_config = ConfigDict(populate_by_name=True)

    is_active: bool | None = Field(None, alias="is-active")
    display_name: str | None = Field(None, alias="display-name")
    password: str | None = None


class UserUpdateData(BaseModel):
    type: str = "users"
    attributes: UserUpdateAttributes


class UserUpdateRequest(BaseModel):
    data: UserUpdateData


@router.patch("/users/{email}")
async def update_user(
    email: str,
    body: UserUpdateRequest,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update a user (admin only). Deactivating revokes all sessions."""
    result = await db.execute(select(User).where(User.email == email))
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    attrs = body.data.attributes
    if attrs.display_name is not None:
        target.display_name = attrs.display_name

    if attrs.password is not None:
        try:
            validate_password_strength(attrs.password, [email])
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(e),
            ) from None
        target.password_hash = hash_password(attrs.password)
        logger.info("Reset password for user", target_email=email, by=user.email)

    if attrs.is_active is not None:
        target.is_active = attrs.is_active
        if not attrs.is_active:
            # Revoke all sessions for deactivated user
            from terrapod.auth.sessions import revoke_all_user_sessions

            await revoke_all_user_sessions(email)
            logger.info("Deactivated user and revoked sessions", target_email=email, by=user.email)

    await db.commit()
    await db.refresh(target)
    return {"data": _user_to_jsonapi(target)}


@router.delete("/users/{email}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    email: str,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a user (admin only). Cascades: revokes sessions, deletes role assignments."""
    result = await db.execute(select(User).where(User.email == email))
    target = result.scalar_one_or_none()
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Revoke sessions
    from terrapod.auth.sessions import revoke_all_user_sessions

    await revoke_all_user_sessions(email)

    # Delete role assignments
    await db.execute(delete(RoleAssignment).where(RoleAssignment.email == email))
    await db.execute(delete(PlatformRoleAssignment).where(PlatformRoleAssignment.email == email))

    # Delete user
    await db.delete(target)
    await db.commit()
    logger.info("Deleted user", target_email=email, by=user.email)
