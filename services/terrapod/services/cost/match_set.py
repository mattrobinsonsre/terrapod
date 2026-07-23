"""Match sets — the engine's common vocabulary for "what describes this thing".

A :class:`MatchSet` is an immutable set of ``(key, value)`` string pairs, and
almost everything in cost estimation is expressed as one:

* a resource flattens into the set of its attributes
  (``type=aws_instance``, ``values.instance_class=db.t3.medium``, …);
* every pricesheet product carries two — a *resource* set naming which resources
  it can price, and a *pricing* set holding its billing dimensions (region,
  purchase option, usage bounds, …).

A product prices a resource exactly when the product's resource set is a
**subset** of the resource's attribute set — every pair the product requires is
present on the resource. Serialised, a match set is ``k=v&k=v`` with
URL-percent-encoded values, which is how the values arrive from the pricesheet.
"""

from __future__ import annotations

from urllib.parse import unquote

_Pair = tuple[str, str]


class MatchSet:
    """An immutable bag of ``(key, value)`` pairs with subset/union semantics."""

    __slots__ = ("_pairs",)

    def __init__(self, pairs: frozenset[_Pair]) -> None:
        self._pairs = pairs

    @classmethod
    def from_pairs(cls, pairs: list[_Pair]) -> MatchSet:
        """Build directly from a list of ``(key, value)`` tuples."""
        return cls(frozenset(pairs))

    @classmethod
    def parse(cls, text: str) -> MatchSet:
        """Read the ``k=v&k=v`` wire form (values percent-decoded).

        Empty ``&``-separated segments are ignored, so leading/trailing/doubled
        separators are harmless. A non-empty segment with no ``=`` is a malformed
        pair and raises ``ValueError``.
        """
        pairs: set[_Pair] = set()
        for segment in text.split("&"):
            if not segment:
                continue
            key, sep, value = unquote(segment).partition("=")
            if not sep:
                raise ValueError(f"match set segment missing '=': {segment!r}")
            pairs.add((key, value))
        return cls(frozenset(pairs))

    def to_list(self) -> list[_Pair]:
        return sorted(self._pairs)

    def find_by_key(self, key: str) -> _Pair | None:
        return next(((k, v) for k, v in self._pairs if k == key), None)

    def contains(self, key: str, value: str) -> bool:
        return (key, value) in self._pairs

    def union(self, other: MatchSet) -> MatchSet:
        return MatchSet(self._pairs | other._pairs)

    def is_subset_of(self, superset: MatchSet) -> bool:
        """True when every pair of ``self`` also appears in ``superset``."""
        return self._pairs <= superset._pairs

    def __eq__(self, other: object) -> bool:
        return isinstance(other, MatchSet) and self._pairs == other._pairs

    def __hash__(self) -> int:
        return hash(self._pairs)

    def __repr__(self) -> str:
        return "MatchSet(" + "&".join(f"{k}={v}" for k, v in self.to_list()) + ")"
