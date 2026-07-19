"""Numeric ranges — port of OpenInfraQuote's ``oiq_range.ml`` (MPL-2.0).

A ``Range`` is a ``(min, max)`` pair. Cost estimates are ranges because usage
assumptions (and, when unfiltered, the cheapest-vs-dearest matching product)
give a lower and upper bound rather than a single number.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Range[T: (int, float)]:
    min: T
    max: T


def append[T: (int, float)](f: Callable[[T, T], T], t1: Range[T], t2: Range[T]) -> Range[T]:
    """Combine two ranges component-wise (e.g. ``append(add, a, b)`` sums them)."""
    return Range(min=f(t1.min, t2.min), max=f(t1.max, t2.max))


def overlap[T: (int, float)](t1: Range[T], t2: Range[T]) -> Range[T] | None:
    """Intersection of two ranges, or ``None`` if they don't overlap.

    Mirrors ``oiq_range.overlap``: no overlap when ``t1.max < t2.min`` or
    ``t2.max < t1.min``; otherwise ``(max(mins), min(maxes))``.
    """
    if t1.max < t2.min or t2.max < t1.min:
        return None
    return Range(min=max(t1.min, t2.min), max=min(t1.max, t2.max))
