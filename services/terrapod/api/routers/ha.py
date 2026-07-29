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

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from terrapod.api.dependencies import AuthenticatedUser, require_admin_or_audit
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.services import component_status, ha_role, replication

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


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


@router.get("/status")
async def status(
    _user: AuthenticatedUser = Depends(require_admin_or_audit),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Whether this node is converging with its peer, and how much margin it has.

    Answered entirely from local state — no call to the peer. That keeps the
    endpoint fast, and keeps it working when the peer is the thing that has
    broken, which is exactly when somebody is reading it.

    The two sides of a pair need different numbers:

    - a **follower** needs to know its last pull succeeded and that no class is
      still mid-backfill (a node backfilling is not in sync, however recent its
      last cycle);
    - a **leader** needs to know how much margin its follower has — the oldest
      retained event creeping toward the retention window means the follower is
      close to falling off the end and having to backfill from scratch.

    Deliberately no "N events behind": that needs the peer's latest event id,
    and seconds-since-last-successful-pull is the more honest number anyway. A
    completed pull that returned nothing means caught up *as of then*, which is
    what a human is actually asking.
    """
    node = ha_role.node_id()
    role = await ha_role.get_role()
    cfg = settings.ha
    state = await replication.read_status(db)
    components = await component_status.read()

    now = datetime.now(UTC)
    since_sync = (
        int((now - state.last_sync_at.astimezone(UTC)).total_seconds())
        if state.last_sync_at
        else None
    )
    oldest_age = (
        int((now - state.oldest_event_at.astimezone(UTC)).total_seconds())
        if state.oldest_event_at
        else None
    )

    return {
        "data": {
            "type": "ha-status",
            "id": node or "unnamed",
            "attributes": {
                "node-id": node,
                "role": role,
                "peer-configured": bool(cfg.peer.url),
                "replication-enabled": cfg.replication.enabled,
                # Follower side.
                "last-sync-at": _iso(state.last_sync_at),
                "seconds-since-last-sync": since_sync,
                "backfilling-classes": state.backfilling,
                "in-sync": cfg.replication.enabled
                and not state.backfilling
                and since_sync is not None,
                # Leader side — the follower's margin before it must backfill.
                "events-retained": state.events_retained,
                "oldest-event-age-seconds": oldest_age,
                "retention-seconds": cfg.replication.retention_days * 86400,
                "replicated-classes": sorted(replication.registered()),
                # In-cluster readiness (#1122). A pair that replicates
                # flawlessly is still not highly available if it serves from a
                # single API pod.
                "components": [
                    {"name": c.name, "ready": c.ready, "desired": c.desired}
                    for c in (components.components or [])
                ],
                "components-sampled-at": components.sampled_at,
                # Distinct from an empty list: "I cannot see" is not "nothing
                # is running", and an operator declining the Role is a normal
                # answer rather than a fault.
                "components-unavailable-reason": components.unavailable_reason,
                # Components on exactly one ready replica. Named rather than
                # left for the reader to derive, because it is the thing being
                # looked for.
                "single-replica-components": components.single_points_of_failure,
            },
        }
    }
