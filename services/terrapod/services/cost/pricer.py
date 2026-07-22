"""Pricer — port of OpenInfraQuote's ``oiq_pricer.ml`` price path (MPL-2.0).

Given resources matched to products, this computes each resource's monthly cost
range: group a resource's products by their default-usage entry, bound usage to
any per-usage / provision tiers, quote each product as
``usage_bound / divisor * unit_price``, and take the cheapest and dearest quote
as the resource's ``(min, max)``. Removed resources are negated; the run-level
``diff`` sums adds+removes; ``prev = total - diff``.

Only the numeric pricing is here — matching (resource ↔ product) is done in
:mod:`terrapod.services.cost.engine`. The optional ``--match-query`` product
filter of the upstream CLI is not needed by Terrapod and is omitted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace

from terrapod.services.cost import usage as usage_mod
from terrapod.services.cost.match_set import MatchSet
from terrapod.services.cost.prices import PriceKind, Product
from terrapod.services.cost.range import Range, append, overlap
from terrapod.services.cost.tf import Change, Resource
from terrapod.services.cost.usage import Entry, Usage

# Sentinel for "Inf" usage bounds. A large finite int keeps ranges int-typed;
# real (finite) consumption always wins the min() clamps in the bounding logic.
_INF = 2**63 - 1


class ResourceMissingAttr(Exception):
    """An attribute-priced product needs a resource attr that isn't present."""


def _int_of_usage(v: str) -> int:
    return _INF if v == "Inf" else int(v)


@dataclass(frozen=True)
class PricedProduct:
    service: str
    product_family: str
    price: Range[float]
    usage_description: str
    ccy: str


@dataclass(frozen=True)
class PricedResource:
    address: str
    name: str
    type: str
    change: Change
    price: Range[float]
    products: list[PricedProduct]
    # The usage assumptions folded into this resource's cost — each a
    # {description, dimension, unit, low, typical, high} for a usage-driven
    # component (requests, data, duration) whose quantity was guessed, not read
    # from the plan. Empty for a fully-deterministic resource. Surfaced so the
    # UI can flag assumptions and the AI can refine them per-resource (#962).
    usage_assumptions: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class PriceResult:
    total: Range[float]
    diff: Range[float]
    prev: Range[float]
    resources: list[PricedResource]
    currency: str


# --- the four transform steps (mirror oiq_pricer) ---------------------------


def _synthesize_usage_from_attr(
    resource_ms: MatchSet, entry: Entry, products: list[Product]
) -> tuple[Entry, list[Product]]:
    all_attr = all(p.price.kind is PriceKind.ATTR for p in products)
    if all_attr:
        if not products:
            if entry.usage is not None:
                return (entry, products)
            raise AssertionError("empty attr product group with no usage")
        attr = products[0].price.attr
        assert attr is not None
        found = resource_ms.find_by_key(attr)
        if found is None:
            raise ResourceMissingAttr(f"resource missing attr {attr!r} for pricing")
        usage_val = int(found[1])
        usage = Usage.from_data_range(Range(usage_val, usage_val))
        return (entry.with_usage(usage), products)
    if entry.usage is not None:
        return (entry, products)
    raise AssertionError("non-attr product group with no usage entry")


def _apply_provision_amount(entry: Entry, products: list[Product]) -> tuple[Entry, list[Product]]:
    assert entry.usage is not None
    kept: list[Product] = []
    for product in products:
        ms = product.pricing_match_set
        spa = ms.find_by_key("start_provision_amount")
        epa = ms.find_by_key("end_provision_amount")
        if spa is not None and epa is not None:
            provision = Range(_int_of_usage(spa[1]), _int_of_usage(epa[1]))
            priced_by = _priced_by_range(product, entry.usage)
            if overlap(provision, priced_by) is not None:
                kept.append(product)
        elif spa is None and epa is None:
            kept.append(product)
        # else: malformed row (one bound only) — drop, mirroring the assert path
    return (entry, kept)


def _priced_by_range(product: Product, usage: Usage) -> Range[int]:
    kind = product.price.kind
    if kind is PriceKind.PER_TIME:
        return usage.time
    if kind is PriceKind.PER_OPERATION:
        return usage.operations
    return usage.data  # PER_DATA and ATTR


