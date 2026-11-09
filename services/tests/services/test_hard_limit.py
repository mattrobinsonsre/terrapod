"""The workspace's `hard_limit` is a ceiling a run cannot widen past (#2451).

Two properties carry the feature and neither is visible from a single fixture:

* **Composition is EXACT, not approximate.** `TestTheCeilingIsExact` drives the
  real `ansible-inventory` with the real composer's output over a 7x10 matrix of
  ceilings and run limits, and asserts the resolved host set equals
  `run ∩ ceiling` every time. A test that only checked one simple ceiling
  against one bare run limit would pass with the composition written the other
  way round, which is how the first version of this feature was written.

* **The ceiling's admissible shapes are decided by what composes exactly**, not
  by taste, so the validator's accept and refuse sets are both asserted. The
  refusals matter more than they look: a ceiling of two bare terms is the
  shape an operator will reach for first, and the composition for it is wrong
  in a way no gate downstream can see.

The measured semantics everything here rests on. Verified against ansible-core
**2.18.3 and 2.21.5** -- the second being what `docker/Dockerfile.test-base`
installs, so the driven tests below RUN in CI rather than skipping, and an
ansible upgrade that changed this would fail there:

    (union of the bare terms) INTERSECT (every `&` term)
                              MINUS (union of the `!` terms)

-- a partition by operator, order-independent. `services/inventory_narrowing.py`
had this as a left-to-right accumulator and the correction is what made an exact
bound possible at all; `TestTheMeasuredSemantics` pins the two readings apart so
the wrong one cannot come back.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from terrapod.engines.terraform import ceiling_terms, combine_limits
from terrapod.services.workspace_settings import validate_hard_limit

# --------------------------------------------------------------------------
# the real-ansible harness
# --------------------------------------------------------------------------

#: `canary` and `web` overlap on h2, `web` and `prod` on h3, and h4 is in `prod`
#: alone. Chosen so that a composition that unions where it should intersect
#: shows up as a specific extra host rather than as a count being off.
INVENTORY = """\
all:
  children:
    canary:
      hosts:
        h1: {tag: a}
        h2: {tag: a}
    prod:
      hosts:
        h3: {tag: a}
        h4: {tag: a}
    web:
      hosts:
        h2: {tag: a}
        h3: {tag: a}
