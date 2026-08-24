"""Operator surface for the container registry (#1423).

The distribution spec covers deleting a manifest and a blob, and nothing else —
no repository deletion, no way to see what is holding space. Those are the
operations an operator actually reaches for, so they live here on the native
surface rather than being invented under `/v2/`, which is a contract with
container clients rather than with people.

Mounted only when the registry is (`engine_gating.capability_enabled("oci")`), so
a deployment that has switched Ansible off carries none of it — see #1429.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user, require_admin
from terrapod.auth import capabilities as cap
from terrapod.auth.capabilities import has_capability
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services.oci import registry_service
from terrapod.services.registry_rbac_service import resolve_registry_capabilities_for

logger = get_logger(__name__)

router = APIRouter(prefix="/oci", tags=["oci-admin"])


async def _admin_repository(db: AsyncSession, user: AuthenticatedUser, name: str):
    """Find a repository the caller may administer, or 404.

    404 rather than 403 for a caller without access, matching `/v2/` — the
    existence of a repository is itself something not everyone may learn.
    """
    repository = await registry_service.get_repository(db, name)
    if repository is None:
        raise HTTPException(status_code=404, detail=f"No such repository: {name}")
    caps = await resolve_registry_capabilities_for(
        db, user, repository.name, repository.labels or {}, repository.owner_email
    )
    if not has_capability(caps, cap.REGISTRY_ADMIN):
        raise HTTPException(status_code=404, detail=f"No such repository: {name}")
    return repository


@router.get("/repositories/{name:path}/untagged")
async def list_untagged(
    name: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Manifests in this repository that no tag points at.

    This is the listing that makes a filling registry explicable. Re-pushing
    `:latest` leaves the previous manifest untagged and still holding its layers;
    nothing collects it, because it is a manifest rather than an orphaned blob.
    Over a year of CI that is most of the space, and until now there was no way to
    see it — and an operator cannot delete what they cannot see.

    Referrers whose subject has been deleted show up here too, which is intended.
    """
    repository = await _admin_repository(db, user, name)
    manifests = await registry_service.list_untagged_manifests(db, repository)
    return {
        "data": [
            {
                "type": "oci-manifests",
                "id": m.digest,
                "attributes": {
                    "digest": m.digest,
                    "media-type": m.media_type,
                    "size": m.size,
                    "artifact-type": m.artifact_type,
                    "subject-digest": m.subject_digest,
                    "created-at": m.created_at.isoformat().replace("+00:00", "Z"),
                },
            }
            for m in manifests
        ],
        "meta": {"count": len(manifests)},
    }


@router.delete("/repositories/{name:path}")
async def delete_repository(
    name: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete a repository, its tags, its manifests and its blob links.

    Blob rows survive deliberately: they are shared between repositories, so
    removing them here would take content another repository is still serving.
    Dropping this repository's links makes its blobs reclaimable, and only those
    nothing else references.

    Requires `registry:admin`, and is audited. This destroys content that may
    exist nowhere else — a mirrored repository will refill on the next pull, but
    a pushed one will not.
    """
    repository = await _admin_repository(db, user, name)
    counts = await registry_service.delete_repository(db, repository)
    await db.commit()
    logger.info("Deleted registry repository", repository=name, actor=user.email, **counts)
    return {"data": {"type": "oci-repositories", "id": name, "attributes": counts}}


@router.post("/collect")
async def collect_now(user: AuthenticatedUser = Depends(require_admin)) -> dict:
    """Run a collection cycle now rather than waiting for the hourly one.

    Deletion un-references content; the collector is what frees it. Waiting up to
    an hour is confusing for someone who has just deleted a large image
    specifically to make room, and the cycle already exists — so this only removes
    the wait.

    Platform admin, not repository admin: a cycle sweeps every repository, so
    authority over one is not authority to run it.
    """
    from terrapod.services.oci.gc import collect

    result = await collect()
    logger.info("Collection run on request", actor=user.email, blobs=result.blobs_deleted)
    return {
        "data": {
            "type": "oci-collections",
            "attributes": {
                "blobs-deleted": result.blobs_deleted,
                "bytes-reclaimed": result.bytes_reclaimed,
                "repositories": result.repositories,
                "skipped-reason": result.skipped_reason,
                "errors": result.errors,
            },
        }
    }
