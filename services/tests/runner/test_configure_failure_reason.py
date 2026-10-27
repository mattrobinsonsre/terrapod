"""How a failed or interrupted configure is reported (#1977).

Two properties. One is behavioural -- an ansible exit code says more than a
log scrape can. The other is structural, and it is the one that actually bit:
`job_entrypoint` holds TWO lists of phase-log paths twenty lines apart, the
roll-in loop and the `failure_reason` call, and `_CONFIGURE_LOG` had been added
to the first and not the second. So a failed configure's reason was scraped
from the apply, plan and init logs and never from its own.
"""

from __future__ import annotations

import ast
import inspect
import pathlib

from terrapod.runner import job_entrypoint
from terrapod.runner.phases import failure_reason


class TestAnsibleExitCodesAreNamed:
    """Measured against ansible-core 2.21.5 in the published EE image: one
    SIGINT mid-task exits 99 and emits no PLAY RECAP."""

    def test_99_reads_as_interrupted_rather_than_as_a_failure(self):
        reason = failure_reason.ansible_exit_reason(99)
        assert reason is not None
        assert "interrupt" in reason.lower()

    def test_a_failed_play_and_an_unreachable_host_are_distinguishable(self):
        """The distinction #1973's remainder depends on: 'interrupted' tells an
        operator to retry the remainder, 'unreachable' tells them to fix a host."""
        assert failure_reason.ansible_exit_reason(2) != failure_reason.ansible_exit_reason(4)
        assert failure_reason.ansible_exit_reason(99) != failure_reason.ansible_exit_reason(2)

    def test_a_clean_exit_and_an_unknown_code_name_nothing(self):
        """None rather than a guess, so the caller falls through to the log
        scrape instead of inventing a reason."""
        assert failure_reason.ansible_exit_reason(0) is None
        assert failure_reason.ansible_exit_reason(137) is None


class TestEveryPhaseLogListAgrees:
    """The invariant is that the collections AGREE WITH EACH OTHER, which is
    precisely what failed: `_CONFIGURE_LOG` was added to the roll-in loop and
    not to the `failure_reason` call twenty lines below it.

    Stated that way it needs no exemption ledger. A first version derived the
    subject from the `_*_LOG` name pattern and immediately had to carve out
    `_COMBINED_LOG`, which is the aggregate the phases roll INTO rather than a
    phase -- the recorded sign that the invariant, not the subject, was wrong.
    """

    @staticmethod
    def _collections() -> list[tuple[int, set[str]]]:
        """Every list/tuple built ENTIRELY out of the module's `_*_LOG` names."""
        names = {
            n
            for n in dir(job_entrypoint)
            if n.startswith("_")
            and n.endswith("_LOG")
            and isinstance(getattr(job_entrypoint, n), pathlib.Path)
        }
        tree = ast.parse(inspect.getsource(job_entrypoint))
        out = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.List, ast.Tuple)) or not node.elts:
                continue
            used = {e.id for e in node.elts if isinstance(e, ast.Name) and e.id in names}
            if used and len(used) == len(node.elts):
                out.append((getattr(node, "lineno", 0), used))
        return out

    def test_there_are_at_least_two_of_them(self):
        """A floor, so a renamed helper fails here rather than passing on
        nothing -- the roll-in loop and the `failure_reason` call."""
        found = self._collections()
        assert len(found) >= 2, f"expected at least two phase-log collections, got {found}"

    def test_they_all_name_the_same_phases(self):
        found = self._collections()
        union: set[str] = set().union(*(u for _, u in found))
        disagreeing = [(ln, sorted(union - u)) for ln, u in found if u != union]
        assert not disagreeing, (
            "a phase-log collection omits a log its sibling names, so that "
            f"phase's log is never consulted there: {disagreeing}"
        )

    def test_the_agreed_set_covers_every_RUNNER_PHASE(self):
        """Agreement alone would be satisfied by both dropping configure, so
        the set is also held to the phase vocabulary a runner token can carry."""
        from terrapod.auth.runner_tokens import RUNNER_PHASES

        union: set[str] = set().union(*(u for _, u in self._collections()))
        missing = {f"_{p.upper()}_LOG" for p in RUNNER_PHASES} - union
        assert not missing, f"no phase-log collection names {sorted(missing)}"
