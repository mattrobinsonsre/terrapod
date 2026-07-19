"""Match sets — port of OpenInfraQuote's ``oiq_match_set.ml`` (MPL-2.0).

A ``MatchSet`` is a set of ``(key, value)`` string pairs. It is the common
currency of the engine:

* A resource is flattened into a match set of its attributes
  (``type=aws_instance``, ``values.instance_class=db.t3.medium``, …).
* Each pricesheet row carries two match sets — a *resource* match set (which
  resources it prices) and a *pricing* match set (its billing dimensions:
  region, purchase_option, usage bounds, …).

A product prices a resource when the product's resource match set is a
**subset** of the resource's flattened match set (``subset(super=resource,
sub=product)``).

The serialised form is ``k=v&k=v`` with percent-encoded values (URL-style),
matching the CSV columns.
"""

from __future__ import annotations

from urllib.parse import unquote


class MatchSet:
    """An immutable set of (key, value) pairs with subset/union semantics."""

    __slots__ = ("_pairs",)

    def __init__(self, pairs: frozenset[tuple[str, str]]) -> None:
        self._pairs = pairs

    @classmethod
    def of_list(cls, pairs: list[tuple[str, str]]) -> MatchSet:
        return cls(frozenset(pairs))

    @classmethod
    def of_string(cls, s: str) -> MatchSet:
        """Parse ``k=v&k=v`` (percent-decoded, empties dropped).

        Raises ``ValueError`` if any non-empty segment lacks a ``=`` — mirrors
        ``oiq_match_set.of_string`` returning ``Error``.
        """
        pairs: list[tuple[str, str]] = []
        for seg in s.split("&"):
            if seg == "":
                continue
            decoded = unquote(seg)
            key, sep, value = decoded.partition("=")
            if sep == "":
                raise ValueError(f"match set segment missing '=': {seg!r}")
            pairs.append((key, value))
        return cls(frozenset(pairs))

    def to_list(self) -> list[tuple[str, str]]:
        return sorted(self._pairs)

    def find_by_key(self, key: str) -> tuple[str, str] | None:
        for k, v in self._pairs:
            if k == key:
                return (k, v)
        return None

    def contains(self, key: str, value: str) -> bool:
        return (key, value) in self._pairs

    def union(self, other: MatchSet) -> MatchSet:
        return MatchSet(self._pairs | other._pairs)

    def is_subset_of(self, superset: MatchSet) -> bool:
        """True when every pair of ``self`` is in ``superset``."""
        return self._pairs <= superset._pairs

    def __eq__(self, other: object) -> bool:
        return isinstance(other, MatchSet) and self._pairs == other._pairs

    def __hash__(self) -> int:
        return hash(self._pairs)

    def __repr__(self) -> str:
        return "MatchSet(" + "&".join(f"{k}={v}" for k, v in self.to_list()) + ")"
