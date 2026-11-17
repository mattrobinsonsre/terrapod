"""Terraform/tofu CLI binary cache + provider cache admin endpoints.

Provides pull-through caching for terraform and tofu CLI binaries.
Runner Jobs fetch their binary from here at startup instead of using
images with a baked-in binary version.

Also provides admin endpoints for the provider binary cache (network mirror).

UX CONTRACT: Admin cache endpoints are consumed by the web frontend:
  - web/src/app/admin/binary-cache/page.tsx (list, warm, purge for both caches)
  Changes to response shapes, attribute names, or status codes here MUST be
  matched by corresponding updates to that frontend page.

Endpoints:
    GET    /api/terrapod/v1/platform-tools                                                    — pinned opa/trivy/checkov versions
    GET    /api/terrapod/v1/binary-cache/{tool}/{version}/{os}/{arch}                        — download (redirect)
    GET    /api/terrapod/v1/admin/binary-cache                                                — list cached binaries
    POST   /api/terrapod/v1/admin/binary-cache/warm                                           — pre-warm binary
    DELETE /api/terrapod/v1/admin/binary-cache/{tool}/{version}                               — purge binary
    GET    /api/terrapod/v1/admin/provider-cache                                              — list cached providers
    DELETE /api/terrapod/v1/admin/provider-cache/{hostname}/{namespace}/{type}/{version}      — purge provider
"""

import asyncio
import os

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user, require_admin
from terrapod.api.pagination import paginate
from terrapod.api.serialization import rfc3339
from terrapod.config import WarmBinaryEntry, WarmProviderEntry, settings
from terrapod.db.session import get_db
from terrapod.http_retry import exception_diagnostic, redact_urls
from terrapod.logging_config import get_logger
from terrapod.services.binary_cache_service import (
    get_or_cache_binary,
    get_or_cache_sums,
    list_available_versions,
    list_cached_binaries,
    purge_binary,
    resolve_version,
    warm_binary,
)
from terrapod.services.cache_errors import CacheOnlyError
from terrapod.services.cache_warm_service import warm_from_manifest
from terrapod.services.provider_cache_service import (
    list_cached_providers,
    purge_cached_provider,
)
from terrapod.storage import get_storage
from terrapod.storage.protocol import ObjectStore

router = APIRouter(tags=["binary-cache"])
logger = get_logger(__name__)


class WarmBinaryRequest(BaseModel):
    tool: str = "terraform"
    version: str
    os: str = "linux"
    arch: str = "amd64"


# --- Platform tools ---


@router.get("/platform-tools")
async def platform_tool_versions(
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> JSONResponse:
    """The opa/trivy/checkov versions this deployment pins, plus Pulumi's default.

    The runner reads this to know what to ask the binary cache for. A platform
    tool is a platform-scoped setting, so there is one answer for the whole
    deployment and it does not vary by workspace or run — which is why this is a
    plain read rather than another field on the per-run payload.

    Pulumi stopped being one of those in #1559: its version is the workspace's
    `engine-version` now, and it reaches the runner in the run's own env. The key
    stays here, answering with the deployment *default*, because a listener up to
    N-2 minors behind does not emit that env var and this is the only thing it
    knows to ask. Dropping the key would strand exactly those runs.

    Available to any authenticated caller, runner tokens included: the pinned
    version of a third-party tool is not sensitive, and the runner has to be able
    to ask for it before it can fetch anything.
    """
    cfg = settings.registry.platform_tools
    return JSONResponse(
        content={
            "data": {
                "type": "platform-tools",
                "id": "default",
                "attributes": {
                    "opa-version": cfg.opa_version,
                    "trivy-version": cfg.trivy_version,
                    "checkov-version": cfg.checkov_version,
                    # Not a pin any more (#1559) — the deployment default, for a
                    # runner whose listener predates the per-run env var.
                    "pulumi-version": settings.default_pulumi_version,
                    # The Node a Pulumi TypeScript/JavaScript program runs on
                    # (#1566). Partial, resolved by the cache like the rest.
                    "node-version": settings.default_node_version,
                    "go-version": settings.default_go_version,
                    "dotnet-version": settings.default_dotnet_version,
                },
            }
        }
    )


# --- Version suggestions ---


@router.get("/binary-cache/versions")
async def available_versions(
    tool: str = "tofu",
    user: AuthenticatedUser = Depends(get_current_user),
) -> JSONResponse:
    """List available stable versions for a tool.

    Returns version strings sorted newest first, including major.minor shortcuts.
    Cached in Redis for 1 hour. Any authenticated user can access.
    """
    try:
        versions = await list_available_versions(tool)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=redact_urls(str(e))
        ) from e
    except Exception as e:
        logger.error("Failed to fetch versions", tool=tool, error=exception_diagnostic(e))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch versions for {tool}: {exception_diagnostic(e)}",
        ) from e

    return JSONResponse(content={"data": versions})


