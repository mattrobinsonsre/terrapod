"""Which Ansible execution environments this deployment can run (#2154).

    GET /api/v1/ansible/execution-environments   — list · any authenticated

Terrapod-native: no CLI consumes it, so it is not on the TFE compatibility
surface. It exists because the version picker cannot be a hand-kept list -- the
high end of the supported ansible-core range floats as upstream publishes new
lines, and a list baked into the code would mean a Terrapod release per ansible
release.

**Both kinds come back from one route, tagged.** A caller picking an environment
has to choose between the built-in images and whatever the operator has added,
and two routes would mean every caller merging two documents and getting the
precedence wrong somewhere. The precedence is not symmetric -- an operator label
overrides a workspace's `ansible_version` outright -- so it is stated here once.

`meta.listing` is load-bearing rather than diagnostic. The built-in set is read
from a registry, so it can be unknown, and a caller must be able to tell "this
deployment publishes nothing" from "I could not find out": with `unknown` the
write-time check accepts any line (#2154 D7), so a picker should let someone
type one rather than offering an empty select and implying there is nothing to
choose. Reads are open to any authenticated user -- the answer is which images a
deployment can pull, which every workspace editor needs and which reveals
nothing an operator's own Helm values do not.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, get_current_user
from terrapod.api.pagination import paginate
from terrapod.db.session import get_db
from terrapod.logging_config import get_logger
from terrapod.services import ansible_ee_service

router = APIRouter(tags=["ansible"])
logger = get_logger(__name__)

_TYPE = "ansible-execution-environments"


@router.get("/ansible/execution-environments")
async def list_execution_environments(
    request: Request = None,
    user: AuthenticatedUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """The selectable execution environments, built-in and operator-supplied."""
    available = await ansible_ee_service.availability(db)

    items: list[dict] = []
    for line in available.lines or []:
        items.append(
            {
                "type": _TYPE,
                "id": line,
                "attributes": {
                    "kind": ansible_ee_service.KIND_BUILTIN,
                    "ansible-core-line": line,
                    "label": None,
                    # The reference a workspace selecting this line would run,
                    # resolved by the same single resolver the run path uses --
                    # so what is shown here cannot drift from what is executed.
                    "image": ansible_ee_service.effective_ee_image(
                        ansible_version=line, ee_label=None
                    ),
                },
            }
        )
    for label, uri in sorted(available.environments.items()):
        items.append(
            {
                "type": _TYPE,
                "id": label,
                "attributes": {
                    "kind": ansible_ee_service.KIND_OPERATOR,
                    "ansible-core-line": None,
                    "label": label,
                    "image": uri,
                },
            }
        )

    page_items, meta = paginate(items, request)
    # "complete" vs "unknown" rather than a boolean, because the next state this
    # may need is "partial" (a listing we trust for one source and not another),
    # and a boolean would have to be deprecated to say it.
    meta["listing"] = "complete" if available.listing_is_authoritative else "unknown"
    return JSONResponse(content={"data": page_items, "meta": meta})
