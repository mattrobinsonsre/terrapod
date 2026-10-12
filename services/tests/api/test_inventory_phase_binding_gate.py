"""Every inventory WRITE is bound to the apply phase; every READ is not.

A runner token carries the run's phase, and the inventory router's `_authorize`
enforces it -- but only when the call site passes `phase="apply"`. That is an
ARGUMENT at 37 call sites, so the invariant is one careless addition away from
a write route a plan-phase token can reach, and no behavioural test would
notice: the phase tests in `test_inventory.py` drive a single route, while the
router has twenty write call sites.

That matters because a plan runs code from a pull request. A plan-phase token
that could write the inventory would let a fork PR rewrite the host set a later
configure targets -- so this is the same class as the fork-PR plan gate, not a
tidiness rule.

So the gate is derived from the router's own source rather than from a list:
find every `_authorize(...)` call, read its `required=` and `phase=`, and
require the two to agree. A route added later is in scope automatically.
"""

import ast
import pathlib

from terrapod.api.routers import inventory

ROUTER = pathlib.Path(inventory.__file__)


def _authorize_calls() -> list[tuple[int, str | None, str | None]]:
    """(line, required, phase) for every `_authorize(...)` call in the router.

    Read off the source rather than by patching, because what is being asserted
    is what the call sites SAY -- a runtime check would only ever see the routes
    a test happened to drive, which is the gap this exists to close.
    """
    tree = ast.parse(ROUTER.read_text())
    out: list[tuple[int, str | None, str | None]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
        if name != "_authorize":
            continue
        required = phase = None
        for kw in node.keywords:
            if kw.arg == "required":
                # cap.INVENTORY_WRITE -> "INVENTORY_WRITE"
                required = getattr(kw.value, "attr", None) or getattr(kw.value, "id", None)
            elif kw.arg == "phase":
                phase = kw.value.value if isinstance(kw.value, ast.Constant) else "<non-literal>"
        out.append((node.lineno, required, phase))
    return out


class TestEveryInventoryWriteIsPhaseBound:
    def test_every_write_call_site_passes_phase_apply(self):
        calls = _authorize_calls()
        writes = [c for c in calls if c[1] == "INVENTORY_WRITE"]
        # A floor, so a renamed helper fails here rather than matching nothing
        # and passing. Twenty when written; re-count from this test's own
        # output rather than from the comment if it ever trips.
        assert len(writes) >= 20, (
            f"found only {len(writes)} INVENTORY_WRITE call sites; the derivation has broken"
        )
        unbound = [(line, phase) for line, _req, phase in writes if phase != "apply"]
        assert not unbound, (
            "inventory write routes not bound to the apply phase at "
            f"{ROUTER.name} lines {[line for line, _ in unbound]} -- a plan-phase "
            'runner token could write through them. Pass phase="apply".'
        )

    def test_every_read_call_site_is_unphased(self):
        """Reads are unphased on purpose: a plan diffs the inventory.

        Asserted so the fix for the above is not "bind everything", which would
        break every plan that reads its own inventory.
        """
        calls = _authorize_calls()
        reads = [c for c in calls if c[1] == "INVENTORY_READ"]
        assert len(reads) >= 15, (
            f"found only {len(reads)} INVENTORY_READ call sites; the derivation has broken"
        )
        phased = [(line, phase) for line, _req, phase in reads if phase is not None]
        assert not phased, (
            f"inventory read routes bound to a phase at {ROUTER.name} lines "
            f"{[line for line, _ in phased]} -- a plan reads the inventory to diff it, "
            "so binding a read to the apply phase breaks every plan on a VCS workspace."
        )

    def test_the_only_capabilities_used_are_the_two_inventory_ones(self):
        """A third capability would mean a tier this gate says nothing about."""
        seen = {req for _line, req, _phase in _authorize_calls() if req}
        assert seen == {"INVENTORY_READ", "INVENTORY_WRITE"}, (
            f"unexpected capabilities in the inventory router: {sorted(seen)} -- "
            "extend this gate to say what phase each one requires"
        )
