"""Admin list, totals and eviction for the two caches that had none (#2474).

The CLI-binary and provider-binary caches have had admin routes since they
existed; they stay in `binary_cache.py`, where they are, because moving them
would be churn with no reader. These are the other two.

**The totals are computed over the whole cache, not over a page.** A total summed
from a listing is wrong by however much the page left out, and wrong in the
reassuring direction -- an operator looking for what is filling a bucket would be
shown a number smaller than the problem. For the package cache that is a `GROUP
BY` in the database; for the archive cache there is nothing to group, so the walk
carries a ceiling and the response says when it hit it.

**Several routers, because this surface is gated at three different grains.** The
package cache
is an optional capability (`registry.package_cache.enabled`) and the convention
this repository enforces is that a switched-off capability's surface is ABSENT
rather than answering 404 -- `scripts/airgap-gate.sh --gated-off` asserts it
against a running stack, and `oci_admin` is the direct precedent: its comment
reads "it exists only for the registry, so it goes when the registry does".
`POST /admin/package-cache/warm` already behaves that way, so mounting these four
unconditionally made that namespace half-present -- four routes visible and the
fifth gone, which is worse than either consistent answer. The VCS archive cache
has no capability flag at all (it is part of what Terrapod is, like the provider
mirror), so its routes are unconditional.

The third grain is the **operator-upload** routes (#2599), one per ecosystem on
its own router, because an upload is for one cache rather than for the cache: it
goes with `registry.package_cache.<eco>.enabled`. On the shared router a
deployment that had switched PyPI off still offered somewhere to put a PyPI
artifact it would never serve -- the same half-present namespace, one level in.
`build_upload_router` mounts whichever of them this deployment serves.

The capability's own documented meaning is what makes gating these right rather
than merely tidy: `services/capabilities.py` states that turning one off "stops
serving and stops its background work; stored images and cached artifacts stay
where they are and come back untouched when it is turned back on". Off is a
pause, not a decommission -- so there is no state an operator is locked out of,
only a surface that is quiet while the thing it describes is.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, require_admin
from terrapod.api.pagination import MAX_PAGE_SIZE, build_meta, parse_page_params
from terrapod.api.serialization import rfc3339
from terrapod.config import settings
from terrapod.db.models import CachedPackageFile
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import cache_inventory
from terrapod.services.capabilities import capability_enabled
from terrapod.services.package_cache import (
    galaxy,
    goproxy,
    npm,
    nuget,
    pulumi_plugins,
    pypi,
    substrate,
    uploads,
)
from terrapod.storage import get_storage
from terrapod.storage.protocol import ObjectStore

logger = get_logger(__name__)

#: The package cache's admin surface, mounted only while that capability serves.
#: See the module docstring: this is the gated half.
package_router = APIRouter(tags=["cache-admin"])

#: The VCS archive cache's admin surface, always mounted.
router = APIRouter(tags=["cache-admin"])

#: One upload router per ecosystem, mounted only while THAT ecosystem serves.
#:
#: Separate from `package_router` because the gating is finer: the listing, the
#: totals, the evictions and the verify route are about the cache as a whole and
#: go with `registry.package_cache.enabled`, while an upload is for one ecosystem
#: and must go with `registry.package_cache.<eco>.enabled`. Leaving them on the
#: shared router left a deployment that had switched PyPI off still offering
#: `POST .../package-cache/pypi/artifacts` -- an endpoint to put an artifact into
#: a cache that will not serve it, which `tests/api/test_capability_gating_
#: endpoints.py` refuses by name.
#:
#: Six routers and a factory rather than one router and six `if`s inside a
#: handler, for the reason `package_cache.build_router` gives: a switched-off
#: capability's surface is ABSENT, not present-and-refusing, so it must not reach
#: the schema either.
pypi_upload_router = APIRouter(tags=["cache-admin"])
npm_upload_router = APIRouter(tags=["cache-admin"])
nuget_upload_router = APIRouter(tags=["cache-admin"])
go_upload_router = APIRouter(tags=["cache-admin"])
galaxy_upload_router = APIRouter(tags=["cache-admin"])
pulumi_upload_router = APIRouter(tags=["cache-admin"])


def build_upload_router() -> APIRouter | None:
    """The upload routes for the ecosystems this deployment actually serves.

    None when it serves none of them, so the application mounts nothing — mirroring
    `package_cache.build_router`, including being a factory rather than a
    module-level aggregate so building the application twice in one process (which
    the tests do constantly) cannot accumulate duplicate routes.
    """
    pairs = (
        ("pypi", pypi_upload_router),
        ("npm", npm_upload_router),
        ("nuget", nuget_upload_router),
        ("go", go_upload_router),
        ("galaxy", galaxy_upload_router),
        ("pulumi", pulumi_upload_router),
    )
    aggregate = APIRouter()
    mounted = False
    for capability, upload_router in pairs:
        if capability_enabled(capability):
            aggregate.include_router(upload_router)
            mounted = True
    return aggregate if mounted else None


class PackageEvictRequest(BaseModel):
    """What a bulk package eviction targets.

    `extra="forbid"` so a misspelled filter is a 422 rather than an eviction
    wider than the caller meant -- which on this endpoint is the difference
    between clearing one ecosystem and clearing the cache.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    ecosystem: str = Field(default="", description="galaxy | pypi | npm | go | nuget | pulumi")
    older_than_days: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Evict artifacts not ACCESSED in this many days. Access-based, never "
            "write-based: an artifact every run pulls would otherwise be evicted "
            "merely for being old and re-fetched immediately."
        ),
    )


class ArchiveEvictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    connection_id: str = Field(default="", description="Narrow to one VCS connection")
    older_than_days: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Evict archives written more than this many days ago. Write-based here, "
            "because an archive is immutable once written and there is no access "
            "column to prefer."
        ),
    )
    after: str = Field(
        default="",
        description=(
            "Resume a bounded sweep from this key -- the `next-cursor` the "
            "previous call returned. Omit to start at the beginning. Pass the "
            "SAME filters back, or the second window answers a different "
            "question from the first."
        ),
    )


def _sealed_note() -> dict[str, object]:
    """What an eviction has to say on a node that cannot re-fetch (#2474).

    The scheduled retention sweep SKIPS these caches entirely when
    `registry.cache_only` is set, and its own comment gives the reason: "an
    un-refetchable artifact would be lost permanently". These routes are the
    manual path to the same deletion, so on a sealed node an eviction here is
    not an eviction at all -- it is a deletion with nothing able to restore it.

    It WARNS rather than refusing, and the 409 the warm route answers does NOT
    transfer. Warming is an upstream fetch by definition, so a sealed node
    genuinely cannot do it; eviction can, and refusing would leave an operator
    with no way to remove a bad artifact from exactly the deployment where a bad
    artifact is hardest to work around. The rule is the project's own: refuse
    only what cannot work, and warn about what works and cannot be undone.

    So the response carries the fact, and every consumer -- the admin page's
    confirmation, the SDK, an MCP tool -- can surface it without each one having
    to know what `cache_only` implies.
    """
    if not settings.registry.cache_only:
        return {"sealed": False}
    return {
        "sealed": True,
        "warning": (
            "This node is sealed (registry.cache_only), so nothing evicted here can "
            "be fetched again. The scheduled retention sweep skips these caches for "
            "that reason; a manual eviction is a permanent deletion."
        ),
    }


# ── package cache ──────────────────────────────────────────────────────────
#
# On `package_router`: these go when `registry.package_cache.enabled` does.
# A new route here belongs on that router, and needs a sample request in
# `tests/api/test_package_cache.py::SAMPLES` -- the auth gate there walks
# every route whose path carries `package-cache` and fails on one it has no
# sample for, which is how it stays unable to skip a new endpoint.


