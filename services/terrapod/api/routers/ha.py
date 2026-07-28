"""Node identity and role, for leader/follower resolution (#960 phase 1, #1101).

`whoami` is what makes DNS-derived leadership work: a node probes the shared
name and asks whoever answers who they are. If the answer is itself, it owns
the name and is the leader.

**Unauthenticated by necessity.** The probe runs before any trust exists
between nodes — peer authentication is a later phase, and a node must be able
to ask this question of a name that may route to either side. What it discloses
is an operator-chosen node name and a role, neither of which is a secret. The
response is `no-store` so a CDN or WAF in front of the external name cannot
pin leadership to a stale answer.

It also serves the operator directly: "what does this node currently think it
is" should be answerable without reading logs.
"""

from fastapi import APIRouter, Response

from terrapod.services import ha_role

router = APIRouter(prefix="/ha", tags=["ha"])


@router.get("/whoami")
async def whoami(response: Response) -> dict:
    """Report this node's identity and the role it currently holds."""
    response.headers["Cache-Control"] = "no-store"
    node = ha_role.node_id()
    return {
        "data": {
            "type": "ha-nodes",
            "id": node or "unnamed",
            "attributes": {
                "node-id": node,
                "role": await ha_role.get_role(),
            },
        }
    }
