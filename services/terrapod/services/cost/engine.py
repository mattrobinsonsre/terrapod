"""Top-level cost engine — port of OpenInfraQuote's ``oiq.ml`` orchestration.

:func:`estimate` is the one public entry point. Given ``terraform show -json``
(plan or state, or a raw state v4 file) and an open ``prices.csv``, it:

1. flattens each resource to a match set (:mod:`terrapod.services.cost.tf`),
2. streams the pricesheet once, attaching every product whose match set is a
   subset of a resource's (:mod:`terrapod.services.cost.prices`),
3. prices the matches (:mod:`terrapod.services.cost.pricer`),

and returns a :class:`CostEstimate` — monthly ``total`` / ``prev`` / ``diff``
ranges, per-resource costs, and the list of resources nothing priced (the
"Unpriced" bucket surfaced in the UX).

The engine is pure and synchronous. On the API event loop it must be called via
``asyncio.to_thread`` (streaming a ~200k-row CSV is CPU-bound — rule 13).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import IO, Any

from terrapod.services.cost import fleet_resolver
from terrapod.services.cost import usage as usage_mod
from terrapod.services.cost.pricer import price
from terrapod.services.cost.prices import load_pricesheet
from terrapod.services.cost.tf import (
    Change,
    provider_regions,
    resolve_region,
    resources_from_json,
)


@dataclass(frozen=True)
class ResourceCost:
    address: str
    type: str
    name: str
    change: Change
    monthly_min: float
    monthly_max: float
    # Usage-driven components whose quantity was assumed, each a
    # {description, dimension, unit, low, typical, high} — so the estimate can
    # flag them and the AI can refine them per-resource (#962).
    usage_assumptions: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class UnpricedResource:
    address: str
    type: str
    change: Change


@dataclass(frozen=True)
class CostEstimate:
    currency: str
    total_min: float
    total_max: float
    prev_min: float
    prev_max: float
    diff_min: float
    diff_max: float
    resources: list[ResourceCost] = field(default_factory=list)
    unpriced: list[UnpricedResource] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the ``cost_estimate.json`` run-artifact shape."""
        return {
            "currency": self.currency,
            "total": {"min": self.total_min, "max": self.total_max},
            "previous": {"min": self.prev_min, "max": self.prev_max},
            "diff": {"min": self.diff_min, "max": self.diff_max},
            "resources": [
                {
                    "address": r.address,
                    "type": r.type,
                    "name": r.name,
                    "change": r.change,
                    "monthly": {"min": r.monthly_min, "max": r.monthly_max},
                    # Additive (#962): usage-driven line items whose quantity was
                    # assumed, with low/typical/high bands. Omitted when empty so
                    # deterministic resources' payload is unchanged.
                    **({"usage_assumptions": r.usage_assumptions} if r.usage_assumptions else {}),
                }
                for r in self.resources
            ],
            "unpriced": [
                {"address": u.address, "type": u.type, "change": u.change} for u in self.unpriced
            ],
        }