def _package_file_json(row: CachedPackageFile) -> dict[str, object]:
    """One cached artifact as JSON:API, for the listing and for an upload.

    Shared by both deliberately: the upload response is the same resource the
    listing shows, so a second hand-written copy of these attributes is how the
    page an operator reads and the answer they get back come to disagree about
    the same row.

    **`attestation` and `uploaded-by` are the load-bearing pair** (#2599). An
    uploaded artifact must never be indistinguishable from a fetched one here: if
    a wheel an operator supplied reads as fetched-and-verified, the ledger is
    lying about the one thing on it that matters.
    """
    return {
        "id": str(row.id),
        "type": "cached-package-files",
        "attributes": {
            "ecosystem": row.ecosystem,
            "name": row.name,
            "version": row.version,
            "filename": row.filename,
            "size": row.size,
            # The digest this row's bytes were admitted against, in the form its
            # SOURCE expresses it. For a fetched row that is upstream's own, so
            # an operator can match it against what the registry published
            # rather than against what we stored; for an upload there is no
            # upstream, so it is the digest computed on receipt. `attestation`
            # says which.
            "digest": row.digest,
            # How it was admitted -- `substrate.ATTESTATIONS`. NULL means a build
            # that recorded nothing admitted it, which the serve path treats as
            # a miss and re-verifies.
            "attestation": row.attestation,
            "uploaded-by": row.uploaded_by,
            "upload-reference": row.upload_reference,
            "replaced-digest": row.replaced_digest,
            "replaced-at": rfc3339(row.replaced_at) if row.replaced_at else None,
            "cached-at": rfc3339(row.cached_at),
            "last-accessed-at": rfc3339(row.last_accessed_at),
        },
    }


