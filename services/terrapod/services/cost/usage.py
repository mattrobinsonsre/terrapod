"""Usage assumptions — port of OpenInfraQuote's ``oiq_usage.ml`` (MPL-2.0).

Many resources cost nothing to *declare*; their price depends on runtime usage
(S3 GB stored, Lambda invocations, RDS IOPS). OpenInfraQuote ships default
assumed usage as a catalogue of *entries* — each a match query plus assumed
``time`` / ``operations`` / ``data`` amounts (as ranges, so the estimate itself
is a range). The first entry whose query matches a product's combined match set
supplies that product's usage.

The default catalogue is vendored verbatim from OpenInfraQuote at
``usage_defaults.json`` (MPL-2.0). ``time`` for compute is 730 (hours/month);
storage/operations have their own defaults. An entry may carry a ``divisor``
(e.g. price is "per 1,000 operations").
"""

from __future__ import annotations

import json as _json
from collections.abc import Callable
from dataclasses import dataclass, replace
from importlib import resources
from typing import Any

from terrapod.services.cost.match_query import MatchQuery
from terrapod.services.cost.match_set import MatchSet
from terrapod.services.cost.range import Range

_ZERO: Range[int] = Range(0, 0)


@dataclass(frozen=True)
class Usage:
    time: Range[int] = _ZERO
    operations: Range[int] = _ZERO
    data: Range[int] = _ZERO

    @staticmethod
    def from_data_range(rng: Range[int]) -> Usage:
        """``oiq_usage.Usage.make`` — data set, time/operations default."""
        return Usage(time=_ZERO, operations=_ZERO, data=rng)


def _parse_range(value: Any) -> Range[int]:
    if isinstance(value, dict):
        return Range(int(value["min"]), int(value["max"]))
    return Range(int(value), int(value))


def _parse_usage(obj: dict[str, Any] | None) -> Usage | None:
    if obj is None:
        return None
    return Usage(
        time=_parse_range(obj["time"]) if "time" in obj else _ZERO,
        operations=_parse_range(obj["operations"]) if "operations" in obj else _ZERO,
        data=_parse_range(obj["data"]) if "data" in obj else _ZERO,
    )


@dataclass(frozen=True)
class Entry:
    description: str
    divisor: int | None
    match_query: MatchQuery
    usage: Usage | None
    # A component priced through this entry is a USAGE ASSUMPTION (its quantity
    # is a guess about runtime traffic — requests, data processed, duration —
    # not something the plan tells us) when ``assumption`` is set. ``bands`` then
    # carries our low/typical/high judgement so the estimate can flag the
    # assumption and the AI layer can refine it per-resource (#962). Deterministic
    # entries (always-on hours, storage from the resource's own size attr) leave
    # both unset. ``bands`` shape: {dimension, unit, low, typical, high}.
    assumption: bool = False
    bands: dict[str, Any] | None = None
    # A per-unit component whose quantity scales with a resource attribute —
    # e.g. an ElastiCache cluster's cost is per-NODE (× num_cache_nodes), a
    # Kinesis stream's is per-SHARD (× shard_count). ``count`` names that
    # attribute; the pricer multiplies this entry's component cost by it. Shape:
    # {attr: "values.num_cache_nodes", default: 1}. Absent/unset ⇒ factor 1 (the
    # common single-unit case), so existing entries are unchanged.
    count: dict[str, Any] | None = None
    # A component priced by a FIXED per-size TIER — an Azure managed disk's cost
    # is a flat monthly fee for the tier its ``disk_size_gb`` falls into (P10 =
    # up to 128 GiB = $19.71/mo), not a per-GB rate. ``size_tier`` names the
    # size attribute; the pricer keeps only the product whose ``tier_max_gb``
    # pricing dim is the smallest that is ≥ the resource's size, then charges it
    # flat (usage = 1). Shape: {attr: "values.disk_size_gb"}. Unset ⇒ no tiering.
    size_tier: dict[str, Any] | None = None

    def with_usage(self, usage: Usage) -> Entry:
        return replace(self, usage=usage)

    def usage_assumption(self) -> dict[str, Any] | None:
        """This entry's usage-assumption descriptor for the cost result, or None
        if it's a deterministic entry."""
        if not self.assumption:
            return None
        b = self.bands or {}
        return {
            "description": self.description,
            "dimension": b.get("dimension"),
            "unit": b.get("unit"),
            "low": b.get("low"),
            "typical": b.get("typical"),
            "high": b.get("high"),
        }


# Accessors for the three usage dimensions (get/set a Range on a Usage).
@dataclass(frozen=True)
class Accessor:
    get: Callable[[Usage], Range[int]]
    set: Callable[[Range[int], Usage], Usage]


TIME = Accessor(
    get=lambda u: u.time,
    set=lambda r, u: replace(u, time=r),
)
OPERATIONS = Accessor(
    get=lambda u: u.operations,
    set=lambda r, u: replace(u, operations=r),
)
DATA = Accessor(
    get=lambda u: u.data,
    set=lambda r, u: replace(u, data=r),
)


def bound_to_usage_amount(accessor: Accessor, tier: Range[int], entry: Entry) -> Entry | None:
    """Bound an entry's usage to a pricing tier ``[tier.min, tier.max]``.

    Mirrors ``oiq_usage.Entry.bound_to_usage_amount``: returns ``None`` when the
    entry's assumed usage doesn't reach this tier, else clamps the usage to the
    portion that falls within it (so tiered per-usage prices can each be applied
    to their own slice). ``entry.usage`` must be set.
    """
    assert entry.usage is not None
    current = accessor.get(entry.usage)
    if current.max < tier.min:
        return None
    diff = tier.max - tier.min
    consumption = current.max - tier.min
    new_min = max(0, min(current.min, tier.max) - tier.min)
    new_max = min(consumption, diff)
    new_usage = accessor.set(Range(new_min, new_max), entry.usage)
    return replace(entry, usage=new_usage)


def _entry_from_obj(obj: dict[str, Any]) -> Entry:
    return Entry(
        description=obj["description"],
        divisor=obj.get("divisor"),
        match_query=MatchQuery.of_string(obj["match_query"]),
        usage=_parse_usage(obj.get("usage")),
        assumption=bool(obj.get("assumption", False)),
        bands=obj.get("bands"),
        count=obj.get("count"),
        size_tier=obj.get("size_tier"),
    )


def _load_defaults() -> list[Entry]:
    text = resources.files("terrapod.services.cost").joinpath("usage_defaults.json").read_text()
    return [_entry_from_obj(o) for o in _json.loads(text)]


_DEFAULTS: list[Entry] | None = None


def default_entries() -> list[Entry]:
    """The vendored OpenInfraQuote default usage catalogue (cached)."""
    global _DEFAULTS
    if _DEFAULTS is None:
        _DEFAULTS = _load_defaults()
    return _DEFAULTS


def load_usage(user_entries_json: list[dict[str, Any]] | None = None) -> list[Entry]:
    """User-supplied entries take precedence, appended by the defaults."""
    user = [_entry_from_obj(o) for o in (user_entries_json or [])]
    return user + default_entries()


def match_entry(ms: MatchSet, entries: list[Entry]) -> Entry | None:
    """First entry whose query matches the given match set (``oiq_usage.match_``)."""
    for entry in entries:
        if entry.match_query.eval(ms):
            return entry
    return None
