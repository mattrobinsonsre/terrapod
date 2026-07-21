"""Shared recipe engine (#893) — provider-agnostic.

Applies a resource recipe (defaults + components) to a stream of normalized
``Unit``s (from any provider's adapter) and yields OpenInfraQuote-shaped rows:

    service, product_family, match_set, pricing_match_set, price, price_type, ccy

Field resolution supports direct attributes AND regex extraction, because the
value we need often lives inside a string field (Azure's OS/tier in
``productName``/``skuName``; GCP's machine family in ``description``) rather than
a clean attribute like AWS's ``instanceType``.

Two recipe kinds:
  * direct   — ``components:`` — match a unit, emit its price (AWS, Azure). Run
    by :func:`generate`.
  * computed — ``computed:`` — assemble a row from several units by arithmetic
    (GCP prices per vCPU-core hour + per GiB-RAM hour separately, so a machine
    type costs ``vCPU × core-rate + RAM_GiB × ram-rate``). Run by
    :func:`generate_computed`, which enumerates machine types from a compact
    per-family shape catalog in the recipe and pre-computes each total, so the
    emitted rows match ``values.machine_type`` like any other instance — no
    consumer arithmetic needed.

Diagnostics (#922): ``generate`` optionally records a :class:`RecipeStats` — row
counts and, crucially, the *reason* each unit was dropped. The high-signal
reason is **unmapped**: a value the feed carries for a mapped field that our
``map:`` doesn't cover (a new ``volumeType``/``databaseEdition``/machine family).
That's the canonical "the cloud changed and our recipe logic needs an update"
signal — the scheduled generator surfaces it so drift is alerted, not silently
dropped. An "absent" miss (the source attr simply isn't on this unit) is the
expected, low-signal case and is counted separately.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field


def _snake(camel: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower()


def _resolve2(spec, attrs: dict) -> tuple[str | None, str | None, str | None]:
    """Resolve a field spec to ``(value, miss, offending)``.

    ``miss`` is ``None`` when resolved, ``"absent"`` when the source attribute
    isn't present on the unit (expected — this unit just isn't for us), or
    ``"unmapped"`` when the source IS present but our ``map:``/``regex`` doesn't
    cover its value (**actionable drift** — ``offending`` carries that value).

    Forms:
    * a literal string                 — always resolves
    * ``{attr: X}``                    — pull attribute X
    * ``{attr: X, regex: 'pat(cap)'}`` — extract a capture from a string field
    * ``{attr: X, map: {...}}``        — translate; unmapped value -> miss
    * ``{from: [a, b], map: {...}}``   — COMBINE several attrs (joined by '|')
      then look up, e.g. RDS (databaseEngine, databaseEdition) -> engine value.
    """
    if isinstance(spec, str):
        return spec, None, None
    if isinstance(spec, dict) and "const" in spec:
        # a fixed literal to STAMP on the row (e.g. values.architectures=arm64 on
        # the ARM-duration component) — distinct from a bare string in a `match`,
        # which _match_set reads as an attribute NAME.
        return spec["const"], None, None
    if isinstance(spec, dict) and "from" in spec:
        vals = [attrs.get(a) for a in spec["from"]]
        key = "|".join(v or "" for v in vals)
        mapping = spec.get("map", {})
        if key in mapping:
            return mapping[key], None, None
        # all source attrs empty -> genuinely absent; else an uncovered combo.
        if all(v is None for v in vals):
            return None, "absent", None
        return None, "unmapped", key
    if isinstance(spec, dict) and "attr" in spec:
        v = attrs.get(spec["attr"])
        if v is None:
            return None, "absent", None
        raw = v
        if "regex" in spec:
            m = re.search(spec["regex"], v)
            if not m:
                return None, "unmapped", raw
            v = m.group(1) if m.groups() else m.group(0)
        mapping = spec.get("map")
        if mapping is not None:
            if v not in mapping:
                return None, "unmapped", raw
            return mapping[v], None, None
        return v, None, None
    raise ValueError(f"bad field spec: {spec!r}")


def resolve(spec, attrs: dict) -> str | None:
    """Resolve a field spec to a string, or None to SKIP the unit."""
    return _resolve2(spec, attrs)[0]


@dataclass
class RecipeStats:
    """Per-recipe diagnostics for one generate() run (#922).

    ``unmapped`` is the drift signal: ``field -> {offending_value: count}`` for
    values the feed carried that our recipe's map/regex didn't cover.
    """

    rows: int = 0
    units_total: int = 0
    skipped_select: int = 0
    skipped_absent: int = 0
    skipped_unmapped: int = 0
    unmapped: dict[str, Counter] = field(default_factory=dict)
    price_min: float | None = None
    price_max: float | None = None

    def _note_unmapped(self, fieldname: str, value: str) -> None:
        self.skipped_unmapped += 1
        self.unmapped.setdefault(fieldname, Counter())[value] += 1

    def _note_price(self, usd: str) -> None:
        try:
            v = float(usd)
        except (TypeError, ValueError):
            return
        self.price_min = v if self.price_min is None else min(self.price_min, v)
        self.price_max = v if self.price_max is None else max(self.price_max, v)


def _requirement_ok(want, got: str | None) -> bool:
    """A select requirement: a literal (==), a list (membership), or a
    ``{regex: 'pat', negate: bool}`` (string match / anti-match — for the
    Azure/GCP cases where the variant lives in a free-text field)."""
    if isinstance(want, list):
        return got in want
    if isinstance(want, dict) and "regex" in want:
        hit = got is not None and re.search(want["regex"], got) is not None
        return not hit if want.get("negate") else hit
    return got == want


def _passes(component: dict, defaults: dict, attrs: dict) -> bool:
    select = component.get("select", {})
    # canonical dimension defaults, applied only where the unit HAS the attr,
    # unless the component overrides that dimension in its own select.
    for dim, canon in defaults.get("canonical", {}).items():
        if dim in select:
            continue
        if dim in attrs and attrs[dim] != canon:
            return False
    return all(_requirement_ok(want, attrs.get(dim)) for dim, want in select.items())


def _match_set(
    component: dict, rtype: str, attrs: dict
) -> tuple[str | None, tuple[str, str, str] | None]:
    """Return ``(match_set, miss)`` where miss is ``(field, reason, value)`` for
    the first field that didn't resolve, or None when the whole set resolved."""
    parts = [f"type={rtype}"]
    # A component with no `match` constrains only the resource type (matches all
    # of that type) — e.g. Lambda requests apply to every lambda.
    match = component.get("match", {})
    items = (
        {_snake(a): {"attr": a} for a in match} if isinstance(match, list) else match
    )
    for tf_key, spec in items.items():
        if isinstance(spec, str):
            spec = {"attr": spec}
        v, miss, offending = _resolve2(spec, attrs)
        if v is None:
            return None, (tf_key, miss or "absent", offending or "")
        parts.append(f"values.{tf_key}={v}")
    return "&".join(parts), None


def _tier_bounds(component: dict, attrs: dict) -> tuple[str, str] | None:
    """The exceptions layer: some tier boundaries aren't in the vendor API (EBS
    io2's 32000/64000 breaks, gp3's free 3000-IOPS baseline). A component's
    optional ``tier_bounds`` maps them by matching AWS attributes:
        tier_bounds:
          - when: { volumeApiName: gp3, group: EBS IOPS }
            start: "3000"
            end: Inf
    Returns (start, end) for the first matching rule, or None."""
    for rule in component.get("tier_bounds", []):
        if all(attrs.get(k) == v for k, v in rule.get("when", {}).items()):
            return str(rule["start"]), str(rule["end"])
    return None


def _tier_fields(
    mode: str, begin: str, end: str, tiered: bool, forced: bool
) -> list[str]:
    if mode == "usage":
        return [f"start_usage_amount={begin}", f"end_usage_amount={end}"]
    if mode == "provision" and (forced or tiered or str(begin) != "0" or end != "Inf"):
        return [f"start_provision_amount={begin}", f"end_provision_amount={end}"]
    return []


def generate(recipe: dict, defaults: dict, units, *, service: str, stats=None):
    """Yield 7-tuples for every unit any component of the recipe covers.

    Deduplicates identical rows — vendor feeds carry redundant SKUs (AWS lists
    the same RDS instance twice at the same price), and we emit one clean row.

    When ``stats`` (a :class:`RecipeStats`) is passed, records row counts and the
    reason each unit was dropped — the drift-diagnostics foundation for #922.
    """
    rtype = recipe["resource_type"]
    units = list(units)  # reused across components
    seen: set = set()

    for component in recipe["components"]:
        fam_re = re.compile(component["product_family"])
        want_unit = component.get("price", {}).get("unit")
        ptype = component["price"]["type"]
        tier_mode = component.get("tier", "none")
        service_class = component["service_class"]

        for unit in units:
            if not fam_re.search(unit.family):
                continue
            # Unit is in-family for this component — now it counts toward stats.
            if stats is not None:
                stats.units_total += 1
            if not _passes(component, defaults, unit.attrs):
                if stats is not None:
                    stats.skipped_select += 1
                continue
            match_set, miss = _match_set(component, rtype, unit.attrs)
            if match_set is None:
                if stats is not None:
                    fieldname, reason, value = miss  # type: ignore[misc]
                    if reason == "unmapped":
                        stats._note_unmapped(f"match.{fieldname}", value)
                    else:
                        stats.skipped_absent += 1
                continue

            base = dict(defaults.get("pricing_common", {}))
            base["service_class"] = service_class
            base.update(component.get("pricing", {}))
            resolved: dict[str, str] = {}
            skip = False
            for k, spec in base.items():
                v, miss_reason, offending = _resolve2(spec, unit.attrs)
                if v is None:
                    if stats is not None:
                        if miss_reason == "unmapped":
                            stats._note_unmapped(f"pricing.{k}", offending or "")
                        else:
                            stats.skipped_absent += 1
                    skip = True
                    break
                resolved[k] = v
            if skip:
                continue

            dims = [p for p in unit.prices if not want_unit or p.unit == want_unit]
            tiered = len(dims) > 1
            bounds = _tier_bounds(component, unit.attrs)
            for p in dims:
                begin, end = bounds if bounds else (p.begin, p.end)
                pricing_parts = [f"{k}={v}" for k, v in resolved.items()]
                pricing_parts += _tier_fields(
                    tier_mode, begin, end, tiered, forced=bounds is not None
                )
                row = (
                    service,
                    unit.family,
                    match_set,
                    "&".join(pricing_parts),
                    p.usd,
                    ptype,
                    "USD",
                )
                if row in seen:
                    continue
                seen.add(row)
                if stats is not None:
                    stats.rows += 1
                    stats._note_price(p.usd)
                yield row


# --- computed kind (GCP compute: Σ vCPU-core + RAM-GiB) ---------------------


def generate_computed(recipe: dict, defaults: dict, units, *, service: str, stats=None):
    """Yield rows for a ``computed:`` recipe (GCP Compute Engine).

    GCP prices compute per **vCPU-core hour** and per **GiB-RAM hour** as
    separate SKUs (e.g. "N2 Instance Core running in Americas" / "... Ram ..."),
    so a machine type's hourly cost is ``vCPU × core-rate + RAM_GiB × ram-rate``.
    We read each family's Core+Ram rate from the units, then enumerate machine
    types from a compact per-family shape catalog and pre-compute each total —
    emitting one ``values.machine_type=<type>`` row per type, matched by the
    consumer exactly like an AWS/Azure instance (no consumer arithmetic).

    Recipe ``computed`` block::

        computed:
          region: us-central1                 # match units in this region
          family_regex: '^(\\w+)(?: Predefined)? Instance (Core|Ram) running in'
          vcpus: [1, 2, 4, 8, 16, 32, 64, 96]
          families:                            # key = machine prefix (lowercased
            n2: { standard: 4, highmem: 8, highcpu: 1 }   # token); shape -> GiB/vCPU
            e2: { standard: 4, highmem: 8, highcpu: 1 }

    Custom machine types (``n2-custom-…``) aren't enumerable and are left
    unpriced (a documented gap); over-enumerated sizes that don't exist are
    harmless (they never match a real plan).
    """
    spec = recipe["computed"]
    rtype = recipe["resource_type"]
    region = spec["region"]
    fam_re = re.compile(spec["family_regex"])
    canon = defaults.get("canonical", {})

    # 1. Collect each family's Core/Ram rate from the OnDemand, in-region units.
    rates: dict[str, dict[str, float]] = {}
    for unit in units:
        a = unit.attrs
        if a.get("region") != region:
            continue
        if any(a.get(dim) != c for dim, c in canon.items() if dim in a):
            continue
        m = fam_re.match(a.get("description", ""))
        if not m:
            continue
        fam, kind = m.group(1).lower(), m.group(2)
        if unit.prices:
            rates.setdefault(fam, {})[kind] = float(unit.prices[0].usd)

    # 2. Enumerate machine types and pre-compute each total.
    base = dict(defaults.get("pricing_common", {}))
    base.pop("region", None)  # region is per-computed-spec, emitted below
    base["service_class"] = "instance"
    base["region"] = region
    pricing = "&".join(
        [f"{k}={v}" for k, v in base.items()]
        + ["start_usage_amount=0", "end_usage_amount=Inf"]
    )
    seen: set = set()
    for fam, shapes in spec["families"].items():
        fam_rates = rates.get(fam)
        if not fam_rates or "Core" not in fam_rates or "Ram" not in fam_rates:
            if stats is not None:
                stats._note_unmapped("family", fam)  # rates missing -> drift signal
            continue
        core, ram = fam_rates["Core"], fam_rates["Ram"]
        for shape, gib_per_vcpu in shapes.items():
            for vcpu in spec["vcpus"]:
                machine_type = f"{fam}-{shape}-{vcpu}"
                ram_gib = vcpu * gib_per_vcpu
                hourly = vcpu * core + ram_gib * ram
                match_set = f"type={rtype}&values.machine_type={machine_type}"
                row = (
                    service,
                    "Compute Instance",
                    match_set,
                    pricing,
                    f"{hourly:.10f}",
                    "t",
                    "USD",
                )
                if row in seen:
                    continue
                seen.add(row)
                if stats is not None:
                    stats.rows += 1
                    stats.units_total += 1
                    stats._note_price(f"{hourly:.10f}")
                yield row