# --- Runner-facing endpoint ---


@router.get("/binary-cache/{tool}/{version}/{os}/{arch}")
async def download_binary(
    tool: str,
    version: str,
    os: str,
    arch: str,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> RedirectResponse:
    """Get a terraform/tofu binary, pulling through from upstream on cache miss.

    Returns a redirect to a presigned download URL. Used by runner Jobs
    to fetch the exact binary version needed for a workspace.
    Requires authentication (runner token, API token, or session).
    """
    if not settings.registry.binary_cache.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Binary cache is disabled",
        )

    try:
        resolved = await resolve_version(tool, version)
        url = await get_or_cache_binary(db, storage, tool, resolved, os, arch)
    except CacheOnlyError as e:
        # Sealed mode + cache miss — genuinely not available; don't fetch upstream.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=redact_urls(str(e))
        ) from e
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=redact_urls(str(e))
        ) from e
    except Exception as e:
        logger.error(
            "Failed to fetch binary",
            tool=tool,
            version=version,
            os=os,
            arch=arch,
            error=exception_diagnostic(e),
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch {tool} {version}: {exception_diagnostic(e)}",
        ) from e

    await db.commit()
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


@router.get("/binary-cache/{tool}/{version}/sha256sums")
async def get_binary_sha256sums(
    tool: str,
    version: str,
    user: AuthenticatedUser = Depends(get_current_user),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Serve the publisher SHA256SUMS manifest for a tool/version (#607).

    The runner fetches this (and `.sig`) to independently verify the executable
    against the publisher's signature with its own pinned key. Lazily warmed
    from upstream if not yet persisted. Same source as the binary (cache path).
    """
    if not settings.registry.binary_cache.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Binary cache is disabled"
        )
    try:
        resolved = await resolve_version(tool, version)
        manifest, _sig = await get_or_cache_sums(storage, tool, resolved)
    except CacheOnlyError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=redact_urls(str(e))
        ) from e
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=redact_urls(str(e))
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to obtain SHA256SUMS for {tool} {version}: {exception_diagnostic(e)}",
        ) from e
    return Response(content=manifest, media_type="text/plain")


@router.get("/binary-cache/{tool}/{version}/sha256sums.sig")
async def get_binary_sha256sums_sig(
    tool: str,
    version: str,
    user: AuthenticatedUser = Depends(get_current_user),
    storage: ObjectStore = Depends(get_storage),
) -> Response:
    """Serve the detached GPG signature over the SHA256SUMS manifest (#607)."""
    if not settings.registry.binary_cache.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Binary cache is disabled"
        )
    try:
        resolved = await resolve_version(tool, version)
        _manifest, sig = await get_or_cache_sums(storage, tool, resolved)
    except CacheOnlyError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=redact_urls(str(e))
        ) from e
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=redact_urls(str(e))
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to obtain SHA256SUMS signature for {tool} {version}: {exception_diagnostic(e)}",
        ) from e
    return Response(content=sig, media_type="application/pgp-signature")


# --- Admin endpoints ---


