"""Runner/listener wire-contract gate (#550) — freeze the ARC wire protocol.

The single most skew-sensitive surface: runners and listeners run as ephemeral
Jobs / long-lived Deployments that can lag the API by several minor versions (or
live in a separately-upgraded cluster). Their contract with the API is NOT the
JSON:API router serializers (those are frozen by test_attribute_contract) — it's
a set of string literals that the router attribute gate can't see:

- the **SSE event names** the API publishes and the listener dispatches on
  (`run_available`, `check_job_status`, `stream_logs`, `cancel_job`);
- the **`runs/next` attributes** the listener reads to launch a Job (built into a
  variable dict on the API side, so invisible to the attribute gate) — a
  *renamed* attribute silently reverts to a default and mis-launches the Job;
- the runner→API **result body keys** (e.g. `has_changes` on `plan-result`).

This gate extracts those literals from the source and diffs them against a
committed snapshot. Removing/renaming one is a **breaking change** for a lagging
runner/listener (MAJOR bump or deprecation, not a regen); adding one is additive
(regenerate with `UPDATE_API_CONTRACT=1 pytest tests/api/test_wire_contract.py`).
It also asserts every SSE event the listener handles is actually published by the
API (no producer/consumer drift).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import terrapod

# `terrapod` is an implicit namespace package (no __init__.py) → __file__ is None;
# use __path__ for the package directory.
_ROOT = Path(next(iter(terrapod.__path__))).resolve()
_LISTENER = _ROOT / "runner" / "listener.py"
_UPLOADS = _ROOT / "runner" / "phases" / "uploads.py"
_RUN_SERVICE = _ROOT / "services" / "run_service.py"
_RECONCILER = _ROOT / "services" / "run_reconciler.py"
_SNAPSHOT = Path(__file__).parent / "api_wire_contract.json"


def _sse_event_names() -> set[str]:
    """The SSE event names the listener dispatches on (`event_type == "..."`)."""
    src = _LISTENER.read_text()
    return set(re.findall(r'event_type == "([a-z_]+)"', src))


def _runs_next_attributes() -> set[str]:
    """The `runs/next` response attributes the listener reads (`attrs.get("...")`)."""
    src = _LISTENER.read_text()
    return set(re.findall(r'attrs\.get\("([a-z][a-z0-9-]*)"', src))


def _listener_read_keys() -> set[str]:
    """Every dict key the listener reads off an API payload via ``.get("key")`` —
    the *comprehensive* wire surface, beyond the top-level `runs/next` attrs:
    nested item keys (`hcl` on a terraform-var, `hook_point`/`name`/`script` on an
    execution-hook) and the `check_job_status`/`stream_logs` event-payload keys
    the reconciler sends (`job_name`, `job_namespace`, `run_id`, `phase`,
    `tail_lines`, …). Renaming any of these is a runner/listener wire break the
    top-level attr freeze can't see. Restricted to lower-case-initial keys to skip
    env-var reads like `os.environ.get("POD_NAME")`."""
    src = _LISTENER.read_text()
    return set(re.findall(r'\.get\("([a-z][a-z0-9_-]*)"', src))


def wire_contract() -> dict[str, list[str]]:
    return {
        "sse_event_names": sorted(_sse_event_names()),
        "runs_next_attributes": sorted(_runs_next_attributes()),
        "listener_read_keys": sorted(_listener_read_keys()),
    }


def test_wire_contract_unchanged() -> None:
    current = wire_contract()

    if os.environ.get("UPDATE_API_CONTRACT"):
        _SNAPSHOT.write_text(json.dumps(current, indent=2) + "\n")
        return

    assert _SNAPSHOT.exists(), (
        f"Wire contract snapshot missing at {_SNAPSHOT}. Generate it with:\n"
        "  UPDATE_API_CONTRACT=1 pytest tests/api/test_wire_contract.py"
    )
    snapshot: dict[str, list[str]] = json.loads(_SNAPSHOT.read_text())

    problems: list[str] = []
    for key, snap_vals in snapshot.items():
        removed = sorted(set(snap_vals) - set(current.get(key, [])))
        if removed:
            problems.append(
                f"BREAKING [{key}]: removed/renamed {removed}. A runner/listener that "
                "lags the API across version skew depends on these — a MAJOR bump or a "
                "documented deprecation window is required, NOT a snapshot regen."
            )
    added: list[str] = []
    for key, cur_vals in current.items():
        new = sorted(set(cur_vals) - set(snapshot.get(key, [])))
        if new:
            added.append(f"[{key}] added {new}")
    if added:
        problems.append(
            "New wire keys added (additive). Regenerate:\n"
            "  UPDATE_API_CONTRACT=1 pytest tests/api/test_wire_contract.py\n  "
            + "\n  ".join(added)
        )
    assert not problems, "\n\n".join(problems)


def test_every_handled_sse_event_is_published_by_the_api() -> None:
    # No producer/consumer drift: every event the listener dispatches on must be
    # published somewhere in the API (run_service or the reconciler).
    published = _RUN_SERVICE.read_text() + _RECONCILER.read_text()
    unpublished = sorted(n for n in _sse_event_names() if f'"{n}"' not in published)
    assert not unpublished, (
        f"Listener handles SSE events the API never publishes: {unpublished} — "
        "producer/consumer wire drift."
    )


def test_plan_result_body_key_present() -> None:
    # The runner reports plan outcome via a `has_changes` body key on plan-result;
    # renaming it breaks the reconciler's has_changes handling (a real past incident).
    assert '"has_changes"' in _UPLOADS.read_text()


def test_extractors_are_nonempty() -> None:
    # Bite-check: a regex that silently matched nothing would disable the gate.
    assert len(_sse_event_names()) >= 4
    assert len(_runs_next_attributes()) >= 15
