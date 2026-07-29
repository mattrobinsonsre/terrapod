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
from dataclasses import asdict, dataclass

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
    """One component's readiness.

    ``desired`` comes from the Deployment rather than being inferred from the
    pod list — that is what makes `1/3` legible as an incident rather than as a
    deliberately small deployment.
    """

    name: str
    ready: int = 0
    desired: int = 0


@dataclass
class ComponentStatus:
    #: None when the sample could not be taken. Deliberately distinct from an
    #: empty list: "I cannot see" is not "nothing is running", and conflating
    #: them would report a healthy deployment as dead the moment a permission
    #: was missing.
    components: list[ComponentReplicas] | None = None
    sampled_at: str | None = None
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


def _sample_blocking(namespace: str, instance: str) -> list[ComponentReplicas]:
    """Read pod readiness and Deployment intent. Synchronous — see the module
    docstring; the caller offloads it."""
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()

    core = client.CoreV1Api()
    apps = client.AppsV1Api()
    out: list[ComponentReplicas] = []

    for component in COMPONENTS:
        selector = f"app.kubernetes.io/component={component}"
        if instance:
            selector += f",app.kubernetes.io/instance={instance}"

        pods = core.list_namespaced_pod(namespace, label_selector=selector)
        ready = sum(
            1
            for pod in pods.items
            if any(c.type == "Ready" and c.status == "True" for c in (pod.status.conditions or []))
        )

        deployments = apps.list_namespaced_deployment(namespace, label_selector=selector)
        desired = sum(d.spec.replicas or 0 for d in deployments.items)

        # A component with no Deployment is not deployed at all (web can be
        # disabled). Reporting 0/0 says that plainly; omitting it would look
        # like a gap in the sample.
        out.append(ComponentReplicas(name=component, ready=ready, desired=desired))

    return out


async def sample() -> ComponentStatus:
    """Take a reading and cache it. Never raises."""
    from datetime import UTC, datetime

    namespace = _namespace()
    if not namespace:
        return ComponentStatus(unavailable_reason="not running in a Kubernetes namespace")

    try:
        components = await asyncio.to_thread(
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
    "read",
    "sample",
    "sample_cycle",
]