def _apply_usage_amount(entry: Entry, products: list[Product]) -> list[tuple[Entry, list[Product]]]:
    has_usage_amount = any(
        p.pricing_match_set.find_by_key("start_usage_amount") is not None
        and p.pricing_match_set.find_by_key("end_usage_amount") is not None
        for p in products
    )
    if not has_usage_amount:
        return [(entry, products)]

    groups: dict[tuple[Range[int], str], list[Product]] = {}
    for product in products:
        ms = product.pricing_match_set
        sua = _int_of_usage(ms.find_by_key("start_usage_amount")[1])  # type: ignore[index]
        eua = _int_of_usage(ms.find_by_key("end_usage_amount")[1])  # type: ignore[index]
        rng = Range(sua, eua)
        kind = product.price.kind
        priced_by = (
            "time"
            if kind is PriceKind.PER_TIME
            else "operations"
            if kind is PriceKind.PER_OPERATION
            else "data"
        )
        groups.setdefault((rng, priced_by), []).append(product)

    out: list[tuple[Entry, list[Product]]] = []
    for (usage_range, priced_by), group in groups.items():
        accessor = {
            "time": usage_mod.TIME,
            "operations": usage_mod.OPERATIONS,
            "data": usage_mod.DATA,
        }[priced_by]
        bounded = usage_mod.bound_to_usage_amount(accessor, usage_range, entry)
        if bounded is not None:
            out.append((bounded, group))
    return out


def _price_products(
    entry: Entry, products: list[Product]
) -> tuple[PricedProduct, float, float] | None:
    assert entry.usage is not None
    divisor = float(entry.divisor if entry.divisor is not None else 1)
    priced: list[tuple[Product, float]] = []
    for product in products:
        unit_price = product.price.value
        base = _priced_by_range(product, entry.usage)
        priced.append((product, float(base.min) / divisor * unit_price))
        priced.append((product, float(base.max) / divisor * unit_price))
    if not priced:
        return None
    priced.sort(key=lambda pq: pq[1])
    (product_min, min_val) = priced[0]
    (_product_max, max_val) = priced[-1]
    pp = PricedProduct(
        service=product_min.service,
        product_family=product_min.product_family,
        price=Range(min_val, max_val),
        usage_description=entry.description,
        ccy=product_min.ccy,
    )
    return (pp, min_val, max_val)


def _apply_size_tier(entry: Entry, products: list[Product], resource_ms: MatchSet) -> list[Product]:
    """For a fixed-per-size-tier component (Azure managed disk), keep only the
    product whose ``tier_max_gb`` is the smallest that is ≥ the resource's size
    attribute — i.e. the tier the disk rounds up into. Unknown/absent size ⇒
    keep all (the estimate becomes a range across tiers rather than crashing)."""
    spec = entry.size_tier
    if not spec:
        return products
    attr = spec.get("attr")
    found = resource_ms.find_by_key(attr) if attr else None
    if found is None:
        return products
    try:
        size = float(found[1])
    except (ValueError, TypeError):
        return products
    candidates: list[tuple[float, Product]] = []
    for p in products:
        tm = p.pricing_match_set.find_by_key("tier_max_gb")
        if tm is None:
            continue
        try:
            cap = float(tm[1])
        except (ValueError, TypeError):
            continue
        if cap >= size:
            candidates.append((cap, p))
    if not candidates:
        return products
    best = min(cap for cap, _ in candidates)
    return [p for cap, p in candidates if cap == best]


def _quote_group(
    resource_ms: MatchSet, entry: Entry, group_products: list[Product]
) -> list[PricedProduct]:
    """Run the full price chain for one entry-group, returning its priced products.

    Raises ``ResourceMissingAttr`` / ``AssertionError`` on a pathological combo
    (the caller treats that as unpriced).
    """
    out: list[PricedProduct] = []
    syn_entry, syn_products = _synthesize_usage_from_attr(resource_ms, entry, group_products)
    # Fixed-per-size-tier (Azure disk): pick the tier the size rounds into first.
    syn_products = _apply_size_tier(syn_entry, syn_products, resource_ms)
    prov_entry, prov_products = _apply_provision_amount(syn_entry, syn_products)
    for bounded_entry, bounded_products in _apply_usage_amount(prov_entry, prov_products):
        if not bounded_products:
            continue
        quoted = _price_products(bounded_entry, bounded_products)
        if quoted is not None:
            out.append(quoted[0])
    return out


