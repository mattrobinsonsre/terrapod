"""Terraform plan/state adapter — port of OpenInfraQuote's ``oiq_tf.ml`` (MPL-2.0).

Turns ``terraform show -json`` output — of a **plan** or of **state** — into a
flat list of ``(resource, change)`` pairs, where each resource is flattened
into a :class:`~terrapod.services.cost.match_set.MatchSet` of its attributes
(``type=aws_instance``, ``values.instance_class=…``) for matching against the
pricesheet.

* **Plan**: diff ``planned_values`` against ``prior_state`` by address → each
  resource is labelled ``add`` / ``remove`` / ``noop``. This is what powers the
  per-run cost *delta*.
* **State** (``terraform show -json`` of state): every resource is ``noop``.
  This powers a workspace's *current* managed-cost total.

A convenience :func:`resources_from_state_v4` also accepts a raw Terraform
state file (version 4, ``resources[].instances[].attributes``) so the API can
price a stored state version without shelling out to ``terraform show`` — the
same ``{type, values.*}`` shape falls out either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from terrapod.services.cost.match_set import MatchSet

Change = Literal["noop", "add", "remove"]


def _ocaml_string_of_float(f: float) -> str:
    """Match OCaml ``string_of_float`` (``1.0`` → ``"1."``, ``2.5`` → ``"2.5"``).

    Terraform rarely uses float-valued *match* attributes, but staying faithful
    keeps the differential test against ``oiq`` exact.
    """
    s = f"{f:.12g}"
    if "." not in s and "e" not in s and "E" not in s and "n" not in s:
        s += "."
    return s


def flatten(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Flatten a JSON value into ``(dotted_key, string_value)`` pairs.

    Mirrors ``oiq_tf.Resource.flatten``: dicts recurse with ``prefix.key``;
    **lists reuse the same prefix for every item** (no index), so list-valued
    attributes collapse into the set; scalars stringify (bool → ``true``/
    ``false``, null → ``null``).
    """
    out: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, val in value.items():
            new_prefix = key if prefix == "" else f"{prefix}.{key}"
            out.extend(flatten(val, new_prefix))
    elif isinstance(value, list):
        for item in value:
            out.extend(flatten(item, prefix))
    elif isinstance(value, bool):
        out.append((prefix, "true" if value else "false"))
    elif isinstance(value, int):
        out.append((prefix, str(value)))
    elif isinstance(value, float):
        out.append((prefix, _ocaml_string_of_float(value)))
    elif value is None:
        out.append((prefix, "null"))
    elif isinstance(value, str):
        out.append((prefix, value))
    else:  # pragma: no cover - defensive
        out.append((prefix, str(value)))
    return out


@dataclass(frozen=True)
class Resource:
    address: str
    name: str
    type: str
    data: dict[str, Any]

    def to_match_set(self) -> MatchSet:
        return MatchSet.of_list(flatten(self.data))


def _resource_of_obj(obj: dict[str, Any]) -> Resource:
    return Resource(
        address=obj["address"],
        name=obj["name"],
        type=obj["type"],
        data=obj,
    )


def _load_module_resources(module: dict[str, Any]) -> list[Resource]:
    """Recursively collect resources from a ``root_module`` / child module."""
    resources = [_resource_of_obj(r) for r in module.get("resources", [])]
    for child in module.get("child_modules", []):
        resources.extend(_load_module_resources(child))
    return resources


def detect_type(json: dict[str, Any]) -> Literal["plan", "state", "unknown"]:
    """Distinguish ``terraform show -json`` plan output from state output."""
    has_plan = "planned_values" in json
    has_state = "values" in json
    if has_plan and has_state:
        return "unknown"
    if has_plan:
        return "plan"
    if has_state:
        return "state"
    return "unknown"


def resources_from_plan(json: dict[str, Any]) -> list[tuple[Resource, Change]]:
    """Diff a plan's planned vs prior state → ``(resource, change)`` pairs."""
    planned = _load_module_resources(json["planned_values"]["root_module"])
    prior_root = json.get("prior_state", {}).get("values", {}).get("root_module", {"resources": []})
    prior = _load_module_resources(prior_root)

    prior_by_addr = {r.address: r for r in prior}
    planned_by_addr = {r.address: r for r in planned}
    prior_addrs = set(prior_by_addr)
    planned_addrs = set(planned_by_addr)

    out: list[tuple[Resource, Change]] = []
    # noop: present in both — take the prior object (mirrors CCSet.inter s1 s2).
    for addr in prior_addrs & planned_addrs:
        out.append((prior_by_addr[addr], "noop"))
    for addr in planned_addrs - prior_addrs:
        out.append((planned_by_addr[addr], "add"))
    for addr in prior_addrs - planned_addrs:
        out.append((prior_by_addr[addr], "remove"))
    return out


