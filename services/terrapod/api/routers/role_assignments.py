"""Role assignment management endpoints (admin only).

UX CONTRACT: Role assignment endpoints are consumed by the web frontend:
  - web/src/app/admin/roles/page.tsx (assignments tab)
  Changes to response shapes, attribute names, or status codes here MUST be
  matched by corresponding updates to that frontend page.

Endpoints:
    GET    /api/v2/role-assignments                       — list all assignments
    GET    /api/v2/role-assignments/identities             — list known identities (for autocomplete)
    PUT    /api/v2/role-assignments                       — set roles for (provider, email)
    DELETE /api/v2/role-assignments/{provider}/{email}/{role} — remove single assignment
"""

from datetime import UTC

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, require_admin, require_admin_or_audit
from terrapod.auth.builtin_roles import is_builtin_role, is_platform_role
from terrapod.auth.recent_users import list_recent_users
from terrapod.db.models import PlatformRoleAssignment, Role, RoleAssignment, User
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger

router = APIRouter(prefix="/api/v2", tags=["role-assignments"])
logger = get_logger(__name__)


def _rfc3339(dt) -> str:
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        select(RoleAssignment).order_by(RoleAssignment.email, RoleAssignment.role_name)
    )
    for ra in result.scalars().all():
        data.append(_assignment_json(ra.provider_name, ra.email, ra.role_name, ra.created_at))

    return JSONResponse(content={"data": data})


@router.get("/role-assignments/identities")
async def list_identities(
    user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List known identities for role assignment autocomplete.

    Merges three sources:
    1. Local users from the users table (provider_name="local")
    2. Recent SSO logins from Redis (any provider)
    3. (provider, email) pairs with existing DB assignments not in 1 or 2

    Each identity includes its current role list. Deduplicated by (provider, email).
    """
    seen: dict[tuple[str, str], dict] = {}

    # 1. Local users
    result = await db.execute(select(User).where(User.is_active.is_(True)).order_by(User.email))
    for u in result.scalars().all():
        key = ("local", u.email)
        seen[key] = {
            "provider-name": "local",
            "email": u.email,
            "display-name": u.display_name,
            "roles": [],
        }

    # 2. Recent SSO logins from Redis
    try:
        recent = await list_recent_users()
    except Exception:
        logger.debug("Failed to load recent users from Redis", exc_info=True)
        recent = []

    for ru in recent:
        key = (ru.provider_name, ru.email)
        if key not in seen:
            seen[key] = {
                "provider-name": ru.provider_name,
                "email": ru.email,
                "display-name": ru.display_name,
                "roles": [],
            }

    # 3. Existing assignments not yet seen (pre-provisioned or stale)
    all_assignments: list[tuple[str, str, str]] = []

    pra_result = await db.execute(select(PlatformRoleAssignment))
    for pra in pra_result.scalars().all():
        all_assignments.append((pra.provider_name, pra.email, pra.role_name))

    ra_result = await db.execute(select(RoleAssignment))
    for ra in ra_result.scalars().all():
        all_assignments.append((ra.provider_name, ra.email, ra.role_name))

    for provider, email, role_name in all_assignments:
        key = (provider, email)
        if key not in seen:
            seen[key] = {
                "provider-name": provider,
                "email": email,
                "display-name": None,
                "roles": [],
            }
        seen[key]["roles"].append(role_name)

    # Sort: local first, then by email
    identities = sorted(
        seen.values(),
        key=lambda i: (0 if i["provider-name"] == "local" else 1, i["email"]),
    )

    return JSONResponse(content={"data": identities})


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
            db.add(
                PlatformRoleAssignment(
                    provider_name=provider_name,
                    email=email,
                    role_name=rn,
                )
            )
        else:
            db.add(
                RoleAssignment(
                    provider_name=provider_name,
                    email=email,
                    role_name=rn,
                )
            )

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