@package_router.get("/admin/package-cache")
async def list_package_cache(
    ecosystem: str | None = None,
    name: str | None = None,
    request: Request = None,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Cached package artifacts, newest-ACCESSED first.

    `name` is a prefix match: a practitioner looking for `community.` wants the
    collection family, and an exact name is the degenerate case of that query.
    """
    page_number, page_size = parse_page_params(request)
    # `page_size` None is the documented non-paged request (no `page[*]` params
    # at all), which returns the whole result set -- an SDK that sends none
    # relies on that. Anything else is sliced in the DATABASE: this used to
    # fetch every matching row and slice the list in Python, so a page of a
    # 60,000-artifact cache materialised all 60,000 ORM rows (#2492).
    limit = min(page_size, MAX_PAGE_SIZE) if page_size else None
    offset = (page_number - 1) * limit if limit else 0
    entries, total = await cache_inventory.list_package_files(
        db, ecosystem=ecosystem, name=name, limit=limit, offset=offset
    )
    items = [_package_file_json(e) for e in entries]
    return JSONResponse(content={"data": items, "meta": build_meta(total, page_number, page_size)})


@package_router.get("/admin/package-cache/totals")
async def package_cache_totals(
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Count and bytes per ecosystem, aggregated in the database."""
    totals = await cache_inventory.package_totals(db)
    return JSONResponse(
        content={
            "data": [
                {
                    "type": "package-cache-totals",
                    "id": t.ecosystem,
                    "attributes": {"ecosystem": t.ecosystem, "files": t.files, "bytes": t.bytes},
                }
                for t in totals
            ],
            "meta": {
                "files": sum(t.files for t in totals),
                "bytes": sum(t.bytes for t in totals),
                # Reported HERE as well as on the eviction responses, because a
                # client needs it before it asks rather than after: a
                # confirmation that cannot say "these bytes will not come back"
                # is the one place this fact matters most. A panel loads the
                # totals anyway, so it costs nothing.
                **_sealed_note(),
            },
        }
    )


@package_router.delete("/admin/package-cache/{file_id}")
async def delete_package_cache_file(
    file_id: str,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Evict one artifact, by row id.

    By id rather than by `(ecosystem, name, filename)`: that tuple is the unique
    constraint, so either would address exactly one row, and an id cannot be
    mangled by a filename carrying a slash or a `+` the way a path segment can.
    """
    try:
        removed = await cache_inventory.delete_package_file(db, storage, file_id)
    except Exception as exc:  # a malformed uuid reaches the driver, not us
        raise HTTPException(status_code=404, detail="no such cached artifact") from exc
    if not removed:
        raise HTTPException(status_code=404, detail="no such cached artifact")
    await db.commit()
    return JSONResponse(content={"status": "evicted", **_sealed_note()})


@package_router.post("/admin/package-cache/evict")
async def evict_package_cache(
    body: PackageEvictRequest,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Evict in bulk by ecosystem and/or age, up to a bounded number per call.

    Neither filter means the whole cache, which is a thing an operator
    legitimately wants after a bad publish. Nothing is lost: every artifact here
    is re-fetchable from the registry it came from, which is what makes this
    cache evictable rather than a store.

    **`truncated` is true when more matched than this call removed**, and is the
    reason the bound is safe to have. It used to delete every match inside one
    handler -- 50,000 artifacts at ~30 ms each is about 25 minutes, so the
    gateway timed out, the transaction rolled back, and the objects already
    deleted left rows pointing at keys that no longer existed (#2492). Repeating
    the call continues from where this one stopped, with no cursor to carry: the
    rows that matched are the rows that went.
    """
    result = await cache_inventory.evict_package_files(
        db,
        storage,
        ecosystem=body.ecosystem or None,
        older_than_days=body.older_than_days,
    )
    await db.commit()
    return JSONResponse(
        content={
            "status": "evicted",
            "files": result.files,
            "bytes": result.bytes,
            # The honest half. A bounded sweep reported as a complete one is
            # worse than a partial result: the operator reloads the totals, sees
            # the same size, and reads it as "the purge did nothing" (#2491).
            "truncated": result.truncated,
            **_sealed_note(),
        }
    )


# ── operator uploads ───────────────────────────────────────────────────────
#
# An operator uploads an artifact they have vetted with their own process, and a
# sealed deployment then serves that and nothing else (#2599). The trust model is
# `services/package_cache/uploads.py`; what belongs here is the surface.
#
# **One route per cache, not one taking a `kind`.** Settled on the issue, and for
# a reason rather than as a style: each ecosystem already computes its cache key
# from exactly the coordinates an upload must supply, so the validation stays
# where that scheme is defined — `<ecosystem>.upload_coordinates` — instead of
# one endpoint switching on six naming conventions it would then own a second
# copy of. An upload is the same cache key arrived at from the other direction.
#
# **Admin, never workspace.** Who may put bytes into what this deployment serves
# is a deployment-wide trust assertion. `require_admin` is also what makes the
# Galaxy publish path's explicit runner-token refusal unnecessary here: a runner
# token carries no platform role, so it cannot reach these at all.
#
# **These work while SEALED, which is the entire point.** A `cache_only`
# deployment has no other way to get an artifact in, so nothing here consults
# `substrate.sealed()` and nothing here reaches upstream — including to check
# that the coordinates exist, which would fail the feature for its most
# important case (a patched or internal artifact upstream never published).
#
# The request is multipart so a browser form can drive it unchanged; the BFF's
# `/api/*` proxy is a Route Handler forwarding `request.body` with
# `duplex: 'half'`, so a large artifact streams rather than being buffered.
#
# **Each of these is on its OWN router**, not on `package_router` with the rest of
# the namespace, and `build_upload_router` is what mounts them. The gating is
# finer: an upload is for one ecosystem, so it goes with
# `registry.package_cache.<eco>.enabled` rather than with the cache's own flag.
# On the shared router a deployment that had switched PyPI off still offered
# somewhere to put a PyPI artifact it would never serve.


async def _upload_artifact(
    *,
    ecosystem: str,
    build: Callable[[], substrate.Coordinates],
    file: UploadFile,
    reference: str,
    replace: bool,
    user: AuthenticatedUser,
    db: AsyncSession,
    storage: ObjectStore,
) -> JSONResponse:
    """Stream one uploaded artifact in, record who vouched for it, answer.

    The plumbing every per-cache route shares. `build` is the ecosystem's own
    `upload_coordinates` call, deferred so its refusal is translated once here
    rather than in six identical `except` blocks.

    The part is re-streamed to the ephemeral PVC rather than read into memory:
    Starlette rolls a large multipart part over to the RAM-backed `/tmp`, which
    is what rule 14 exists to keep large uploads out of.
    """
    try:
        coordinates = build()
    except substrate.CoordinateError as exc:
        # 422, not 404: the request is well-formed and the coordinates are not
        # ones this ecosystem can address. A 404 would imply we had looked
        # somewhere for them, which is exactly what this path must not do.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    from terrapod.api.upload_stream import stream_upload_to_tempfile

    tmp_path, size = await stream_upload_to_tempfile(
        file, suffix=".package-upload", max_bytes=uploads.MAX_UPLOAD_BYTES
    )
    try:
        if size == 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Empty upload. The artifact's bytes go in the `file` part; "
                    "the coordinates are the other form fields."
                ),
            )
        try:
            admission = await uploads.admit(
                db,
                storage,
                ecosystem=ecosystem,
                coordinates=coordinates,
                tmp_path=tmp_path,
                uploaded_by=user.email or "",
                reference=reference,
                replace=replace,
            )
        except uploads.UploadConflict as exc:
            # 409 naming what is there and who put it there. Requirement 4 on the
            # issue: a replacement has to be asked for, because what it displaces
            # may be what a run already depends on.
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        meta: dict[str, object] = {
            "replaced": admission.replaced,
            "unchanged": admission.unchanged,
        }
        if admission.replaced:
            meta["previous-digest"] = admission.previous_digest
            # A replacement DELETED the bytes that were there, and on a sealed
            # node nothing can fetch them back -- which is precisely what
            # `_sealed_note` says about an eviction. Omitted on a first upload
            # and on an identical re-upload, because neither destroys anything.
            meta.update(_sealed_note())
        # Serialised before the commit: the row is already flushed, so its id is
        # there, and reading an ORM attribute after a commit is the one place an
        # async handler can trip over a lazy refresh.
        body = {"data": _package_file_json(admission.record), "meta": meta}
        await db.commit()
    finally:
        try:
            await asyncio.to_thread(os.unlink, tmp_path)
        except OSError:
            pass

    return JSONResponse(
        status_code=200 if (admission.replaced or admission.unchanged) else 201,
        content=body,
    )


