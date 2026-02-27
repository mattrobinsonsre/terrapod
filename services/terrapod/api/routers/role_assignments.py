"""Role assignment management endpoints (admin only).

Endpoints:
    GET    /api/v2/role-assignments                       — list all assignments
    PUT    /api/v2/role-assignments                       — set roles for (provider, email)
    DELETE /api/v2/role-assignments/{provider}/{email}/{role} — remove single assignment
"""

from fastapi import APIRouter, Body, Depends, HTTPException, Path, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, require_admin, require_admin_or_audit
from terrapod.auth.builtin_roles import is_builtin_role, is_platform_role
from terrapod.db.models import PlatformRoleAssignment, Role, RoleAssignment
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger

router = APIRouter(prefix="/api/v2", tags=["role-assignments"])
logger = get_logger(__name__)


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    from datetime import timezone
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _assignment_json(provider: str, email: str, role_name: str, created_at=None) -> dict:
    return {
        "type": "role-assignments",
        "attributes": {
            "provider-name": provider,
            "email": email,
            "role-name": role_name,
            "created-at": _rfc3339(created_at) if created_at else "",
        },
    }


@router.get("/role-assignments")
async def list_role_assignments(
    user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all role assignments (custom + platform)."""
    data = []

    # Platform role assignments (admin, audit)
    result = await db.execute(
        select(PlatformRoleAssignment).order_by(
            PlatformRoleAssignment.email, PlatformRoleAssignment.role_name
        )
    )
    for pra in result.scalars().all():
        data.append(_assignment_json(pra.provider_name, pra.email, pra.role_name, pra.created_at))

    # Custom role assignments
    result = await db.execute(
        select(RoleAssignment).order_by(
            RoleAssignment.email, RoleAssignment.role_name
        )
    )
    for ra in result.scalars().all():
        data.append(_assignment_json(ra.provider_name, ra.email, ra.role_name, ra.created_at))

    return JSONResponse(content={"data": data})


@router.put("/role-assignments")
async def set_role_assignments(
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Set roles for a (provider, email) pair.

    Replaces all existing assignments for the given provider+email with the
    provided role list. Supports both platform roles (admin, audit) and
    custom roles in a single call.
    """
    attrs = body.get("data", {}).get("attributes", {})
    provider_name = attrs.get("provider-name", "local")
    email = attrs.get("email", "")
    role_names = attrs.get("roles", [])

    if not email:
        raise HTTPException(status_code=422, detail="Email is required")
    if not isinstance(role_names, list):
        raise HTTPException(status_code=422, detail="Roles must be a list")

    # Validate custom role names exist
    for rn in role_names:
        if not is_builtin_role(rn):
            result = await db.execute(select(Role).where(Role.name == rn))
            if result.scalar_one_or_none() is None:
                raise HTTPException(status_code=422, detail=f"Role '{rn}' not found")

    # Remove existing assignments for this provider+email
    existing_platform = await db.execute(
        select(PlatformRoleAssignment).where(
            PlatformRoleAssignment.provider_name == provider_name,
            PlatformRoleAssignment.email == email,
        )
    )
    for pra in existing_platform.scalars().all():
        await db.delete(pra)

    existing_custom = await db.execute(
        select(RoleAssignment).where(
            RoleAssignment.provider_name == provider_name,
            RoleAssignment.email == email,
        )
    )
    for ra in existing_custom.scalars().all():
        await db.delete(ra)

    # Create new assignments
    for rn in role_names:
        if rn == "everyone":
            continue  # everyone is implicit, don't store
        if is_platform_role(rn):
            db.add(PlatformRoleAssignment(
                provider_name=provider_name,
                email=email,
                role_name=rn,
            ))
        else:
            db.add(RoleAssignment(
                provider_name=provider_name,
                email=email,
                role_name=rn,
            ))

    await db.commit()

    # Invalidate cached roles for this user
    from terrapod.redis.client import get_redis_client

    redis = get_redis_client()
    await redis.delete(f"tp:token_roles:{email}")

    logger.info("Role assignments updated", provider=provider_name, email=email, roles=role_names)

    # Return the new state
    data = []
    for rn in role_names:
        if rn != "everyone":
            data.append(_assignment_json(provider_name, email, rn))

    return JSONResponse(content={"data": data})


@router.delete("/role-assignments/{provider_name}/{email}/{role_name}", status_code=204)
async def delete_role_assignment(
    provider_name: str = Path(...),
    email: str = Path(...),
    role_name: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Remove a single role assignment."""
    if is_platform_role(role_name):
        result = await db.execute(
            select(PlatformRoleAssignment).where(
                PlatformRoleAssignment.provider_name == provider_name,
                PlatformRoleAssignment.email == email,
                PlatformRoleAssignment.role_name == role_name,
            )
        )
        pra = result.scalar_one_or_none()
        if pra is None:
            raise HTTPException(status_code=404, detail="Assignment not found")
        await db.delete(pra)
    else:
        result = await db.execute(
            select(RoleAssignment).where(
                RoleAssignment.provider_name == provider_name,
                RoleAssignment.email == email,
                RoleAssignment.role_name == role_name,
            )
        )
        ra = result.scalar_one_or_none()
        if ra is None:
            raise HTTPException(status_code=404, detail="Assignment not found")
        await db.delete(ra)

    await db.commit()

    # Invalidate cached roles
    from terrapod.redis.client import get_redis_client

    redis = get_redis_client()
    await redis.delete(f"tp:token_roles:{email}")

    logger.info("Role assignment deleted", provider=provider_name, email=email, role=role_name)
