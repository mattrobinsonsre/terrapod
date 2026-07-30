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

from terrapod.api.dependencies import (
    AuthenticatedUser,
    get_current_user,
    require_admin_or_audit,
)
from terrapod.config import settings
from terrapod.db.session import get_db
from terrapod.services import blob_readiness as blob_readiness_service
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
    user: AuthenticatedUser = Depends(get_current_user),
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

    **Any authenticated user may read the node's own disposition** — its role,
    whether a peer is configured, and whether it is converging (#1165). Hiding
    "you are talking to a follower" from the person whose next `apply` is about
    to be refused is the opposite of useful, and it is not a secret: the follower
    already says so, loudly, in the 503.

    The **in-cluster** half is different. Ready-vs-desired per component, node
    and zone concentration, and the disruption-budget findings describe the
    deployment rather than the node's own posture, so they stay `admin`/`audit`.
    A caller without that role gets `components-restricted: true` and empty
    lists — deliberately distinct from `components-unavailable-reason`, which
    means the cluster could not be read at all. "You may not see this" and "I
    cannot see this" are different answers and an operator debugging the second
    must not be shown the first.
    """
    node = ha_role.node_id()
    role = await ha_role.get_role()
    cfg = settings.ha
    state = await replication.read_status(db)

    privileged = bool({"admin", "audit"} & set(user.roles or []))
    # Not merely filtered out of the response: an unprivileged caller must not
    # cause the Kubernetes reads at all.
    components = await component_status.read() if privileged else None

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
                # How far behind, concretely (#1165). Null is "unknown" — never
                # pulled, or a peer that does not report it — and is
                # deliberately NOT the same as 0, which means caught up. Both
                # describe the last successful pull rather than this instant,
                # which is the honest thing they can describe without asking the
                # peer on the status path.
                "events-behind": state.events_behind,
                "behind-seconds": (
                    int((now - state.oldest_unapplied_at.astimezone(UTC)).total_seconds())
                    if state.oldest_unapplied_at
                    else None
                ),
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
                # True when the caller lacks admin/audit. Distinct from
                # `components-unavailable-reason` below — see the docstring.
                "components-restricted": not privileged,
                "components": [
                    {
                        "name": c.name,
                        "ready": c.ready,
                        "desired": c.desired,
                        # Observations, not judgements — whether concentration
                        # is a problem depends on the cluster, and that verdict
                        # lives in `ha-findings`.
                        "nodes": c.nodes,
                        "zones": c.zones,
                        "pdb": c.pdb,
                        "pdb-permits-disruption": c.pdb_permits_disruption,
                    }
                    for c in ((components.components or []) if components else [])
                ],
                # Cluster shape, and the reason a finding can be raised at all.
                # Null means node reads were declined: placement is reported
                # but never called a problem, because an inevitable single node
                # cannot be told from an avoidable one.
                "schedulable-nodes": components.schedulable_nodes if components else None,
                "cluster-zones": components.cluster_zones if components else None,
                # Specific and actionable, raised ONLY where the cluster could
                # have done better. Never a verdict on the deployment.
                "ha-findings": [
                    {"component": f.component, "kind": f.kind, "detail": f.detail}
                    for f in ((components.findings or []) if components else [])
                ],
                "components-sampled-at": components.sampled_at if components else None,
                # Distinct from an empty list: "I cannot see" is not "nothing
                # is running", and an operator declining the Role is a normal
                # answer rather than a fault.
                "components-unavailable-reason": (
                    components.unavailable_reason if components else None
                ),
                # Components on exactly one ready replica. Named rather than
                # left for the reader to derive, because it is the thing being
                # looked for.
                "single-replica-components": (
                    (components.single_points_of_failure or []) if components else []
                ),
            },
        }
    }


@router.get("/blob-readiness")
async def blob_readiness(
    full: bool = False,
    sample: int = blob_readiness_service.DEFAULT_SAMPLE,
    _user: AuthenticatedUser = Depends(require_admin_or_audit),
) -> dict:
    """Are the objects this node's rows name actually in its object store? (#1147)

    Separate from `/status` on purpose. `/status` is answered from local state in
    milliseconds and an operator refreshes it freely; this one makes real
    round trips to the object store, so putting it inline would make the cheap
    endpoint expensive and tempt callers to poll it.

    `full=true` checks every row of every class — thousands of requests on a real
    estate. The default samples, and the response says which it did: `sampled`
    plus per-class `checked` against `total-rows`, so nobody can read a clean
    sample as a clean estate.
    """
    result = await blob_readiness_service.check(full=full, sample=sample)

    return {
        "data": {
            "type": "ha-blob-readiness",
            "id": ha_role.node_id() or "unknown",
            "attributes": {
                # The honest headline. `missing-total` is a count over what was
                # CHECKED; `sampled` says whether that was everything.
                "sampled": result.sampled,
                "missing-total": result.missing_total,
                # The list that should stop a failover. Everything else is a
                # judgement call about a given deployment; this is not.
                "irreplaceable-missing": result.irreplaceable_missing,
                # The counterpart that makes the list above trustworthy: an
                # irreplaceable class that is configured off, or that no row
                # guarantees, produces zero missing objects — indistinguishable
                # from a pass unless it is named. On a sealed node this is where
                # the escalated caches surface.
                "irreplaceable-unchecked": result.irreplaceable_unchecked,
                "duration-ms": result.duration_ms,
                "unavailable-reason": result.unavailable_reason,
                "classes": [
                    {
                        "name": c.name,
                        # irreplaceable | history | rederivable, from #1114. The
                        # tier is a property of the deployment as much as the
                        # artifact: a cold provider cache re-warms itself unless
                        # the node is sealed, in which case it is fatal. This is
                        # the EFFECTIVE tier — sealing escalates it here rather
                        # than expecting the operator to remember the rule.
                        "tier": c.tier,
                        # off | verify | copy, from `ha.blobs`. Answers "why is
                        # this class empty" without a second look at the config.
                        "mode": c.mode,
                        # False when no row guarantees these objects exist, so
                        # presence cannot be derived from the database. A stated
                        # boundary of the method, not a gap to be filled later.
                        "verifiable": c.verifiable,
                        # Why nothing was checked, when nothing was.
                        "note": c.note,
                        "total-rows": c.total_rows,
                        "checked": c.checked,
                        "missing": c.missing,
                        # A few, not all. A wholly absent class would otherwise
                        # return a wall of keys nobody reads.
                        "missing-examples": c.missing_examples,
                        # True only when nothing was held back, so `missing` is
                        # the complete answer for this class rather than a
                        # sample's worth.
                        "complete": c.complete,
                        "error": c.error,
                    }
                    for c in result.classes
                ],
            },
        }
    }
