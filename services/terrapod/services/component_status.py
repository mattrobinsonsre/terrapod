"""In-cluster component readiness (#1122).

"HA" means two things to an operator, and the pair is only one of them. A
deployment that replicates flawlessly is still not highly available if it is
serving from a single API pod. This answers the second question: **is any
component a single point of failure right now?**

Reads the Kubernetes API with a namespace-scoped Role the chart grants the API
ServiceAccount. That the API could not previously see this was a consequence of
permissions we choose, not of the architecture — the listener has carried a
comparable Role since it shipped.

**Two sources, for a real reason.** API and web pods are always co-located with
the API pod answering the request, so Kubernetes sees them. Listeners may be in
a different cluster entirely — that is the whole point of the ARC design — so
their replica counts keep coming from the Redis heartbeats that already cross
that boundary. Reporting a listener as absent because it is merely elsewhere
would be worse than not reporting it.

**Sampled on a timer, never on the request path.** The Kubernetes client is
synchronous, so a per-request read would both stall the event loop and hammer
the cluster API every time somebody polls the status endpoint.

**It reports readiness, not a verdict.** "2 of 3 API replicas ready" is a fact.
"HA is configured correctly" would be a claim this cannot support —
PodDisruptionBudgets, anti-affinity and topology spread are chart-level and not
all visible from a pod list.
"""

import asyncio
import json
from dataclasses import asdict, dataclass, field

import structlog

from terrapod.config import settings

logger = structlog.get_logger(__name__)

#: Components the chart deploys with an `app.kubernetes.io/component` label.
#: Listeners are deliberately absent — see the module docstring.
COMPONENTS = ("api", "web")

#: Where the sample lives so every replica answers identically without each
#: one hammering the cluster API. TTL is generous relative to the sample
#: interval: a stale count is better than a missing one, and staleness is
#: visible through `sampled-at`.
_CACHE_KEY = "tp:component_status"
_CACHE_TTL = 600

_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"


@dataclass
class ComponentReplicas:
    """One component's readiness and placement.

    ``desired`` comes from the Deployment rather than being inferred from the
    pod list — that is what makes `1/3` legible as an incident rather than as a
    deliberately small deployment.

    ``nodes`` and ``zones`` are **observations**, never judgements. Three
    replicas on one node is inevitable on a single-node cluster and a real gap
    on a four-node one; which it is depends on the cluster, so the finding is
    raised elsewhere and only when the spread was actually achievable.
    ``zones`` is None when node labels are not readable.
    """

    name: str
    ready: int = 0
    desired: int = 0
    #: Distinct nodes hosting this component's ready pods.
    nodes: int = 0
    #: Distinct zones, or None when node labels are not readable.
    zones: int | None = None
    #: Name of the PodDisruptionBudget covering it, or "" if none.
    pdb: str = ""
    #: Whether that PDB actually permits a voluntary eviction. A budget that
    #: permits none looks protective and is the opposite — it blocks node
    #: drains rather than making them safe.
    pdb_permits_disruption: bool | None = None


@dataclass
class Finding:
    """A specific, actionable gap — never a verdict on the deployment.

    Only raised where the cluster could have done better. "Every replica on one
    node" is not a finding on a one-node cluster, and "one zone" is not a
    finding where there is one zone.
    """

    component: str
    kind: str
    detail: str


@dataclass
class ComponentStatus:
    #: None when the sample could not be taken. Deliberately distinct from an
    #: empty list: "I cannot see" is not "nothing is running", and conflating
    #: them would report a healthy deployment as dead the moment a permission
    #: was missing.
    components: list[ComponentReplicas] | None = None
    sampled_at: str | None = None
    #: Cluster shape, and the reason findings can be raised at all. None means
    #: node reads were declined — in which case concentration is reported but
    #: never called a problem, because we cannot know it was avoidable.
    schedulable_nodes: int | None = None
    cluster_zones: int | None = None
    findings: list[Finding] = field(default_factory=list)
    #: Set when the Role is absent. Not an error — an operator may decline the
    #: permission, and the honest answer is then "unknown".
    unavailable_reason: str = ""

    @property
    def single_points_of_failure(self) -> list[str]:
        """Components running exactly one ready replica."""
        return [c.name for c in self.components or [] if c.ready == 1]


