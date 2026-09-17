"""Run status contract gate (#1704) — what a run, policy check or task stage reports.

Clients branch on status strings, and the `tofu`/`terraform` CLI most of all: it
offers an override only while a run is `policy_override` or
`post_plan_awaiting_decision`, and it fails outright on a task-stage or policy
check status it does not know. So the vocabulary is a contract, pinned here
against `api_run_status_contract.json`:

- **Removing or renaming** a status is a breaking change: a client waiting for
  it waits forever. It needs a MAJOR bump, never a snapshot regen.
- **Adding** one is additive; accept it by regenerating the snapshot:

      UPDATE_API_CONTRACT=1 pytest tests/api/test_run_status_contract.py

Separately from the snapshot, everything Terrapod reports to the CLI in the
Terraform Enterprise vocabulary must be a value go-tfe defines, checked against
the lists below, copied from go-tfe v1.101.0 (the version OpenTofu 1.12 builds
with). Update them from go-tfe's source, never to make a status pass.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

from terrapod.api.routers.run_tasks import tfe_task_stage_status
from terrapod.services import policy_check_service, run_service, run_task_service

_SNAPSHOT = Path(__file__).parent / "api_run_status_contract.json"

# go-tfe v1.101.0: run.go RunStatus, policy_check.go PolicyStatus,
# task_stages.go TaskStageStatus.
GO_TFE_RUN_STATUSES = frozenset(
    {
        "applied", "applying", "apply_queued", "canceled", "confirmed",
        "cost_estimated", "cost_estimating", "discarded", "errored", "fetching",
        "fetching_completed", "pending", "planned", "planned_and_finished",
        "planned_and_saved", "planning", "plan_queued", "policy_checked",
        "policy_checking", "policy_override", "policy_soft_failed",
        "post_plan_awaiting_decision", "post_plan_completed", "post_plan_running",
        "pre_apply_running", "pre_apply_completed", "pre_plan_completed",
        "pre_plan_running", "queuing", "queuing_apply",
    }
)  # fmt: skip
GO_TFE_POLICY_STATUSES = frozenset(
    {
        "canceled", "errored", "hard_failed", "overridden", "passed", "pending",
        "queued", "soft_failed", "unreachable",
    }
)  # fmt: skip
GO_TFE_TASK_STAGE_STATUSES = frozenset(
    {
        "pending", "running", "passed", "failed", "awaiting_override", "canceled",
        "errored", "unreachable",
    }
)  # fmt: skip


def _tfe_stage_statuses() -> set[str]:
    held = MagicMock(status="planning", plan_finished_at=object())
    errored = MagicMock(status="errored", plan_finished_at=object())
    return {
        tfe_task_stage_status(MagicMock(status=s), run)
        for s in run_task_service.STAGE_STATUSES
        for run in (held, errored)
    }


def current_vocabulary() -> dict[str, list[str]]:
    stored = set(run_service.VALID_TRANSITIONS) | set().union(
        *run_service.VALID_TRANSITIONS.values()
    )
    return {
        "run.stored": sorted(stored),
        "run.reported_for_a_hold": sorted(run_service.TFE_POST_PLAN_STATUSES),
        "policy_check": sorted(policy_check_service.STATUSES),
        "task_stage.stored": sorted(run_task_service.STAGE_STATUSES),
        "task_stage.reported_to_the_cli": sorted(_tfe_stage_statuses()),
    }


def test_run_status_contract_unchanged() -> None:
    current = current_vocabulary()
    if os.environ.get("UPDATE_API_CONTRACT"):
        _SNAPSHOT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n")
        return
    pinned = json.loads(_SNAPSHOT.read_text())
    problems = []
    for key in sorted(set(pinned) | set(current)):
        removed = sorted(set(pinned.get(key, [])) - set(current.get(key, [])))
        added = sorted(set(current.get(key, [])) - set(pinned.get(key, [])))
        if removed:
            problems.append(f"{key}: REMOVED {removed} (breaking: needs a MAJOR)")
        if added:
            problems.append(f"{key}: added {added} (regenerate with UPDATE_API_CONTRACT=1)")
    assert not problems, "Run status contract changed:\n  " + "\n  ".join(problems)


def test_the_cli_is_only_shown_statuses_it_knows() -> None:
    vocab = current_vocabulary()
    assert set(vocab["run.reported_for_a_hold"]) <= GO_TFE_RUN_STATUSES
    assert set(vocab["policy_check"]) <= GO_TFE_POLICY_STATUSES
    assert set(vocab["task_stage.reported_to_the_cli"]) <= GO_TFE_TASK_STAGE_STATUSES
