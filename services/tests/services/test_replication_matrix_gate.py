"""Every replicated class must carry its full test matrix (#1112).

Registering a class is deliberately one line — the flush hook picks it up
automatically, so no write path can forget to emit an event. The flip side is
that registering one is *too easy*: nothing otherwise stops a class shipping
with no delete test, no idempotency test, and no merge-rule test. That gap would
not surface in CI, in review, or in normal operation. It would surface at a
failover, as a row that quietly did not converge.

So this fails the build rather than reminding anyone.

**The requirement is derived, not declared.** A per-class manifest of "which
rows apply here" is something a contributor can quietly weaken; instead the
required rows are computed from the class spec and its model. A class that gains
a counter therefore *starts* failing until its monotonic test exists — which is
exactly the case where losing an update issues extra credentials rather than
merely losing information.

**Coverage is claimed at the test**, with `@pytest.mark.replication_matrix`, not
in a list of node ids. A manifest rots the moment a test is renamed; a mark
moves with the test it describes.
"""

import ast
from pathlib import Path

from terrapod.crypto.types import EncryptedText
from terrapod.services import replication

#: Rows every replicated class must cover, whatever it holds.
ALWAYS_REQUIRED = frozenset(
    {
        "backfill-from-empty",
        "delta-apply",
        "idempotent-reapply",
        "delete",
        "role-change-conflict",
    }
)

#: Rows required only when the class actually has the thing they protect.
CONDITIONAL = {
    "monotonic-never-regresses": lambda spec: bool(spec.monotonic_fields),
    "one-way-never-reverts": lambda spec: bool(spec.one_way_true_fields),
    "encrypted-columns": lambda spec: _has_encrypted_column(spec),
}

ALL_ROWS = ALWAYS_REQUIRED | set(CONDITIONAL)

_TESTS_ROOT = Path(__file__).resolve().parents[1]


def _has_encrypted_column(spec) -> bool:
    from sqlalchemy import inspect as sa_inspect

    return any(
        isinstance(col.expression.type, EncryptedText)
        for col in sa_inspect(spec.model).column_attrs
    )


def required_rows(spec) -> set[str]:
    rows = set(ALWAYS_REQUIRED)
    rows.update(row for row, applies in CONDITIONAL.items() if applies(spec))
    return rows


def _collect_marks() -> dict[str, set[str]]:
    """Parse the test tree for `replication_matrix` marks.

    Parsing rather than importing keeps this independent of collection order and
    of whether the marked module imports cleanly in isolation.
    """
    covered: dict[str, set[str]] = {}
    for path in _TESTS_ROOT.rglob("test_*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call):
                    continue
                func = dec.func
                if not (isinstance(func, ast.Attribute) and func.attr == "replication_matrix"):
                    continue
                args = [a.value for a in dec.args if isinstance(a, ast.Constant)]
                if len(args) >= 2:
                    covered.setdefault(str(args[0]), set()).add(str(args[1]))
    return covered


class TestMatrixIsComplete:
    def test_every_registered_class_covers_its_required_rows(self):
        covered = _collect_marks()
        gaps = []
        for name, spec in replication.registered().items():
            missing = required_rows(spec) - covered.get(name, set())
            if missing:
                gaps.append(f"{name}: missing {sorted(missing)}")

        assert not gaps, (
            "Replicated classes are missing test-matrix rows. A class that is not "
            "tested for these does not converge, and you find out at a failover:\n  "
            + "\n  ".join(gaps)
        )

    def test_no_class_is_registered_without_any_coverage(self):
        """The blunt case the derived rules would otherwise report row by row."""
        covered = _collect_marks()
        unmarked = [name for name in replication.registered() if name not in covered]

        assert not unmarked, f"registered but entirely untested: {unmarked}"


class TestTheGateItself:
    """A gate that cannot fail is not a gate."""

    def test_a_counter_class_requires_the_monotonic_row(self):
        spec = replication.get("agent_pool_tokens")

        assert "monotonic-never-regresses" in required_rows(spec)

    def test_a_plain_class_does_not(self):
        """Requiring a monotonic test of a class with no counter would train
        people to write empty tests to satisfy the gate."""
        spec = replication.get("agent_pools")

        assert "monotonic-never-regresses" not in required_rows(spec)
        assert "one-way-never-reverts" not in required_rows(spec)

    def test_every_class_always_needs_the_base_rows(self):
        for spec in replication.registered().values():
            assert ALWAYS_REQUIRED <= required_rows(spec)

    def test_marks_are_actually_found(self):
        """Guards the parser: if this silently returned nothing, the gate above
        would pass for every class forever."""
        covered = _collect_marks()

        assert covered, "the mark parser found nothing — the gate is inert"
        assert "agent_pools" in covered

    def test_only_known_rows_are_claimed(self):
        """A typo'd row name would otherwise look like coverage while satisfying
        nothing."""
        unknown = {
            (name, row)
            for name, rows in _collect_marks().items()
            for row in rows
            if row not in ALL_ROWS
        }

        assert not unknown, f"unrecognised matrix rows claimed: {sorted(unknown)}"

    def test_marks_name_registered_classes(self):
        registered = set(replication.registered())
        stray = set(_collect_marks()) - registered

        assert not stray, f"coverage claimed for classes that are not registered: {sorted(stray)}"