@router.get("/admin/binary-cache")
async def list_cached_binaries_endpoint(
    tool: str | None = None,
    request: Request = None,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all cached binaries."""
    entries = await list_cached_binaries(db, tool=tool)
    items = [
        {
            "id": str(e.id),
            "type": "cached-binaries",
            "attributes": {
                "tool": e.tool,
                "version": e.version,
                "os": e.os,
                "arch": e.arch,
                "shasum": e.shasum,
                "download-url": e.download_url,
                "cached-at": rfc3339(e.cached_at),
                # Provenance (#2599). A binary is EXECUTED on every run, so where
                # it came from is the most consequential thing this listing says:
                # `operator-upload` means a person vouched for it instead of the
                # publisher's signed SHA256SUMS the fetch path checks.
                "attestation": e.attestation,
                "uploaded-by": e.uploaded_by,
                "upload-reference": e.upload_reference,
                "replaced-digest": e.replaced_digest,
                "replaced-at": rfc3339(e.replaced_at) if e.replaced_at else None,
            },
        }
        for e in entries
    ]
    page_items, meta = paginate(items, request)
    return JSONResponse(content={"data": page_items, "meta": meta})


@router.post("/admin/binary-cache/artifacts", status_code=201)
async def upload_binary_artifact(
    tool: str = Form(..., description="tofu, terraform, terragrunt, opa, trivy, checkov, ..."),
    version: str = Form(..., description="The exact version, as the publisher names it."),
    os_: str = Form(..., alias="os", description="linux, darwin, windows."),
    arch: str = Form(..., description="amd64, arm64."),
    file: UploadFile = File(..., description="The binary's bytes."),
    reference: str = Form(
        "", description="Free text: the operator's own ticket or approval record."
    ),
    replace: bool = Form(
        False,
        description=(
            "Overwrite a binary already at these coordinates. Without it such an upload "
            "is refused with 409, because what it would displace is what every run on "
            "this version executes."
        ),
    ),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Put an engine or platform-tool binary an operator vetted into the cache.

    **This is the most consequential upload Terrapod accepts: the binary is
    executed on every run.** The fetch path verifies one against the publisher's
    signed SHA256SUMS before recording it (#607) and an upload has no signature to
    check -- which is the posture, not a hole in it. What stands in for the
    signature is provenance: the row records `operator-upload`, who uploaded it,
    and their reference, so an audit names the person who vouched rather than
    assuming a publisher relationship nobody re-checks.

    It is also the upload a sealed deployment most needs: without it a sealed node
    cannot be given `tofu` itself, and no run can execute at all.
    """
    from terrapod.api.routers.cache_admin import _sealed_note
    from terrapod.api.upload_stream import stream_upload_to_tempfile
    from terrapod.services import binary_cache_uploads, cache_admission
    from terrapod.services.package_cache import substrate, uploads

    tmp_path, size = await stream_upload_to_tempfile(
        file, suffix=".binary-upload", max_bytes=uploads.MAX_UPLOAD_BYTES
    )
    try:
        if size == 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Empty upload. The binary's bytes go in the `file` part; the "
                    "coordinates are the other form fields."
                ),
            )
        try:
            record, outcome, previous = await binary_cache_uploads.admit(
                db,
                storage,
                tool=tool,
                version=version,
                os_=os_,
                arch=arch,
                tmp_path=tmp_path,
                uploaded_by=user.email or "",
                reference=reference,
                replace=replace,
            )
        except substrate.CoordinateError as exc:
            # 422, not 404: the request is well-formed and the coordinates are not
            # ones this cache can address.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except binary_cache_uploads.BinaryUploadConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        replaced = outcome is cache_admission.Outcome.REPLACE
        unchanged = outcome is cache_admission.Outcome.UNCHANGED
        meta: dict[str, object] = {"replaced": replaced, "unchanged": unchanged}
        if replaced:
            meta["previous-digest"] = previous
            # A replacement DELETED bytes that a sealed node cannot fetch back.
            meta.update(_sealed_note())
        body = {
            "data": {
                "id": str(record.id),
                "type": "cached-binaries",
                "attributes": {
                    "tool": record.tool,
                    "version": record.version,
                    "os": record.os,
                    "arch": record.arch,
                    "shasum": record.shasum,
                    "attestation": record.attestation,
                    "uploaded-by": record.uploaded_by,
                    "upload-reference": record.upload_reference,
                    "cached-at": rfc3339(record.cached_at),
                },
            },
            "meta": meta,
        }
        await db.commit()
    finally:
        try:
            await asyncio.to_thread(os.unlink, tmp_path)
        except OSError:
            pass

    return JSONResponse(status_code=200 if (replaced or unchanged) else 201, content=body)