"""

#: Every host carries a var deliberately. `ansible-inventory --list` emits an
#: EMPTY `_meta.hostvars` for a host with no variables, so a harness that reads
#: the host set from there against a var-less inventory measures nothing and
#: reports every pattern as matching no hosts -- which reads as the patterns
#: being wrong rather than the fixture.
_HOST_VAR_REQUIRED = True


def _working_ansible_inventory() -> str | None:
    """A usable `ansible-inventory`, or None.

    Probed rather than located, for the reason `test_inventory_resolve` records:
    a stale console script whose `ansible` module is gone dies with
    `ModuleNotFoundError`, which reads as a failure of the code under test.
    """
    found = shutil.which("ansible-inventory")
    if found is None:
        return None
    try:
        probe = subprocess.run(  # noqa: S603
            [found, "--version"], capture_output=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return found if probe.returncode == 0 else None


ANSIBLE_INVENTORY = _working_ansible_inventory()
needs_ansible = pytest.mark.skipif(ANSIBLE_INVENTORY is None, reason="no working ansible-inventory")


@pytest.fixture(scope="module")
def inventory_file():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "hosts.yml"
        path.write_text(INVENTORY)
        yield str(path)


def _resolve(inventory_file: str, limit: str) -> frozenset[str]:
    """The host set ansible itself resolves `limit` to."""
    argv = [str(ANSIBLE_INVENTORY), "-i", inventory_file, "--list"]
    if limit:
        argv += ["--limit", limit]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)  # noqa: S603
    assert proc.returncode == 0, f"{limit!r}: {proc.stderr}"
    return frozenset(json.loads(proc.stdout).get("_meta", {}).get("hostvars", {}))


#: Ceilings spanning every admissible shape: a group, a wildcard, an exclusion
#: with no bare term at all, the same written out, a group with a hole, and a
#: bare `&`.
CEILINGS = ["canary", "web", "web*", "!prod", "all:!prod", "canary:!h1", "&web"]

#: Run limits spanning every shape a run can supply, including the ones a
#: naive composition gets wrong: a union, an exclusion, an intersect of its
#: own, and a host the ceiling forbids.
RUN_LIMITS = ["", "all", "web", "prod", "h1:h4", "canary:prod", "all:!h1", "web:&prod", "*", "h3"]


@needs_ansible
class TestTheCeilingIsExact:
    def test_the_composed_limit_resolves_to_run_intersect_ceiling(self, inventory_file):
        """The whole feature, driven through the tool that enforces it.

        `run ∩ ceiling` is computed by asking ansible for each side separately,
        so the expectation comes from ansible rather than from this file's own
        model of it -- the model is what was wrong before.
        """
        everything = _resolve(inventory_file, "")
        assert everything == {"h1", "h2", "h3", "h4"}, "fixture is not resolving"

        mismatches = []
        for ceiling in CEILINGS:
            ceiling_hosts = _resolve(inventory_file, ceiling)
            assert ceiling_hosts, f"ceiling {ceiling!r} resolves to nothing"
            for run in RUN_LIMITS:
                composed = combine_limits(ceiling, run)
                got = _resolve(inventory_file, composed)
                want = (_resolve(inventory_file, run) if run else everything) & ceiling_hosts
                if got != want:
                    mismatches.append(
                        f"ceiling={ceiling!r} run={run!r} composed={composed!r} "
                        f"got={sorted(got)} want={sorted(want)}"
                    )
        assert not mismatches, "\n".join(mismatches)

    def test_no_run_limit_reaches_a_host_outside_the_ceiling(self, inventory_file):
        """The property an operator is buying, asserted on its own.

        Separate from the equality above because the two fail differently: a
        composition that is merely too NARROW breaks the equality and is a
        usability bug, while one that admits a forbidden host breaks this and
        is the thing the ceiling exists to prevent.
        """
        escapes = []
        for ceiling in CEILINGS:
            ceiling_hosts = _resolve(inventory_file, ceiling)
            for run in RUN_LIMITS:
                got = _resolve(inventory_file, combine_limits(ceiling, run))
                if got - ceiling_hosts:
                    escapes.append(f"{ceiling!r} + {run!r} reached {sorted(got - ceiling_hosts)}")
        assert not escapes, "\n".join(escapes)


@needs_ansible
class TestTheMeasuredSemantics:
    """Pin the partition reading apart from the accumulator reading.

    These are not assertions about Terrapod; they are the measurement the
    composition is derived from, committed so that an ansible upgrade which
    changes it fails here rather than silently widening a ceiling.
    """

    def test_a_term_after_an_intersect_joins_the_include_union(self, inventory_file):
        # Accumulator reading: (web ∩ canary) ∪ prod == {h2, h3, h4}.
        # Partition reading:   (web ∪ prod) ∩ canary == {h2}.
        assert _resolve(inventory_file, "web:&canary:prod") == {"h2"}

    def test_intersect_terms_and_rather_than_or(self, inventory_file):
        """Why a two-bare-term ceiling has no pattern form at all."""
        assert _resolve(inventory_file, "all:&canary:&prod") == frozenset()

    def test_term_order_is_irrelevant(self, inventory_file):
        assert _resolve(inventory_file, "prod:web:&canary") == _resolve(
            inventory_file, "web:prod:&canary"
        )
        assert _resolve(inventory_file, "all:&web:&canary") == _resolve(
            inventory_file, "all:&canary:&web"
        )

    def test_an_empty_include_set_reads_as_all(self, inventory_file):
        """What makes a ceiling of `!prod` alone a valid limit on its own."""
        assert _resolve(inventory_file, "!prod") == {"h1", "h2"}

    def test_an_exclusion_applies_to_the_whole_result(self, inventory_file):
        assert _resolve(inventory_file, "canary:prod:!h1") == {"h2", "h3", "h4"}


# --------------------------------------------------------------------------
# the composer, without ansible
# --------------------------------------------------------------------------


class TestTheComposer:
    @pytest.mark.parametrize(
        ("hard", "run", "expected"),
        [
            # Neither side present.
            ("", "", ""),
            # One side only: the other adds nothing, which is what keeps a
            # deployment that sets no ceiling behaving exactly as before.
            ("", "web", "web"),
            ("canary", "", "canary"),
            ("!prod", "", "!prod"),
            # The run is kept VERBATIM and the ceiling appended, so a run
            # pattern of any shape survives intact.
            ("canary", "web", "web:&canary"),
            ("canary", "web:prod", "web:prod:&canary"),
            ("canary", "all:!h1", "all:!h1:&canary"),
            ("canary", "web:&prod", "web:&prod:&canary"),
            # A ceiling term keeps its own operator; a bare one becomes `&`.
            ("!prod", "web", "web:!prod"),
            ("all:!prod", "web", "web:&all:!prod"),
            ("canary:!h1", "web", "web:&canary:!h1"),
            ("&web", "all", "all:&web"),
            # Whitespace around either side is not a term.
            ("  canary  ", "  web  ", "web:&canary"),
        ],
    )
    def test_composition(self, hard, run, expected):
        assert combine_limits(hard, run) == expected

    def test_the_run_pattern_is_a_prefix_of_the_result(self):
        """The ceiling is only ever APPENDED.

        Asserted as a property rather than per-case because a composer that
        rewrote the run's own pattern -- reordering it, or folding its terms --
        would satisfy every expectation above that happens to be order-
        insensitive while changing what the operator asked for.
        """
        for run in RUN_LIMITS:
            if not run:
                continue
            for ceiling in CEILINGS:
                assert combine_limits(ceiling, run).startswith(run)

    @pytest.mark.parametrize(
        ("pattern", "expected"),
        [
            ("canary", ["canary"]),
            ("all:!prod", ["all", "!prod"]),
            ("a,&b:!c", ["a", "&b", "!c"]),
            # Both separators work -- measured, `all,&canary` resolves the same
            # as `all:&canary` -- so a splitter that knew only `:` would let a
            # comma-separated two-term ceiling past the validator.
            ("a,b", ["a", "b"]),
            ("", []),
            (":", []),
        ],
    )
    def test_ceiling_terms(self, pattern, expected):
        assert ceiling_terms(pattern) == expected


# --------------------------------------------------------------------------
# the validator
# --------------------------------------------------------------------------


class TestWhatACeilingMayBe:
    @pytest.mark.parametrize(
        "value",
        [
            "",
            "canary",
            "web*",
            r"~web\..*",
            # No bare term at all: "no run may touch production", which is the
            # most valuable ceiling there is.
            "!prod",
            "all:!prod",
            "canary:!h1",
            "&web",
            "all:&web:&canary",
        ],
    )
    def test_admitted(self, value):
        assert validate_hard_limit(value) == value

    def test_none_is_empty(self):
        assert validate_hard_limit(None) == ""

    @pytest.mark.parametrize(
        ("value", "because"),
        [
            # THE refusal. `&` ANDs, so this has no pattern form, and writing
            # it anyway makes the second term an include -- measured, a run
            # asking for `h4` under this ceiling configures `h2`.
            ("canary:prod", "at most one"),
            ("a,b", "at most one"),
            ("canary:prod:!h1", "at most one"),
            (":", "separators alone"),
            (",,", "separators alone"),
            ("!", "need a pattern after"),
            ("&", "need a pattern after"),
            (" canary", "whitespace"),
            ("canary ", "whitespace"),
            ("a b", "whitespace"),
        ],
    )
    def test_refused(self, value, because):
        with pytest.raises(ValueError, match=because):
            validate_hard_limit(value)

    def test_a_non_string_is_refused(self):
        with pytest.raises(ValueError, match="must be a string"):
            validate_hard_limit(123)

    def test_the_refusal_names_both_offending_terms(self):
        """So an operator can see which two to group, not just that it failed."""
        with pytest.raises(ValueError) as exc:
            validate_hard_limit("canary:prod")
        assert "'canary'" in str(exc.value)
        assert "'prod'" in str(exc.value)

    def test_every_admitted_shape_composes_without_adding_a_bare_term(self):
        """The validator's accept set and the composer's assumption are one thing.

        A shape the validator admits but the composer turns into an extra
        INCLUDE is the defect this rule exists to prevent, so it is asserted
        structurally here as well as behaviourally against ansible above --
        the behavioural test skips without ansible installed, and this one
        does not.
        """
        for ceiling in CEILINGS:
            assert validate_hard_limit(ceiling) == ceiling
            composed = combine_limits(ceiling, "somerun")
            appended = ceiling_terms(composed)[1:]
            bare = [t for t in appended if t[0] not in "&!"]
            assert not bare, f"{ceiling!r} appended a bare term: {bare}"
