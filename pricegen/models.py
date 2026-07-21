"""The provider-agnostic shape the engine operates on (#893).

Each cloud's price feed is radically different — AWS splits products + terms,
Azure is a flat item list, GCP is services -> skus with tiered rates. A
per-provider *adapter* normalizes its feed into these two types, and the shared
engine only ever sees them. Adding a cloud = an adapter + recipes; the engine
never changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Price:
    """One priced quantity of a unit. ``begin``/``end`` are the usage/provision
    tier bounds (AWS beginRange/endRange, Azure tierMinimumUnits, GCP
    startUsageAmount); ``unit`` is the charge unit (Hrs, GB-Mo, IOPS-Mo, …)."""

    usd: str
    begin: str = "0"
    end: str = "Inf"
    unit: str = ""


@dataclass
class Unit:
    """A priceable thing from a cloud feed, normalized. ``family`` is the
    provider's product-family label the recipe matches on (by regex);
    ``attrs`` are its normalized string attributes; ``prices`` are its price
    tiers (>1 = tiered)."""

    family: str
    attrs: dict[str, str]
    prices: list[Price] = field(default_factory=list)