@router.post("/admin/binary-cache/warm")
async def warm_binary_endpoint(
    body: WarmBinaryRequest,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Pre-warm a specific binary version into the cache."""
    if not settings.registry.binary_cache.enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Binary cache is disabled",
        )

    try:
        url = await warm_binary(db, storage, body.tool, body.version, body.os, body.arch)
    except CacheOnlyError as e:
        # Sealed mode can't fetch upstream — warming must run before sealing.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=redact_urls(str(e))
        ) from e
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=redact_urls(str(e))
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to warm binary {body.tool} {body.version}: {exception_diagnostic(e)}",
        ) from e

    await db.commit()
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "cached", "download_url": url},
    )


class BulkWarmRequest(BaseModel):
    """Bulk cache pre-population request — warm many binaries + provider
    platforms in one call. Powers the admin UI bulk-warm and automation."""

    binaries: list[WarmBinaryEntry] = []
    providers: list[WarmProviderEntry] = []


@router.post("/admin/binary-cache/warm-bulk")
async def warm_bulk_endpoint(
    body: BulkWarmRequest,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Warm a batch of binaries and/or provider platforms.

    Resilient: each (entry, platform) is warmed independently and reported back
    with its own ok/error, so one missing version doesn't fail the whole batch.
    Returns HTTP 200 with per-entry results even when some entries failed; the
    caller inspects `failed`/`results` to surface partial outcomes.
    """
    if not body.binaries and not body.providers:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No entries to warm — provide at least one binary or provider.",
        )

    summary = await warm_from_manifest(db, storage, body.binaries, body.providers)
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "total": summary.total,
            "succeeded": summary.succeeded,
            "failed": summary.failed,
            "results": [
                {"kind": r.kind, "ref": r.ref, "ok": r.ok, "error": redact_urls(r.error)}
                for r in summary.results
            ],
        },
    )