def estimate(
    tf_json: dict[str, Any],
    pricesheet: IO[str],
    usage_json: list[dict[str, Any]] | None = None,
    default_region: str = "us-east-1",
) -> CostEstimate:
    """Estimate monthly cost for a plan or state against a pricesheet.

    ``tf_json``   — parsed ``terraform show -json`` (plan or state) or a raw
                    state v4 file.
    ``pricesheet`` — an open text file object over OpenInfraQuote's
                    ``prices.csv`` (streamed once, never fully materialised).
    ``usage_json`` — optional user usage overrides; prepended to the vendored
                    OpenInfraQuote defaults.
    ``default_region`` — fallback region for a resource whose region can't be
                    resolved from its attributes or provider config. Region is
                    resolved **per resource** (a plan can span regions, #871).
    """
    resources = resources_from_json(tf_json)
    # Resolve each resource's region (own attr → provider config → default).
    provider_region_map = provider_regions(tf_json)
    region_by_address = {
        res.address: resolve_region(res, provider_region_map, default_region)
        for res, _change in resources
    }

    # Fleet resources (#1029) bill as N units whose priceable shape lives on a
    # nested block or a referenced resource — resolve them into synthetic
    # instance-shaped units, price those through the normal path, and ADD the
    # total to the fleet's own line. Fleets stay in normal matching too, because
    # some carry a direct cost on top of their nodes (an AKS cluster has a
    # management fee *and* its default node pool's VMs).
    fleets = fleet_resolver.resolve_fleets(resources)
    synth_meta: dict[str, tuple[int, float]] = {}  # synth addr → (fleet idx, count)
    synth_pairs: list[tuple[Any, Change]] = []
    for fi, fleet in enumerate(fleets):
        fregion = region_by_address.get(fleet.address, default_region)
        for addr, sres, count in fleet_resolver.synth_resources(fleet, fregion):
            synth_pairs.append((sres, fleet.change))
            region_by_address[addr] = fregion
            synth_meta[addr] = (fi, count)

    priceable = list(resources) + synth_pairs

    # Accumulate matching products per resource in a single pass over the sheet.
    acc = [(res, change, res.to_match_set(), []) for res, change in priceable]
    for product in load_pricesheet(pricesheet):
        prod_ms = product.match_set
        for _res, _change, res_ms, products in acc:
            if prod_ms.is_subset_of(res_ms):
                products.append(product)

    matches = [(res, change, products) for res, change, _ms, products in acc]
    usage_entries = usage_mod.load_usage(usage_json)
    result, unpriced = price(matches, usage_entries, region_by_address)

    # Priced non-synth resources (fleets included, carrying their DIRECT cost).
    priced_by_addr: dict[str, ResourceCost] = {}
    for pr in result.resources:
        if pr.address in synth_meta:
            continue
        priced_by_addr[pr.address] = ResourceCost(
            address=pr.address,
            type=pr.type,
            name=pr.name,
            change=pr.change,
            monthly_min=pr.price.min,
            monthly_max=pr.price.max,
            usage_assumptions=pr.usage_assumptions,
        )

    # Sum each fleet's synth parts × count, then add to (or create) its line.
    fleet_synth: dict[int, list[float]] = {}
    for pr in result.resources:
        meta = synth_meta.get(pr.address)
        if meta is None:
            continue
        fi, count = meta
        agg = fleet_synth.setdefault(fi, [0.0, 0.0])
        agg[0] += pr.price.min * count
        agg[1] += pr.price.max * count
    for fi, (lo, hi) in fleet_synth.items():
        f = fleets[fi]
        existing = priced_by_addr.get(f.address)
        if existing is not None:
            priced_by_addr[f.address] = replace(
                existing,
                monthly_min=existing.monthly_min + lo,
                monthly_max=existing.monthly_max + hi,
            )
        else:
            priced_by_addr[f.address] = ResourceCost(
                address=f.address,
                type=f.type,
                name=f.name,
                change=f.change,
                monthly_min=lo,
                monthly_max=hi,
            )

    resource_costs = list(priced_by_addr.values())
    priced_addrs = set(priced_by_addr)
    # Unpriced: real resources nothing priced — never the internal synth-unit
    # addresses, and never a fleet that ended up priced (directly or via nodes).
    unpriced_resources = [
        UnpricedResource(address=res.address, type=res.type, change=change)
        for res, change in unpriced
        if res.address not in synth_meta and res.address not in priced_addrs
    ]

    # Recompute the totals from the FINAL resource lines (#1029 fix): price()'s
    # totals were summed over the synth fleet-units priced at ×1, so they miss
    # the ×count fold and the fleet-line merge. The per-resource costs already
    # carry the count-multiplied, correctly-signed value (removes are negative
    # via price()), so summing them reproduces total/diff/prev under the same
    # convention (prev = total − diff; diff counts add/remove only).
    total_min = sum(rc.monthly_min for rc in resource_costs)
    total_max = sum(rc.monthly_max for rc in resource_costs)
    diff_min = sum(rc.monthly_min for rc in resource_costs if rc.change in ("add", "remove"))
    diff_max = sum(rc.monthly_max for rc in resource_costs if rc.change in ("add", "remove"))
    return CostEstimate(
        currency=result.currency,
        total_min=total_min,
        total_max=total_max,
        prev_min=total_min - diff_min,
        prev_max=total_max - diff_max,
        diff_min=diff_min,
        diff_max=diff_max,
        resources=resource_costs,
        unpriced=unpriced_resources,
    )
