"""Which named storage locations this deployment has (#2547).

    GET /api/v1/storage/locations   — list · any authenticated

Terrapod-native: no CLI consumes it, so it is not on the TFE compatibility
surface.

It exists because a workspace's `storage-location` has been settable through the
API since the server half of #2547 landed, and **nothing could tell a client
which values are valid.** `validate_storage_location` refuses anything outside
`settings.storage.locations`, so without this route a picker cannot be populated
and the attribute stays API-only in practice -- reachable from `curl` and from
nowhere an operator looks.

**`default` is deliberately NOT in the list.** It is the absence of a location
rather than a location: `validate_storage_location` refuses the literal string
(#2621) precisely so a provider reading the server's answer back cannot write
`storage_location = "default"` into state and fail every apply. A caller offering
a choice therefore offers its own null option, labelled in its own words, and
this route answers only "what named locations exist". An empty list is the
honest answer for a deployment with none, and a caller should offer no control
at all rather than an empty select.

**Names and backends only.** A `StorageLocationConfig` also carries the bucket,
region, endpoint and a `credentials` block holding `role_arn` and `external_id`.
Nothing a picker does needs any of it, and the credential block sits directly
beside the addressing fields in the config model -- the kind of adjacency that
makes "while we are here, add the bucket" cheap to write and wrong to ship. The
backend is included because "eu-restricted (s3)" is materially more useful to
choose between than a bare name, and it discloses nothing the operator's own
Helm values do not.

Reads are open to any authenticated user, matching the execution-environment
listing, which is the same shape: an operator-configured set that a workspace
selects from. Worth saying why that is not a widening, since a location name can
encode a compliance boundary: `storage-location` is already on the workspace
document, so the name of every location IN USE is readable by anyone who can
read a workspace. This adds the names of locations configured but not yet used.
The alternative -- gating it -- buys nothing, because the set is derivable from
the workspaces a caller can already read, and costs the picker for every
non-admin who may legitimately set the attribute.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.api.pagination import paginate
from terrapod.logging_config import get_logger

router = APIRouter(tags=["storage"])
logger = get_logger(__name__)

_TYPE = "storage-locations"


@router.get("/storage/locations")
async def list_storage_locations(
    request: Request = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> JSONResponse:
    """The named storage locations a workspace may be assigned to."""
    # Read at call time rather than at import: `settings` is built when
    # `terrapod.config` is imported, and a test that overrides the locations
    # would otherwise be reading a snapshot taken before it did.
    from terrapod.config import settings

    items = [
        {
            "type": _TYPE,
            "id": name,
            "attributes": {"name": name, "backend": location.backend.value},
        }
        for name, location in sorted(settings.storage.locations.items())
    ]
    page_items, meta = paginate(items, request)
    return JSONResponse(content={"data": page_items, "meta": meta})
