"""The parallelism setting's one rule (#1431).

Small surface, but four paths write it — workspace create, workspace update, the
autodiscovery rule template and bulk update — which is exactly why the rule lives
in one place rather than being re-typed at each of them.
"""

from __future__ import annotations

import pytest

from terrapod.services.parallelism import (
    DEFAULT_PARALLELISM,
    MAX_PARALLELISM,
    validate_parallelism,
)


class TestTheDefault:
    def test_it_is_terraforms_own(self) -> None:
        """Load-bearing: any other value would have changed every existing
        workspace's behaviour the moment this column was added."""
        assert DEFAULT_PARALLELISM == 10


class TestAccepted:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(10, 10), (1, 1), (MAX_PARALLELISM, MAX_PARALLELISM), ("25", 25), ("  4 ", 4)],
    )
    def test_it_coerces(self, raw: object, expected: int) -> None:
        assert validate_parallelism(raw) == expected


class TestRejected:
    def test_zero_is_not_unlimited_here(self) -> None:
        """terraform reads 0 as "no limit"; Pulumi has no zero form at all.

        A shared setting cannot honour a value that means different things per
        engine, so it is refused rather than quietly meaning one of them.
        """
        with pytest.raises(ValueError, match="at least 1"):
            validate_parallelism(0)

    def test_negative(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            validate_parallelism(-4)

    def test_above_the_ceiling(self) -> None:
        with pytest.raises(ValueError, match="at most"):
            validate_parallelism(MAX_PARALLELISM + 1)

    def test_a_boolean_is_not_one(self) -> None:
        """`bool` subclasses `int`, so True would otherwise quietly mean 1."""
        with pytest.raises(ValueError, match="boolean"):
            validate_parallelism(True)

    @pytest.mark.parametrize("raw", ["", "abc", "1.5", 1.5, None, [], {}])
    def test_non_numbers(self, raw: object) -> None:
        with pytest.raises(ValueError):
            validate_parallelism(raw)