def _sum_price(priced_products: list[PricedProduct]) -> Range[float]:
    summed = Range(0.0, 0.0)
    for pp in priced_products:
        summed = append(lambda a, b: a + b, summed, pp.price)
    return summed


def _band_dimension_accessor(entry: Entry, typical: int) -> usage_mod.Accessor | None:
    """Locate the usage dimension a banded entry varies — the one whose assumed
    value equals the band's ``typical`` (the memory invariant "typical == usage
    value"). ``None`` if it can't be located (misconfigured band)."""
    if entry.usage is None:
        return None
    for acc in (usage_mod.TIME, usage_mod.OPERATIONS, usage_mod.DATA):
        cur = acc.get(entry.usage)
        if cur.min == cur.max == typical:
            return acc
    return None


def _cost_band(
    resource_ms: MatchSet, entry: Entry, group_products: list[Product]
) -> tuple[float, float, float] | None:
    """For a banded usage-driven entry, the monthly cost at the band's
    low / typical / high usage — the dollar impact of the assumption (#962).

    Re-prices the same product group with the varied dimension pinned to each
    band point (so tiered/provisioned pricing is honoured at each point). Returns
    ``None`` for a deterministic entry or a band we can't map to a dimension.
    """
    b = entry.bands or {}
    lo, ty, hi = b.get("low"), b.get("typical"), b.get("high")
    if not entry.assumption or lo is None or ty is None or hi is None:
        return None
    acc = _band_dimension_accessor(entry, int(ty))
    if acc is None or entry.usage is None:
        return None

    def _at(v: int) -> float:
        pinned = entry.with_usage(acc.set(Range(v, v), entry.usage))
        # The upper bound of the point quote (== lower when there's no pricing
        # ambiguity like license variants) is the cost at this usage level.
        return _sum_price(_quote_group(resource_ms, pinned, group_products)).max

    try:
        return (_at(int(lo)), _at(int(ty)), _at(int(hi)))
    except (ResourceMissingAttr, AssertionError):
        return None


def _count_factor(entry: Entry, resource_ms: MatchSet) -> float:
    """Multiplier for a per-unit component whose quantity scales with a resource
    attribute — ElastiCache node cost × ``num_cache_nodes``, Kinesis ×
    ``shard_count``, or a value parsed out of a string attribute (Cloud SQL's
    ``db-custom-N-M`` tier → N vCPUs, M/1024 GiB RAM).

    Spec shape: {attr, default?, regex?, divisor?}. ``regex`` extracts capture
    group 1 from the attribute string; ``divisor`` scales the result (e.g. RAM
    MB → GiB). Defaults to 1 (or ``default``) when unset, absent, or
    unparseable, so nothing regresses."""
    spec = entry.count
    if not spec:
        return 1.0
    default = float(spec.get("default", 1) or 1)
    attr = spec.get("attr")
    if not attr:
        return default
    found = resource_ms.find_by_key(attr)
    if found is None:
        return default
    raw = found[1]
    rx = spec.get("regex")
    if rx:
        m = re.search(rx, raw)
        if m is None:
            return default
        raw = m.group(1)
    divisor = float(spec.get("divisor", 1) or 1)
    try:
        # No floor of 1 here: a parsed RAM count can be fractional GiB, and a
        # count of 0 means the component contributes nothing. Absent/unparseable
        # still fall back to ``default`` above.
        return max(0.0, float(raw) / divisor)
    except (ValueError, TypeError):
        return default


def _region_matches(product: Product, region: str | None) -> bool:
    """Keep region-agnostic products and region-specific products in ``region``.

    OpenInfraQuote's ``--region`` filters globally; we instead filter each
    resource's products to *its own* resolved region (#871), because a plan can
    span regions (AWS provider v6 puts ``region`` on every resource; Azure/GCP
    have per-resource ``location``/``region``). Products with no ``region`` in
    their pricing match set are global (e.g. Route53) and always kept.
    """
    pr = product.pricing_match_set.find_by_key("region")
    # No region dim, or an EMPTY one, means the product prices globally (Route53,
    # CloudFront — their AWS `regionCode` attribute is empty) — always keep it.
    if pr is None or pr[1] == "":
        return True
    return region is None or pr[1] == region


