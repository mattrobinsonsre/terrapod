"""Every per-phase log reaches the uploaded artifact (#1972).

`_CONFIGURE_LOG` did not. The configure phase writes ansible's entire output to
it -- every TASK line, the PLAY RECAP, and on a failure the only diagnostic
there is -- and the roll-in loop listed `_INIT_LOG`, `_PLAN_LOG` and
`_APPLY_LOG` only, so none of it was appended to the combined log and none of it
was uploaded. The stored configure log held the orchestrator's own structlog
lines and nothing else: four live runs, 1841-2493 bytes each, zero `PLAY RECAP`
and zero task lines, while `kubectl logs` showed the lot. A configure that
FAILED was therefore indistinguishable from one that did nothing.

The gate is DERIVED rather than a second list, because a hand-kept list is what
was wrong in the first place: it reads the module's own `_*_LOG` constants and
requires each one to be rolled in. A fifth phase gets this for free.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[2] / "terrapod" / "runner" / "job_entrypoint.py"

#: Written by `LogCapture` itself rather than by a phase, so it is the sink
#: rather than a source -- appending it to itself is not a thing to assert.
_SINK = "_COMBINED_LOG"


def _tree() -> ast.Module:
    return ast.parse(_SRC.read_text())


def _phase_log_constants() -> set[str]:
    """Module-level `_X_LOG = Path(...)` names, minus the sink."""
    found = set()
    for node in _tree().body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if (
                isinstance(target, ast.Name)
                and target.id.startswith("_")
                and target.id.endswith("_LOG")
                and target.id != _SINK
            ):
                found.add(target.id)
    return found


def _rolled_in() -> set[str]:
    """The names the roll-in loop actually iterates."""
    for node in ast.walk(_tree()):
        if not isinstance(node, ast.For):
            continue
        body_calls = [
            c
            for c in ast.walk(node)
            if isinstance(c, ast.Call) and getattr(c.func, "attr", None) == "append_file"
        ]
        if not body_calls:
            continue
        if isinstance(node.iter, (ast.Tuple, ast.List)):
            return {e.id for e in node.iter.elts if isinstance(e, ast.Name)}
    return set()


def test_the_constants_and_the_loop_are_both_found():
    """A floor, so a rename cannot make this gate pass by matching nothing."""
    consts, rolled = _phase_log_constants(), _rolled_in()
    assert len(consts) >= 4, f"found only {sorted(consts)}; the matcher has rotted"
    assert len(rolled) >= 4, f"found only {sorted(rolled)}; the roll-in loop was not located"


@pytest.mark.parametrize("name", sorted(_phase_log_constants()))
def test_every_phase_log_is_rolled_in(name):
    assert name in _rolled_in(), (
        f"`{name}` is written by a phase and never appended to the combined log, "
        "so nothing it contains is uploaded. That is what happened to "
        "`_CONFIGURE_LOG`: ansible's whole output went to a file the upload step "
        "did not know about, and the only durable record of a playbook run held "
        "the orchestrator's bookkeeping instead."
    )