def _namespace() -> str:
    """The namespace this pod runs in."""
    if settings.ha.component_status.namespace:
        return settings.ha.component_status.namespace
    try:
        with open(_NAMESPACE_FILE) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def _observed_nodes(core, namespace: str, instance: str) -> int:
    """Distinct nodes hosting ANY pod we can already see in this namespace.

    The fallback when cluster-scoped node reads are declined, and safe because
    it is **asymmetric**: seeing two nodes PROVES spread was possible; seeing
    one proves nothing, because every pod may simply have landed together.

    That asymmetry points the right way — proof is only needed to *raise* a
    finding, never to suppress one. It under-reports (a four-node cluster whose
    pods all happen to sit on one node yields nothing) and never false-reports,
    which is the trade this whole feature is built around.
    """
    selector = f"app.kubernetes.io/instance={instance}" if instance else None
    try:
        pods = core.list_namespaced_pod(namespace, label_selector=selector)
    except Exception:  # noqa: BLE001
        return 0
    return len({p.spec.node_name for p in pods.items if p.spec and p.spec.node_name})


def _read_cluster_shape(core) -> tuple[int | None, int | None, dict[str, str]]:
    """Schedulable nodes, distinct zones, and a node -> zone map.

    Returns ``(None, None, {})`` when node reads are declined — deliberately
    distinct from ``(1, 1, ...)``. Not knowing the cluster's shape means no
    concentration finding can be raised, because we cannot tell an inevitable
    single node from an avoidable one.
    """
    if not settings.ha.component_status.read_nodes:
        return None, None, {}
    try:
        nodes = core.list_node()
    except Exception:  # noqa: BLE001 — a declined ClusterRole is a normal answer
        return None, None, {}

    zone_of: dict[str, str] = {}
    schedulable = 0
    for node in nodes.items:
        # An unschedulable node cannot host a replica, so counting it would
        # invent spread that was never available.
        if (node.spec and node.spec.unschedulable) or False:
            continue
        schedulable += 1
        labels = node.metadata.labels or {}
        zone = labels.get("topology.kubernetes.io/zone") or labels.get(
            "failure-domain.beta.kubernetes.io/zone"
        )
        if zone:
            zone_of[node.metadata.name] = zone

    # None and 1 mean genuinely different things and must not be conflated:
    #
    #   None -> no node carries a zone label. Bare metal, on-prem, an unzoned
    #           environment. Nothing can be said about zone redundancy, so
    #           nothing is.
    #   1    -> labels ARE present and every node reports the same zone. That
    #           is a zoned environment with the whole cluster in one AZ, which
    #           is worth saying even though no scheduling choice fixes it.
    zones = set(zone_of.values())
    return schedulable, (len(zones) if zones else None), zone_of


def _pdb_permits_disruption(pdb, ready: int) -> bool:
    """Whether a voluntary eviction is possible under this budget.

    `status.disruptionsAllowed` is the cluster's own answer and is preferred.
    Falling back to the spec matters because the status is briefly absent on a
    freshly created PDB, and reporting "blocks eviction" for a few seconds
    after every deploy would be noise.
    """
    allowed = getattr(pdb.status, "disruptions_allowed", None) if pdb.status else None
    if allowed is not None:
        return allowed > 0

    spec = pdb.spec
    if spec.max_unavailable is not None:
        return str(spec.max_unavailable) not in ("0", "0%")
    if spec.min_available is not None and ready:
        raw = str(spec.min_available)
        if raw.endswith("%"):
            return int(raw[:-1]) < 100
        return int(raw) < ready
    return True


