"""Warm-ahead submission and status (#1420).

The binary cache has had `POST /admin/binary-cache/warm` since it existed. The
caches added since — the container registry and the PyPI/npm proxies — did not,
and they are the ones whose contents an operator is least able to enumerate from
memory.

Submission returns a job id rather than a result, because warming a real
dependency closure outlives an HTTP request. The endpoints live together so that
status stays reachable whenever any warming is possible, rather than
disappearing with whichever capability happened to be switched off.

**Every ecosystem, not two (#2486).** This accepted `pypi` and `npm` only, while
`galaxy`, `go`, `nuget` and `pulumi` all had working proxies — so the caches an
operator was least able to fill by hand were the ones with no way to fill them.
The validation here is DERIVED from `warm_ahead.PACKAGE_ECOSYSTEMS` rather than
listed again, because a hand-kept copy is how a surface comes to accept a
different set from the one that can serve it.

**`source` names a mirror, never a URL.** See `warm_ahead`'s docstring: the
configured upstream is the whole of this proxy's request-forgery surface
*because* it is operator-set, so a per-request URL would undo that for an
admin-authenticated caller. `GET /admin/package-cache/sources` is the other half
— what a submission would pull from, before it is submitted, because "it fetched
from somewhere" is not a thing an operator preparing a sealed node can check.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from terrapod.api.dependencies import AuthenticatedUser, require_admin
from terrapod.config import settings
from terrapod.logging_config import get_logger
from terrapod.services import warm_ahead
from terrapod.services.capabilities import capability_enabled
from terrapod.services.scheduler import enqueue_trigger
from terrapod.services.warm_ahead import MAX_ITEMS, WarmItem

logger = get_logger(__name__)

router = APIRouter(tags=["cache-warm"])


class PackageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ecosystem: str = Field(
        description="galaxy | pypi | npm | go | nuget | pulumi — every ecosystem the "
        "package cache holds."
    )
    name: str = Field(
        description="The artifact, named the way its own ecosystem names it: "
        "`namespace.collection` for galaxy, a module path for go, a package id for "
        "nuget and pypi, `[kind:]plugin` for pulumi."
    )
    version: str = Field(
        default="",
        description="An EXPLICIT version, which is what makes a warm reproducible and "
        "checkable against a lockfile. Empty resolves to whatever upstream calls newest "
        "— a convenience, and not available for pulumi, whose download protocol has no "
        "index to resolve against.",
    )
    source: str = Field(
        default="",
        description="A label in `registry.package_cache.<ecosystem>.mirrors` to warm "
        "FROM, instead of the serving upstream. Empty means the upstream. A label and "
        "not a URL: see the module docstring. `GET /admin/package-cache/sources` lists "
        "what is declared.",
    )
    platforms: list[str] = Field(
        default_factory=list,
        description="`os/arch` pairs, for pulumi only — a plugin is published per "
        "platform. Empty warms the runner's own (linux/amd64, linux/arm64); warming one "
        "arch leaves the other to be discovered after sealing. Ignored by the other "
        "five, which have one artifact set per version.",
    )


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
            "links": {"self": f"/api/v1/admin/warm-jobs/{job.job_id}"},
        }
    }


@router.get("/admin/package-cache/sources")
async def package_cache_sources(
    user: AuthenticatedUser = Depends(require_admin),
) -> dict:
    """Where a warm of each ecosystem would pull from, before one is submitted.

    The "say what will happen before it happens" half of #2486. An operator
    warming from the UI could see neither which upstream a submission would
    reach nor that a nearer one had been declared, so the only feedback was that
    bytes arrived from somewhere — which on a node about to be sealed is the one
    thing worth being sure of.

    `resolves-latest` is here because it decides whether a version may be
    omitted, and that differs per ecosystem rather than per deployment: pulumi's
    protocol has no index, so a bare name has nothing to resolve against.
    """
    return {
        "data": [
            {
                "type": "package-cache-sources",
                "id": ecosystem,
                "attributes": {
                    "ecosystem": ecosystem,
                    "enabled": info["enabled"],
                    "upstream": info["upstream"],
                    "mirrors": info["mirrors"],
                    "resolves-latest": info["resolves_latest"],
                },
            }
            for ecosystem, info in warm_ahead.effective_sources().items()
        ],
        "meta": {
            "max-items": MAX_ITEMS,
            # Reported here rather than only on a 409, so a UI can say warming
            # is unavailable instead of offering a form that cannot submit.
            "sealed": bool(settings.registry.cache_only),
        },
    }


@router.post("/admin/package-cache/warm", status_code=202)
async def warm_packages(
    body: PackageWarmRequest,
    user: AuthenticatedUser = Depends(require_admin),
) -> dict:
    """Cache packages from any proxied ecosystem ahead of a seal."""
    _reject_when_sealed()
    items: list[WarmItem] = []
    for spec in body.packages:
        if spec.ecosystem not in warm_ahead.PACKAGE_ECOSYSTEMS:
            known = ", ".join(warm_ahead.PACKAGE_ECOSYSTEMS)
            raise HTTPException(
                status_code=422,
                detail=f"Unknown ecosystem: {spec.ecosystem} (known: {known})",
            )
        if not capability_enabled(spec.ecosystem):
            raise HTTPException(status_code=404, detail=f"{spec.ecosystem} proxy is not enabled")
        if not spec.name:
            raise HTTPException(status_code=422, detail="Every item needs a name")
        if not spec.version and spec.ecosystem not in warm_ahead.RESOLVES_LATEST:
            # Refused at the door rather than per item, because the whole
            # submission is otherwise accepted and the operator finds out it
            # warmed nothing only by reading the outcomes.
            raise HTTPException(
                status_code=422,
                detail=(
                    f"{spec.ecosystem} has no index to resolve 'latest' against, so "
                    f"{spec.name} needs an explicit version"
                ),
            )
        try:
            warm_ahead.resolve_source(spec.ecosystem, spec.source)
        except ValueError as exc:
            # 422 rather than 404: the ecosystem exists and the request is the
            # thing that is wrong, and the message names what IS declared.
            raise HTTPException(status_code=422, detail=str(exc)) from None
        items.append(
            WarmItem(
                ecosystem=spec.ecosystem,
                name=spec.name,
                version=spec.version,
                source=spec.source,
                platforms=list(spec.platforms),
            )
        )
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
                        # `fetched` 0 against a non-zero `files` is "already
                        # cached", which is a different answer from "fetched"
                        # and the one a re-run is looking for (#2486).
                        "fetched": o.fetched,
                        "source": o.source,
                    }
                    for o in job.outcomes
                ],
            },
        }
    }
