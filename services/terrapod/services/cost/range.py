"""Bounded numeric intervals used throughout cost estimation.

Every figure the engine produces is an interval, not a single number: usage
assumptions give a low/high band, and where a resource matches several products
(cheapest vs dearest) the quote spans both. :class:`Range` carries that
``(min, max)`` pair and supports ordinary ``+`` / ``-`` so totals and deltas fold
naturally; :func:`intersect` returns the overlapping sub-interval (or ``None``).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Range[T: (int, float)]:
    """A closed ``[min, max]`` interval of numbers.

    Addition and subtraction act on both bounds independently, so a sum of
    intervals is the interval of sums and a difference is the interval of
    differences — exactly how monthly totals and plan deltas combine.
    """

    min: T
    max: T

    def __add__(self, other: Range[T]) -> Range[T]:
        return Range(self.min + other.min, self.max + other.max)

    def __sub__(self, other: Range[T]) -> Range[T]:
        return Range(self.min - other.min, self.max - other.max)


def intersect[T: (int, float)](a: Range[T], b: Range[T]) -> Range[T] | None:
    """The overlapping sub-interval of ``a`` and ``b``, or ``None`` if disjoint.

    They are disjoint when one ends before the other begins; otherwise the
    overlap runs from the larger lower bound to the smaller upper bound.
    """
    if a.max < b.min or b.max < a.min:
        return None
    return Range(min=max(a.min, b.min), max=min(a.max, b.max))