def price(
    matches: list[tuple[Resource, Change, list[Product]]],
    usage_entries: list[Entry],
    region_by_address: dict[str, str] | None = None,
) -> tuple[PriceResult, list[tuple[Resource, Change]]]:
    """Price matched resources. Returns ``(result, unpriced)``.

    ``region_by_address`` maps each resource address to its resolved region;
    a resource's region-specific products are filtered to that region so a
    multi-region plan prices each resource correctly (#871). ``unpriced`` lists
    resources that matched no priceable product (or whose products carried no
    usage entry) — the "Unpriced" bucket in the UX.
    """
    region_by_address = region_by_address or {}
    priced_resources: list[PricedResource] = []
    unpriced: list[tuple[Resource, Change]] = []
    currency = "USD"

    for resource, change, all_products in matches:
        resource_ms = resource.to_match_set()
        region = region_by_address.get(resource.address)
        products = [p for p in all_products if _region_matches(p, region)]
        # Group products by their matched usage entry (keyed by query text).
        entry_by_key: dict[str, Entry] = {}
        products_by_key: dict[str, list[Product]] = {}
        for product in products:
            combined = resource_ms.union(product.match_set).union(product.pricing_match_set)
            entry = usage_mod.match_entry(combined, usage_entries)
            if entry is None:
                continue
            key = entry.match_query.to_string()
            entry_by_key.setdefault(key, entry)
            products_by_key.setdefault(key, []).append(product)

        priced_products: list[PricedProduct] = []
        # Per-usage-dimension monthly cost band (low, typical, high) for the
        # usage-driven components, keyed by the band's dimension (#962).
        cost_band_by_dim: dict[str, tuple[float, float, float]] = {}
        for key, group_products in products_by_key.items():
            entry = entry_by_key[key]
            try:
                group_pps = _quote_group(resource_ms, entry, group_products)
            except (ResourceMissingAttr, AssertionError):
                # Pathological product/usage combo — treat this resource as
                # unpriced rather than crashing the whole estimate.
                continue
            # Scale the whole component by its unit count (× num_cache_nodes,
            # × shard_count, …). Factor 1 for the common single-unit case.
            factor = _count_factor(entry, resource_ms)
            if factor != 1.0:
                group_pps = [
                    replace(pp, price=Range(pp.price.min * factor, pp.price.max * factor))
                    for pp in group_pps
                ]
            for pp in group_pps:
                priced_products.append(pp)
                currency = pp.ccy
            if group_pps:
                band = _cost_band(resource_ms, entry, group_products)
                if band is not None and factor != 1.0:
                    band = (band[0] * factor, band[1] * factor, band[2] * factor)
                if band is not None and entry.bands:
                    dim = entry.bands.get("dimension")
                    if dim is not None:
                        cost_band_by_dim[dim] = band

        if priced_products:
            sign = -1.0 if change == "remove" else 1.0
            summed = Range(0.0, 0.0)
            for pp in priced_products:
                summed = append(lambda a, b: a + b, summed, pp.price)
            resource_price = Range(sign * summed.min, sign * summed.max)
            # Collect the usage assumptions that fed this resource's cost (one per
            # dimension) — the usage-driven entries that matched its products.
            assumptions: list[dict] = []
            seen_dims: set = set()
            for used_entry in entry_by_key.values():
                ua = used_entry.usage_assumption()
                if ua is not None and ua["dimension"] not in seen_dims:
                    # Attach the monthly cost at low/typical/high usage (the
                    # dollar impact of the assumption) when we could price it.
                    band = cost_band_by_dim.get(ua["dimension"])
                    if band is not None:
                        ua = {
                            **ua,
                            "cost_low": band[0],
                            "cost_typical": band[1],
                            "cost_high": band[2],
                        }
                    assumptions.append(ua)
                    seen_dims.add(ua["dimension"])
            priced_resources.append(
                PricedResource(
                    address=resource.address,
                    name=resource.name,
                    type=resource.type,
                    change=change,
                    price=resource_price,
                    products=priced_products,
                    usage_assumptions=assumptions,
                )
            )
        else:
            unpriced.append((resource, change))

    total = Range(0.0, 0.0)
    for pr in priced_resources:
        total = append(lambda a, b: a + b, total, pr.price)
    diff = Range(0.0, 0.0)
    for pr in priced_resources:
        if pr.change in ("add", "remove"):
            diff = append(lambda a, b: a + b, diff, pr.price)
    prev = append(lambda a, b: a - b, total, diff)

    result = PriceResult(
        total=total, diff=diff, prev=prev, resources=priced_resources, currency=currency
    )
    return (result, unpriced)
