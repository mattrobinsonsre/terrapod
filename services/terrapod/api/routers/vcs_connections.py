"""VCS connection CRUD endpoints.

VCS connections are platform-level resources that configure auth for a VCS
provider (GitHub App installation, GitLab access token). Workspaces reference
a connection to link to a repository.

Endpoints:
    GET    /api/v2/organizations/{org}/vcs-connections   (list connections)
    POST   /api/v2/organizations/{org}/vcs-connections   (create connection)
    GET    /api/v2/vcs-connections/{id}                  (show connection)
    DELETE /api/v2/vcs-connections/{id}                  (delete connection)
"""

import uuid
from datetime import UTC

from fastapi import APIRouter, Body, Depends, HTTPException, Path
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, require_admin
from terrapod.db.models import VCSConnection, generate_uuid7
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger

router = APIRouter(prefix="/api/v2", tags=["vcs-connections"])
logger = get_logger(__name__)

DEFAULT_ORG = "default"
SUPPORTED_PROVIDERS = {"github", "gitlab"}


def _rfc3339(dt) -> str:
    if dt is None:
        return ""

    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validate_org(org: str) -> None:
    if org != DEFAULT_ORG:
        raise HTTPException(status_code=404, detail="Organization not found")


def _connection_json(conn: VCSConnection) -> dict:
    """Serialize a VCSConnection to JSON:API format."""
    attrs: dict = {
        "name": conn.name,
        "provider": conn.provider,
        "server-url": conn.server_url,
        "status": conn.status,
        "has-token": conn.token_encrypted is not None and conn.token_encrypted != "",
        "created-at": _rfc3339(conn.created_at),
        "updated-at": _rfc3339(conn.updated_at),
    }

    # Include GitHub-specific fields when relevant
    if conn.provider == "github":
        attrs["github-app-id"] = conn.github_app_id
        attrs["github-installation-id"] = conn.github_installation_id
        attrs["github-account-login"] = conn.github_account_login
        attrs["github-account-type"] = conn.github_account_type

    return {
        "id": f"vcs-{conn.id}",
        "type": "vcs-connections",
        "attributes": attrs,
        "relationships": {
            "organization": {
                "data": {"id": conn.org_name, "type": "organizations"},
            },
        },
    }


async def _list_connections(db: AsyncSession, org_name: str) -> list[VCSConnection]:
    result = await db.execute(
        select(VCSConnection)
        .where(VCSConnection.org_name == org_name)
        .order_by(VCSConnection.created_at)
    )
    return list(result.scalars().all())


async def _get_connection(db: AsyncSession, connection_id: uuid.UUID) -> VCSConnection | None:
    result = await db.execute(select(VCSConnection).where(VCSConnection.id == connection_id))
    return result.scalar_one_or_none()


@router.get("/organizations/{org}/vcs-connections")
async def list_connections(
    org: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all VCS connections (admin only)."""
    _validate_org(org)
    connections = await _list_connections(db, org)
    return JSONResponse(content={"data": [_connection_json(c) for c in connections]})


@router.post("/organizations/{org}/vcs-connections", status_code=201)
async def create_connection(
    org: str = Path(...),
    body: dict = Body(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a VCS connection (admin only).

    For GitHub: provide github-app-id, github-installation-id, and private-key
    (the PEM-encoded GitHub App private key). Optionally server-url for GHE.
    For GitLab: provide token and optionally server-url (defaults to gitlab.com).
    """
    _validate_org(org)

    attrs = body.get("data", {}).get("attributes", {})
    name = attrs.get("name", "")
    provider = attrs.get("provider", "github")

    if not name:
        raise HTTPException(status_code=422, detail="Connection name is required")
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported provider '{provider}'. Supported: {', '.join(sorted(SUPPORTED_PROVIDERS))}",
        )

    from terrapod.services.encryption_service import encrypt_value

    # Provider-specific validation
    token_encrypted = None

    if provider == "github":
        app_id = attrs.get("github-app-id", 0)
        installation_id = attrs.get("github-installation-id", 0)
        private_key = attrs.get("private-key", "")
        if not app_id:
            raise HTTPException(
                status_code=422, detail="github-app-id is required for GitHub connections"
            )
        if not installation_id:
            raise HTTPException(
                status_code=422, detail="github-installation-id is required for GitHub connections"
            )
        if not private_key:
            raise HTTPException(
                status_code=422, detail="private-key is required for GitHub connections"
            )
        # Encrypt the private key for storage
        token_encrypted = encrypt_value(private_key)
        # Check for duplicate GitHub installation
        existing = await db.execute(
            select(VCSConnection).where(
                VCSConnection.provider == "github",
                VCSConnection.github_installation_id == installation_id,
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(
                status_code=422,
                detail=f"GitHub installation {installation_id} is already connected",
            )

    elif provider == "gitlab":
        token = attrs.get("token", "")
        if not token:
            raise HTTPException(status_code=422, detail="token is required for GitLab connections")
        # Encrypt the token for storage
        token_encrypted = encrypt_value(token)

    conn = VCSConnection(
        id=generate_uuid7(),
        org_name=org,
        provider=provider,
        name=name,
        server_url=attrs.get("server-url", ""),
        token_encrypted=token_encrypted,
        # GitHub-specific
        github_app_id=attrs.get("github-app-id", 0),
        github_installation_id=attrs.get("github-installation-id", 0),
        github_account_login=attrs.get("github-account-login", ""),
        github_account_type=attrs.get("github-account-type", ""),
        status="active",
    )
    db.add(conn)
    await db.commit()
    await db.refresh(conn)

    logger.info(
        "VCS connection created",
        connection_id=str(conn.id),
        name=name,
        provider=provider,
    )

    return JSONResponse(content={"data": _connection_json(conn)}, status_code=201)


@router.get("/vcs-connections/{connection_id}")
async def show_connection(
    connection_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Show a VCS connection (admin only)."""
    conn_uuid = uuid.UUID(connection_id.removeprefix("vcs-"))
    conn = await _get_connection(db, conn_uuid)
    if conn is None:
        raise HTTPException(status_code=404, detail="VCS connection not found")
    return JSONResponse(content={"data": _connection_json(conn)})


@router.delete("/vcs-connections/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: str = Path(...),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete a VCS connection (admin only)."""
    conn_uuid = uuid.UUID(connection_id.removeprefix("vcs-"))
    conn = await _get_connection(db, conn_uuid)
    if conn is None:
        raise HTTPException(status_code=404, detail="VCS connection not found")
    await db.delete(conn)
    await db.commit()
    logger.info("VCS connection deleted", connection_id=str(conn.id))
