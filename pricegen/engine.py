"""Shared recipe engine (#893) — provider-agnostic.

Applies a resource recipe (defaults + components) to a stream of normalized
``Unit``s (from any provider's adapter) and yields OpenInfraQuote-shaped rows:

    service, product_family, match_set, pricing_match_set, price, price_type, ccy

Field resolution supports direct attributes AND regex extraction, because the
value we need often lives inside a string field (Azure's OS/tier in
``productName``/``skuName``; GCP's machine family in ``description``) rather than
a clean attribute like AWS's ``instanceType``.

Two component kinds:
  * direct   — match a unit, emit its price (AWS, Azure).
  * computed — assemble a row from several units × an external catalog (GCP
    machine_type -> Σ vCPU-core + RAM-GB). Not implemented yet; the direct kind
    is what the AWS/Azure recipes use. The seam is here so GCP slots in without
    an engine rewrite.
"""

from __future__ import annotations

import re


def _snake(camel: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower()


def resolve(spec, attrs: dict) -> str | None:
    """Resolve a field spec to a string, or None to SKIP the unit. Forms:
    * a literal string
    * ``{attr: X}``                 — pull attribute X
    * ``{attr: X, regex: 'pat(cap)'}`` — extract a capture from a string field
    * ``{attr|from + map: {...}}``  — translate; an unmapped value -> None
    * ``{from: [a, b], map: {...}}``  — COMBINE several attrs (joined by '|')
      then look up, e.g. RDS (databaseEngine, databaseEdition) -> engine value.
    """
    if isinstance(spec, str):
        return spec
    if isinstance(spec, dict) and "from" in spec:
        key = "|".join(attrs.get(a) or "" for a in spec["from"])
        return spec.get("map", {}).get(key)
    if isinstance(spec, dict) and "attr" in spec:
        v = attrs.get(spec["attr"])
        if v is None:
            return None
        if "regex" in spec:
            m = re.search(spec["regex"], v)
            if not m:
                return None
            v = m.group(1) if m.groups() else m.group(0)
        mapping = spec.get("map")
        return mapping.get(v) if mapping is not None else v
    raise ValueError(f"bad field spec: {spec!r}")


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


def _match_set(component: dict, rtype: str, attrs: dict) -> str | None:
    parts = [f"type={rtype}"]
    match = component["match"]
    items = (
        {_snake(a): {"attr": a} for a in match} if isinstance(match, list) else match
    )
    for tf_key, spec in items.items():
        if isinstance(spec, str):
            spec = {"attr": spec}
        v = resolve(spec, attrs)
        if v is None:
            return None
        parts.append(f"values.{tf_key}={v}")
    return "&".join(parts)


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


def generate(recipe: dict, defaults: dict, units, *, service: str):
    """Yield 7-tuples for every unit any component of the recipe covers.

    Deduplicates identical rows — vendor feeds carry redundant SKUs (AWS lists
    the same RDS instance twice at the same price), and we emit one clean row.
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
            if not _passes(component, defaults, unit.attrs):
                continue
            match_set = _match_set(component, rtype, unit.attrs)
            if match_set is None:
                continue

            base = dict(defaults.get("pricing_common", {}))
            base["service_class"] = service_class
            base.update(component.get("pricing", {}))
            resolved: dict[str, str] = {}
            skip = False
            for k, spec in base.items():
                v = resolve(spec, unit.attrs)
                if v is None:
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
                yield row
