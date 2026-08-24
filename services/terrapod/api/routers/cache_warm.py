"""Warm-ahead submission and status (#1420).

The binary cache has had `POST /admin/binary-cache/warm` since it existed. The
caches added since — the container registry and the PyPI/npm proxies — did not,
and they are the ones whose contents an operator is least able to enumerate from
memory.

Submission returns a job id rather than a result, because warming a real
dependency closure outlives an HTTP request. The three endpoints live together so
that status stays reachable whenever any warming is possible, rather than
disappearing with whichever capability happened to be switched off.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from terrapod.api.dependencies import AuthenticatedUser, require_admin
from terrapod.config import settings
from terrapod.logging_config import get_logger
from terrapod.services import warm_ahead
from terrapod.services.engine_gating import capability_enabled
from terrapod.services.scheduler import enqueue_trigger
from terrapod.services.warm_ahead import MAX_ITEMS, WarmItem

logger = get_logger(__name__)

router = APIRouter(tags=["cache-warm"])


class PackageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ecosystem: str = Field(description="pypi or npm")
    name: str
    version: str = ""


class PackageWarmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    packages: list[PackageSpec] = Field(default_factory=list)


class ImageWarmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: `host/repo:tag`, the same reference a client would pull.
    images: list[str] = Field(default_factory=list)


def _reject_when_sealed() -> None:
    """Warming is an upstream fetch by definition.

    A sealed node reporting "0 succeeded" would read as a set of missing packages
    rather than a configuration that forbids fetching at all — and the operator
    would go looking upstream for a problem that is on this side.
    """
    if settings.registry.cache_only:
        raise HTTPException(
            status_code=409,
            detail=(
                "This node is sealed (registry.cache_only), so nothing can be fetched "
                "from upstream. Warm before sealing, or unseal to warm."
            ),
        )


async def _submit(items: list[WarmItem], user: AuthenticatedUser) -> dict:
    if not items:
        raise HTTPException(status_code=422, detail="Nothing to warm")
    if len(items) > MAX_ITEMS:
        raise HTTPException(
            status_code=422,
            detail=f"At most {MAX_ITEMS} items per submission, got {len(items)}",
        )
    job = await warm_ahead.create_job(items)
    await enqueue_trigger(
        "cache_warm",
        {"job_id": job.job_id, "items": [vars(i) for i in items]},
    )
    logger.info("Warm job submitted", job_id=job.job_id, items=len(items), actor=user.email)
    return {
        "data": {
            "type": "warm-jobs",
            "id": job.job_id,
            "attributes": {"status": job.status, "total": job.total},
            "links": {"self": f"/api/terrapod/v1/admin/warm-jobs/{job.job_id}"},
        }
    }


@router.post("/admin/package-cache/warm", status_code=202)
async def warm_packages(
    body: PackageWarmRequest,
    user: AuthenticatedUser = Depends(require_admin),
) -> dict:
    """Cache PyPI projects and npm packages ahead of a seal."""
    _reject_when_sealed()
    items: list[WarmItem] = []
    for spec in body.packages:
        if spec.ecosystem not in ("pypi", "npm"):
            raise HTTPException(status_code=422, detail=f"Unknown ecosystem: {spec.ecosystem}")
        if not capability_enabled(spec.ecosystem):
            raise HTTPException(status_code=404, detail=f"{spec.ecosystem} proxy is not enabled")
        items.append(WarmItem(ecosystem=spec.ecosystem, name=spec.name, version=spec.version))
    return await _submit(items, user)


@router.post("/admin/oci/warm", status_code=202)
async def warm_images(
    body: ImageWarmRequest,
    user: AuthenticatedUser = Depends(require_admin),
) -> dict:
    """Pull images through the mirror ahead of a seal, blobs included."""
    _reject_when_sealed()
    if not capability_enabled("oci"):
        raise HTTPException(status_code=404, detail="The container registry is not enabled")

    items: list[WarmItem] = []
    for reference in body.images:
        name, _, tag = reference.rpartition(":")
        # No colon at all, or a colon that belongs to a port rather than a tag:
        # `rpartition` gives an empty name for the first, and a name containing a
        # slash after the colon for the second.
        if not name or "/" in tag:
            name, tag = reference, "latest"
        items.append(WarmItem(ecosystem="oci", name=name, version=tag))
    return await _submit(items, user)


@router.get("/admin/warm-jobs/{job_id}")
async def warm_job_status(
    job_id: str,
    user: AuthenticatedUser = Depends(require_admin),
) -> dict:
    """Progress and per-item outcomes.

    Per item, because "failed" over a submission of twenty is not something an
    operator can act on, and finding the specific gaps before sealing is the
    entire purpose.
    """
    job = await warm_ahead.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"No such warm job: {job_id}")
    return {
        "data": {
            "type": "warm-jobs",
            "id": job.job_id,
            "attributes": {
                "status": job.status,
                "total": job.total,
                "completed": job.completed,
                "succeeded": job.succeeded,
                "failed": job.failed,
                "submitted-at": job.submitted_at,
                "outcomes": [
                    {
                        "ecosystem": o.ecosystem,
                        "ref": o.ref,
                        "ok": o.ok,
                        "detail": o.detail,
                        "files": o.files,
                    }
                    for o in job.outcomes
                ],
            },
        }
    }
