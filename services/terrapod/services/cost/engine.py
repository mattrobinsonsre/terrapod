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

from dataclasses import dataclass, field
from typing import IO, Any

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
    # Accumulate matching products per resource in a single pass over the sheet.
    acc = [(res, change, res.to_match_set(), []) for res, change in resources]
    for product in load_pricesheet(pricesheet):
        prod_ms = product.match_set
        for _res, _change, res_ms, products in acc:
            if prod_ms.is_subset_of(res_ms):
                products.append(product)

    matches = [(res, change, products) for res, change, _ms, products in acc]
    usage_entries = usage_mod.load_usage(usage_json)
    result, unpriced = price(matches, usage_entries, region_by_address)

    resource_costs = [
        ResourceCost(
            address=pr.address,
            type=pr.type,
            name=pr.name,
            change=pr.change,
            monthly_min=pr.price.min,
            monthly_max=pr.price.max,
            usage_assumptions=pr.usage_assumptions,
        )
        for pr in result.resources
    ]
    unpriced_resources = [
        UnpricedResource(address=res.address, type=res.type, change=change)
        for res, change in unpriced
    ]
    return CostEstimate(
        currency=result.currency,
        total_min=result.total.min,
        total_max=result.total.max,
        prev_min=result.prev.min,
        prev_max=result.prev.max,
        diff_min=result.diff.min,
        diff_max=result.diff.max,
        resources=resource_costs,
        unpriced=unpriced_resources,
    )