@router.delete("/admin/binary-cache/{tool}/{version}")
async def purge_binary_endpoint(
    tool: str,
    version: str,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Purge all cached binaries for a tool+version."""
    count = await purge_binary(db, storage, tool, version)
    await db.commit()
    return JSONResponse(
        content={"status": "purged", "count": count},
    )


# --- Provider cache admin endpoints ---


@router.get("/admin/provider-cache")
async def list_cached_providers_endpoint(
    hostname: str | None = None,
    request: Request = None,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """List all cached provider binaries."""
    entries = await list_cached_providers(db, hostname=hostname)
    items = [
        {
            "id": str(e.id),
            "type": "cached-providers",
            "attributes": {
                "hostname": e.hostname,
                "namespace": e.namespace,
                "provider-type": e.type,
                "version": e.version,
                "os": e.os,
                "arch": e.arch,
                "shasum": e.shasum,
                "cached-at": rfc3339(e.cached_at),
                # Provenance (#2599). For a provider the h1 is what `terraform
                # init` checks, so "who vouched for these bytes" is the question
                # an audit asks of this listing.
                "attestation": e.attestation,
                "uploaded-by": e.uploaded_by,
                "upload-reference": e.upload_reference,
                "replaced-digest": e.replaced_digest,
                "replaced-at": rfc3339(e.replaced_at) if e.replaced_at else None,
            },
        }
        for e in entries
    ]
    page_items, meta = paginate(items, request)
    return JSONResponse(content={"data": page_items, "meta": meta})


@router.post("/admin/provider-cache/artifacts", status_code=201)
async def upload_provider_artifact(
    hostname: str = Form(..., description="The registry host, e.g. registry.terraform.io."),
    namespace: str = Form(..., description="The publisher namespace, e.g. hashicorp."),
    type_: str = Form(..., alias="type", description="The provider type, e.g. aws."),
    version: str = Form(..., description="The version, without a leading v."),
    os_: str = Form(..., alias="os", description="linux, darwin, windows."),
    arch: str = Form(..., description="amd64, arm64."),
    file: UploadFile = File(..., description="The publisher's provider .zip."),
    reference: str = Form(
        "", description="Free text: the operator's own ticket or approval record."
    ),
    replace: bool = Form(
        False,
        description=(
            "Overwrite an archive already at these coordinates. Without it such an "
            "upload is refused with 409, because what it would displace may be what a "
            "run already depends on."
        ),
    ),
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Put a provider archive an operator vetted into the cache.

    The other half of what a sealed deployment needs: given `tofu` and no
    providers, a run still cannot plan anything.

    **The h1 hash is computed from the uploaded zip, never accepted from the
    uploader.** It is what `terraform init` compares a provider against via a
    workspace's `.terraform.lock.hcl`, so a hand-supplied value that did not match
    would surface as an unexplained `init` failure on somebody else's workspace.
    Computing it also validates the archive: a file that is not a readable zip
    cannot produce one and is refused before anything is stored.
    """
    from terrapod.api.routers.cache_admin import _sealed_note
    from terrapod.api.upload_stream import stream_upload_to_tempfile
    from terrapod.services import cache_admission, provider_cache_uploads
    from terrapod.services.package_cache import substrate, uploads

    tmp_path, size = await stream_upload_to_tempfile(
        file, suffix=".provider-upload", max_bytes=uploads.MAX_UPLOAD_BYTES
    )
    try:
        if size == 0:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Empty upload. The archive's bytes go in the `file` part; the "
                    "coordinates are the other form fields."
                ),
            )
        try:
            record, outcome, previous = await provider_cache_uploads.admit(
                db,
                storage,
                hostname=hostname,
                namespace=namespace,
                type_=type_,
                version=version,
                os_=os_,
                arch=arch,
                tmp_path=tmp_path,
                uploaded_by=user.email or "",
                reference=reference,
                replace=replace,
            )
        except substrate.CoordinateError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except provider_cache_uploads.NotAProviderArchive as exc:
            # 422 rather than 400: the request is well-formed and the BYTES are
            # not a provider archive, which is a statement about the content.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except provider_cache_uploads.ProviderUploadConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        replaced = outcome is cache_admission.Outcome.REPLACE
        unchanged = outcome is cache_admission.Outcome.UNCHANGED
        meta: dict[str, object] = {"replaced": replaced, "unchanged": unchanged}
        if replaced:
            meta["previous-digest"] = previous
            meta.update(_sealed_note())
        body = {
            "data": {
                "id": str(record.id),
                "type": "cached-providers",
                "attributes": {
                    "hostname": record.hostname,
                    "namespace": record.namespace,
                    # `provider-type`, not `type`: in JSON:API the resource's own
                    # `type` is a reserved member of the resource object, so an
                    # attribute of the same name is ambiguous to a client walking
                    # the document. The listing route already made that call, and
                    # one resource must not answer to two names depending on which
                    # route returned it.
                    "provider-type": record.type,
                    "version": record.version,
                    "os": record.os,
                    "arch": record.arch,
                    "filename": record.filename,
                    "shasum": record.shasum,
                    "h1-hash": record.h1_hash,
                    "attestation": record.attestation,
                    "uploaded-by": record.uploaded_by,
                    "upload-reference": record.upload_reference,
                    "cached-at": rfc3339(record.cached_at),
                },
            },
            "meta": meta,
        }
        await db.commit()
    finally:
        try:
            await asyncio.to_thread(os.unlink, tmp_path)
        except OSError:
            pass

    return JSONResponse(status_code=200 if (replaced or unchanged) else 201, content=body)


@router.delete("/admin/provider-cache/{hostname}/{namespace}/{type}/{version}")
async def purge_provider_endpoint(
    hostname: str,
    namespace: str,
    type: str,
    version: str,
    user: AuthenticatedUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    storage: ObjectStore = Depends(get_storage),
) -> JSONResponse:
    """Purge all cached platforms for a provider version."""
    count = await purge_cached_provider(db, storage, hostname, namespace, type, version)
    await db.commit()
    return JSONResponse(
        content={"status": "purged", "count": count},
    )