def _sample_blocking(
    namespace: str, instance: str
) -> tuple[list[ComponentReplicas], int | None, int | None]:
    """Read pod readiness, Deployment intent, PDB coverage and placement.

    Synchronous — see the module docstring; the caller offloads it.
    """
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    core = client.CoreV1Api()
    apps = client.AppsV1Api()
    policy = client.PolicyV1Api()

    schedulable_nodes, cluster_zones, zone_of = _read_cluster_shape(core)
    if schedulable_nodes is None:
        # Node reads declined. Fall back to what our own pods prove — see
        # `_observed_nodes`. Zones have no equivalent: the labels live only on
        # Nodes, so zone spread stays genuinely unknown.
        observed = _observed_nodes(core, namespace, instance)
        if observed > 1:
            schedulable_nodes = observed

    try:
        budgets = policy.list_namespaced_pod_disruption_budget(namespace).items
    except Exception:  # noqa: BLE001 — reported as "no PDB visible", not an error
        budgets = []

    out: list[ComponentReplicas] = []

    for component in COMPONENTS:
        selector = f"app.kubernetes.io/component={component}"
        if instance:
            selector += f",app.kubernetes.io/instance={instance}"

        pods = core.list_namespaced_pod(namespace, label_selector=selector)
        ready_pods = [
            pod
            for pod in pods.items
            if any(c.type == "Ready" and c.status == "True" for c in (pod.status.conditions or []))
        ]

        deployments = apps.list_namespaced_deployment(namespace, label_selector=selector)
        desired = sum(d.spec.replicas or 0 for d in deployments.items)

        node_names = {p.spec.node_name for p in ready_pods if p.spec and p.spec.node_name}
        zones = len({zone_of[n] for n in node_names if n in zone_of}) or None if zone_of else None

        # Match a PDB by its selector against this component's label, rather
        # than by name — an operator's own PDB is as valid as the chart's.
        covering = next(
            (
                b
                for b in budgets
                if (b.spec.selector.match_labels or {}).get("app.kubernetes.io/component")
                == component
            ),
            None,
        )

        out.append(
            ComponentReplicas(
                name=component,
                ready=len(ready_pods),
                desired=desired,
                nodes=len(node_names),
                zones=zones,
                pdb=covering.metadata.name if covering else "",
                pdb_permits_disruption=(
                    _pdb_permits_disruption(covering, len(ready_pods)) if covering else None
                ),
            )
        )

    return out, schedulable_nodes, cluster_zones


def derive_findings(
    components: list[ComponentReplicas],
    schedulable_nodes: int | None,
    cluster_zones: int | None,
) -> list[Finding]:
    """Raise a finding only where the cluster could have done better.

    This is the whole safety property. A single-node k3s or kind cluster puts
    every replica on one node necessarily; an on-prem or single-AZ deployment
    cannot spread across zones. Reporting either would be a false finding, and
    false findings in an HA readout are worse than silence — they teach an
    operator to ignore it.
    """
    findings: list[Finding] = []

    # Cluster-level, and distinct from any component's placement. A zoned
    # environment whose every node sits in one availability zone is not
    # something Terrapod can schedule around — but it IS a real gap in the
    # deployment, and the zone labels are what make it safe to say. With no
    # labels at all (`cluster_zones is None`) the environment is unzoned and
    # nothing is claimed.
    if cluster_zones == 1:
        findings.append(
            Finding(
                component="cluster",
                kind="single-zone-cluster",
                detail=(
                    "every node reports the same availability zone — the cluster itself "
                    "is not zone-redundant, so no placement of replicas can survive "
                    "losing that zone"
                ),
            )
        )

    for c in components:
        if c.ready <= 1:
            # A single-replica component is already reported through
            # `single-replica-components`; piling on adds noise, not signal.
            continue

        if not c.pdb:
            findings.append(
                Finding(
                    component=c.name,
                    kind="no-pdb",
                    detail=(
                        f"{c.ready} replicas with no PodDisruptionBudget — "
                        "a node drain can evict all of them at once"
                    ),
                )
            )
        elif c.pdb_permits_disruption is False:
            findings.append(
                Finding(
                    component=c.name,
                    kind="pdb-blocks-eviction",
                    detail=(
                        f"PodDisruptionBudget {c.pdb} permits no voluntary eviction, "
                        "so a node drain will stall rather than proceed safely"
                    ),
                )
            )

        # Concentration is only a finding when spread was available.
        if schedulable_nodes is not None and schedulable_nodes > 1 and c.nodes == 1:
            findings.append(
                Finding(
                    component=c.name,
                    kind="node-concentration",
                    detail=(
                        f"{c.ready} replicas on 1 node, with at least {schedulable_nodes} "
                        "available — losing that node loses the component"
                    ),
                )
            )

        if cluster_zones is not None and cluster_zones > 1 and c.zones == 1:
            findings.append(
                Finding(
                    component=c.name,
                    kind="zone-concentration",
                    detail=(f"{c.ready} replicas in 1 zone, with {cluster_zones} available"),
                )
            )

    return findings