#: What every upload route takes besides its own coordinates.
#:
#: `replace` is the deliberateness of requirement 4 made explicit: without it an
#: upload at coordinates that are already held is refused, so a repeat upload
#: cannot silently overwrite something runs depend on. `reference` is the
#: operator's own ticket or approval record, never interpreted.
_REFERENCE = Form(
    "",
    description="Free text: the operator's own ticket or approval record for this artifact.",
)
_REPLACE = Form(
    False,
    description=(
        "Overwrite an artifact already held at these coordinates. Without it such "
        "an upload is refused with 409, because what it would displace may be what "
        "a run already depends on. The displaced digest is recorded on the row."
    ),
)


@pypi_upload_router.post("/admin/package-cache/pypi/artifacts", status_code=201)
async def upload_pypi_artifact(
    project: str = Form(..., description="The PyPI project name, before normalisation."),
    filename: str = Form(
        ...,
        description=(
            "The distribution's own filename, exactly as pip will ask for it "
            "(`flask-3.0.0-py3-none-any.whl`). The version is read out of it."
        ),
    ),
    file: UploadFile = File(..., description="The distribution's bytes."),
    reference: str = _REFERENCE,
    replace: bool = _REPLACE,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Upload a vetted wheel or sdist for the PyPI proxy to serve."""
    return await _upload_artifact(
        ecosystem=pypi.ECOSYSTEM,
        build=lambda: pypi.upload_coordinates(project, filename),
        file=file,
        reference=reference,
        replace=replace,
        user=user,
        db=db,
        storage=storage,
    )


@npm_upload_router.post("/admin/package-cache/npm/artifacts", status_code=201)
async def upload_npm_artifact(
    name: str = Form(..., description="The package name, scope and all (`@scope/pkg`)."),
    version: str = Form(..., description="The version these bytes are."),
    file: UploadFile = File(..., description="The tarball's bytes."),
    reference: str = _REFERENCE,
    replace: bool = _REPLACE,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Upload a vetted npm tarball.

    The filename is derived rather than supplied, because npm's own registry
    derives it and the serve path matches on the derived form — a supplied one
    that differed would be stored at a key no install ever asks for.
    """
    return await _upload_artifact(
        ecosystem=npm.ECOSYSTEM,
        build=lambda: npm.upload_coordinates(name, version),
        file=file,
        reference=reference,
        replace=replace,
        user=user,
        db=db,
        storage=storage,
    )


@nuget_upload_router.post("/admin/package-cache/nuget/artifacts", status_code=201)
async def upload_nuget_artifact(
    package_id: str = Form(..., description="The NuGet package id."),
    version: str = Form(..., description="The version these bytes are."),
    file: UploadFile = File(..., description="The `.nupkg` bytes."),
    reference: str = _REFERENCE,
    replace: bool = _REPLACE,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Upload a vetted `.nupkg`."""
    return await _upload_artifact(
        ecosystem=nuget.ECOSYSTEM,
        build=lambda: nuget.upload_coordinates(package_id, version),
        file=file,
        reference=reference,
        replace=replace,
        user=user,
        db=db,
        storage=storage,
    )


@go_upload_router.post("/admin/package-cache/go/artifacts", status_code=201)
async def upload_go_artifact(
    module: str = Form(..., description="The escaped module path."),
    version: str = Form(..., description="The module version, starting `v`."),
    suffix: str = Form(
        ...,
        description=(
            "Which of the version's files this is: `.info`, `.mod` or `.zip`. A "
            "module version is three artifacts to the proxy protocol and each is "
            "uploaded in its own right -- `go mod download` needs all three."
        ),
    ),
    file: UploadFile = File(..., description="That file's bytes."),
    reference: str = _REFERENCE,
    replace: bool = _REPLACE,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Upload one vetted file of a Go module version."""
    return await _upload_artifact(
        ecosystem=goproxy.ECOSYSTEM,
        build=lambda: goproxy.upload_coordinates(module, version, suffix),
        file=file,
        reference=reference,
        replace=replace,
        user=user,
        db=db,
        storage=storage,
    )


@galaxy_upload_router.post("/admin/package-cache/galaxy/artifacts", status_code=201)
async def upload_galaxy_artifact(
    namespace: str = Form(..., description="The collection namespace."),
    name: str = Form(..., description="The collection name."),
    version: str = Form(..., description="The version these bytes are."),
    file: UploadFile = File(..., description="The collection tarball's bytes."),
    reference: str = _REFERENCE,
    replace: bool = _REPLACE,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Upload a vetted collection tarball into the Galaxy proxy's cache.

    Not the same thing as `ansible-galaxy collection publish`, which publishes
    into Terrapod's **native** collection registry and reads every coordinate out
    of the archive. That path remains the right one for an operator who wants to
    sign what they vetted, because `ansible-galaxy` then verifies the signature
    against their own keyring — a stronger claim than a ledger entry, and the one
    kind where it happens to be free.
    """
    return await _upload_artifact(
        ecosystem=galaxy.ECOSYSTEM,
        build=lambda: galaxy.upload_coordinates(namespace, name, version),
        file=file,
        reference=reference,
        replace=replace,
        user=user,
        db=db,
        storage=storage,
    )


@pulumi_upload_router.post("/admin/package-cache/pulumi/artifacts", status_code=201)
async def upload_pulumi_artifact(
    kind: str = Form(..., description="The plugin kind, e.g. `resource`."),
    name: str = Form(..., description="The plugin name, e.g. `aws`."),
    version: str = Form(..., description="The plugin version, with or without a leading `v`."),
    os_: str = Form(..., alias="os", description="The target OS, e.g. `linux`."),
    arch: str = Form(..., description="The target architecture, e.g. `amd64`."),
    file: UploadFile = File(..., description="The plugin tarball's bytes."),
    reference: str = _REFERENCE,
    replace: bool = _REPLACE,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Upload a vetted Pulumi plugin tarball for one platform."""
    return await _upload_artifact(
        ecosystem=pulumi_plugins.ECOSYSTEM,
        build=lambda: pulumi_plugins.upload_coordinates(kind, name, version, os_, arch),
        file=file,
        reference=reference,
        replace=replace,
        user=user,
        db=db,
        storage=storage,
    )


@package_router.post("/admin/package-cache/{file_id}/verify")
async def verify_package_cache_file(
    file_id: str,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Re-hash a cached artifact's stored object and compare it with its row.

    **This is the check the serve path cannot do.** Every cache read ends in a
    redirect to object storage, so the API never sees the bytes a client is
    served and cannot hash them per request; what it can compare on the serve
    path is the object's size against the row, which it does for an uploaded
    artifact. This is the full digest check, on demand.

    It matters most for an upload, and that asymmetry is a requirement rather
    than a caveat: a fetched artifact can be re-verified any time because the
    publisher's material is still upstream, while an uploaded one has nothing to
    be re-verified against except the digest computed when it arrived. So that
    digest is the whole of its integrity story, and this is the thing that stops
    "we trust the operator vetted it" becoming "we never look at it again".

    Reads the whole object, so for a multi-gigabyte artifact it takes as long as
    reading a multi-gigabyte object takes. Nothing is changed either way: a
    mismatch is reported, never repaired or evicted, because what to do about one
    is the operator's decision and an uploaded artifact cannot be re-fetched.
    """
    try:
        record = await cache_inventory.get_package_file(db, file_id)
    except Exception as exc:  # a malformed uuid reaches the driver, not us
        raise HTTPException(status_code=404, detail="no such cached artifact") from exc
    if record is None:
        raise HTTPException(status_code=404, detail="no such cached artifact")

    result = await uploads.verify_object(storage, record)
    return JSONResponse(
        content={
            "data": {
                "id": str(record.id),
                "type": "package-cache-verifications",
                "attributes": {
                    # `null` when there was nothing to verify against -- a
                    # `none-published` row carries no digest, and "unverifiable"
                    # is a third answer that must not read as a pass or a fail.
                    "ok": result.ok,
                    "detail": result.detail,
                    "algorithm": result.algorithm,
                    "recorded-digest": result.recorded,
                    "computed-digest": result.computed,
                    "recorded-size": result.recorded_size,
                    "stored-size": result.stored_size,
                    "attestation": record.attestation,
                    "uploaded-by": record.uploaded_by,
                },
            }
        }
    )


# ── VCS archive cache ──────────────────────────────────────────────────────
#
# On `router`: unconditional. The archive cache has no capability flag, so
# there is nothing for these to be gated on.


@router.get("/admin/vcs-archive-cache")
async def list_vcs_archive_cache(
    connection_id: str | None = None,
    after: str = "",
    limit: int = 100,
    user: AuthenticatedUser = Depends(require_admin),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """One page of cached archives, with a KEY cursor to resume from.

    A key cursor rather than `page[number]`, and so NOT the shared `paginate`
    helper: there are no rows here, so a page comes from a storage walk, and
    `list_prefix` is ordered by key on every backend precisely so a cursor is
    meaningful. An offset would re-walk the whole prefix per page.

    `meta.next-cursor` is empty when this is the last page.
    """
    entries, cursor = await cache_inventory.list_archives(
        storage, connection_id=connection_id, after=after, limit=limit
    )
    return JSONResponse(
        content={
            "data": [
                {
                    "type": "cached-vcs-archives",
                    "id": e.key,
                    "attributes": {
                        "key": e.key,
                        "connection-id": e.connection_id,
                        "owner": e.owner,
                        "repo": e.repo,
                        "sha": e.sha,
                        # "full" for a whole-repo archive, otherwise the hash of
                        # the path-narrowing set the workspace asked for.
                        "paths-hash": e.paths_hash,
                        "size": e.size,
                        "last-modified": e.last_modified,
                    },
                }
                for e in entries
            ],
            "meta": {"next-cursor": cursor, "page-size": len(entries)},
        }
    )


@router.get("/admin/vcs-archive-cache/totals")
async def vcs_archive_cache_totals(
    user: AuthenticatedUser = Depends(require_admin),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Count and bytes per repository, largest first.

    `meta.truncated` is the honest half: there is nothing to aggregate in the
    database, so this walks storage and a walk has to stop somewhere. A total
    that silently omitted whatever lay past the ceiling would understate exactly
    the thing an operator came here to find.
    """
    totals, truncated = await cache_inventory.archive_totals(storage)
    return JSONResponse(
        content={
            "data": [
                {
                    "type": "vcs-archive-cache-totals",
                    "id": f"{t.connection_id}/{t.owner}/{t.repo}",
                    "attributes": {
                        "connection-id": t.connection_id,
                        "owner": t.owner,
                        "repo": t.repo,
                        "archives": t.archives,
                        "bytes": t.bytes,
                    },
                }
                for t in totals
            ],
            "meta": {
                "archives": sum(t.archives for t in totals),
                "bytes": sum(t.bytes for t in totals),
                "truncated": truncated,
                "scan-limit": cache_inventory.MAX_ARCHIVE_SCAN,
                # See the package totals above: before, not after.
                **_sealed_note(),
            },
        }
    )


@router.delete("/admin/vcs-archive-cache")
async def delete_vcs_archive(
    key: str,
    user: AuthenticatedUser = Depends(require_admin),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Evict one archive by its storage key.

    The key arrives as a QUERY parameter rather than a path, because it contains
    slashes and a path would have to encode them. It is refused unless it sits
    under the archive prefix -- this route takes a storage key from a request, so
    without that check an admin typo could delete a state version or a plan
    artifact through an endpoint whose name says "cache".
    """
    if not await cache_inventory.delete_archive(storage, key):
        raise HTTPException(
            status_code=422,
            detail=(
                "key must be a cached VCS archive -- it has to start with "
                f"'{cache_inventory.VCS_ARCHIVE_PREFIX}' and contain no '..'"
            ),
        )
    return JSONResponse(content={"status": "evicted", **_sealed_note()})


@router.post("/admin/vcs-archive-cache/evict")
async def evict_vcs_archive_cache(
    body: ArchiveEvictRequest,
    user: AuthenticatedUser = Depends(require_admin),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Evict archives in bulk by connection and/or age, one bounded window per call.

    **`truncated` and `next-cursor` are what make a large cache purgeable at
    all.** This used to walk a fixed ceiling of keys from the START of the prefix
    on every call and report only a count, so repeating it re-examined the same
    window: with an age filter matching a small fraction of it the window crept
    forward by only as many keys as the call deleted, and with one matching none
    of it the window never moved and the rest of the estate was unreachable for
    ever (#2491). Pass `next-cursor` back as `after`, with the same filters, and
    the sweep continues; an empty one means it finished.
    """
    result = await cache_inventory.evict_archives(
        storage,
        connection_id=body.connection_id or None,
        older_than_days=body.older_than_days,
        after=body.after,
    )
    return JSONResponse(
        content={
            "status": "evicted",
            "archives": result.files,
            "bytes": result.bytes,
            "truncated": result.truncated,
            # Spelled as the LISTING spells it (`meta.next-cursor`), because it
            # is the same kind of thing and a second spelling is how two halves
            # of one surface come to disagree about what they are addressing.
            "next-cursor": result.cursor,
            **_sealed_note(),
        }
    )
