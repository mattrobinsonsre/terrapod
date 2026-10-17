"""Every per-run storage key is reaped, or says why not (#2021, #2148).

`artifact_retention_service` deleted four of the nine per-run keys
`storage/keys.py` defines, and the four were a hand-written tuple inside the
loop. Nothing derived it and nothing failed when it drifted -- so when the
configure spine added `configure_log_key` two days before this test existed,
that log joined three older keys in accumulating for the life of the
deployment.

This is the same shape as `_FIELDS_HANDLED_SEPARATELY` and `_SURFACE_EXEMPT`:
an enumeration trusted to stay in step with another module by hand. The answer
is the one that works for those -- derive the subject from the source of truth,
and make every exemption carry a reason.

**`NOT_REAPED` is now empty, and that is the point.** #2148 closed the last
three and deleted `run_tfvars_key`, which had one occurrence in the whole
repo -- its own definition. The dict stays because the gate needs somewhere
for a future deliberate exemption to go; an empty ledger is the healthy
state, not a dead mechanism. `test_the_ledger_is_not_how_a_key_escapes`
keeps it honest.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

from terrapod.services.artifact_retention_service import RUN_ARTIFACT_KEYS
from terrapod.storage import keys as keys_mod

#: Per-run keys deliberately NOT reaped by the run-artifact sweep, each with the
#: reason. An entry here is a decision; an absence from both this and
#: `RUN_ARTIFACT_KEYS` is the drift this test exists to catch.
NOT_REAPED: dict[str, str] = {}


def _per_run_key_functions() -> set[str]:
    """Every `keys.py` function keyed on exactly (workspace_id, run_id).

    Derived from the signature rather than from a name pattern: `*_key` catches
    keys that have nothing to do with a run, and a prefix match would miss one
    named differently. The signature IS the definition of "per-run".
    """
    found: set[str] = set()
    tree = ast.parse(pathlib.Path(keys_mod.__file__).read_text())
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        args = [a.arg for a in node.args.args]
        if args == ["workspace_id", "run_id"]:
            found.add(node.name)
    return found


class TestEveryPerRunKeyIsAccountedFor:
    def test_the_ledger_is_not_how_a_key_escapes(self):
        """Every per-run key is reaped as at #2148, so the exemption ledger is
        empty. This is not a style preference: an entry here is an object
        nothing deletes, so the bar for adding one is a stated reason that
        survives review -- and a ledger that quietly refills is how the
        original four keys went missing. If you are adding an entry, the
        question to answer first is why the object must outlive its run."""
        assert NOT_REAPED == {}, (
            f"a per-run key has been exempted from reaping: {sorted(NOT_REAPED)}. "
            f"That is a decision, not a default -- state why the object must "
            f"outlive its run, and update this test deliberately."
        )

    def test_the_subject_was_actually_found(self):
        """The floor. A signature matcher that matches nothing would make every
        assertion below vacuously true -- which is how a derived gate comes to
        pass while checking nothing."""
        found = _per_run_key_functions()
        assert len(found) >= 8, f"only found {sorted(found)}; the matcher has drifted"
        assert "plan_log_key" in found and "resolved_inventory_key" in found, sorted(found)

    def test_every_per_run_key_is_reaped_or_has_a_stated_reason(self):
        reaped = {fn.__name__ for fn in RUN_ARTIFACT_KEYS}
        unaccounted = _per_run_key_functions() - reaped - set(NOT_REAPED)
        assert not unaccounted, (
            f"these per-run storage keys are neither reaped nor listed in "
            f"NOT_REAPED: {sorted(unaccounted)}. A per-run object nothing "
            f"deletes accumulates for the life of the deployment -- add it to "
            f"RUN_ARTIFACT_KEYS, or to NOT_REAPED with the reason."
        )

    def test_the_exemptions_are_real_keys(self):
        """An exemption for a key that no longer exists is a stale entry that
        would hide the next real one with the same name."""
        found = _per_run_key_functions()
        stale = set(NOT_REAPED) - found
        assert not stale, f"NOT_REAPED names keys that do not exist: {sorted(stale)}"

    def test_nothing_is_both_reaped_and_exempt(self):
        reaped = {fn.__name__ for fn in RUN_ARTIFACT_KEYS}
        both = reaped & set(NOT_REAPED)
        assert not both, f"listed as both reaped and exempt: {sorted(both)}"

    def test_every_exemption_carries_a_reason(self):
        for name, reason in NOT_REAPED.items():
            assert reason.strip(), f"{name} is exempt with no reason given"

    def test_the_keys_are_callable_with_the_signature_the_loop_uses(self):
        """The loop calls `key_fn(ws_id, run_id)` positionally, so a member whose
        signature differs would raise inside the sweep -- at retention time, in a
        scheduled task, where nobody is watching."""
        for fn in RUN_ARTIFACT_KEYS:
            params = list(inspect.signature(fn).parameters)
            assert params == ["workspace_id", "run_id"], f"{fn.__name__}{params}"
            assert fn("ws-1", "run-1")