async def sample() -> ComponentStatus:
    """Take a reading and cache it. Never raises."""
    from datetime import UTC, datetime

    namespace = _namespace()
    if not namespace:
        return ComponentStatus(unavailable_reason="not running in a Kubernetes namespace")

    try:
        components, schedulable_nodes, cluster_zones = await asyncio.to_thread(
            _sample_blocking, namespace, settings.ha.component_status.instance
        )
    except Exception as exc:  # noqa: BLE001 — a missing Role is a normal answer
        # An operator may legitimately decline the permission, so this is
        # reported rather than raised. Logged at info for the same reason.
        logger.info("Component status unavailable", error=str(exc))
        return ComponentStatus(unavailable_reason=str(exc)[:200])

    status = ComponentStatus(
        components=components,
        sampled_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        schedulable_nodes=schedulable_nodes,
        cluster_zones=cluster_zones,
        findings=derive_findings(components, schedulable_nodes, cluster_zones),
    )
    await _store(status)
    return status


async def _store(status: ComponentStatus) -> None:
    try:
        from terrapod.redis.client import get_redis_client

        await get_redis_client().set(_CACHE_KEY, json.dumps(asdict(status)), ex=_CACHE_TTL)
    except Exception:  # noqa: BLE001
        logger.debug("Could not cache component status", exc_info=True)


async def read() -> ComponentStatus:
    """The most recent sample, from whichever replica took it."""
    if not settings.ha.component_status.enabled:
        return ComponentStatus(unavailable_reason="component status reporting is disabled")
    try:
        from terrapod.redis.client import get_redis_client

        raw = await get_redis_client().get(_CACHE_KEY)
    except Exception:  # noqa: BLE001
        return ComponentStatus(unavailable_reason="cache unreachable")

    if not raw:
        return ComponentStatus(unavailable_reason="not sampled yet")

    data = json.loads(raw)
    comps = data.get("components")
    return ComponentStatus(
        components=[ComponentReplicas(**c) for c in comps] if comps is not None else None,
        sampled_at=data.get("sampled_at"),
        unavailable_reason=data.get("unavailable_reason", ""),
        schedulable_nodes=data.get("schedulable_nodes"),
        cluster_zones=data.get("cluster_zones"),
        findings=[Finding(**f) for f in data.get("findings", [])],
    )


async def sample_cycle() -> None:
    """Periodic task. Refreshes the shared sample and the gauges."""
    if not settings.ha.component_status.enabled:
        return

    status = await sample()
    if status.components is None:
        return

    from terrapod.api.metrics import COMPONENT_DESIRED_REPLICAS, COMPONENT_READY_REPLICAS

    for component in status.components:
        COMPONENT_READY_REPLICAS.labels(component=component.name).set(component.ready)
        COMPONENT_DESIRED_REPLICAS.labels(component=component.name).set(component.desired)


__all__ = [
    "COMPONENTS",
    "ComponentReplicas",
    "ComponentStatus",
    "Finding",
    "derive_findings",
    "read",
    "sample",
    "sample_cycle",
]