def resources_from_state(json: dict[str, Any]) -> list[tuple[Resource, Change]]:
    """All resources from ``terraform show -json`` state output, as ``noop``."""
    resources = _load_module_resources(json["values"]["root_module"])
    return [(r, "noop") for r in resources]


def resources_from_state_v4(json: dict[str, Any]) -> list[tuple[Resource, Change]]:
    """Adapt a raw Terraform state file (version 4) → ``(resource, noop)`` pairs.

    A stored state file lists ``resources[].instances[].attributes``; we lift
    each instance's ``attributes`` under a ``values`` key so it flattens to the
    same ``type=…&values.*`` shape the pricesheet expects — no ``terraform
    show`` needed. Data-source resources (``mode != "managed"``) are skipped.
    """
    out: list[tuple[Resource, Change]] = []
    for res in json.get("resources", []):
        if res.get("mode", "managed") != "managed":
            continue
        rtype = res.get("type", "")
        rname = res.get("name", "")
        module = res.get("module", "")
        base_addr = f"{rtype}.{rname}"
        if module:
            base_addr = f"{module}.{base_addr}"
        instances = res.get("instances", [])
        for idx, inst in enumerate(instances):
            attributes = inst.get("attributes", {})
            index_key = inst.get("index_key")
            if index_key is not None:
                addr = (
                    f"{base_addr}[{index_key!r}]"
                    if isinstance(index_key, str)
                    else f"{base_addr}[{index_key}]"
                )
            elif len(instances) > 1:
                addr = f"{base_addr}[{idx}]"
            else:
                addr = base_addr
            data = {
                "address": addr,
                "type": rtype,
                "name": rname,
                "mode": "managed",
                "values": attributes,
            }
            out.append((Resource(address=addr, name=rname, type=rtype, data=data), "noop"))
    return out


def resources_from_json(json: dict[str, Any]) -> list[tuple[Resource, Change]]:
    """Dispatch on ``terraform show -json`` plan-vs-state, else raw state v4.

    Raises ``ValueError`` when the input is none of the three recognised shapes.
    """
    kind = detect_type(json)
    if kind == "plan":
        return resources_from_plan(json)
    if kind == "state":
        return resources_from_state(json)
    if json.get("version") == 4 and "resources" in json:
        return resources_from_state_v4(json)
    raise ValueError("input is neither a terraform plan, show-state, nor state v4 file")


# ---------------------------------------------------------------------------
# Per-resource region resolution (#871)
# ---------------------------------------------------------------------------
#
# A plan can legitimately span regions — AWS provider v6 puts `region` on every
# resource, and Azure/GCP have always had per-resource `location`/`region`. So
# region is resolved per resource, not per plan, in this precedence:
#   1. the resource's own attribute (`values.region` / `values.location`)
#   2. its provider config's constant region (`configuration.provider_config`)
#   3. a caller-supplied fallback (`cost_estimation.default_region`)

# Ordered attribute keys checked on a resource's `values` for its region.
_REGION_ATTR_KEYS = ("region", "location")


def provider_regions(plan_json: dict[str, Any]) -> dict[str, str]:
    """Map ``provider_name`` → constant region from a plan's provider configs.

    Only providers whose ``region`` is a literal (``constant_value``) are
    included — a region set from a variable/expression can't be resolved from
    the plan JSON. Empty for state inputs (which carry no ``configuration``).
    """
    out: dict[str, str] = {}
    provider_config = plan_json.get("configuration", {}).get("provider_config", {})
    for pc in provider_config.values():
        full = pc.get("full_name") or pc.get("name")
        region = pc.get("expressions", {}).get("region", {}).get("constant_value")
        if isinstance(full, str) and full and isinstance(region, str) and region:
            out[full] = region
    return out


def resolve_region(resource: Resource, provider_region_map: dict[str, str], default: str) -> str:
    """Resolve a resource's region: own attribute → provider config → default."""
    values = resource.data.get("values")
    if isinstance(values, dict):
        for key in _REGION_ATTR_KEYS:
            val = values.get(key)
            if isinstance(val, str) and val:
                return val
    provider = resource.data.get("provider_name")
    if isinstance(provider, str) and provider in provider_region_map:
        return provider_region_map[provider]
    return default
