"""Group/fleet resource resolution for cost estimation (#1029).

A "fleet" is a single Terraform resource that bills as **N units**, where the
priceable unit is often an *instance-shaped* thing (an EC2 instance type, an
Azure VM size, a GCP machine type). The base engine prices a resource by matching
its **own** attributes against the sheet, so a fleet lands in the ``unpriced``
bucket — its cost lives on a nested block or a *referenced* resource, not on a
top-level attribute the matcher reads.

This module runs **before** matching (:func:`resolve_fleets`) and turns each
fleet into a list of :class:`FleetPart`\\ s — a resolved ``(unit_type,
unit_attr, unit_value, count)``. The engine then synthesises a minimal resource
of ``unit_type`` per part (:func:`synth_resources`), prices it through the normal
path, multiplies by ``count``, and re-labels the total to the fleet's address —
so the UI shows ``aws_autoscaling_group.web: $X/mo`` priced as its instances.

Two shapes:

* **self-contained** — the unit type and count are both on the fleet resource,
  just nested in a block (``aws_eks_node_group.scaling_config[0].desired_size`` +
  ``instance_types[0]``; an AKS pool's ``vm_size`` + ``node_count``).
* **referenced** — the count is on the fleet but the unit type is on another
  resource in the plan (an ASG → its launch template's ``instance_type``; a GCP
  MIG → its instance template's ``machine_type``). Resolved via a plan-wide index.

Coverage is a **declarative table** (:data:`DESCRIPTORS`) so breadth is a data
change, not new code. A fleet whose unit type has no sheet coverage yet, or whose
referenced resource isn't in the plan, yields **zero parts** with a ``reason`` —
it stays ``unpriced`` (never crashes), and becomes priceable the moment its unit
recipe ships, with no engine change.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from terrapod.services.cost.tf import Change, Resource

# Unit types we can price today (a sheet recipe exists) → the match attr the
# synthesised unit carries. A fleet whose price_as type isn't here is declared
# but deferred (stays unpriced with a reason) until its recipe lands.
PRICEABLE_UNIT_ATTR: dict[str, str] = {
    "aws_instance": "instance_type",
    "azurerm_linux_virtual_machine": "size",
    "google_compute_instance": "machine_type",
}


@dataclass(frozen=True)
class FleetPart:
    """One priceable slice of a fleet: ``count`` units of ``unit_value``."""

    unit_type: str
    unit_attr: str
    unit_value: str
    count: float


@dataclass(frozen=True)
class Fleet:
    address: str
    type: str
    name: str
    change: Change
    parts: list[FleetPart] = field(default_factory=list)
    # Set when parts is empty: why this fleet couldn't be priced (deferred unit
    # type, or an unresolvable reference). Surfaced in the unpriced bucket.
    reason: str | None = None


# --- path helpers (Terraform plan JSON: list-blocks are lists) ---------------


def _get(values: Any, path: str) -> Any:
    """Read a dotted path from a ``values`` dict; integer segments index lists.

    Terraform flattens a ``foo {}`` block to ``"foo": [{...}]``, so a nested
    attribute is ``foo.0.bar``. Returns None on any missing/short segment.
    """
    cur = values
    for seg in path.split("."):
        if cur is None:
            return None
        if seg.isdigit():
            if not isinstance(cur, list) or int(seg) >= len(cur):
                return None
            cur = cur[int(seg)]
        else:
            if not isinstance(cur, dict):
                return None
            cur = cur.get(seg)
    return cur


def _as_count(v: Any, default: float = 1.0) -> float:
    try:
        n = float(v)
        return n if n >= 0 else default
    except (TypeError, ValueError):
        return default


# --- descriptor model --------------------------------------------------------


@dataclass(frozen=True)
class Descriptor:
    """Declarative fleet rule for the common one-unit-type × one-count shape.

    ``count_path`` / ``unit_path`` read the fleet's own ``values`` (nested ok).
    For the *referenced* shape, ``ref`` is ``(ref_path, target_type,
    target_key_attr, target_unit_attr)``: read the ref value at ``ref_path``,
    find the ``target_type`` resource whose ``target_key_attr`` matches it, and
    take its ``target_unit_attr`` as the unit value. Irregular fleets (multiple
    instance groups, type lists) use ``custom`` instead.
    """

    fleet_type: str
    price_as: str
    count_path: str | None = None
    unit_path: str | None = None
    ref: tuple[str, str, str, str] | None = None
    custom: Callable[[Descriptor, Resource, PlanIndex], list[FleetPart]] | None = None


class PlanIndex:
    """Lookup of plan resources for reference resolution: (type, key) → unit attr.

    Indexes each resource by its ``id`` and ``name`` so a fleet's launch-template
    reference (which may be an id or a name) resolves either way.
    """

    def __init__(self, resources: list[tuple[Resource, Change]]):
        self._by: dict[tuple[str, str], Resource] = {}
        for res, _ch in resources:
            values = res.data.get("values") or {}
            for key_attr in ("id", "name", "arn"):
                key = values.get(key_attr)
                if isinstance(key, str) and key:
                    self._by.setdefault((res.type, key), res)

    def unit_from_ref(self, target_type: str, ref_value: str, target_unit_attr: str) -> str | None:
        res = self._by.get((target_type, ref_value))
        if res is None:
            return None
        v = _get(res.data.get("values") or {}, target_unit_attr)
        return v if isinstance(v, str) and v else None


# --- custom handlers for irregular fleets ------------------------------------


def _asg(desc: Descriptor, res: Resource, index: PlanIndex) -> list[FleetPart]:
    """aws_autoscaling_group: launch template / config / mixed-instances policy.

    Count is ``desired_capacity``. The instance type comes from (in order): a
    ``mixed_instances_policy`` override list (each type gets an equal share of
    capacity), a ``launch_template`` ref (by id or name), or a
    ``launch_configuration`` ref.
    """
    values = res.data.get("values") or {}
    count = _as_count(_get(values, "desired_capacity"), default=0.0)
    if count <= 0:
        count = _as_count(_get(values, "min_size"), default=1.0)

    # mixed_instances_policy: split capacity evenly across the override types.
    overrides = _get(values, "mixed_instances_policy.0.launch_template.0.override")
    if isinstance(overrides, list) and overrides:
        types = [o.get("instance_type") for o in overrides if o.get("instance_type")]
        if types:
            per = count / len(types)
            return [FleetPart("aws_instance", "instance_type", t, per) for t in types]

    # launch_template (id or name) → aws_launch_template.instance_type
    lt_id = _get(values, "launch_template.0.id")
    lt_name = _get(values, "launch_template.0.name")
    unit = None
    for key in (lt_id, lt_name):
        if isinstance(key, str) and key:
            unit = index.unit_from_ref("aws_launch_template", key, "instance_type")
            if unit:
                break
    # launch_configuration (name) → aws_launch_configuration.instance_type
    if unit is None:
        lc = _get(values, "launch_configuration")
        if isinstance(lc, str) and lc:
            unit = index.unit_from_ref("aws_launch_configuration", lc, "instance_type")
    if unit is None:
        return []
    return [FleetPart("aws_instance", "instance_type", unit, count)]


def _gcp_mig(desc: Descriptor, res: Resource, index: PlanIndex) -> list[FleetPart]:
    """google_compute_(region_)instance_group_manager → instance template.

    ``target_size`` units of the template's ``machine_type``. The template is
    referenced by ``version[0].instance_template`` (newer) or ``instance_template``.
    """
    values = res.data.get("values") or {}
    count = _as_count(_get(values, "target_size"), default=0.0)
    if count <= 0:
        return []
    ref = _get(values, "version.0.instance_template") or _get(values, "instance_template")
    if not isinstance(ref, str) or not ref:
        return []
    unit = index.unit_from_ref("google_compute_instance_template", ref, "machine_type")
    if unit is None:
        return []
    return [FleetPart("google_compute_instance", "machine_type", unit, count)]


def _emr(desc: Descriptor, res: Resource, index: PlanIndex) -> list[FleetPart]:
    """aws_emr_cluster: master + core + task instance groups, each type × count."""
    values = res.data.get("values") or {}
    parts: list[FleetPart] = []
    for group in ("master_instance_group", "core_instance_group"):
        itype = _get(values, f"{group}.0.instance_type")
        icount = _as_count(_get(values, f"{group}.0.instance_count"), default=1.0)
        if isinstance(itype, str) and itype:
            parts.append(FleetPart("aws_instance", "instance_type", itype, icount))
    return parts


# --- the declarative table ---------------------------------------------------

DESCRIPTORS: dict[str, Descriptor] = {
    d.fleet_type: d
    for d in [
        # --- self-contained (unit + count on the resource, possibly nested) ---
        Descriptor(
            "aws_eks_node_group",
            "aws_instance",
            count_path="scaling_config.0.desired_size",
            unit_path="instance_types.0",
        ),
        Descriptor(
            "azurerm_kubernetes_cluster",
            "azurerm_linux_virtual_machine",
            count_path="default_node_pool.0.node_count",
            unit_path="default_node_pool.0.vm_size",
        ),
        Descriptor(
            "azurerm_kubernetes_cluster_node_pool",
            "azurerm_linux_virtual_machine",
            count_path="node_count",
            unit_path="vm_size",
        ),
        Descriptor(
            "azurerm_linux_virtual_machine_scale_set",
            "azurerm_linux_virtual_machine",
            count_path="instances",
            unit_path="sku",
        ),
        Descriptor(
            "azurerm_windows_virtual_machine_scale_set",
            "azurerm_linux_virtual_machine",
            count_path="instances",
            unit_path="sku",
        ),
        Descriptor(
            "azurerm_orchestrated_virtual_machine_scale_set",
            "azurerm_linux_virtual_machine",
            count_path="instances",
            unit_path="sku",
        ),
        Descriptor(
            "google_container_node_pool",
            "google_compute_instance",
            count_path="node_count",
            unit_path="node_config.0.machine_type",
        ),
        # --- referenced / irregular (custom handlers) ---
        Descriptor("aws_autoscaling_group", "aws_instance", custom=_asg),
        Descriptor(
            "google_compute_instance_group_manager",
            "google_compute_instance",
            custom=_gcp_mig,
        ),
        Descriptor(
            "google_compute_region_instance_group_manager",
            "google_compute_instance",
            custom=_gcp_mig,
        ),
        Descriptor("aws_emr_cluster", "aws_instance", custom=_emr),
    ]
}

# Fleet types we recognise but can't price yet (no unit recipe in the sheet).
# Declared so they get a clear "needs pricing data" reason instead of a silent
# unpriced — and become priceable the moment the recipe lands.
DEFERRED_FLEETS: dict[str, str] = {
    "aws_redshift_cluster": "node-based (node_type × number_of_nodes) — needs a Redshift pricing recipe",
    "aws_msk_cluster": "broker nodes — needs an MSK pricing recipe",
    "aws_opensearch_domain": "cluster nodes — needs an OpenSearch pricing recipe",
    "aws_elasticsearch_domain": "cluster nodes — needs an OpenSearch pricing recipe",
    "aws_memorydb_cluster": "shards × replicas — needs a MemoryDB pricing recipe",
    "aws_ecs_service": "Fargate tasks — needs a Fargate (vCPU/GB-hour) pricing recipe",
    "google_dataproc_cluster": "master/worker instances — needs Dataproc coverage",
    "google_bigtable_instance": "nodes — needs a Bigtable pricing recipe",
    "azurerm_redis_cache": "capacity/family/sku — needs an Azure Cache for Redis recipe",
}


def _from_descriptor(desc: Descriptor, res: Resource, index: PlanIndex) -> list[FleetPart]:
    if desc.custom is not None:
        return desc.custom(desc, res, index)
    values = res.data.get("values") or {}
    unit = _get(values, desc.unit_path) if desc.unit_path else None
    if not isinstance(unit, str) or not unit:
        return []
    count = _as_count(_get(values, desc.count_path)) if desc.count_path else 1.0
    attr = PRICEABLE_UNIT_ATTR[desc.price_as]
    return [FleetPart(desc.price_as, attr, unit, count)]


def resolve_fleets(resources: list[tuple[Resource, Change]]) -> list[Fleet]:
    """Turn each fleet resource into a :class:`Fleet` (its priceable parts).

    Non-fleet resources are ignored (return only fleets). A recognised fleet that
    resolves to no parts (deferred unit type, missing reference, zero count) is
    returned with ``parts=[]`` and a ``reason`` so the engine can surface it as
    unpriced rather than dropping it silently.
    """
    index = PlanIndex(resources)
    out: list[Fleet] = []
    for res, change in resources:
        desc = DESCRIPTORS.get(res.type)
        if desc is not None:
            parts = _from_descriptor(desc, res, index)
            reason = None if parts else "could not resolve the fleet's unit type/count"
            out.append(Fleet(res.address, res.type, res.name, change, parts, reason))
        elif res.type in DEFERRED_FLEETS:
            out.append(
                Fleet(res.address, res.type, res.name, change, [], DEFERRED_FLEETS[res.type])
            )
    return out


def is_fleet(resource_type: str) -> bool:
    """Whether a resource type is handled by the fleet resolver (priced or
    deferred) — the engine excludes these from normal matching to avoid
    double-handling and lets the resolver own their output."""
    return resource_type in DESCRIPTORS or resource_type in DEFERRED_FLEETS


def synth_resources(fleet: Fleet, region: str) -> list[tuple[str, Resource, float]]:
    """Build a minimal priceable resource per part, stamped with the fleet's
    resolved region. Returns ``(synth_address, resource, count)`` — the engine
    prices the resource as one unit and multiplies the result by ``count``.
    """
    out: list[tuple[str, Resource, float]] = []
    for i, part in enumerate(fleet.parts):
        addr = f"{fleet.address} #fleet-unit-{i}"
        values = {part.unit_attr: part.unit_value, "region": region}
        data = {"address": addr, "type": part.unit_type, "name": fleet.name, "values": values}
        out.append(
            (
                addr,
                Resource(address=addr, name=fleet.name, type=part.unit_type, data=data),
                part.count,
            )
        )
    return out
